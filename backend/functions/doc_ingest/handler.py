"""
Lambda: doc_ingest
Triggered by S3 ObjectCreated events on the documents bucket.

Reads a PDF from S3, splits it into overlapping text chunks, embeds each chunk
using Bedrock Titan Text Embeddings V2, and stores the results in Neon pgvector.
Re-ingesting an existing document replaces its chunks atomically.

Environment variables:
  DOCUMENTS_BUCKET                — S3 bucket name
  BEDROCK_EMBEDDING_MODEL         — Titan model ID (default: amazon.titan-embed-text-v2:0)
  NEON_CONNECTION_STRING_PARAM    — SSM path for Neon connection string

Local dev (set in backend/.env):
  NEON_CONNECTION_STRING          — Neon DSN (skips SSM lookup)
"""
import io
import json
import logging
import os

import boto3
import pypdf
from botocore.exceptions import ClientError

import neon

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3 = boto3.client("s3")
_ssm = boto3.client("ssm")
# Bedrock is not available in us-west-1 — call it cross-region from us-east-1
_bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "us-east-1"))

_ssm_cache: dict[str, str] = {}

CHUNK_SIZE = 500    # words per chunk
CHUNK_OVERLAP = 50  # words shared between adjacent chunks


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


def _extract_pages(pdf_bytes: bytes) -> list[tuple[int, str]]:
    """Return (1-indexed page number, text) for each page."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]


def _chunk_pages(pages: list[tuple[int, str]]) -> list[tuple[str, int]]:
    """Return (chunk_text, start_page) pairs with overlap between chunks."""
    word_pages: list[tuple[str, int]] = []
    for page_num, text in pages:
        for word in text.split():
            word_pages.append((word, page_num))

    chunks = []
    i = 0
    while i < len(word_pages):
        slice_ = word_pages[i : i + CHUNK_SIZE]
        chunk_text = " ".join(w for w, _ in slice_)
        if chunk_text.strip():
            chunks.append((chunk_text, slice_[0][1]))
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


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


def lambda_handler(event: dict, context) -> dict:
    record = event["Records"][0]["s3"]
    bucket = record["bucket"]["name"]
    key = record["object"]["key"]
    doc_name = key.rsplit("/", 1)[-1]

    logger.info("Ingesting document: s3://%s/%s", bucket, key)

    try:
        obj = _s3.get_object(Bucket=bucket, Key=key)
        pdf_bytes = obj["Body"].read()
    except ClientError as exc:
        logger.error("Failed to read s3://%s/%s: %s", bucket, key, exc)
        raise

    pages = _extract_pages(pdf_bytes)
    all_text = " ".join(text for _, text in pages)
    if not all_text.strip():
        logger.warning("No text extracted from %s — skipping", key)
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "empty", "doc": doc_name}),
        }

    chunks = _chunk_pages(pages)
    logger.info("Split %s into %d chunks", doc_name, len(chunks))

    dsn = _resolve_neon_dsn()
    conn = neon.get_connection(dsn)
    neon.ensure_schema(conn)

    conn.run(
        "DELETE FROM document_chunks WHERE doc_name = :doc_name",
        doc_name=doc_name,
    )

    for i, (chunk_text, page_start) in enumerate(chunks):
        embedding = _embed(chunk_text)
        vec = neon.format_vector(embedding)
        conn.run(
            f"INSERT INTO document_chunks (doc_name, chunk_index, content, embedding, page_start)"
            f" VALUES (:doc_name, :chunk_index, :content, '{vec}'::vector, :page_start)",
            doc_name=doc_name,
            chunk_index=i,
            content=chunk_text,
            page_start=page_start,
        )
        logger.info("Stored chunk %d/%d (p.%d) for %s", i + 1, len(chunks), page_start, doc_name)

    logger.info("Ingestion complete: %d chunks stored for %s", len(chunks), doc_name)
    return {
        "statusCode": 200,
        "body": json.dumps({"status": "ok", "doc": doc_name, "chunks": len(chunks)}),
    }
