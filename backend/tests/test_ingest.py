"""
Unit tests for the ingest Lambda handler.

Covers:
- _write_reading: DynamoDB item shape, TTL, battery_soc_pct present/absent
- _fetch_battery_soc: parses SOC from intervals, returns None on empty/errors
- lambda_handler: skips when system_id missing, skips when credentials absent,
  succeeds with mocked Enphase + mocked DynamoDB
"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_handler

# Load under a unique module name so handler.py files don't collide
ingest = load_handler("ingest")

TABLE_NAME = "test-energy-readings"
SYSTEM_ID = "test-system-123"


def _make_table(ddb):
    return ddb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "deviceId", "KeyType": "HASH"},
            {"AttributeName": "timestamp", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "deviceId", "AttributeType": "S"},
            {"AttributeName": "timestamp", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


# ---------------------------------------------------------------------------
# _write_reading
# ---------------------------------------------------------------------------

class TestWriteReading(unittest.TestCase):
    """_write_reading writes the correct item shape to DynamoDB."""

    def _call(self, summary, battery_soc_pct=None):
        mock_table = MagicMock()
        ingest._write_reading(mock_table, SYSTEM_ID, summary, battery_soc_pct)
        return mock_table.put_item.call_args[1]["Item"]

    def test_required_fields_present(self):
        summary = {
            "last_report_at": 1741651200,  # 2026-03-11T00:00:00Z
            "energy_today": 12345,
            "current_power": 3000,
            "summary_date": "2026-03-10",
        }
        item = self._call(summary)
        self.assertEqual(item["deviceId"], f"enphase-{SYSTEM_ID}")
        self.assertEqual(item["energy_wh"], 12345)
        self.assertEqual(item["power_w"], 3000)
        self.assertEqual(item["summary_date"], "2026-03-10")
        self.assertEqual(item["source"], "enphase")
        self.assertIn("timestamp", item)
        self.assertIn("ingested_at", item)
        self.assertIn("ttl", item)

    def test_ttl_is_roughly_90_days_out(self):
        summary = {"last_report_at": None, "energy_today": 0, "current_power": 0}
        item = self._call(summary)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        expected_ttl = now_ts + 90 * 86400
        self.assertAlmostEqual(item["ttl"], expected_ttl, delta=60)

    def test_battery_soc_included_when_provided(self):
        summary = {"energy_today": 0, "current_power": 0}
        item = self._call(summary, battery_soc_pct=72)
        self.assertEqual(item["battery_soc_pct"], 72)

    def test_battery_soc_omitted_when_none(self):
        summary = {"energy_today": 0, "current_power": 0}
        item = self._call(summary, battery_soc_pct=None)
        self.assertNotIn("battery_soc_pct", item)

    def test_timestamp_uses_last_report_at(self):
        from datetime import datetime
        dt = datetime(2026, 3, 11, 0, 0, 0, tzinfo=timezone.utc)
        ts_epoch = int(dt.timestamp())
        summary = {"last_report_at": ts_epoch, "energy_today": 0, "current_power": 0}
        item = self._call(summary)
        self.assertIn("2026-03-11", item["timestamp"])

    def test_timestamp_falls_back_to_now_when_missing(self):
        summary = {"energy_today": 0, "current_power": 0}
        item = self._call(summary)
        # Should be a recent ISO timestamp (within last minute)
        ts = datetime.fromisoformat(item["timestamp"])
        age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
        self.assertLess(abs(age), 60)


# ---------------------------------------------------------------------------
# _fetch_battery_soc
# ---------------------------------------------------------------------------

class TestFetchBatterySOC(unittest.TestCase):
    """_fetch_battery_soc parses the Enphase telemetry response."""

    def _fake_response(self, payload: dict):
        body = json.dumps(payload).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = body
        return mock_resp

    def test_returns_soc_from_latest_interval(self):
        payload = {
            "intervals": [
                {"soc": {"percent": "45.2"}},
                {"soc": {"percent": "72.8"}},
            ]
        }
        with patch("ingest_handler.urlopen", return_value=self._fake_response(payload)):
            result = ingest._fetch_battery_soc(SYSTEM_ID, "key", "token")
        self.assertEqual(result, 73)  # round(72.8) = 73

    def test_returns_none_for_empty_intervals(self):
        payload = {"intervals": []}
        with patch("ingest_handler.urlopen", return_value=self._fake_response(payload)):
            result = ingest._fetch_battery_soc(SYSTEM_ID, "key", "token")
        self.assertIsNone(result)

    def test_returns_none_when_soc_key_missing(self):
        payload = {"intervals": [{"other_data": 123}]}
        with patch("ingest_handler.urlopen", return_value=self._fake_response(payload)):
            result = ingest._fetch_battery_soc(SYSTEM_ID, "key", "token")
        self.assertIsNone(result)

    def test_returns_none_on_http_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("network failure")):
            result = ingest._fetch_battery_soc(SYSTEM_ID, "key", "token")
        self.assertIsNone(result)

    def test_returns_none_when_intervals_key_missing(self):
        payload = {}
        with patch("ingest_handler.urlopen", return_value=self._fake_response(payload)):
            result = ingest._fetch_battery_soc(SYSTEM_ID, "key", "token")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------

def _make_daytime_datetime_mock():
    """Return a mock for `ingest_handler.datetime` pinned to 08:00 Pacific (daytime, div by 4)."""
    from datetime import datetime as real_datetime
    from zoneinfo import ZoneInfo
    pacific = ZoneInfo("America/Los_Angeles")
    fake_pac = real_datetime(2026, 3, 11, 8, 0, tzinfo=pacific)
    fake_utc = fake_pac.astimezone(timezone.utc)

    def fake_now(tz=None):
        if tz is not None and getattr(tz, "key", None) == "America/Los_Angeles":
            return fake_pac
        return fake_utc

    mock_dt = MagicMock()
    mock_dt.now.side_effect = fake_now
    mock_dt.fromisoformat = real_datetime.fromisoformat
    mock_dt.fromtimestamp = real_datetime.fromtimestamp
    return mock_dt


@mock_aws
class TestIngestLambdaHandler(unittest.TestCase):
    """lambda_handler integration: skips gracefully, writes on success."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ingest._dynamo = ddb
        self.table = _make_table(ddb)
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        # Pin time to 10am Pacific so nighttime skip never fires in these tests
        self._dt_patcher = patch("ingest_handler.datetime", _make_daytime_datetime_mock())
        self._dt_patcher.start()

    def tearDown(self):
        self._dt_patcher.stop()
        for key in ("ENERGY_TABLE", "ENPHASE_SYSTEM_ID",
                    "ENPHASE_API_KEY", "ENPHASE_ACCESS_TOKEN",
                    "ENPHASE_CLIENT_ID", "ENPHASE_CLIENT_SECRET"):
            os.environ.pop(key, None)

    def test_returns_skipped_when_no_system_id(self):
        os.environ.pop("ENPHASE_SYSTEM_ID", None)
        result = ingest.lambda_handler({}, None)
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "skipped")

    def test_returns_skipped_when_no_credentials(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        # No credential env vars set — _resolve_credentials falls through to SSM path
        # which will raise KeyError since the *_PARAM vars are also missing
        result = ingest.lambda_handler({}, None)
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "skipped")

    def test_success_writes_to_dynamo_and_returns_ok(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENPHASE_API_KEY"] = "test-key"
        os.environ["ENPHASE_ACCESS_TOKEN"] = "test-token"
        os.environ["ENPHASE_CLIENT_ID"] = "test-client"
        os.environ["ENPHASE_CLIENT_SECRET"] = "test-secret"

        summary_payload = {
            "current_power": 2500,
            "energy_today": 8000,
            "summary_date": "2026-03-11",
            "last_report_at": 1741651200,
        }
        battery_payload = {
            "intervals": [{"soc": {"percent": "80"}}]
        }

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            payload = battery_payload if "battery" in url else summary_payload
            body = json.dumps(payload).encode()
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = body
            return mock_resp

        with patch("ingest_handler.urlopen", side_effect=fake_urlopen):
            result = ingest.lambda_handler({}, None)

        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["energy_today_wh"], 8000)
        self.assertEqual(body["current_power_w"], 2500)
        self.assertEqual(body["battery_soc_pct"], 80)

        # Verify the row landed in DynamoDB
        items = self.table.scan()["Items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["deviceId"], f"enphase-{SYSTEM_ID}")
        self.assertEqual(int(items[0]["energy_wh"]), 8000)
        self.assertEqual(int(items[0]["battery_soc_pct"]), 80)


CONFIG_TABLE_NAME = "test-user-config"


def _make_config_table(ddb):
    return ddb.create_table(
        TableName=CONFIG_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "userId",     "KeyType": "HASH"},
            {"AttributeName": "configType", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "userId",     "AttributeType": "S"},
            {"AttributeName": "configType", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


# ---------------------------------------------------------------------------
# _get_ssm_param / _put_ssm_param
# ---------------------------------------------------------------------------

@mock_aws
class TestSsmHelpers(unittest.TestCase):
    """SSM read/write helpers hit moto and populate the in-process cache."""

    def setUp(self):
        # Clear the module-level cache before every test
        ingest._ssm_cache.clear()
        self.ssm = boto3.client("ssm", region_name="us-east-1")
        ingest._ssm = self.ssm
        self.ssm.put_parameter(
            Name="/test/param",
            Value="secret-value",
            Type="SecureString",
        )

    def tearDown(self):
        ingest._ssm_cache.clear()

    def test_get_ssm_param_returns_value(self):
        val = ingest._get_ssm_param("/test/param")
        self.assertEqual(val, "secret-value")

    def test_get_ssm_param_caches_result(self):
        ingest._get_ssm_param("/test/param")
        # Overwrite in SSM — cached value should still be returned
        self.ssm.put_parameter(Name="/test/param", Value="updated", Type="SecureString", Overwrite=True)
        self.assertEqual(ingest._get_ssm_param("/test/param"), "secret-value")

    def test_get_ssm_param_raises_on_missing(self):
        from botocore.exceptions import ClientError
        with self.assertRaises(ClientError):
            ingest._get_ssm_param("/does/not/exist")

    def test_put_ssm_param_overwrites_value(self):
        ingest._put_ssm_param("/test/param", "new-value")
        resp = self.ssm.get_parameter(Name="/test/param", WithDecryption=True)
        self.assertEqual(resp["Parameter"]["Value"], "new-value")

    def test_put_ssm_param_updates_cache(self):
        ingest._put_ssm_param("/test/param", "cached-new")
        # Cache should reflect the new value without another SSM call
        self.assertEqual(ingest._ssm_cache["/test/param"], "cached-new")


# ---------------------------------------------------------------------------
# _resolve_credentials
# ---------------------------------------------------------------------------

class TestResolveCredentials(unittest.TestCase):
    """_resolve_credentials returns (api_key, access_token, client_id, client_secret)."""

    def _clear_env(self):
        for key in (
            "ENPHASE_API_KEY", "ENPHASE_ACCESS_TOKEN",
            "ENPHASE_CLIENT_ID", "ENPHASE_CLIENT_SECRET",
            "ENPHASE_API_KEY_PARAM", "ENPHASE_ACCESS_TOKEN_PARAM",
            "ENPHASE_CLIENT_ID_PARAM", "ENPHASE_CLIENT_SECRET_PARAM",
        ):
            os.environ.pop(key, None)

    def setUp(self):
        self._clear_env()

    def tearDown(self):
        self._clear_env()

    def test_returns_env_credentials_when_all_set(self):
        os.environ["ENPHASE_API_KEY"]       = "k"
        os.environ["ENPHASE_ACCESS_TOKEN"]  = "t"
        os.environ["ENPHASE_CLIENT_ID"]     = "id"
        os.environ["ENPHASE_CLIENT_SECRET"] = "sec"
        result = ingest._resolve_credentials()
        self.assertEqual(result, ("k", "t", "id", "sec"))

    def test_falls_through_to_ssm_when_env_missing(self):
        # No direct env vars set — SSM path vars are also missing → KeyError
        with self.assertRaises(KeyError):
            ingest._resolve_credentials()

    def test_falls_through_to_ssm_when_value_is_mock(self):
        # "mock" sentinel triggers the SSM path; no PARAM vars → KeyError
        os.environ["ENPHASE_API_KEY"]       = "mock"
        os.environ["ENPHASE_ACCESS_TOKEN"]  = "mock"
        os.environ["ENPHASE_CLIENT_ID"]     = "mock"
        os.environ["ENPHASE_CLIENT_SECRET"] = "mock"
        with self.assertRaises(KeyError):
            ingest._resolve_credentials()

    @mock_aws
    def test_reads_from_ssm_when_param_vars_set(self):
        ssm = boto3.client("ssm", region_name="us-east-1")
        ingest._ssm = ssm
        ingest._ssm_cache.clear()
        for name, val in [
            ("/p/key", "api-key"), ("/p/tok", "access-tok"),
            ("/p/id",  "cli-id"), ("/p/sec", "cli-sec"),
        ]:
            ssm.put_parameter(Name=name, Value=val, Type="SecureString")

        os.environ["ENPHASE_API_KEY_PARAM"]       = "/p/key"
        os.environ["ENPHASE_ACCESS_TOKEN_PARAM"]  = "/p/tok"
        os.environ["ENPHASE_CLIENT_ID_PARAM"]     = "/p/id"
        os.environ["ENPHASE_CLIENT_SECRET_PARAM"] = "/p/sec"

        result = ingest._resolve_credentials()
        self.assertEqual(result, ("api-key", "access-tok", "cli-id", "cli-sec"))

        for key in ("ENPHASE_API_KEY_PARAM", "ENPHASE_ACCESS_TOKEN_PARAM",
                    "ENPHASE_CLIENT_ID_PARAM", "ENPHASE_CLIENT_SECRET_PARAM"):
            os.environ.pop(key, None)
        ingest._ssm_cache.clear()


# ---------------------------------------------------------------------------
# _refresh_tokens
# ---------------------------------------------------------------------------

class TestRefreshTokens(unittest.TestCase):
    """_refresh_tokens exchanges refresh_token for new access/refresh tokens."""

    def setUp(self):
        for k in ("ENPHASE_REFRESH_TOKEN", "ENPHASE_ACCESS_TOKEN",
                  "ENPHASE_REFRESH_TOKEN_PARAM", "ENPHASE_ACCESS_TOKEN_PARAM"):
            os.environ.pop(k, None)
        ingest._ssm_cache.clear()

    def tearDown(self):
        for k in ("ENPHASE_REFRESH_TOKEN", "ENPHASE_ACCESS_TOKEN",
                  "ENPHASE_REFRESH_TOKEN_PARAM", "ENPHASE_ACCESS_TOKEN_PARAM"):
            os.environ.pop(k, None)
        ingest._ssm_cache.clear()

    def _fake_token_response(self, access="new-access", refresh="new-refresh"):
        payload = json.dumps({"access_token": access, "refresh_token": refresh}).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = payload
        return mock_resp

    def test_local_dev_path_updates_env(self):
        os.environ["ENPHASE_REFRESH_TOKEN"] = "old-refresh"
        with patch("ingest_handler.urlopen", return_value=self._fake_token_response()):
            result = ingest._refresh_tokens("client-id", "client-secret")
        self.assertEqual(result, "new-access")
        self.assertEqual(os.environ["ENPHASE_ACCESS_TOKEN"], "new-access")
        self.assertEqual(os.environ["ENPHASE_REFRESH_TOKEN"], "new-refresh")

    @mock_aws
    def test_lambda_path_updates_ssm(self):
        ssm = boto3.client("ssm", region_name="us-east-1")
        ingest._ssm = ssm
        ssm.put_parameter(Name="/p/refresh", Value="old-refresh", Type="SecureString")
        ssm.put_parameter(Name="/p/access", Value="old-access", Type="SecureString")
        ingest._ssm_cache["/p/refresh"] = "old-refresh"

        os.environ["ENPHASE_REFRESH_TOKEN_PARAM"] = "/p/refresh"
        os.environ["ENPHASE_ACCESS_TOKEN_PARAM"]  = "/p/access"

        with patch("ingest_handler.urlopen", return_value=self._fake_token_response()):
            result = ingest._refresh_tokens("client-id", "client-secret")

        self.assertEqual(result, "new-access")
        resp = ssm.get_parameter(Name="/p/access", WithDecryption=True)
        self.assertEqual(resp["Parameter"]["Value"], "new-access")

    def test_raises_when_no_refresh_token(self):
        with self.assertRaises(ValueError, msg="No refresh token available"):
            ingest._refresh_tokens("cid", "csec")


# ---------------------------------------------------------------------------
# _fetch_enphase_summary
# ---------------------------------------------------------------------------

class TestFetchEnphaseSummary(unittest.TestCase):
    """_fetch_enphase_summary GETs /summary and handles 401 with token refresh."""

    def _fake_response(self, payload):
        body = json.dumps(payload).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = body
        return mock_resp

    def test_success_returns_summary_dict(self):
        summary = {"current_power": 3000, "energy_today": 12000, "summary_date": "2026-03-11"}
        with patch("ingest_handler.urlopen", return_value=self._fake_response(summary)):
            result = ingest._fetch_enphase_summary("sys-1", "key", "tok", "cid", "csec")
        self.assertEqual(result["current_power"], 3000)
        self.assertEqual(result["energy_today"], 12000)

    def test_401_triggers_token_refresh_and_retry(self):
        from urllib.error import HTTPError
        summary = {"current_power": 1000, "energy_today": 5000, "summary_date": "2026-03-11"}

        call_count = {"n": 0}

        def fake_urlopen(req, timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None)
            return self._fake_response(summary)

        with patch("ingest_handler.urlopen", side_effect=fake_urlopen), \
             patch("ingest_handler._refresh_tokens", return_value="new-tok") as mock_refresh:
            result = ingest._fetch_enphase_summary("sys-1", "key", "tok", "cid", "csec")

        mock_refresh.assert_called_once_with("cid", "csec")
        self.assertEqual(result["current_power"], 1000)

    def test_non_401_http_error_propagates(self):
        from urllib.error import HTTPError
        err = HTTPError(url="", code=500, msg="Server Error", hdrs=None, fp=MagicMock(read=lambda: b"oops"))
        with patch("ingest_handler.urlopen", side_effect=err):
            with self.assertRaises(HTTPError):
                ingest._fetch_enphase_summary("sys-1", "key", "tok", "cid", "csec")

    def test_url_error_propagates(self):
        from urllib.error import URLError
        with patch("ingest_handler.urlopen", side_effect=URLError("timeout")):
            with self.assertRaises(URLError):
                ingest._fetch_enphase_summary("sys-1", "key", "tok", "cid", "csec")


# ---------------------------------------------------------------------------
# _should_send_curtailment_alert
# ---------------------------------------------------------------------------

@mock_aws
class TestShouldSendCurtailmentAlert(unittest.TestCase):
    """_should_send_curtailment_alert applies battery/power/time/cooldown logic."""

    # Shared fake "now" — both variants must agree so cooldown arithmetic is self-consistent
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo as _ZI
    _FAKE_PAC = datetime(2026, 3, 11, 11, 0, tzinfo=_ZI("America/Los_Angeles"))
    _FAKE_UTC = _FAKE_PAC.astimezone(timezone.utc)

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ingest._dynamo = ddb
        self.config_table = _make_config_table(ddb)

    def _call(self, soc, power_w, hour=11):
        """Call with a fixed Pacific hour; both now(PACIFIC) and now(UTC) use the same instant."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo("America/Los_Angeles")
        fake_pac = datetime(2026, 3, 11, hour, 0, tzinfo=pacific)
        fake_utc = fake_pac.astimezone(__import__("datetime").timezone.utc)

        def fake_now(tz=None):
            if tz is not None and getattr(tz, "key", None) == "America/Los_Angeles":
                return fake_pac
            return fake_utc

        with patch("ingest_handler.datetime") as mock_dt:
            mock_dt.now.side_effect = fake_now
            mock_dt.fromisoformat = datetime.fromisoformat
            return ingest._should_send_curtailment_alert(soc, power_w, self.config_table)

    def test_returns_true_when_all_conditions_met(self):
        self.assertTrue(self._call(soc=96, power_w=500, hour=11))

    def test_returns_false_when_soc_below_threshold(self):
        self.assertFalse(self._call(soc=90, power_w=500, hour=11))

    def test_returns_false_when_soc_is_none(self):
        self.assertFalse(self._call(soc=None, power_w=500, hour=11))

    def test_returns_false_when_power_too_low(self):
        self.assertFalse(self._call(soc=96, power_w=100, hour=11))

    def test_returns_false_outside_daylight_hours(self):
        self.assertFalse(self._call(soc=96, power_w=500, hour=20))

    def test_returns_false_within_cooldown(self):
        from datetime import timedelta
        # 2 hours before _FAKE_UTC — still within the 6-hour cooldown window
        recent = (self._FAKE_UTC - timedelta(hours=2)).isoformat()
        self.config_table.put_item(Item={
            "userId":       "default",
            "configType":   "curtailment_alert",
            "last_sent_at": recent,
        })
        self.assertFalse(self._call(soc=96, power_w=500, hour=11))

    def test_returns_true_when_cooldown_expired(self):
        from datetime import timedelta
        # 8 hours before _FAKE_UTC — past the 6-hour cooldown window
        old = (self._FAKE_UTC - timedelta(hours=8)).isoformat()
        self.config_table.put_item(Item={
            "userId":       "default",
            "configType":   "curtailment_alert",
            "last_sent_at": old,
        })
        self.assertTrue(self._call(soc=96, power_w=500, hour=11))


# ---------------------------------------------------------------------------
# _resolve_ntfy_topic
# ---------------------------------------------------------------------------

class TestResolveNtfyTopic(unittest.TestCase):
    """_resolve_ntfy_topic returns the topic from env or SSM, or None."""

    def setUp(self):
        os.environ.pop("NTFY_TOPIC", None)
        os.environ.pop("NTFY_TOPIC_PARAM", None)
        ingest._ssm_cache.clear()

    def tearDown(self):
        os.environ.pop("NTFY_TOPIC", None)
        os.environ.pop("NTFY_TOPIC_PARAM", None)
        ingest._ssm_cache.clear()

    def test_returns_direct_env_topic(self):
        os.environ["NTFY_TOPIC"] = "my-topic"
        self.assertEqual(ingest._resolve_ntfy_topic(), "my-topic")

    @mock_aws
    def test_fetches_from_ssm_when_param_set(self):
        ssm = boto3.client("ssm", region_name="us-east-1")
        ingest._ssm = ssm
        ssm.put_parameter(Name="/p/ntfy", Value="ssm-topic", Type="SecureString")
        os.environ["NTFY_TOPIC_PARAM"] = "/p/ntfy"
        self.assertEqual(ingest._resolve_ntfy_topic(), "ssm-topic")

    def test_returns_none_when_neither_set(self):
        self.assertIsNone(ingest._resolve_ntfy_topic())

    @mock_aws
    def test_returns_none_when_ssm_param_missing(self):
        ssm = boto3.client("ssm", region_name="us-east-1")
        ingest._ssm = ssm
        ingest._ssm_cache.clear()
        os.environ["NTFY_TOPIC_PARAM"] = "/does/not/exist"
        self.assertIsNone(ingest._resolve_ntfy_topic())


# ---------------------------------------------------------------------------
# _send_curtailment_alert
# ---------------------------------------------------------------------------

@mock_aws
class TestSendCurtailmentAlert(unittest.TestCase):
    """_send_curtailment_alert POSTs to ntfy.sh and records send time in DynamoDB."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ingest._dynamo = ddb
        self.config_table = _make_config_table(ddb)

    def _fake_ntfy_response(self, status=200):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = status
        return mock_resp

    def test_posts_to_ntfy_and_records_timestamp(self):
        with patch("ingest_handler.urlopen", return_value=self._fake_ntfy_response()):
            ingest._send_curtailment_alert("my-topic", 96, 800, self.config_table)

        item = self.config_table.get_item(
            Key={"userId": "default", "configType": "curtailment_alert"}
        ).get("Item")
        self.assertIsNotNone(item)
        self.assertIn("last_sent_at", item)

    def test_send_failure_does_not_raise(self):
        """Network failure should be swallowed (logged only)."""
        with patch("ingest_handler.urlopen", side_effect=Exception("network error")):
            # Should not raise
            ingest._send_curtailment_alert("my-topic", 96, 800, self.config_table)

    def test_message_contains_soc_and_power(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = req.data.decode()
            return self._fake_ntfy_response()

        with patch("ingest_handler.urlopen", side_effect=fake_urlopen):
            ingest._send_curtailment_alert("my-topic", 97, 1200, self.config_table)

        self.assertIn("97%", captured["data"])
        self.assertIn("1200 W", captured["data"])


# ---------------------------------------------------------------------------
# _fetch_weather
# ---------------------------------------------------------------------------

class TestFetchWeather(unittest.TestCase):
    """_fetch_weather parses OWM current weather response."""

    def _fake_response(self, payload: dict):
        body = json.dumps(payload).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = body
        return mock_resp

    def _owm_payload(self, cloud_all=20, temp=15.3, condition="Clear"):
        return {
            "clouds": {"all": cloud_all},
            "main":   {"temp": temp},
            "weather": [{"main": condition}],
        }

    def test__fetch_weather_success(self):
        payload = self._owm_payload(cloud_all=45, temp=12.7, condition="Clouds")
        with patch("ingest_handler.urlopen", return_value=self._fake_response(payload)):
            result = ingest._fetch_weather("37.82", "-121.99", "fake-key")
        self.assertEqual(result["cloud_cover_pct"], 45)
        self.assertEqual(result["temp_c"], 13)          # round(12.7)
        self.assertEqual(result["weather_condition"], "Clouds")

    def test__fetch_weather_http_error(self):
        from urllib.error import HTTPError
        err = HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None)
        with patch("ingest_handler.urlopen", side_effect=err):
            result = ingest._fetch_weather("37.82", "-121.99", "bad-key")
        self.assertIsNone(result)

    def test__fetch_weather_network_error(self):
        from urllib.error import URLError
        with patch("ingest_handler.urlopen", side_effect=URLError("timeout")):
            result = ingest._fetch_weather("37.82", "-121.99", "key")
        self.assertIsNone(result)

    def test__fetch_weather_missing_fields(self):
        # Payload missing "clouds" key — should catch KeyError and return None
        payload = {"main": {"temp": 15.0}, "weather": [{"main": "Clear"}]}
        with patch("ingest_handler.urlopen", return_value=self._fake_response(payload)):
            result = ingest._fetch_weather("37.82", "-121.99", "key")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _write_reading with/without weather
# ---------------------------------------------------------------------------

class TestWriteReadingWeather(unittest.TestCase):
    """_write_reading writes weather fields when provided, omits them when not."""

    def _call(self, summary, battery_soc_pct=None, weather=None):
        mock_table = MagicMock()
        ingest._write_reading(mock_table, SYSTEM_ID, summary, battery_soc_pct, weather)
        return mock_table.put_item.call_args[1]["Item"]

    def test__write_reading_with_weather(self):
        summary = {"energy_today": 0, "current_power": 0}
        weather = {"cloud_cover_pct": 30, "temp_c": 18, "weather_condition": "Clear"}
        item = self._call(summary, weather=weather)
        self.assertEqual(item["cloud_cover_pct"], 30)
        self.assertEqual(item["temp_c"], 18)
        self.assertEqual(item["weather_condition"], "Clear")

    def test__write_reading_without_weather(self):
        summary = {"energy_today": 0, "current_power": 0}
        item = self._call(summary, weather=None)
        self.assertNotIn("cloud_cover_pct", item)
        self.assertNotIn("temp_c", item)
        self.assertNotIn("weather_condition", item)


# ---------------------------------------------------------------------------
# lambda_handler — weather integration
# ---------------------------------------------------------------------------

@mock_aws
class TestIngestLambdaHandlerWeather(unittest.TestCase):
    """lambda_handler includes weather in response and tolerates OWM failures."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ingest._dynamo = ddb
        self.table = _make_table(ddb)
        os.environ["ENERGY_TABLE"]            = TABLE_NAME
        os.environ["ENPHASE_SYSTEM_ID"]       = SYSTEM_ID
        os.environ["ENPHASE_API_KEY"]         = "test-key"
        os.environ["ENPHASE_ACCESS_TOKEN"]    = "test-token"
        os.environ["ENPHASE_CLIENT_ID"]       = "test-client"
        os.environ["ENPHASE_CLIENT_SECRET"]   = "test-secret"
        os.environ["OPENWEATHER_API_KEY"]     = "owm-key"
        os.environ["LOCATION_LAT"]            = "37.8216"
        os.environ["LOCATION_LON"]            = "-121.9999"
        # Pin time to 10am Pacific (divisible by 2 but not 4 — battery throttle test
        # in this class doesn't apply; we just need daytime and avoid nighttime skip)
        self._dt_patcher = patch("ingest_handler.datetime", _make_daytime_datetime_mock())
        self._dt_patcher.start()

    def tearDown(self):
        self._dt_patcher.stop()
        for key in (
            "ENERGY_TABLE", "ENPHASE_SYSTEM_ID",
            "ENPHASE_API_KEY", "ENPHASE_ACCESS_TOKEN",
            "ENPHASE_CLIENT_ID", "ENPHASE_CLIENT_SECRET",
            "OPENWEATHER_API_KEY", "LOCATION_LAT", "LOCATION_LON",
        ):
            os.environ.pop(key, None)

    def _fake_urlopen(self, summary_payload, battery_payload, weather_payload):
        def fake(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "battery" in url:
                payload = battery_payload
            elif "openweathermap" in url:
                payload = weather_payload
            else:
                payload = summary_payload
            body = json.dumps(payload).encode()
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = body
            return mock_resp
        return fake

    def test_lambda_handler_includes_weather(self):
        summary_payload = {
            "current_power": 3000, "energy_today": 9000,
            "summary_date": "2026-03-15", "last_report_at": 1741651200,
        }
        battery_payload = {"intervals": [{"soc": {"percent": "85"}}]}
        weather_payload = {
            "clouds": {"all": 10},
            "main":   {"temp": 20.0},
            "weather": [{"main": "Clear"}],
        }

        with patch("ingest_handler.urlopen",
                   side_effect=self._fake_urlopen(summary_payload, battery_payload, weather_payload)):
            result = ingest.lambda_handler({}, None)

        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["cloud_cover_pct"], 10)
        self.assertEqual(body["temp_c"], 20)
        self.assertEqual(body["weather_condition"], "Clear")

        items = self.table.scan()["Items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["cloud_cover_pct"], 10)
        self.assertEqual(items[0]["weather_condition"], "Clear")

    def test_lambda_handler_weather_failure_non_fatal(self):
        from urllib.error import URLError
        summary_payload = {
            "current_power": 2000, "energy_today": 5000,
            "summary_date": "2026-03-15", "last_report_at": 1741651200,
        }
        battery_payload = {"intervals": []}

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "openweathermap" in url:
                raise URLError("network down")
            payload = battery_payload if "battery" in url else summary_payload
            body = json.dumps(payload).encode()
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = body
            return mock_resp

        with patch("ingest_handler.urlopen", side_effect=fake_urlopen):
            result = ingest.lambda_handler({}, None)

        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "ok")
        self.assertNotIn("cloud_cover_pct", body)
        self.assertNotIn("weather_condition", body)


# ---------------------------------------------------------------------------
# _check_rate_limit_block / _set_rate_limit_block
# ---------------------------------------------------------------------------

@mock_aws
class TestCircuitBreakerHelpers(unittest.TestCase):
    """Unit tests for the rate-limit circuit breaker read/write helpers."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ingest._dynamo = ddb
        self.config_table = _make_config_table(ddb)

    def test_returns_false_when_no_record(self):
        self.assertFalse(ingest._check_rate_limit_block(self.config_table))

    def test_returns_true_when_block_is_active(self):
        from datetime import timedelta
        future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        self.config_table.put_item(Item={
            "userId": "default",
            "configType": "enphase_rate_limit",
            "blocked_until": future,
            "reason": "429 test",
        })
        self.assertTrue(ingest._check_rate_limit_block(self.config_table))

    def test_returns_false_when_block_has_expired(self):
        from datetime import timedelta
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.config_table.put_item(Item={
            "userId": "default",
            "configType": "enphase_rate_limit",
            "blocked_until": past,
            "reason": "old block",
        })
        self.assertFalse(ingest._check_rate_limit_block(self.config_table))

    def test_set_rate_limit_block_writes_future_timestamp(self):
        ingest._set_rate_limit_block(self.config_table, "429 from /summary")
        item = self.config_table.get_item(
            Key={"userId": "default", "configType": "enphase_rate_limit"}
        )["Item"]
        self.assertIn("blocked_until", item)
        self.assertEqual(item["reason"], "429 from /summary")
        # blocked_until must be in the future
        blocked_until = datetime.fromisoformat(item["blocked_until"])
        self.assertGreater(blocked_until, datetime.now(timezone.utc))

    def test_set_then_check_returns_true(self):
        ingest._set_rate_limit_block(self.config_table, "test")
        self.assertTrue(ingest._check_rate_limit_block(self.config_table))


# ---------------------------------------------------------------------------
# Rate limiting — _fetch_enphase_summary raises EnphaseRateLimitError on 429
# ---------------------------------------------------------------------------

class TestEnphase429Handling(unittest.TestCase):
    """_fetch_enphase_summary raises EnphaseRateLimitError on HTTP 429."""

    def test_429_raises_rate_limit_error(self):
        from urllib.error import HTTPError
        err = HTTPError(url="", code=429, msg="Too Many Requests", hdrs=None, fp=None)
        with patch("ingest_handler.urlopen", side_effect=err):
            with self.assertRaises(ingest.EnphaseRateLimitError):
                ingest._fetch_enphase_summary("sys-1", "key", "tok", "cid", "csec")

    def test_429_does_not_attempt_token_refresh(self):
        from urllib.error import HTTPError
        err = HTTPError(url="", code=429, msg="Too Many Requests", hdrs=None, fp=None)
        with patch("ingest_handler.urlopen", side_effect=err), \
             patch("ingest_handler._refresh_tokens") as mock_refresh:
            with self.assertRaises(ingest.EnphaseRateLimitError):
                ingest._fetch_enphase_summary("sys-1", "key", "tok", "cid", "csec")
        mock_refresh.assert_not_called()


# ---------------------------------------------------------------------------
# lambda_handler — rate limit / circuit breaker / nighttime / battery throttle
# ---------------------------------------------------------------------------

def _make_env_with_credentials():
    os.environ["ENPHASE_SYSTEM_ID"]     = SYSTEM_ID
    os.environ["ENPHASE_API_KEY"]       = "test-key"
    os.environ["ENPHASE_ACCESS_TOKEN"]  = "test-token"
    os.environ["ENPHASE_CLIENT_ID"]     = "test-client"
    os.environ["ENPHASE_CLIENT_SECRET"] = "test-secret"


def _clear_env():
    for key in (
        "ENERGY_TABLE", "CONFIG_TABLE", "ENPHASE_SYSTEM_ID",
        "ENPHASE_API_KEY", "ENPHASE_ACCESS_TOKEN",
        "ENPHASE_CLIENT_ID", "ENPHASE_CLIENT_SECRET",
    ):
        os.environ.pop(key, None)


def _fake_datetime_mock(local_hour: int):
    """Return a mock for `ingest_handler.datetime` pinned to `local_hour` Pacific."""
    from datetime import datetime as real_datetime
    from zoneinfo import ZoneInfo
    pacific = ZoneInfo("America/Los_Angeles")
    fake_pac = real_datetime(2026, 4, 22, local_hour, 0, tzinfo=pacific)
    fake_utc = fake_pac.astimezone(timezone.utc)

    def fake_now(tz=None):
        if tz is not None and getattr(tz, "key", None) == "America/Los_Angeles":
            return fake_pac
        return fake_utc

    mock_dt = MagicMock()
    mock_dt.now.side_effect = fake_now
    mock_dt.fromisoformat = real_datetime.fromisoformat
    mock_dt.fromtimestamp = real_datetime.fromtimestamp
    return mock_dt


@mock_aws
class TestRateLimitCircuitBreaker(unittest.TestCase):
    """lambda_handler circuit breaker: 429 sets block; active block skips calls."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ingest._dynamo = ddb
        self.table = _make_table(ddb)
        self.config_table = _make_config_table(ddb)
        os.environ["ENERGY_TABLE"]   = TABLE_NAME
        os.environ["CONFIG_TABLE"]   = CONFIG_TABLE_NAME
        _make_env_with_credentials()

    def tearDown(self):
        _clear_env()

    def _fake_summary_response(self):
        payload = {
            "current_power": 1000, "energy_today": 5000,
            "summary_date": "2026-04-22", "last_report_at": 1745280000,
        }
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(payload).encode()
        return mock_resp

    def test_429_sets_circuit_breaker_and_returns_rate_limited(self):
        """When _fetch_enphase_summary gets 429, handler writes block and returns rate_limited."""
        from urllib.error import HTTPError
        err = HTTPError(url="", code=429, msg="Too Many Requests", hdrs=None, fp=None)

        with patch("ingest_handler.datetime", _fake_datetime_mock(10)), \
             patch("ingest_handler.urlopen", side_effect=err):
            result = ingest.lambda_handler({}, None)

        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "rate_limited")

        # Circuit breaker record must have been written
        item = self.config_table.get_item(
            Key={"userId": "default", "configType": "enphase_rate_limit"}
        ).get("Item")
        self.assertIsNotNone(item)
        blocked_until = datetime.fromisoformat(item["blocked_until"])
        self.assertGreater(blocked_until, datetime.now(timezone.utc))

        # No energy reading should have been written
        self.assertEqual(len(self.table.scan()["Items"]), 0)

    def test_active_circuit_breaker_skips_enphase_calls(self):
        """When circuit breaker is active, no Enphase API calls are made."""
        from datetime import timedelta
        future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        self.config_table.put_item(Item={
            "userId": "default",
            "configType": "enphase_rate_limit",
            "blocked_until": future,
            "reason": "test block",
        })

        with patch("ingest_handler.datetime", _fake_datetime_mock(10)), \
             patch("ingest_handler._fetch_enphase_summary") as mock_fetch:
            result = ingest.lambda_handler({}, None)

        mock_fetch.assert_not_called()
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "rate_limited")
        self.assertEqual(len(self.table.scan()["Items"]), 0)

    def test_expired_circuit_breaker_allows_normal_flow(self):
        """When circuit breaker has expired, normal ingest resumes."""
        # Use a fixed past date so the comparison is stable regardless of mocked time
        past = "2026-01-01T00:00:00+00:00"
        self.config_table.put_item(Item={
            "userId": "default",
            "configType": "enphase_rate_limit",
            "blocked_until": past,
            "reason": "old block",
        })

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "battery" in url:
                payload = {"intervals": [{"soc": {"percent": "70"}}]}
            else:
                payload = {
                    "current_power": 2000, "energy_today": 7000,
                    "summary_date": "2026-04-22", "last_report_at": 1745280000,
                }
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = json.dumps(payload).encode()
            return mock_resp

        # Use hour=8 (divisible by 4) so battery SOC is also fetched
        with patch("ingest_handler.datetime", _fake_datetime_mock(8)), \
             patch("ingest_handler.urlopen", side_effect=fake_urlopen):
            result = ingest.lambda_handler({}, None)

        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(len(self.table.scan()["Items"]), 1)


@mock_aws
class TestNighttimeSkip(unittest.TestCase):
    """lambda_handler skips all Enphase API calls outside solar hours (9pm–6am Pacific)."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ingest._dynamo = ddb
        self.table = _make_table(ddb)
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        _make_env_with_credentials()

    def tearDown(self):
        _clear_env()

    def test_nighttime_hour_returns_skipped(self):
        """Hour 23 (11pm Pacific) should return skipped_nighttime without any API calls."""
        with patch("ingest_handler.datetime", _fake_datetime_mock(23)), \
             patch("ingest_handler._fetch_enphase_summary") as mock_fetch:
            result = ingest.lambda_handler({}, None)

        mock_fetch.assert_not_called()
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(body["status"], "skipped_nighttime")
        self.assertEqual(len(self.table.scan()["Items"]), 0)

    def test_early_morning_hour_returns_skipped(self):
        """Hour 3 (3am Pacific) should also return skipped_nighttime."""
        with patch("ingest_handler.datetime", _fake_datetime_mock(3)), \
             patch("ingest_handler._fetch_enphase_summary") as mock_fetch:
            result = ingest.lambda_handler({}, None)

        mock_fetch.assert_not_called()
        body = json.loads(result["body"])
        self.assertEqual(body["status"], "skipped_nighttime")

    def test_boundary_hour_6_runs_normally(self):
        """Hour 6 (6am) is the first active hour — should attempt ingest."""
        def fake_urlopen(req, timeout=None):
            payload = {
                "current_power": 100, "energy_today": 50,
                "summary_date": "2026-04-22", "last_report_at": 1745280000,
            }
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = json.dumps(payload).encode()
            return mock_resp

        # hour=6 is NOT divisible by 4 (6%4=2), so battery won't be fetched
        with patch("ingest_handler.datetime", _fake_datetime_mock(6)), \
             patch("ingest_handler.urlopen", side_effect=fake_urlopen):
            result = ingest.lambda_handler({}, None)

        body = json.loads(result["body"])
        self.assertNotEqual(body.get("status"), "skipped_nighttime")

    def test_boundary_hour_21_skips(self):
        """Hour 21 (9pm) is the first nighttime hour — should skip."""
        with patch("ingest_handler.datetime", _fake_datetime_mock(21)), \
             patch("ingest_handler._fetch_enphase_summary") as mock_fetch:
            result = ingest.lambda_handler({}, None)

        mock_fetch.assert_not_called()
        body = json.loads(result["body"])
        self.assertEqual(body["status"], "skipped_nighttime")


@mock_aws
class TestBatterySOCThrottle(unittest.TestCase):
    """Battery SOC is only fetched when local_hour % 4 == 0."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ingest._dynamo = ddb
        self.table = _make_table(ddb)
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        _make_env_with_credentials()

    def tearDown(self):
        _clear_env()

    def _fake_summary_urlopen(self, req, timeout=None):
        payload = {
            "current_power": 1500, "energy_today": 6000,
            "summary_date": "2026-04-22", "last_report_at": 1745280000,
        }
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(payload).encode()
        return mock_resp

    def test_battery_soc_fetched_on_hour_divisible_by_4(self):
        with patch("ingest_handler.datetime", _fake_datetime_mock(8)), \
             patch("ingest_handler.urlopen", side_effect=self._fake_summary_urlopen), \
             patch("ingest_handler._fetch_battery_soc", return_value=75) as mock_soc:
            result = ingest.lambda_handler({}, None)

        mock_soc.assert_called_once()
        body = json.loads(result["body"])
        self.assertEqual(body["battery_soc_pct"], 75)

    def test_battery_soc_not_fetched_on_non_divisible_hour(self):
        with patch("ingest_handler.datetime", _fake_datetime_mock(9)), \
             patch("ingest_handler.urlopen", side_effect=self._fake_summary_urlopen), \
             patch("ingest_handler._fetch_battery_soc") as mock_soc:
            result = ingest.lambda_handler({}, None)

        mock_soc.assert_not_called()
        body = json.loads(result["body"])
        self.assertIsNone(body["battery_soc_pct"])

    def test_battery_soc_fetched_at_hour_0(self):
        # Hour 0 is technically nighttime (< 6), so this would be skipped.
        # Hour 12 is the next divisible-by-4 daytime slot to verify.
        with patch("ingest_handler.datetime", _fake_datetime_mock(12)), \
             patch("ingest_handler.urlopen", side_effect=self._fake_summary_urlopen), \
             patch("ingest_handler._fetch_battery_soc", return_value=60) as mock_soc:
            result = ingest.lambda_handler({}, None)

        mock_soc.assert_called_once()
        body = json.loads(result["body"])
        self.assertEqual(body["battery_soc_pct"], 60)


if __name__ == "__main__":
    unittest.main()
