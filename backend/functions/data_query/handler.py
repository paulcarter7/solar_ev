"""
Lambda: data_query
POST /data-query — answers natural language questions about energy data in DynamoDB.

Uses Nova Lite to extract a structured query intent, executes a DynamoDB
query, aggregates the results in Python, then formats the answer naturally.

Environment variables:
  ENERGY_TABLE                 — DynamoDB table name
  ENPHASE_SYSTEM_ID            — Enphase system ID (PK prefix)
  BEDROCK_REGION               — Bedrock region (default: us-east-1)
  BEDROCK_GENERATION_MODEL     — Nova Lite model ID

Local dev:
  ENPHASE_SYSTEM_ID and ENERGY_TABLE must be set in backend/.env
"""
import json
import logging
import os
from datetime import date as date_cls, datetime, timedelta
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

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_INTENT_SYSTEM = """\
You extract structured query intent from natural language questions about a home energy system.
Return ONLY a JSON object — no markdown, no explanation.

Schema:
{{
  "metric": "production_wh" | "battery_soc_pct" | "power_w",
  "aggregation": "total" | "average" | "maximum" | "minimum" | "latest",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD"
}}

Rules:
- production_wh: solar energy produced (Wh/kWh questions)
- battery_soc_pct: home battery state of charge (%)
- power_w: instantaneous solar power output (W/kW)
- total: sum over the period (default for production questions)
- average: mean over all readings in the period
- maximum: highest single value (use for "peak", "most", "best day")
- minimum: lowest single value (use for "worst day", "least")
- latest: most recent reading (use for "current", "right now", "what is")
- Today's date is {today}. Yesterday is {yesterday}.
- "last week" means Monday–Sunday of the previous calendar week.
- "this week" means Monday through today.
- "last month" means the full previous calendar month.
"""

_FORMAT_SYSTEM = """\
You are an energy advisor assistant for a home solar, battery, and EV system.
Format a data query result as a friendly, concise natural language answer (1–2 sentences).
Be specific with numbers. Convert Wh to kWh (divide by 1000, round to 2 decimal places).
Do not add information that was not in the result.
If value is null, say the data is not available for that period.
"""


# ---------------------------------------------------------------------------
# Intent extraction
# ---------------------------------------------------------------------------

