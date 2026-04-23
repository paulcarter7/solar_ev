"""
Lambda: ingest
Triggered hourly by EventBridge.

Fetches live solar production data from the Enphase Enlighten v4 API and
writes a snapshot reading to DynamoDB. Automatically refreshes OAuth tokens
when they expire and persists the new tokens back to SSM. Also fetches
current weather conditions from OpenWeatherMap (free tier) and includes
cloud cover, temperature, and weather condition in each DynamoDB snapshot.

Environment variables:
  ENPHASE_SYSTEM_ID              — system ID (non-sensitive, plain env var)
  ENERGY_TABLE                   — DynamoDB table name
  LOCATION_LAT                   — latitude for weather lookup (e.g. "37.8216")
  LOCATION_LON                   — longitude for weather lookup (e.g. "-121.9999")

  Lambda — SSM paths (fetched + decrypted at runtime):
  ENPHASE_API_KEY_PARAM          — /solar-ev/enphase-api-key
  ENPHASE_ACCESS_TOKEN_PARAM     — /solar-ev/enphase-access-token
  ENPHASE_REFRESH_TOKEN_PARAM    — /solar-ev/enphase-refresh-token
  ENPHASE_CLIENT_ID_PARAM        — /solar-ev/enphase-client-id
  ENPHASE_CLIENT_SECRET_PARAM    — /solar-ev/enphase-client-secret
  NTFY_TOPIC_PARAM               — /solar-ev/ntfy-topic (curtailment alert topic)
  OPENWEATHER_API_KEY_PARAM      — /solar-ev/openweather-api-key

  Local dev — set directly in backend/.env:
  ENPHASE_API_KEY, ENPHASE_ACCESS_TOKEN, ENPHASE_REFRESH_TOKEN,
  ENPHASE_CLIENT_ID, ENPHASE_CLIENT_SECRET
  NTFY_TOPIC                     — ntfy.sh topic name (local dev only)
  OPENWEATHER_API_KEY            — OWM API key (local dev only)
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
from zoneinfo import ZoneInfo

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

PACIFIC = ZoneInfo("America/Los_Angeles")


class EnphaseRateLimitError(Exception):
    """Raised when Enphase API returns HTTP 429 (rate limit / quota exceeded)."""

# ---------------------------------------------------------------------------
# AWS clients — created once per Lambda container
# ---------------------------------------------------------------------------
_ssm    = boto3.client("ssm")
_dynamo = boto3.resource("dynamodb")

NTFY_BASE = "https://ntfy.sh"
OWM_BASE  = "https://api.openweathermap.org/data/2.5"

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
        elif exc.code == 429:
            logger.warning(
                "Enphase API rate limit hit (429) — quota exceeded, blocking calls for %dh",
                _RATE_LIMIT_BLOCK_HOURS,
            )
            raise EnphaseRateLimitError(
                f"429 from /systems/{system_id}/summary"
            ) from exc
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
# Weather (OpenWeatherMap)
# ---------------------------------------------------------------------------

def _resolve_owm_api_key() -> str | None:
    """
    Return the OWM API key, or None if not configured.

    Local dev  — OPENWEATHER_API_KEY env var (set in backend/.env).
    Lambda     — fetched from SSM path in OPENWEATHER_API_KEY_PARAM env var.
    """
    direct = os.environ.get("OPENWEATHER_API_KEY", "")
    if direct:
        return direct
    param_path = os.environ.get("OPENWEATHER_API_KEY_PARAM", "")
    if param_path:
        try:
            return _get_ssm_param(param_path)
        except Exception as exc:
            logger.warning("Could not read OWM API key from SSM: %s", exc)
    return None


def _fetch_weather(lat: str, lon: str, api_key: str) -> dict | None:
    """
    Fetch current weather from OWM and return cloud cover, temperature, and
    condition string, or None on any failure (non-fatal).
    """
    try:
        url = (
            f"{OWM_BASE}/weather"
            f"?lat={lat}&lon={lon}&appid={api_key}&units=metric"
        )
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        cloud_cover_pct   = data["clouds"]["all"]
        temp_c            = round(data["main"]["temp"])
        weather_condition = data["weather"][0]["main"]

        logger.info(
            "Weather: %s, %d°C, cloud cover %d%%",
            weather_condition, temp_c, cloud_cover_pct,
        )
        return {
            "cloud_cover_pct":   cloud_cover_pct,
            "temp_c":            temp_c,
            "weather_condition": weather_condition,
        }
    except Exception as exc:
        logger.warning("Weather fetch failed (%s) — skipping", exc)
        return None


# ---------------------------------------------------------------------------
# Circuit breaker (Enphase API rate limit)
# ---------------------------------------------------------------------------

_RATE_LIMIT_USER_ID     = "default"
_RATE_LIMIT_CONFIG_KEY  = "enphase_rate_limit"
_RATE_LIMIT_BLOCK_HOURS = 24


def _check_rate_limit_block(config_table) -> bool:
    """Return True if Enphase API calls should be skipped due to an active rate-limit block."""
    resp = config_table.get_item(
        Key={"userId": _RATE_LIMIT_USER_ID, "configType": _RATE_LIMIT_CONFIG_KEY}
    )
    if "Item" not in resp:
        return False
    blocked_until = datetime.fromisoformat(resp["Item"]["blocked_until"])
    return datetime.now(timezone.utc) < blocked_until


def _set_rate_limit_block(config_table, reason: str) -> None:
    """Write a rate-limit block record that expires in _RATE_LIMIT_BLOCK_HOURS hours."""
    blocked_until = datetime.now(timezone.utc) + timedelta(hours=_RATE_LIMIT_BLOCK_HOURS)
    config_table.put_item(Item={
        "userId":        _RATE_LIMIT_USER_ID,
        "configType":    _RATE_LIMIT_CONFIG_KEY,
        "blocked_until": blocked_until.isoformat(),
        "reason":        reason,
    })
    logger.warning(
        "Enphase API rate-limit block set until %s (reason: %s)",
        blocked_until.isoformat(), reason,
    )


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

def _write_reading(
    table,
    system_id: str,
    summary: dict,
    battery_soc_pct: int | None = None,
    weather: dict | None = None,
) -> None:
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
    if weather is not None:
        item["cloud_cover_pct"]   = weather["cloud_cover_pct"]
        item["temp_c"]            = weather["temp_c"]
        item["weather_condition"] = weather["weather_condition"]
    table.put_item(Item=item)
    logger.info(
        "Wrote reading: deviceId=%s timestamp=%s energy_wh=%s power_w=%s "
        "battery_soc=%s%% weather=%s",
        item["deviceId"], ts, item["energy_wh"], item["power_w"],
        battery_soc_pct if battery_soc_pct is not None else "n/a",
        weather.get("weather_condition") if weather else "n/a",
    )


# ---------------------------------------------------------------------------
# Curtailment alert
# ---------------------------------------------------------------------------

# Alert conditions: battery nearly full + solar producing during daylight hours.
# De-duplicated via DynamoDB so we don't spam more than once per 6 hours.
_ALERT_BATTERY_THRESHOLD  = 95   # % SOC — battery considered "full enough"
_ALERT_MIN_POWER_W        = 200  # W — must have some solar to trigger
_ALERT_COOLDOWN_HOURS     = 6    # hours between repeated alerts
_ALERT_DAYLIGHT_START     = 9    # Pacific local hour (inclusive)
_ALERT_DAYLIGHT_END       = 17   # Pacific local hour (exclusive)
_ALERT_USER_ID            = "default"
_ALERT_CONFIG_KEY         = "curtailment_alert"


def _should_send_curtailment_alert(
    battery_soc_pct: int | None,
    current_power_w: int,
    config_table,
) -> bool:
    """
    Return True when all three conditions are met:
      1. Battery is at or near full capacity.
      2. Solar is producing (system is curtailing to match home load).
      3. We haven't already alerted in the last _ALERT_COOLDOWN_HOURS hours.
    """
    if battery_soc_pct is None or battery_soc_pct < _ALERT_BATTERY_THRESHOLD:
        return False
    if current_power_w < _ALERT_MIN_POWER_W:
        return False

    now_pacific = datetime.now(PACIFIC)
    if not (_ALERT_DAYLIGHT_START <= now_pacific.hour < _ALERT_DAYLIGHT_END):
        return False

    # De-dup: check last alert timestamp in config table
    try:
        resp = config_table.get_item(
            Key={"userId": _ALERT_USER_ID, "configType": _ALERT_CONFIG_KEY}
        )
        last_sent = resp.get("Item", {}).get("last_sent_at")
        if last_sent:
            last_dt = datetime.fromisoformat(last_sent)
            if datetime.now(timezone.utc) - last_dt < timedelta(hours=_ALERT_COOLDOWN_HOURS):
                logger.info(
                    "Curtailment alert suppressed — last sent %s (cooldown %dh)",
                    last_sent, _ALERT_COOLDOWN_HOURS,
                )
                return False
    except Exception as exc:
        logger.warning("Could not read alert de-dup record: %s", exc)

    return True


def _resolve_ntfy_topic() -> str | None:
    """
    Return the ntfy.sh topic name, or None if not configured.

    Local dev  — NTFY_TOPIC env var (set in backend/.env).
    Lambda     — fetched from SSM path in NTFY_TOPIC_PARAM env var.
    """
    direct = os.environ.get("NTFY_TOPIC", "")
    if direct:
        return direct
    param_path = os.environ.get("NTFY_TOPIC_PARAM", "")
    if param_path:
        try:
            return _get_ssm_param(param_path)
        except Exception as exc:
            logger.warning("Could not read ntfy topic from SSM: %s", exc)
    return None


def _send_curtailment_alert(
    ntfy_topic: str,
    battery_soc_pct: int,
    current_power_w: int,
    config_table,
) -> None:
    """POST a curtailment alert to ntfy.sh and record the timestamp in DynamoDB."""
    message = (
        f"Home battery at {battery_soc_pct}% (full). "
        f"Solar producing {current_power_w} W but throttled to standby load — "
        f"free energy going to waste. Plug in the BMW iX now."
    )
    try:
        req = Request(
            f"{NTFY_BASE}/{ntfy_topic}",
            data=message.encode(),
            headers={
                "Title":    "Plug in your EV -- solar is being curtailed",
                "Priority": "high",
                "Tags":     "warning,electric_plug,sunny",
            },
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            logger.info(
                "Curtailment alert sent via ntfy.sh: battery=%d%% power=%dW status=%s",
                battery_soc_pct, current_power_w, resp.status,
            )
    except Exception as exc:
        logger.error("ntfy.sh publish failed: %s", exc)
        return

    # Record send time so we don't spam
    try:
        config_table.put_item(Item={
            "userId":       _ALERT_USER_ID,
            "configType":   _ALERT_CONFIG_KEY,
            "last_sent_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.warning("Could not write alert de-dup record: %s", exc)


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

    # Nighttime skip: solar produces nothing 9pm–6am Pacific; skip to conserve API quota.
    local_hour = datetime.now(PACIFIC).hour
    if local_hour < 6 or local_hour >= 21:
        logger.info("Skipping Enphase API call outside solar hours (hour=%d)", local_hour)
        return {
            "statusCode": 200,
            "body": json.dumps({"ingested_at": now, "status": "skipped_nighttime"}),
        }

    config_table_name = os.environ.get("CONFIG_TABLE", "")
    config_table = _dynamo.Table(config_table_name) if config_table_name else None

    # Circuit breaker: skip all Enphase calls if we recently hit a 429.
    if config_table:
        try:
            if _check_rate_limit_block(config_table):
                logger.info(
                    "Enphase API rate-limited — skipping until block expires"
                )
                return {
                    "statusCode": 200,
                    "body": json.dumps({"ingested_at": now, "status": "rate_limited"}),
                }
        except Exception as exc:
            logger.warning("Rate limit check failed (%s) — proceeding", exc)

    try:
        summary = _fetch_enphase_summary(
            system_id, api_key, access_token, client_id, client_secret
        )
        # Battery SOC: only fetch every 4 hours — it changes slowly and each call costs quota.
        battery_soc_pct = None
        if local_hour % 4 == 0:
            battery_soc_pct = _fetch_battery_soc(system_id, api_key, access_token)
        else:
            logger.debug("Skipping battery SOC fetch (hour=%d, not divisible by 4)", local_hour)

        # Fetch weather (non-fatal if not configured or fails)
        owm_key = _resolve_owm_api_key()
        lat = os.environ.get("LOCATION_LAT", "")
        lon = os.environ.get("LOCATION_LON", "")
        weather = _fetch_weather(lat, lon, owm_key) if owm_key else None

        table = _dynamo.Table(table_name)
        _write_reading(table, system_id, summary, battery_soc_pct=battery_soc_pct, weather=weather)

        # Alert if battery full and solar is being curtailed
        ntfy_topic = _resolve_ntfy_topic()
        if ntfy_topic and config_table and _should_send_curtailment_alert(
            battery_soc_pct, summary.get("current_power", 0), config_table
        ):
            _send_curtailment_alert(
                ntfy_topic, battery_soc_pct, summary.get("current_power", 0), config_table
            )

    except EnphaseRateLimitError as exc:
        logger.warning("Enphase API rate limit hit — blocking calls for %dh", _RATE_LIMIT_BLOCK_HOURS)
        if config_table:
            try:
                _set_rate_limit_block(config_table, str(exc))
            except Exception as write_exc:
                logger.error("Failed to write rate limit block: %s", write_exc)
        return {
            "statusCode": 200,
            "body": json.dumps({"ingested_at": now, "status": "rate_limited"}),
        }
    except Exception as exc:
        logger.exception("Enphase ingest failed: %s", exc)
        return {
            "statusCode": 500,
            "body": json.dumps({"ingested_at": now, "status": "error", "error": str(exc)}),
        }

    response_body: dict = {
        "ingested_at":     now,
        "status":          "ok",
        "energy_today_wh": summary.get("energy_today"),
        "current_power_w": summary.get("current_power"),
        "battery_soc_pct": battery_soc_pct,
    }
    if weather is not None:
        response_body["cloud_cover_pct"]   = weather["cloud_cover_pct"]
        response_body["temp_c"]            = weather["temp_c"]
        response_body["weather_condition"] = weather["weather_condition"]

    return {"statusCode": 200, "body": json.dumps(response_body)}
