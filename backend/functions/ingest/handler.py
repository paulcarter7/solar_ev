"""
Lambda: ingest
Triggered hourly by EventBridge.

Fetches live solar production data from the Enphase Enlighten v4 API and
writes a snapshot reading to DynamoDB. Automatically refreshes OAuth tokens
when they expire and persists the new tokens back to SSM.

Environment variables:
  ENPHASE_SYSTEM_ID              — system ID (non-sensitive, plain env var)
  ENERGY_TABLE                   — DynamoDB table name

  Lambda — SSM paths (fetched + decrypted at runtime):
  ENPHASE_API_KEY_PARAM          — /solar-ev/enphase-api-key
  ENPHASE_ACCESS_TOKEN_PARAM     — /solar-ev/enphase-access-token
  ENPHASE_REFRESH_TOKEN_PARAM    — /solar-ev/enphase-refresh-token
  ENPHASE_CLIENT_ID_PARAM        — /solar-ev/enphase-client-id
  ENPHASE_CLIENT_SECRET_PARAM    — /solar-ev/enphase-client-secret

  Local dev — set directly in backend/.env:
  ENPHASE_API_KEY, ENPHASE_ACCESS_TOKEN, ENPHASE_REFRESH_TOKEN,
  ENPHASE_CLIENT_ID, ENPHASE_CLIENT_SECRET
"""
import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# AWS clients — created once per Lambda container
# ---------------------------------------------------------------------------
_ssm = boto3.client("ssm")
_dynamo = boto3.resource("dynamodb")

# SSM value cache — avoids repeat network calls on warm invocations
_ssm_cache: dict[str, str] = {}

ENPHASE_BASE = "https://api.enphaseenergy.com/api/v4"
ENPHASE_TOKEN_URL = "https://api.enphaseenergy.com/oauth/token"


# ---------------------------------------------------------------------------
# SSM helpers
# ---------------------------------------------------------------------------

def _get_ssm_param(path: str) -> str:
    """Fetch and cache a SecureString SSM parameter."""
    if path not in _ssm_cache:
        try:
            resp = _ssm.get_parameter(Name=path, WithDecryption=True)
            _ssm_cache[path] = resp["Parameter"]["Value"]
            logger.debug("Loaded SSM param: %s", path)
        except ClientError as exc:
            logger.error("Failed to read SSM param %s: %s", path, exc)
            raise
    return _ssm_cache[path]


def _put_ssm_param(path: str, value: str) -> None:
    """Overwrite a SecureString SSM parameter and update the local cache."""
    _ssm.put_parameter(Name=path, Value=value, Type="SecureString", Overwrite=True)
    _ssm_cache[path] = value


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def _resolve_credentials() -> tuple[str, str, str, str]:
    """
    Returns (api_key, access_token, client_id, client_secret).

    Local dev  — all four read directly from os.environ (loaded from .env).
    Lambda     — each fetched from the SSM path named by the *_PARAM env var.
    """
    api_key       = os.environ.get("ENPHASE_API_KEY", "")
    access_token  = os.environ.get("ENPHASE_ACCESS_TOKEN", "")
    client_id     = os.environ.get("ENPHASE_CLIENT_ID", "")
    client_secret = os.environ.get("ENPHASE_CLIENT_SECRET", "")

    if all(v and v != "mock" for v in [api_key, access_token, client_id, client_secret]):
        logger.debug("Using Enphase credentials from environment (local dev)")
        return api_key, access_token, client_id, client_secret

    # Lambda path — must have all four *_PARAM vars set
    api_key       = _get_ssm_param(os.environ["ENPHASE_API_KEY_PARAM"])
    access_token  = _get_ssm_param(os.environ["ENPHASE_ACCESS_TOKEN_PARAM"])
    client_id     = _get_ssm_param(os.environ["ENPHASE_CLIENT_ID_PARAM"])
    client_secret = _get_ssm_param(os.environ["ENPHASE_CLIENT_SECRET_PARAM"])
    return api_key, access_token, client_id, client_secret


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def _refresh_tokens(client_id: str, client_secret: str) -> str:
    """
    Exchange the stored refresh_token for a new access_token (and possibly a
    new refresh_token). Persists updated tokens to SSM (Lambda) or os.environ
    (local dev). Returns the new access_token.
    """
    refresh_param = os.environ.get("ENPHASE_REFRESH_TOKEN_PARAM", "")
    refresh_token = (
        _get_ssm_param(refresh_param) if refresh_param
        else os.environ.get("ENPHASE_REFRESH_TOKEN", "")
    )
    if not refresh_token:
        raise ValueError("No refresh token available — re-run the OAuth flow")

    creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()

    req = Request(
        ENPHASE_TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {creds_b64}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(req, timeout=15) as resp:
        tokens = json.loads(resp.read().decode())

    new_access  = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", refresh_token)

    access_param = os.environ.get("ENPHASE_ACCESS_TOKEN_PARAM", "")
    if access_param:
        # Lambda: persist to SSM so the next cold start uses the fresh tokens
        _put_ssm_param(access_param, new_access)
        if refresh_param:
            _put_ssm_param(refresh_param, new_refresh)
        logger.info("Refreshed Enphase OAuth tokens and updated SSM")
    else:
        # Local dev: update in-memory env (update .env manually if token expires again)
        os.environ["ENPHASE_ACCESS_TOKEN"] = new_access
        os.environ["ENPHASE_REFRESH_TOKEN"] = new_refresh
        logger.info("Refreshed Enphase tokens (local dev)")

    return new_access


# ---------------------------------------------------------------------------
# Enphase API
# ---------------------------------------------------------------------------

def _fetch_enphase_summary(
    system_id: str,
    api_key: str,
    access_token: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """
    GET /api/v4/systems/{system_id}/summary

    On a 401 the token is refreshed once and the call retried. Any further
    failure propagates to the caller.
    """
    def _call(token: str) -> dict:
        url = f"{ENPHASE_BASE}/systems/{system_id}/summary?key={api_key}"
        req = Request(url, headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        })
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    try:
        data = _call(access_token)
    except HTTPError as exc:
        if exc.code == 401:
            logger.info("Access token expired — refreshing and retrying")
            new_token = _refresh_tokens(client_id, client_secret)
            data = _call(new_token)   # let any second failure propagate
        else:
            body = exc.read().decode()
            logger.error("Enphase API HTTP %s: %s", exc.code, body)
            raise
    except URLError as exc:
        logger.error("Enphase network error: %s", exc)
        raise

    logger.info(
        "Enphase summary: power=%sW, energy_today=%sWh, date=%s",
        data.get("current_power"),
        data.get("energy_today"),
        data.get("summary_date"),
    )
    return data


# ---------------------------------------------------------------------------
# Battery SOC
# ---------------------------------------------------------------------------

def _fetch_battery_soc(system_id: str, api_key: str, access_token: str) -> int | None:
    """
    Call the Enphase battery telemetry endpoint and return the most-recent
    state-of-charge as a whole-number percentage (0-100), or None on failure.
    """
    try:
        url = f"{ENPHASE_BASE}/systems/{system_id}/telemetry/battery?key={api_key}"
        req = Request(url, headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        })
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        intervals = data.get("intervals", [])
        if intervals:
            soc = intervals[-1].get("soc", {}).get("percent")
            if soc is not None:
                soc_int = round(float(soc))
                logger.info("Battery SOC: %d%%", soc_int)
                return soc_int
        logger.info("Battery telemetry returned no intervals")
        return None
    except Exception as exc:
        logger.warning("Battery SOC fetch failed (%s) — skipping", exc)
        return None


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

