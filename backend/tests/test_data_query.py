"""
Unit tests for the data_query Lambda handler.

Covers:
- _extract_intent: JSON parsing, markdown fence stripping
- _aggregate: all metrics, all aggregations, empty data, missing fields
- lambda_handler: happy path, missing query, no data, Bedrock error, bad intent
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_handler

data_query = load_handler("data_query")

TODAY = "2026-06-18"
YESTERDAY = "2026-06-17"


def _event(body: dict | None = None) -> dict:
    return {
        "httpMethod": "POST",
        "path": "/data-query",
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


FAKE_INTENT = {
    "metric": "production_wh",
    "aggregation": "total",
    "start_date": YESTERDAY,
    "end_date": YESTERDAY,
}

FAKE_ROWS = {
    YESTERDAY: [
        {"deviceId": "enphase-123", "timestamp": "2026-06-17T16:00:00Z",
         "energy_wh": 5000, "power_w": 800, "summary_date": YESTERDAY, "battery_soc_pct": 72},
        {"deviceId": "enphase-123", "timestamp": "2026-06-17T20:00:00Z",
         "energy_wh": 22000, "power_w": 200, "summary_date": YESTERDAY, "battery_soc_pct": 95},
    ]
}


# ---------------------------------------------------------------------------
# _extract_intent
# ---------------------------------------------------------------------------

class TestExtractIntent(unittest.TestCase):
    def _mock_invoke(self, text: str):
        return patch.object(data_query._bedrock, "invoke_model",
                            return_value=_nova_response(text))

    def test_parses_clean_json(self):
        intent_json = json.dumps(FAKE_INTENT)
        with self._mock_invoke(intent_json):
            result = data_query._extract_intent("How much did I produce yesterday?", TODAY)
        self.assertEqual(result["metric"], "production_wh")
        self.assertEqual(result["aggregation"], "total")
        self.assertEqual(result["start_date"], YESTERDAY)

    def test_strips_markdown_fences(self):
        fenced = f"```json\n{json.dumps(FAKE_INTENT)}\n```"
        with self._mock_invoke(fenced):
            result = data_query._extract_intent("any query", TODAY)
        self.assertEqual(result["metric"], "production_wh")

    def test_strips_bare_fences(self):
        fenced = f"```\n{json.dumps(FAKE_INTENT)}\n```"
        with self._mock_invoke(fenced):
            result = data_query._extract_intent("any query", TODAY)
        self.assertEqual(result["metric"], "production_wh")


# ---------------------------------------------------------------------------
# _aggregate
# ---------------------------------------------------------------------------

class TestAggregate(unittest.TestCase):
    def test_production_total(self):
        result = data_query._aggregate(FAKE_ROWS, {**FAKE_INTENT, "aggregation": "total"})
        # max(energy_wh) for the day = 22000
        self.assertAlmostEqual(result["value"], 22000.0)
        self.assertEqual(result["unit"], "Wh")

    def test_production_average(self):
        rows = {
            "2026-06-16": [{"energy_wh": 10000, "summary_date": "2026-06-16"}],
            "2026-06-17": [{"energy_wh": 20000, "summary_date": "2026-06-17"}],
        }
        result = data_query._aggregate(rows, {**FAKE_INTENT, "aggregation": "average",
                                               "start_date": "2026-06-16", "end_date": "2026-06-17"})
        self.assertAlmostEqual(result["value"], 15000.0)

    def test_production_maximum_returns_best_day(self):
        rows = {
            "2026-06-16": [{"energy_wh": 10000, "summary_date": "2026-06-16"}],
            "2026-06-17": [{"energy_wh": 30000, "summary_date": "2026-06-17"}],
        }
        result = data_query._aggregate(rows, {**FAKE_INTENT, "aggregation": "maximum"})
        self.assertEqual(result["best_day"], "2026-06-17")
        self.assertAlmostEqual(result["value"], 30000.0)

    def test_production_minimum_returns_worst_day(self):
        rows = {
            "2026-06-16": [{"energy_wh": 5000, "summary_date": "2026-06-16"}],
            "2026-06-17": [{"energy_wh": 30000, "summary_date": "2026-06-17"}],
        }
        result = data_query._aggregate(rows, {**FAKE_INTENT, "aggregation": "minimum"})
        self.assertEqual(result["worst_day"], "2026-06-16")

    def test_battery_soc_average(self):
        result = data_query._aggregate(
            FAKE_ROWS,
            {"metric": "battery_soc_pct", "aggregation": "average",
             "start_date": YESTERDAY, "end_date": YESTERDAY},
        )
        self.assertAlmostEqual(result["value"], 83.5)  # (72 + 95) / 2
        self.assertEqual(result["unit"], "%")

    def test_battery_soc_latest(self):
        result = data_query._aggregate(
            FAKE_ROWS,
            {"metric": "battery_soc_pct", "aggregation": "latest",
             "start_date": YESTERDAY, "end_date": YESTERDAY},
        )
        self.assertAlmostEqual(result["value"], 95.0)

    def test_power_maximum(self):
        result = data_query._aggregate(
            FAKE_ROWS,
            {"metric": "power_w", "aggregation": "maximum",
             "start_date": YESTERDAY, "end_date": YESTERDAY},
        )
        self.assertAlmostEqual(result["value"], 800.0)

    def test_empty_rows_returns_null_value(self):
        result = data_query._aggregate({}, FAKE_INTENT)
        self.assertIsNone(result["value"])
        self.assertEqual(result["days_with_data"], 0)

    def test_rows_missing_metric_returns_null(self):
        rows = {YESTERDAY: [{"deviceId": "x", "timestamp": "2026-06-17T12:00:00Z",
                              "summary_date": YESTERDAY}]}
        result = data_query._aggregate(
            rows,
            {"metric": "battery_soc_pct", "aggregation": "average",
             "start_date": YESTERDAY, "end_date": YESTERDAY},
        )
        self.assertIsNone(result["value"])


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------

class TestLambdaHandler(unittest.TestCase):
    ENV = {"ENERGY_TABLE": "solar-ev-energy-readings", "ENPHASE_SYSTEM_ID": "123"}

    def _setup_mocks(self, rows=None):
        if rows is None:
            rows = FAKE_ROWS
        return (
            patch.object(data_query, "_extract_intent", return_value=FAKE_INTENT),
            patch.object(data_query, "_fetch_days", return_value=rows),
            patch.object(data_query, "_format_answer", return_value="You produced 22 kWh yesterday."),
        )

    def test_missing_query_returns_400(self):
        with patch.dict(os.environ, self.ENV):
            result = data_query.lambda_handler(_event({"query": ""}), None)
        self.assertEqual(result["statusCode"], 400)

    def test_null_body_returns_400(self):
        with patch.dict(os.environ, self.ENV):
            result = data_query.lambda_handler(_event(None), None)
        self.assertEqual(result["statusCode"], 400)

    def test_missing_env_returns_503(self):
        with patch.dict(os.environ, {"ENERGY_TABLE": "", "ENPHASE_SYSTEM_ID": ""}, clear=False):
            result = data_query.lambda_handler(_event({"query": "How much yesterday?"}), None)
        self.assertEqual(result["statusCode"], 503)

    def test_happy_path_returns_response(self):
        mocks = self._setup_mocks()
        with patch.dict(os.environ, self.ENV), mocks[0], mocks[1], mocks[2]:
            result = data_query.lambda_handler(_event({"query": "How much did I produce yesterday?"}), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["response"], "You produced 22 kWh yesterday.")
        self.assertIn("intent", body)
        self.assertIn("result", body)

    def test_no_data_returns_200_with_message(self):
        mocks = self._setup_mocks(rows={})
        with patch.dict(os.environ, self.ENV), mocks[0], mocks[1], mocks[2]:
            result = data_query.lambda_handler(_event({"query": "How much last Tuesday?"}), None)

        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertIn("don't have any energy data", body["response"])

    def test_bad_intent_json_returns_422(self):
        with (
            patch.dict(os.environ, self.ENV),
            patch.object(data_query, "_extract_intent", side_effect=json.JSONDecodeError("bad", "", 0)),
        ):
            result = data_query.lambda_handler(_event({"query": "Something?"}), None)
        self.assertEqual(result["statusCode"], 422)

    def test_bedrock_error_returns_502(self):
        from botocore.exceptions import ClientError
        err = {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}}
        with (
            patch.dict(os.environ, self.ENV),
            patch.object(data_query, "_extract_intent",
                         side_effect=ClientError(err, "InvokeModel")),
        ):
            result = data_query.lambda_handler(_event({"query": "Something?"}), None)
        self.assertEqual(result["statusCode"], 502)

    def test_unexpected_error_returns_500(self):
        with (
            patch.dict(os.environ, self.ENV),
            patch.object(data_query, "_extract_intent", side_effect=RuntimeError("boom")),
        ):
            result = data_query.lambda_handler(_event({"query": "Something?"}), None)
        self.assertEqual(result["statusCode"], 500)


if __name__ == "__main__":
    unittest.main()
