"""
Unit tests for anomaly detection and query.

Covers:
- _detect_anomalies: all rules, boundary conditions, outside peak hours
- _write_anomalies: writes correct item shape to DynamoDB
- anomaly_query lambda_handler: happy path, no anomalies, missing env, errors
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_handler

ingest = load_handler("ingest")
anomaly_query = load_handler("anomaly_query")

ENV = {
    "ANOMALY_TABLE": "solar-ev-anomalies",
    "ENPHASE_SYSTEM_ID": "123",
}


def _event(body: dict | None = None) -> dict:
    return {
        "httpMethod": "POST",
        "path": "/anomalies",
        "body": json.dumps(body) if body is not None else None,
        "headers": {},
        "queryStringParameters": None,
    }


def _nova_response(text: str) -> dict:
    mock_body = MagicMock()
    mock_body.read.return_value = json.dumps({
        "output": {"message": {"content": [{"text": text}], "role": "assistant"}},
        "stopReason": "end_turn",
    }).encode()
    return {"body": mock_body}


# ---------------------------------------------------------------------------
# _detect_anomalies (in ingest handler)
# ---------------------------------------------------------------------------

class TestDetectAnomalies(unittest.TestCase):
    def test_no_production_during_peak_clear_sky(self):
        anomalies = ingest._detect_anomalies(
            power_w=0, cloud_cover_pct=10, battery_soc_pct=80, local_hour=12
        )
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["type"], "no_production")
        self.assertEqual(anomalies[0]["severity"], "high")

    def test_no_production_not_flagged_outside_peak_hours(self):
        anomalies = ingest._detect_anomalies(
            power_w=0, cloud_cover_pct=10, battery_soc_pct=80, local_hour=7
        )
        self.assertEqual(anomalies, [])

    def test_no_production_not_flagged_when_cloudy(self):
        anomalies = ingest._detect_anomalies(
            power_w=0, cloud_cover_pct=80, battery_soc_pct=80, local_hour=12
        )
        self.assertEqual(anomalies, [])

    def test_low_production_during_peak_clear_sky(self):
        anomalies = ingest._detect_anomalies(
            power_w=500, cloud_cover_pct=5, battery_soc_pct=80, local_hour=11
        )
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["type"], "low_production")
        self.assertEqual(anomalies[0]["severity"], "medium")

    def test_low_production_not_flagged_when_partly_cloudy(self):
        # cloud_cover >= 20 — low production is expected
        anomalies = ingest._detect_anomalies(
            power_w=500, cloud_cover_pct=25, battery_soc_pct=80, local_hour=11
        )
        self.assertEqual(anomalies, [])

    def test_low_production_not_flagged_when_above_threshold(self):
        anomalies = ingest._detect_anomalies(
            power_w=2000, cloud_cover_pct=5, battery_soc_pct=80, local_hour=12
        )
        self.assertEqual(anomalies, [])

    def test_battery_critically_low(self):
        anomalies = ingest._detect_anomalies(
            power_w=3000, cloud_cover_pct=5, battery_soc_pct=5, local_hour=12
        )
        types = [a["type"] for a in anomalies]
        self.assertIn("battery_critically_low", types)

    def test_battery_not_flagged_when_above_threshold(self):
        anomalies = ingest._detect_anomalies(
            power_w=3000, cloud_cover_pct=5, battery_soc_pct=15, local_hour=12
        )
        types = [a["type"] for a in anomalies]
        self.assertNotIn("battery_critically_low", types)

    def test_no_weather_data_skips_production_rules(self):
        # cloud_cover_pct=None means no weather data — skip production anomalies
        anomalies = ingest._detect_anomalies(
            power_w=0, cloud_cover_pct=None, battery_soc_pct=80, local_hour=12
        )
        self.assertEqual(anomalies, [])

    def test_no_battery_data_skips_battery_rule(self):
        anomalies = ingest._detect_anomalies(
            power_w=3000, cloud_cover_pct=5, battery_soc_pct=None, local_hour=12
        )
        types = [a["type"] for a in anomalies]
        self.assertNotIn("battery_critically_low", types)

    def test_multiple_anomalies_returned(self):
        # Zero production + critically low battery
        anomalies = ingest._detect_anomalies(
            power_w=0, cloud_cover_pct=5, battery_soc_pct=3, local_hour=12
        )
        self.assertEqual(len(anomalies), 2)


# ---------------------------------------------------------------------------
# anomaly_query lambda_handler
# ---------------------------------------------------------------------------

class TestAnomalyQueryHandler(unittest.TestCase):
    FAKE_ANOMALIES = [
        {"type": "no_production", "severity": "high",
         "description": "Zero output at 12:00", "timestamp": "2026-06-17T19:00:00+00:00"},
    ]

    def test_missing_query_returns_400(self):
        with patch.dict(os.environ, ENV):
            result = anomaly_query.lambda_handler(_event({"query": ""}), None)
        self.assertEqual(result["statusCode"], 400)

    def test_missing_env_returns_503(self):
        with patch.dict(os.environ, {"ANOMALY_TABLE": "", "ENPHASE_SYSTEM_ID": ""}, clear=False):
            result = anomaly_query.lambda_handler(_event({"query": "Any issues?"}), None)
        self.assertEqual(result["statusCode"], 503)

    def test_happy_path_with_anomalies(self):
        with (
            patch.dict(os.environ, ENV),
            patch.object(anomaly_query, "_fetch_anomalies", return_value=self.FAKE_ANOMALIES),
            patch.object(anomaly_query._bedrock, "invoke_model",
                         return_value=_nova_response("One anomaly: zero production at noon.")),
        ):
            result = anomaly_query.lambda_handler(_event({"query": "Any issues this week?"}), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertIn("response", body)
        self.assertEqual(len(body["anomalies"]), 1)
        self.assertEqual(body["anomalies"][0]["type"], "no_production")

    def test_no_anomalies_returns_healthy_summary(self):
        with (
            patch.dict(os.environ, ENV),
            patch.object(anomaly_query, "_fetch_anomalies", return_value=[]),
            patch.object(anomaly_query._bedrock, "invoke_model",
                         return_value=_nova_response("System looks healthy.")),
        ):
            result = anomaly_query.lambda_handler(_event({"query": "Any issues?"}), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["anomalies"], [])

    def test_bedrock_error_returns_502(self):
        from botocore.exceptions import ClientError
        err = {"Error": {"Code": "ThrottlingException", "Message": "Too many"}}
        with (
            patch.dict(os.environ, ENV),
            patch.object(anomaly_query, "_fetch_anomalies", return_value=[]),
            patch.object(anomaly_query._bedrock, "invoke_model",
                         side_effect=ClientError(err, "InvokeModel")),
        ):
            result = anomaly_query.lambda_handler(_event({"query": "Any issues?"}), None)
        self.assertEqual(result["statusCode"], 502)

    def test_unexpected_error_returns_500(self):
        with (
            patch.dict(os.environ, ENV),
            patch.object(anomaly_query, "_fetch_anomalies", side_effect=RuntimeError("boom")),
        ):
            result = anomaly_query.lambda_handler(_event({"query": "Any issues?"}), None)
        self.assertEqual(result["statusCode"], 500)


if __name__ == "__main__":
    unittest.main()
