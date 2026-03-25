"""
Lambda: history
GET /solar/history?days=N  (default 14, max 90)

Returns N days of daily solar production totals, queried from DynamoDB.
Falls back to realistic mock data if the table is unavailable or empty.

DynamoDB row shape (written by ingest/handler.py):
  deviceId     PK  "enphase-{system_id}"
  timestamp    SK  ISO-8601 UTC (last_report_at from Enphase)
  energy_wh       cumulative energy_today for the day (Wh) — resets at Pacific midnight
  power_w         instantaneous power at time of ingest (W)
  summary_date    YYYY-MM-DD in local/Pacific time (from Enphase API)

NOTE on daily totals
---------------------
Enphase stores energy_today as a *daily cumulative* that resets at local
(Pacific) midnight. The daily production total equals max(energy_wh) for all
items with that summary_date — we use max rather than the last item to guard
against out-of-order writes.

NOTE on UTC timestamp range
----------------------------
A single Pacific calendar day spans two UTC dates. We query a timestamp range
that starts at {start_date}T00:00:00Z (before any Pacific day begins) and ends
at {end_date+1}T08:00:00Z, which is after the latest possible Pacific midnight
in PST (UTC-8). Items are then grouped by summary_date, discarding any UTC
overlap from adjacent Pacific days.
"""
import json
import logging
import os
import sys
from datetime import date as date_cls, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../shared"))
try:
    from utils import api_response, today_iso
except ImportError:
    def api_response(status_code, body, *, cors=True):
        headers = {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        }
        return {"statusCode": status_code, "headers": headers, "body": json.dumps(body, default=str)}

    def today_iso():
        return datetime.now(timezone.utc).date().isoformat()


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_dynamo = boto3.resource("dynamodb")
PACIFIC = ZoneInfo("America/Los_Angeles")

DEFAULT_DAYS = 14
MAX_DAYS = 90


# ---------------------------------------------------------------------------
# DynamoDB path
# ---------------------------------------------------------------------------

def _query_dynamo_range(table_name: str, system_id: str, start_date: str, end_date: str) -> list[dict]:
    """
    Fetch all ingest snapshot rows for Pacific dates [start_date, end_date].

    Uses a single between query on the sort key for efficiency.
    The timestamp window is padded to cover the Pacific-to-UTC boundary:
      - start: {start_date}T00:00:00Z  (before any Pacific day in this range starts)
      - end:   {end_date+1}T08:00:00Z  (after PST midnight, which is UTC+8)

    Callers should group by summary_date to get the correct Pacific-day grouping.
    """
    device_id = f"enphase-{system_id}"
    table = _dynamo.Table(table_name)

    start_ts = f"{start_date}T00:00:00Z"
    end_utc_date = (date_cls.fromisoformat(end_date) + timedelta(days=1)).isoformat()
    end_ts = f"{end_utc_date}T08:00:00Z"

    items: list[dict] = []
    kwargs: dict = {
        "KeyConditionExpression": (
            Key("deviceId").eq(device_id) &
            Key("timestamp").between(start_ts, end_ts)
        )
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    return items


def _compute_daily_totals(items: list[dict], start_date: str, end_date: str) -> list[dict]:
    """
    Group DynamoDB items by summary_date and compute daily production totals.

    energy_wh is cumulative from Pacific midnight, so max(energy_wh) per day
    equals total daily production. Days with no items get a zero-production entry.
    """
    by_date: dict[str, list[dict]] = {}
    for item in items:
        d = item.get("summary_date", "")
        if d:
            by_date.setdefault(d, []).append(item)

    days = []
    current = date_cls.fromisoformat(start_date)
    end = date_cls.fromisoformat(end_date)

    while current <= end:
        date_str = current.isoformat()
        date_items = by_date.get(date_str, [])
        if date_items:
            total_wh = max(int(i.get("energy_wh", 0)) for i in date_items)
            peak_w = max(int(i.get("power_w", 0)) for i in date_items)
            days.append({
                "date": date_str,
                "total_production_kwh": round(total_wh / 1000, 2),
                "peak_power_w": peak_w,
                "data_source": "enphase",
            })
        else:
            days.append({
                "date": date_str,
                "total_production_kwh": 0.0,
                "peak_power_w": 0,
                "data_source": "no_data",
            })
        current += timedelta(days=1)

    return days


# ---------------------------------------------------------------------------
# Mock fallback
# ---------------------------------------------------------------------------

def _mock_history_days(start_date: str, end_date: str) -> list[dict]:
    """
    Generate realistic-looking daily production totals for each day in range.
    Uses the date string as a seed so the same date always returns the same values.
    """
    import random

    days = []
    current = date_cls.fromisoformat(start_date)
    end = date_cls.fromisoformat(end_date)

    while current <= end:
        date_str = current.isoformat()
        rng = random.Random(date_str)  # deterministic per date
        daily_kwh = round(rng.uniform(16.0, 27.0), 2)
        peak_w = rng.randint(3200, 4800)
        days.append({
            "date": date_str,
            "total_production_kwh": daily_kwh,
            "peak_power_w": peak_w,
            "data_source": "enphase",
        })
        current += timedelta(days=1)

    return days


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    logger.info("history invoked: %s", json.dumps(event))

    try:
        params = event.get("queryStringParameters") or {}

        try:
            days_requested = int(params.get("days", DEFAULT_DAYS))
        except (ValueError, TypeError):
            return api_response(400, {"error": "Invalid 'days' parameter — must be an integer"})

        days_requested = max(1, min(MAX_DAYS, days_requested))

        system_id = os.environ.get("ENPHASE_SYSTEM_ID", "")
        table_name = os.environ.get("ENERGY_TABLE", "")

        # Compute date range in Pacific time so day boundaries align with local midnight
        today_pacific = datetime.now(PACIFIC).date()
        end_date = today_pacific.isoformat()
        start_date = (today_pacific - timedelta(days=days_requested - 1)).isoformat()

        days_data = None
        data_source = "mock"

        if system_id and table_name:
            try:
                items = _query_dynamo_range(table_name, system_id, start_date, end_date)
                if items:
                    days_data = _compute_daily_totals(items, start_date, end_date)
                    data_source = "enphase"
                    logger.info(
                        "DynamoDB returned %d items for %s→%s",
                        len(items), start_date, end_date,
                    )
                else:
                    logger.info("No DynamoDB items in range — using mock data")
            except Exception as exc:
                logger.warning("DynamoDB query failed (%s) — using mock data", exc)

        if days_data is None:
            days_data = _mock_history_days(start_date, end_date)
            data_source = "mock"

        production_values = [d["total_production_kwh"] for d in days_data]
        avg_kwh = round(sum(production_values) / len(production_values), 2) if production_values else 0.0

        payload = {
            "start_date": start_date,
            "end_date": end_date,
            "days_requested": days_requested,
            "days": days_data,
            "avg_production_kwh": avg_kwh,
            "data_source": data_source,
        }

        logger.info(
            "Returning %d days of history, avg=%.1f kWh, source=%s",
            len(days_data), avg_kwh, data_source,
        )
        return api_response(200, payload)

    except Exception as exc:
        logger.exception("history error: %s", exc)
        return api_response(500, {"error": "Internal server error", "detail": str(exc)})
