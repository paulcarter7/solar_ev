"""
Lambda: anomaly_query
POST /anomalies — returns recent anomalies detected by the ingest pipeline
and generates a natural language summary with Nova Lite.

Environment variables:
  ANOMALY_TABLE                — DynamoDB anomaly table name
  ENPHASE_SYSTEM_ID            — Enphase system ID (PK prefix)
  BEDROCK_REGION               — Bedrock region (default: us-east-1)
  BEDROCK_GENERATION_MODEL     — Nova Lite model ID
"""
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

PACIFIC = ZoneInfo("America/Los_Angeles")
_dynamo = boto3.resource("dynamodb")
_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.environ.get("BEDROCK_REGION", "us-east-1"),
)

_DEFAULT_LOOKBACK_DAYS = 7

_SUMMARY_SYSTEM = """\
You are an energy advisor assistant. Summarise a list of anomalies detected
in a home solar, battery, and EV charging system. Be concise and specific.
Group similar anomalies. If there are no anomalies, say the system looks healthy.
Do not add information that wasn't in the anomaly list.
"""


def _fetch_anomalies(table_name: str, system_id: str, days: int) -> list[dict]:
    """Query the anomaly table for the past N days."""
    table = _dynamo.Table(table_name)
    since = (datetime.now(tz=PACIFIC) - timedelta(days=days)).isoformat()
    resp = table.query(
        KeyConditionExpression=(
            Key("systemId").eq(f"enphase-{system_id}") &
            Key("timestamp").gte(since)
        ),
        ScanIndexForward=False,  # newest first
    )
    return resp.get("Items", [])


def _summarise(query: str, anomalies: list[dict]) -> str:
    model_id = os.environ.get("BEDROCK_GENERATION_MODEL", "us.amazon.nova-lite-v1:0")
    if anomalies:
        anomaly_text = "\n".join(
            f"- [{a['type']} / {a['severity']}] {a['description']}" for a in anomalies
        )
        user_text = f"User question: {query}\n\nAnomalies detected (newest first):\n{anomaly_text}"
    else:
        user_text = f"User question: {query}\n\nNo anomalies detected in this period."

    body = {
        "system": [{"text": _SUMMARY_SYSTEM}],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        "inferenceConfig": {"max_new_tokens": 512},
    }
    resp = _bedrock.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())["output"]["message"]["content"][0]["text"]


def lambda_handler(event: dict, context) -> dict:
    cors_headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

    try:
        body = json.loads(event.get("body") or "{}")
        query = (body.get("query") or "").strip()
        days = int(body.get("days", _DEFAULT_LOOKBACK_DAYS))
    except (json.JSONDecodeError, AttributeError, ValueError):
        return {"statusCode": 400, "headers": cors_headers,
                "body": json.dumps({"error": "Request body must be JSON"})}

    if not query:
        return {"statusCode": 400, "headers": cors_headers,
                "body": json.dumps({"error": "'query' field is required"})}

    table_name = os.environ.get("ANOMALY_TABLE", "")
    system_id = os.environ.get("ENPHASE_SYSTEM_ID", "")
    if not table_name or not system_id:
        return {"statusCode": 503, "headers": cors_headers,
                "body": json.dumps({"error": "ANOMALY_TABLE or ENPHASE_SYSTEM_ID not configured"})}

    logger.info("anomaly_query: %.200s (days=%d)", query, days)

    try:
        anomalies = _fetch_anomalies(table_name, system_id, days)
        logger.info("Found %d anomalies in past %d days", len(anomalies), days)

        response = _summarise(query, anomalies)
        return {
            "statusCode": 200,
            "headers": cors_headers,
            "body": json.dumps({
                "response": response,
                "anomalies": [
                    {"type": a["type"], "severity": a["severity"],
                     "description": a["description"], "timestamp": a["timestamp"]}
                    for a in anomalies
                ],
                "days_searched": days,
            }),
        }

    except ClientError as exc:
        logger.error("AWS service error: %s", exc)
        return {"statusCode": 502, "headers": cors_headers,
                "body": json.dumps({"error": "Upstream AWS service error"})}
    except Exception as exc:
        logger.exception("anomaly_query error: %s", exc)
        return {"statusCode": 500, "headers": cors_headers,
                "body": json.dumps({"error": "Internal error"})}
