"""
Lambda: rag_query
POST /chat — answers natural language queries using RAG over ingested documents.

Embeds the query with Bedrock Titan, retrieves the top-K most similar chunks
from Neon pgvector, then calls Bedrock Claude with the retrieved context.

Environment variables:
  BEDROCK_EMBEDDING_MODEL         — Titan model ID (default: amazon.titan-embed-text-v2:0)
  BEDROCK_GENERATION_MODEL        — Claude model ID (default: claude-3-haiku)
  NEON_CONNECTION_STRING_PARAM    — SSM path for Neon connection string

Local dev (set in backend/.env):
  NEON_CONNECTION_STRING          — Neon DSN (skips SSM lookup)
"""
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

import neon

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_ssm = boto3.client("ssm")
# Bedrock is not available in us-west-1 — call it cross-region from us-east-1
_bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "us-east-1"))

_ssm_cache: dict[str, str] = {}

TOP_K = 5
# Cosine distance above this means the query is unrelated to anything in the documents.
# Range is 0 (identical) to 2 (opposite). 0.7 is a practical cutoff for off-topic queries.
RELEVANCE_THRESHOLD = float(os.environ.get("RELEVANCE_THRESHOLD", "0.7"))

SYSTEM_PROMPT = (
    "You are an energy advisor assistant for a home solar, battery, and EV charging system. "
    "Answer questions using only the provided document context. Be concise and specific. "
    "Reference the source document when relevant. "
    "If the answer is not in the context, say so clearly rather than guessing."
)


def _get_ssm_param(path: str) -> str:
    if path not in _ssm_cache:
        resp = _ssm.get_parameter(Name=path, WithDecryption=True)
        _ssm_cache[path] = resp["Parameter"]["Value"]
    return _ssm_cache[path]


def _resolve_neon_dsn() -> str:
    direct = os.environ.get("NEON_CONNECTION_STRING", "")
    if direct:
        return direct
    return _get_ssm_param(os.environ["NEON_CONNECTION_STRING_PARAM"])


def _embed(text: str) -> list[float]:
    model_id = os.environ.get(
        "BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0"
    )
    response = _bedrock.invoke_model(
        modelId=model_id,
        body=json.dumps({"inputText": text, "dimensions": 1024, "normalize": True}),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["embedding"]


def _retrieve_chunks(conn, query_embedding: list[float]) -> list[dict]:
    vec = neon.format_vector(query_embedding)
    rows = conn.run(
        f"SELECT doc_name, content, page_start, embedding <=> '{vec}'::vector AS distance"
        f" FROM document_chunks"
        f" ORDER BY distance"
        f" LIMIT :top_k",
        top_k=TOP_K,
    )
    return [
        {"doc_name": row[0], "content": row[1], "page_start": row[2], "distance": float(row[3])}
        for row in rows
    ]


def _generate(query: str, chunks: list[dict], model_id: str) -> str:
    if chunks:
        context_parts = [
            f"[From {c['doc_name']}, p.{c['page_start']}]\n{c['content']}" for c in chunks
        ]
        context = "\n\n---\n\n".join(context_parts)
        user_text = f"Context:\n{context}\n\nQuestion: {query}"
    else:
        user_text = (
            f"Question: {query}\n\n"
            "(No relevant document context was found. Please say so in your answer.)"
        )

    # Amazon Nova API format (differs from Claude's anthropic_version format)
    body = {
        "system": [{"text": SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        "inferenceConfig": {"max_new_tokens": 1024},
    }
    response = _bedrock.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(response["body"].read())["output"]["message"]["content"][0]["text"]


def lambda_handler(event: dict, context) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
        query = (body.get("query") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Request body must be JSON with a 'query' field"}),
        }

    if not query:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "'query' field is required"}),
        }

    logger.info("RAG query: %.200s", query)

    try:
        query_embedding = _embed(query)

        dsn = _resolve_neon_dsn()
        conn = neon.get_connection(dsn)
        chunks = _retrieve_chunks(conn, query_embedding)

        if not chunks or chunks[0]["distance"] > RELEVANCE_THRESHOLD:
            logger.info(
                "Query rejected — best distance %.4f exceeds threshold %.4f",
                chunks[0]["distance"] if chunks else float("inf"),
                RELEVANCE_THRESHOLD,
            )
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
                "body": json.dumps({
                    "response": "I can only answer questions about your energy system and uploaded documents.",
                    "sources": [],
                }),
            }

        logger.info(
            "Retrieved %d chunks (closest distance: %.4f)",
            len(chunks),
            chunks[0]["distance"],
        )

        generation_model = os.environ.get(
            "BEDROCK_GENERATION_MODEL",
            "us.amazon.nova-lite-v1:0",
        )
        answer = _generate(query, chunks, generation_model)

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps({
                "response": answer,
                "sources": [
                    {"doc": c["doc_name"], "page": c["page_start"], "distance": c["distance"]}
                    for c in chunks
                ],
            }),
        }

    except ClientError as exc:
        logger.error("AWS service error: %s", exc)
        return {
            "statusCode": 502,
            "body": json.dumps({"error": "Upstream AWS service error"}),
        }
    except Exception as exc:
        logger.exception("RAG query failed: %s", exc)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal error"}),
        }
