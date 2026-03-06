"""
Lambda: recommendation
GET /recommendation

Analyzes today's solar forecast and TOU rate schedule to recommend the
best window to charge the EV. Currently uses mock solar data; later will
read from DynamoDB after the ingest Lambda populates it.

BMW iX 45 specs:
  Battery: 76.6 kWh usable
  On-board charger: 11 kW AC (Level 2)
  Typical home charge rate: 7.2–11 kW depending on EVSE
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone

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

# BMW iX 45 defaults (user will be able to override via config table later)
EV_BATTERY_KWH = 76.6
EV_CHARGE_RATE_KW = 7.2   # typical Level 2 home charging rate
DEFAULT_TARGET_SOC = 0.80  # charge to 80% by default

# PG&E E-TOU-C simplified (hourly rate in $/kWh, Pacific time)
HOURLY_RATES = {
    0: 0.28, 1: 0.28, 2: 0.28, 3: 0.28, 4: 0.28, 5: 0.28,
    6: 0.28, 7: 0.28, 8: 0.28,
    9: 0.17, 10: 0.17, 11: 0.17, 12: 0.17, 13: 0.17,  # super off-peak
    14: 0.28, 15: 0.28,
    16: 0.48, 17: 0.48, 18: 0.48, 19: 0.48, 20: 0.48,  # peak
    21: 0.28, 22: 0.28, 23: 0.28,
}

# Approximate hourly solar production (Wh) — same mock profile as solar_data
MOCK_HOURLY_SOLAR_WH = [
    0, 0, 0, 0, 0, 0, 0,
    120, 680, 1540, 2310, 2870,
    3100, 3050, 2820, 2450,
    1920, 1280, 540, 80, 0,
    0, 0, 0,
]


def _score_window(start_hour: int, duration_hours: int) -> dict:
    """Score a charging window by cost and solar coverage."""
    hours = [(start_hour + i) % 24 for i in range(duration_hours)]
    total_cost = sum(HOURLY_RATES.get(h, 0.28) * EV_CHARGE_RATE_KW for h in hours)
    solar_wh = sum(MOCK_HOURLY_SOLAR_WH[h] for h in hours)
    # Assume home uses ~500 Wh/hr baseline; excess solar goes to EV
    net_solar_for_ev_kwh = max(0, solar_wh - 500 * duration_hours) / 1000
    solar_coverage = min(net_solar_for_ev_kwh / (EV_CHARGE_RATE_KW * duration_hours), 1.0)
    # Lower score = better (low cost + high solar is ideal)
    adjusted_cost = total_cost * (1 - solar_coverage * 0.8)
    return {
        "start_hour": start_hour,
        "end_hour": (start_hour + duration_hours) % 24,
        "duration_hours": duration_hours,
        "estimated_cost_usd": round(total_cost, 2),
        "solar_coverage_pct": round(solar_coverage * 100, 1),
        "score": round(adjusted_cost, 3),
    }


def lambda_handler(event: dict, context) -> dict:
    logger.info("recommendation invoked: %s", json.dumps(event))

    try:
        params = event.get("queryStringParameters") or {}
        current_soc = float(params.get("current_soc", 0.30))   # 0.0–1.0
        target_soc = float(params.get("target_soc", DEFAULT_TARGET_SOC))

        energy_needed_kwh = EV_BATTERY_KWH * (target_soc - current_soc)
        energy_needed_kwh = max(0.0, energy_needed_kwh)
        hours_needed = max(1, round(energy_needed_kwh / EV_CHARGE_RATE_KW))

        # Score every possible start hour
        candidates = []
        for start in range(24):
            if start + hours_needed > 24:
                continue  # don't wrap past midnight for simplicity
            candidates.append(_score_window(start, hours_needed))

        candidates.sort(key=lambda x: x["score"])
        best = candidates[0]

        # Characterize the window
        avg_rate = HOURLY_RATES.get(best["start_hour"], 0.28)
        if avg_rate <= 0.17:
            rate_label = "super off-peak"
        elif avg_rate <= 0.28:
            rate_label = "off-peak"
        else:
            rate_label = "peak"

        recommendation = {
            "date": today_iso(),
            "ev_model": "2026 BMW iX 45",
            "battery_kwh": EV_BATTERY_KWH,
            "charge_rate_kw": EV_CHARGE_RATE_KW,
            "current_soc_pct": round(current_soc * 100),
            "target_soc_pct": round(target_soc * 100),
            "energy_needed_kwh": round(energy_needed_kwh, 1),
            "hours_needed": hours_needed,
            "best_window": {
                "start": f"{best['start_hour']:02d}:00",
                "end": f"{best['end_hour']:02d}:00",
                "rate_period": rate_label,
                "estimated_cost_usd": best["estimated_cost_usd"],
                "solar_coverage_pct": best["solar_coverage_pct"],
            },
            "summary": (
                f"Charge from {best['start_hour']:02d}:00–{best['end_hour']:02d}:00 "
                f"({rate_label}, ~${best['estimated_cost_usd']:.2f}, "
                f"{best['solar_coverage_pct']:.0f}% solar coverage)"
            ),
            "all_candidates": candidates[:5],  # top 5 windows for the UI
            "data_source": "mock",
        }

        return api_response(200, recommendation)

    except Exception as exc:
        logger.exception("recommendation error: %s", exc)
        return api_response(500, {"error": "Internal server error", "detail": str(exc)})
