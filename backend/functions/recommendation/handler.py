"""
Lambda: recommendation
GET /recommendation

Analyzes today's solar production and TOU rate schedule to recommend the
best window to charge the EV. Reads real hourly production from DynamoDB
(written by the ingest Lambda); falls back to a mock profile if no data exists.

BMW iX 45 specs:
  Battery: 76.6 kWh usable
  On-board charger: 11 kW AC (Level 2)
  Typical home charge rate: 7.2–11 kW depending on EVSE
"""
import json
import logging
import os
import sys
from datetime import date as date_cls, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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

# BMW iX 45 defaults
EV_BATTERY_KWH = 76.6
EV_CHARGE_RATE_KW = 7.2
DEFAULT_TARGET_SOC = 0.80

# PG&E E-TOU-C ($/kWh, Pacific time hours)
HOURLY_RATES = {
    0: 0.28, 1: 0.28, 2: 0.28, 3: 0.28, 4: 0.28, 5: 0.28,
    6: 0.28, 7: 0.28, 8: 0.28,
    9: 0.17, 10: 0.17, 11: 0.17, 12: 0.17, 13: 0.17,  # super off-peak
    14: 0.28, 15: 0.28,
    16: 0.48, 17: 0.48, 18: 0.48, 19: 0.48, 20: 0.48,  # peak
    21: 0.28, 22: 0.28, 23: 0.28,
}

# Fallback mock profile (Wh per hour) — sunny California day
MOCK_HOURLY_SOLAR_WH = [
    0, 0, 0, 0, 0, 0, 0,
    120, 680, 1540, 2310, 2870,
    3100, 3050, 2820, 2450,
    1920, 1280, 540, 80, 0,
    0, 0, 0,
]


# ---------------------------------------------------------------------------
# DynamoDB — fetch today's real per-hour solar production
# ---------------------------------------------------------------------------

def _get_hourly_solar_wh(
    table_name: str, system_id: str, date_str: str
) -> tuple[list[int], str, int | None]:
    """
    Return a 24-slot list of per-Pacific-hour solar production (Wh) indexed
    0–23, a data_source string ("enphase" or "mock"), and the most-recent
    home battery SOC percentage (or None if unavailable).

    Queries both the requested UTC date and the next UTC date (a Pacific day
    crosses midnight UTC), then filters by summary_date to remove yesterday's
    evening bleed. Items are processed in chronological order so energy diffs
    are always correct across the Pacific-day boundary.
    """
    if not (table_name and system_id):
        return list(MOCK_HOURLY_SOLAR_WH), "mock", None

    try:
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

        # Keep only rows Enphase reported for this local date
        items = [i for i in all_items if i.get("summary_date") == date_str]
        items = sorted(items, key=lambda x: x["timestamp"])

        if not items:
            logger.info("No DynamoDB rows for %s — recommendation using mock solar", date_str)
            return list(MOCK_HOURLY_SOLAR_WH), "mock", None

        # Diff consecutive cumulative totals → per-snapshot production,
        # then accumulate by Pacific local hour
        by_local_hour: dict[int, int] = {}
        prev = 0
        for item in items:
            ts = item.get("timestamp", "")
            try:
                dt         = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local_hour = dt.astimezone(PACIFIC).hour
            except (ValueError, AttributeError):
                logger.warning("Skipping unparseable timestamp: %s", ts)
                continue
            energy = int(item.get("energy_wh", 0))
            by_local_hour[local_hour] = by_local_hour.get(local_hour, 0) + max(0, energy - prev)
            prev = energy

        hourly = [by_local_hour.get(h, 0) for h in range(24)]

        # Most-recent item that includes a battery SOC reading
        battery_soc_pct: int | None = None
        for item in reversed(items):
            if "battery_soc_pct" in item:
                battery_soc_pct = int(item["battery_soc_pct"])
                break

        logger.info(
            "Loaded %d snapshots for recommendation (%s), battery_soc=%s%%",
            len(items), date_str,
            battery_soc_pct if battery_soc_pct is not None else "n/a",
        )
        return hourly, "enphase", battery_soc_pct

    except Exception as exc:
        logger.warning("DynamoDB query failed for recommendation (%s) — using mock", exc)
        return list(MOCK_HOURLY_SOLAR_WH), "mock", None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_window(start_hour: int, duration_hours: int, hourly_solar_wh: list[int]) -> dict:
    """Score a charging window by cost and solar coverage."""
    hours = [(start_hour + i) % 24 for i in range(duration_hours)]
    total_cost = sum(HOURLY_RATES.get(h, 0.28) * EV_CHARGE_RATE_KW for h in hours)
    solar_wh = sum(hourly_solar_wh[h] for h in hours)
    # Assume ~500 Wh/hr baseline home load; excess goes to the EV
    net_solar_for_ev_kwh = max(0, solar_wh - 500 * duration_hours) / 1000
    solar_coverage = min(net_solar_for_ev_kwh / (EV_CHARGE_RATE_KW * duration_hours), 1.0)
    # Lower score = better (low cost + high solar coverage)
    adjusted_cost = total_cost * (1 - solar_coverage * 0.8)
    return {
        "start_hour": start_hour,
        "end_hour": (start_hour + duration_hours) % 24,
        "duration_hours": duration_hours,
        "estimated_cost_usd": round(total_cost, 2),
        "solar_coverage_pct": round(solar_coverage * 100, 1),
        "score": round(adjusted_cost, 3),
    }


