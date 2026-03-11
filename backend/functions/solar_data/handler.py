"""
Lambda: solar_data
GET /solar/today?date=YYYY-MM-DD

Returns today's solar production data. Queries DynamoDB for real Enphase
ingest snapshots; falls back to mock data if the table is empty or unavailable.

DynamoDB row shape (written by ingest/handler.py):
  deviceId     PK  "enphase-{system_id}"
  timestamp    SK  ISO-8601 UTC (last_report_at from Enphase)
  energy_wh       cumulative energy_today for the day (Wh)
  power_w         instantaneous power at time of ingest (W)
  summary_date    YYYY-MM-DD in local/Pacific time (from Enphase API)

NOTE on UTC vs. local dates
----------------------------
Enphase stores energy_today as a *daily cumulative* that resets at local
(Pacific) midnight. A single Pacific calendar day spans two UTC dates — e.g.
Pacific 2026-03-10 runs from UTC 2026-03-10T08:00 through UTC 2026-03-11T07:59.

We therefore:
  1. Query DynamoDB for both the requested UTC date and the next UTC date.
  2. Filter results to rows where summary_date == requested date (Enphase's
     local-date field), discarding yesterday's evening bleed.
  3. Process rows in chronological order so energy diffs are always correct.
  4. Bucket production by Pacific local hour so slots align with the TOU
     schedule (which is also in Pacific time).
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

# PG&E / MCE E-TOU-C rate schedule (simplified, Pacific time)
TOU_SCHEDULE = [
    {"label": "Super Off-Peak", "start": "09:00", "end": "14:00", "rate_kwh": 0.17, "color": "#22c55e"},
    {"label": "Off-Peak",       "start": "00:00", "end": "09:00", "rate_kwh": 0.28, "color": "#f59e0b"},
    {"label": "Off-Peak",       "start": "14:00", "end": "16:00", "rate_kwh": 0.28, "color": "#f59e0b"},
    {"label": "Peak",           "start": "16:00", "end": "21:00", "rate_kwh": 0.48, "color": "#ef4444"},
    {"label": "Off-Peak",       "start": "21:00", "end": "24:00", "rate_kwh": 0.28, "color": "#f59e0b"},
]


# ---------------------------------------------------------------------------
# DynamoDB path
# ---------------------------------------------------------------------------

def _query_dynamo(table_name: str, system_id: str, date_str: str) -> list[dict]:
    """
    Return ingest snapshot rows for the given LOCAL date, sorted by timestamp.

    We query both the requested UTC date and the following UTC date (a Pacific
    day crosses a UTC midnight boundary), then keep only rows where
    summary_date matches the requested date.
    """
    next_date = (date_cls.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    table     = _dynamo.Table(table_name)
    device_id = f"enphase-{system_id}"

    all_items: list[dict] = []
    for query_date in (date_str, next_date):
        resp = table.query(
            KeyConditionExpression=(
                Key("deviceId").eq(device_id) &
                Key("timestamp").begins_with(query_date)
            )
        )
        all_items.extend(resp.get("Items", []))

    # Keep only rows Enphase reported as belonging to the requested local date
    items = [i for i in all_items if i.get("summary_date") == date_str]
    return sorted(items, key=lambda x: x["timestamp"])


def _readings_from_dynamo(items: list[dict], date_str: str) -> list[dict]:
    """
    Convert DynamoDB snapshots into 24 per-local-hour production readings.

    Items must be sorted chronologically. Diffs are computed sequentially so
    the Enphase daily energy reset at Pacific midnight is handled correctly.
    Output slots are in Pacific local hours (0–23) to match the TOU schedule.
    Timestamps in the output are local-time strings (no Z suffix) so the
    browser's Date constructor returns the correct local hour directly.
    """
    by_local_hour: dict[int, dict] = {}
    prev_energy_wh = 0

    for item in items:
        ts = item.get("timestamp", "")
        try:
            dt         = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            local_hour = dt.astimezone(PACIFIC).hour
        except (ValueError, AttributeError):
            logger.warning("Skipping row with unparseable timestamp: %s", ts)
            continue

        energy_wh     = int(item.get("energy_wh", 0))
        production_wh = max(0, energy_wh - prev_energy_wh)
        prev_energy_wh = energy_wh
        power_w        = int(item.get("power_w", 0))

        # Multiple snapshots in the same hour → accumulate production, keep latest power
        if local_hour in by_local_hour:
            by_local_hour[local_hour]["production_wh"] += production_wh
            by_local_hour[local_hour]["power_w"]        = power_w
        else:
            by_local_hour[local_hour] = {"production_wh": production_wh, "power_w": power_w}

    readings = []
    for hour in range(24):
        # Local-time timestamp — browser getHours() returns this hour directly
        ts = f"{date_str}T{hour:02d}:00:00"
        if hour in by_local_hour:
            readings.append({
                "timestamp":     ts,
                "hour":          hour,
                "production_wh": by_local_hour[hour]["production_wh"],
                "power_w":       by_local_hour[hour]["power_w"],
                "source":        "enphase",
            })
        else:
            readings.append({
                "timestamp":     ts,
                "hour":          hour,
                "production_wh": 0,
                "power_w":       0,
                "source":        "no_data",
            })

    return readings


# ---------------------------------------------------------------------------
# Mock fallback
# ---------------------------------------------------------------------------

def _mock_solar_readings(date_str: str) -> list[dict]:
    """
    Realistic-looking hourly solar production for a sunny California day.
    Used when no real DynamoDB data exists for the requested date.
    """
    import random
    hourly_profile = [
        0, 0, 0, 0, 0, 0, 0,        # 00-06: night
        120, 680, 1540, 2310, 2870,  # 07-11: morning ramp
        3100, 3050, 2820, 2450,      # 12-15: midday plateau
        1920, 1280, 540, 80, 0,      # 16-20: afternoon decline
        0, 0, 0,                      # 21-23: night
    ]
    readings = []
    for hour, wh in enumerate(hourly_profile):
        jitter = random.uniform(0.93, 1.07) if wh > 0 else 1.0
        readings.append({
            "timestamp":     f"{date_str}T{hour:02d}:00:00Z",
            "hour":          hour,
            "production_wh": round(wh * jitter),
            "power_w":       round(wh * jitter),
            "source":        "mock",
        })
    return readings


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    logger.info("solar_data invoked: %s", json.dumps(event))

    try:
        params     = event.get("queryStringParameters") or {}
        date_str   = params.get("date", today_iso())
        system_id  = os.environ.get("ENPHASE_SYSTEM_ID", "")
        table_name = os.environ.get("ENERGY_TABLE", "")

        readings    = None
        data_source = "mock"

        # --- Real data from DynamoDB ---
        if system_id and table_name:
            try:
                items = _query_dynamo(table_name, system_id, date_str)
                if items:
                    readings    = _readings_from_dynamo(items, date_str)
                    data_source = "enphase"
                    logger.info(
                        "DynamoDB returned %d snapshot(s) for local date %s",
                        len(items), date_str
                    )
                else:
                    logger.info("No DynamoDB rows for %s — using mock data", date_str)
            except Exception as exc:
                logger.warning("DynamoDB query failed (%s) — using mock data", exc)

        # --- Mock fallback ---
        if not readings:
            readings    = _mock_solar_readings(date_str)
            data_source = "mock"

        total_wh = sum(r["production_wh"] for r in readings)

        payload = {
            "date":                  date_str,
            "system_id":             system_id or "mock-system",
            "total_production_wh":   total_wh,
            "total_production_kwh":  round(total_wh / 1000, 2),
            "hourly_readings":       readings,
            "tou_schedule":          TOU_SCHEDULE,
            "data_source":           data_source,
        }

        logger.info(
            "Returning %d readings for %s, total %.1f Wh, source=%s",
            len(readings), date_str, total_wh, data_source,
        )
        return api_response(200, payload)

    except Exception as exc:
        logger.exception("solar_data error: %s", exc)
        return api_response(500, {"error": "Internal server error", "detail": str(exc)})