def _write_reading(table, system_id: str, summary: dict, battery_soc_pct: int | None = None) -> None:
    """Write one snapshot row to DynamoDB with a 90-day TTL."""
    now = datetime.now(timezone.utc)
    ttl = int((now + timedelta(days=90)).timestamp())

    last_report_ts = summary.get("last_report_at")
    ts = (
        datetime.fromtimestamp(last_report_ts, tz=timezone.utc).isoformat()
        if last_report_ts else now.isoformat()
    )

    item = {
        "deviceId":     f"enphase-{system_id}",
        "timestamp":    ts,
        "energy_wh":    summary.get("energy_today", 0),
        "power_w":      summary.get("current_power", 0),
        "summary_date": summary.get("summary_date", now.date().isoformat()),
        "source":       "enphase",
        "ingested_at":  now.isoformat(),
        "ttl":          ttl,
    }
    if battery_soc_pct is not None:
        item["battery_soc_pct"] = battery_soc_pct
    table.put_item(Item=item)
    logger.info(
        "Wrote reading: deviceId=%s timestamp=%s energy_wh=%s power_w=%s battery_soc=%s%%",
        item["deviceId"], ts, item["energy_wh"], item["power_w"],
        battery_soc_pct if battery_soc_pct is not None else "n/a",
    )


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    now        = datetime.now(timezone.utc).isoformat()
    system_id  = os.environ.get("ENPHASE_SYSTEM_ID", "")
    table_name = os.environ.get("ENERGY_TABLE", "")

    if not system_id:
        logger.warning("[%s] ENPHASE_SYSTEM_ID not set — skipping", now)
        return {"statusCode": 200, "body": json.dumps({"ingested_at": now, "status": "skipped"})}

    try:
        api_key, access_token, client_id, client_secret = _resolve_credentials()
    except (KeyError, ValueError) as exc:
        logger.warning("[%s] Credentials not configured: %s — skipping", now, exc)
        return {"statusCode": 200, "body": json.dumps({"ingested_at": now, "status": "skipped"})}

    try:
        summary = _fetch_enphase_summary(
            system_id, api_key, access_token, client_id, client_secret
        )
        # Fetch battery SOC in parallel (non-fatal if it fails)
        battery_soc_pct = _fetch_battery_soc(system_id, api_key, access_token)
        table = _dynamo.Table(table_name)
        _write_reading(table, system_id, summary, battery_soc_pct=battery_soc_pct)
    except Exception as exc:
        logger.exception("Enphase ingest failed: %s", exc)
        return {
            "statusCode": 500,
            "body": json.dumps({"ingested_at": now, "status": "error", "error": str(exc)}),
        }

    return {
        "statusCode": 200,
        "body": json.dumps({
            "ingested_at":     now,
            "status":          "ok",
            "energy_today_wh": summary.get("energy_today"),
            "current_power_w": summary.get("current_power"),
            "battery_soc_pct": battery_soc_pct,
        }),
    }
