"""
Lambda: chat
POST /chat — classifies the query and routes to either rag_query (document RAG)
or data_query (DynamoDB energy history).

Keeps rag_query and data_query as independent Lambdas; this handler is a thin
routing layer that adds a single Nova Lite classification call then forwards
the original event via boto3 Lambda.invoke().

Environment variables:
  RAG_QUERY_FUNCTION_NAME      — name of the rag_query Lambda
  DATA_QUERY_FUNCTION_NAME     — name of the data_query Lambda
  BEDROCK_REGION               — Bedrock region (default: us-east-1)
  BEDROCK_GENERATION_MODEL     — Nova Lite model ID
"""
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.environ.get("BEDROCK_REGION", "us-east-1"),
)
_lambda = boto3.client("lambda")

_CLASSIFY_SYSTEM = """\
Classify a home energy system question as "documents", "data", or "anomalies".

documents — asks about how something works, rates, specs, policies, or equipment:
  "What are peak hours on my rate plan?"
  "What is my battery's max discharge rate?"
  "How does NEM 3.0 affect my solar credits?"

data — asks about specific measurements, history, or numbers from the system:
  "How much did I produce yesterday?"
  "What was my battery SOC last Tuesday?"
  "Which day last month had the most solar?"
  "What's my average daily production this week?"

anomalies — asks about problems, issues, alerts, or unusual behaviour:
  "Were there any problems this week?"
  "Why was my production low yesterday?"
  "Any anomalies recently?"
  "Has anything unusual happened with my system?"
  "Are there any alerts?"

Reply with exactly one word: documents, data, or anomalies.
"""


def _classify(query: str) -> str:
    model_id = os.environ.get("BEDROCK_GENERATION_MODEL", "us.amazon.nova-lite-v1:0")
    body = {
        "system": [{"text": _CLASSIFY_SYSTEM}],
        "messages": [{"role": "user", "content": [{"text": query}]}],
        "inferenceConfig": {"max_new_tokens": 8},
    }
    resp = _bedrock.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    raw = json.loads(resp["body"].read())["output"]["message"]["content"][0]["text"]
    result = raw.strip().lower()
    if result.startswith("data"):
        return "data"
    if result.startswith("anomal"):
        return "anomalies"
    return "documents"


def _invoke_lambda(function_name: str, event: dict) -> dict:
    resp = _lambda.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )
    return json.loads(resp["Payload"].read())


def lambda_handler(event: dict, context) -> dict:
    cors_headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

    try:
        body = json.loads(event.get("body") or "{}")
        query = (body.get("query") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        return {"statusCode": 400, "headers": cors_headers,
                "body": json.dumps({"error": "Request body must be JSON with a 'query' field"})}

    if not query:
        return {"statusCode": 400, "headers": cors_headers,
                "body": json.dumps({"error": "'query' field is required"})}

    try:
        route = _classify(query)
        logger.info("Query routed to: %s — %.200s", route, query)

        if route == "data":
            function_name = os.environ["DATA_QUERY_FUNCTION_NAME"]
        elif route == "anomalies":
            function_name = os.environ["ANOMALY_QUERY_FUNCTION_NAME"]
        else:
            function_name = os.environ["RAG_QUERY_FUNCTION_NAME"]

        inner = _invoke_lambda(function_name, event)

        # Inject route into the response body so the frontend can show it
        try:
            inner_body = json.loads(inner.get("body", "{}"))
            inner_body["route"] = route
            inner["body"] = json.dumps(inner_body)
        except (json.JSONDecodeError, TypeError):
            pass

        return inner

    except ClientError as exc:
        logger.error("AWS service error: %s", exc)
        return {"statusCode": 502, "headers": cors_headers,
                "body": json.dumps({"error": "Upstream AWS service error"})}
    except KeyError as exc:
        logger.error("Missing env var: %s", exc)
        return {"statusCode": 503, "headers": cors_headers,
                "body": json.dumps({"error": f"Router misconfigured — missing env var: {exc}"})}
    except Exception as exc:
        logger.exception("chat router error: %s", exc)
        return {"statusCode": 500, "headers": cors_headers,
                "body": json.dumps({"error": "Internal error"})}