def _extract_intent(query: str, today: str) -> dict:
    yesterday = (date_cls.fromisoformat(today) - timedelta(days=1)).isoformat()
    system = _INTENT_SYSTEM.format(today=today, yesterday=yesterday)

    body = {
        "system": [{"text": system}],
        "messages": [{"role": "user", "content": [{"text": query}]}],
        "inferenceConfig": {"max_new_tokens": 128},
    }
    resp = _bedrock.invoke_model(
        modelId=os.environ.get("BEDROCK_GENERATION_MODEL", "us.amazon.nova-lite-v1:0"),
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    raw = json.loads(resp["body"].read())["output"]["message"]["content"][0]["text"]
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# DynamoDB fetch
# ---------------------------------------------------------------------------

def _fetch_days(table_name: str, device_id: str, start: str, end: str) -> dict[str, list[dict]]:
    """
    Return DynamoDB rows grouped by summary_date for every day in [start, end].

    Queries both the UTC date and the following UTC date for each Pacific day
    (a Pacific calendar day crosses a UTC midnight boundary), then filters by
    summary_date to keep only rows belonging to that Pacific day.
    """
    table = _dynamo.Table(table_name)
    rows_by_day: dict[str, list[dict]] = {}
    current = date_cls.fromisoformat(start)
    end_date = date_cls.fromisoformat(end)

    while current <= end_date:
        date_str = current.isoformat()
        next_date = (current + timedelta(days=1)).isoformat()

        items: list[dict] = []
        for query_date in (date_str, next_date):
            resp = table.query(
                KeyConditionExpression=(
                    Key("deviceId").eq(device_id) &
                    Key("timestamp").begins_with(query_date)
                )
            )
            items.extend(resp.get("Items", []))

        day_items = [i for i in items if i.get("summary_date") == date_str]
        if day_items:
            rows_by_day[date_str] = sorted(day_items, key=lambda x: x["timestamp"])

        current += timedelta(days=1)

    return rows_by_day


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _daily_production_wh(rows: list[dict]) -> float:
    """
    Daily solar production from cumulative energy snapshots.
    energy_wh resets to 0 at Pacific midnight — max value = day's total.
    """
    values = [float(r["energy_wh"]) for r in rows if "energy_wh" in r]
    return max(values) if values else 0.0


def _aggregate(rows_by_day: dict[str, list[dict]], intent: dict) -> dict:
    metric = intent["metric"]
    aggregation = intent["aggregation"]

    if not rows_by_day:
        return {"value": None, "unit": _unit(metric), "days_with_data": 0}

    if metric == "production_wh":
        daily = {d: _daily_production_wh(rows) for d, rows in rows_by_day.items()}
        if aggregation == "total":
            return {"value": round(sum(daily.values()), 1), "unit": "Wh", "days_with_data": len(daily)}
        if aggregation == "average":
            return {"value": round(sum(daily.values()) / len(daily), 1), "unit": "Wh", "days_with_data": len(daily)}
        if aggregation == "maximum":
            best = max(daily, key=daily.__getitem__)
            return {"value": round(daily[best], 1), "unit": "Wh", "best_day": best, "days_with_data": len(daily)}
        if aggregation == "minimum":
            worst = min(daily, key=daily.__getitem__)
            return {"value": round(daily[worst], 1), "unit": "Wh", "worst_day": worst, "days_with_data": len(daily)}
        return {"value": round(sum(daily.values()), 1), "unit": "Wh", "days_with_data": len(daily)}

    # Flatten all readings for battery_soc_pct and power_w
    readings = [
        float(row[metric])
        for rows in rows_by_day.values()
        for row in rows
        if metric in row
    ]

    if not readings:
        return {"value": None, "unit": _unit(metric), "days_with_data": 0}

    if aggregation in ("latest",):
        value = readings[-1]
    elif aggregation == "maximum":
        value = max(readings)
    elif aggregation == "minimum":
        value = min(readings)
    else:
        value = sum(readings) / len(readings)

    return {"value": round(value, 1), "unit": _unit(metric), "days_with_data": len(rows_by_day)}


def _unit(metric: str) -> str:
    return {"production_wh": "Wh", "battery_soc_pct": "%", "power_w": "W"}.get(metric, "")


# ---------------------------------------------------------------------------
# Answer formatting
# ---------------------------------------------------------------------------

def _format_answer(query: str, intent: dict, result: dict) -> str:
    context = (
        f"User question: {query}\n"
        f"Query intent: {json.dumps(intent)}\n"
        f"Result: {json.dumps(result)}"
    )
    body = {
        "system": [{"text": _FORMAT_SYSTEM}],
        "messages": [{"role": "user", "content": [{"text": context}]}],
        "inferenceConfig": {"max_new_tokens": 256},
    }
    resp = _bedrock.invoke_model(
        modelId=os.environ.get("BEDROCK_GENERATION_MODEL", "us.amazon.nova-lite-v1:0"),
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())["output"]["message"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

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

    table_name = os.environ.get("ENERGY_TABLE", "")
    system_id = os.environ.get("ENPHASE_SYSTEM_ID", "")
    if not table_name or not system_id:
        return {"statusCode": 503, "headers": cors_headers,
                "body": json.dumps({"error": "ENERGY_TABLE or ENPHASE_SYSTEM_ID not configured"})}

    logger.info("data_query: %.200s", query)

    try:
        today = datetime.now(tz=PACIFIC).date().isoformat()
        intent = _extract_intent(query, today)
        logger.info("Intent: %s", json.dumps(intent))

        rows_by_day = _fetch_days(table_name, f"enphase-{system_id}",
                                  intent["start_date"], intent["end_date"])

        if not rows_by_day:
            return {
                "statusCode": 200,
                "headers": cors_headers,
                "body": json.dumps({
                    "response": f"I don't have any energy data for {intent['start_date']} to {intent['end_date']}.",
                    "intent": intent,
                    "result": None,
                }),
            }

        result = _aggregate(rows_by_day, intent)
        logger.info("Result: %s", json.dumps(result, default=str))

        answer = _format_answer(query, intent, result)
        return {
            "statusCode": 200,
            "headers": cors_headers,
            "body": json.dumps({"response": answer, "intent": intent, "result": result}),
        }

    except ClientError as exc:
        logger.error("AWS service error: %s", exc)
        return {"statusCode": 502, "headers": cors_headers,
                "body": json.dumps({"error": "Upstream AWS service error"})}
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Intent extraction failed: %s", exc)
        return {"statusCode": 422, "headers": cors_headers,
                "body": json.dumps({"error": "Could not understand that query — try rephrasing it."})}
    except Exception as exc:
        logger.exception("data_query error: %s", exc)
        return {"statusCode": 500, "headers": cors_headers,
                "body": json.dumps({"error": "Internal error"})}