def _charging_source(
    battery_soc_pct: int | None,
    solar_coverage_pct: float,
    rate_label: str,
) -> tuple[str, str]:
    """
    Determine the recommended charging source based on home battery SOC and
    the best window's solar coverage.

    Returns (source_key, human_label) where source_key is one of:
      "solar_direct"       — window covered mostly by solar, no grid needed
      "solar_plus_battery" — solar + home battery can cover most of charging
      "home_battery"       — use stored home battery to avoid peak grid rates
      "grid"               — rely on cheapest available grid rate
    """
    if solar_coverage_pct >= 70:
        if battery_soc_pct is not None and battery_soc_pct >= 60:
            return "solar_plus_battery", "Solar + Home Battery"
        return "solar_direct", "Direct Solar"
    if battery_soc_pct is not None and battery_soc_pct >= 60 and rate_label == "peak":
        return "home_battery", "Home Battery (avoid peak)"
    if battery_soc_pct is not None and battery_soc_pct >= 80 and solar_coverage_pct >= 30:
        return "solar_plus_battery", "Solar + Home Battery"
    return "grid", f"Grid ({rate_label.title()})"


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    logger.info("recommendation invoked: %s", json.dumps(event))

    try:
        params = event.get("queryStringParameters") or {}
        current_soc  = float(params.get("current_soc", 0.30))
        target_soc   = float(params.get("target_soc", DEFAULT_TARGET_SOC))
        date_str     = params.get("date", today_iso())
        system_id    = os.environ.get("ENPHASE_SYSTEM_ID", "")
        table_name   = os.environ.get("ENERGY_TABLE", "")

        energy_needed_kwh = max(0.0, EV_BATTERY_KWH * (target_soc - current_soc))
        hours_needed = max(1, round(energy_needed_kwh / EV_CHARGE_RATE_KW))

        # Fetch real (or mock) solar production for scoring
        hourly_solar_wh, data_source, battery_soc_pct = _get_hourly_solar_wh(table_name, system_id, date_str)

        # Score every valid start hour
        candidates = []
        for start in range(24):
            if start + hours_needed > 24:
                continue
            candidates.append(_score_window(start, hours_needed, hourly_solar_wh))
        candidates.sort(key=lambda x: x["score"])
        best = candidates[0]

        avg_rate = HOURLY_RATES.get(best["start_hour"], 0.28)
        if avg_rate <= 0.17:
            rate_label = "super off-peak"
        elif avg_rate <= 0.28:
            rate_label = "off-peak"
        else:
            rate_label = "peak"

        recommendation = {
            "date":               date_str,
            "ev_model":           "2026 BMW iX 45",
            "battery_kwh":        EV_BATTERY_KWH,
            "charge_rate_kw":     EV_CHARGE_RATE_KW,
            "current_soc_pct":    round(current_soc * 100),
            "target_soc_pct":     round(target_soc * 100),
            "energy_needed_kwh":  round(energy_needed_kwh, 1),
            "hours_needed":       hours_needed,
            "best_window": {
                "start":               f"{best['start_hour']:02d}:00",
                "end":                 f"{best['end_hour']:02d}:00",
                "rate_period":         rate_label,
                "estimated_cost_usd":  best["estimated_cost_usd"],
                "solar_coverage_pct":  best["solar_coverage_pct"],
            },
            "summary": (
                f"Charge from {best['start_hour']:02d}:00–{best['end_hour']:02d}:00 "
                f"({rate_label}, ~${best['estimated_cost_usd']:.2f}, "
                f"{best['solar_coverage_pct']:.0f}% solar coverage)"
            ),
            "all_candidates":      candidates[:5],
            "data_source":         data_source,
            "home_battery_soc_pct": battery_soc_pct,
        }

        logger.info(
            "Recommendation: %s–%s, cost=$%.2f, solar=%.0f%%, source=%s",
            best["start_hour"], best["end_hour"],
            best["estimated_cost_usd"], best["solar_coverage_pct"], data_source,
        )
        return api_response(200, recommendation)

    except Exception as exc:
        logger.exception("recommendation error: %s", exc)
        return api_response(500, {"error": "Internal server error", "detail": str(exc)})
