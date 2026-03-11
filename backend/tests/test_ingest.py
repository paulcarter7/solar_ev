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

@mock_aws
class TestIngestLambdaHandler(unittest.TestCase):
    """lambda_handler integration: skips gracefully, writes on success."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ingest._dynamo = ddb
        self.table = _make_table(ddb)
        os.environ["ENERGY_TABLE"] = TABLE_NAME

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
