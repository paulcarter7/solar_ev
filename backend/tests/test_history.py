"""
Unit tests for the history Lambda handler.

Covers:
- _compute_daily_totals: date range generation, max energy_wh as daily total,
  peak power, missing days filled with zeros, items outside range ignored
- _query_dynamo_range: between query covers correct UTC timestamp range,
  including Pacific-to-UTC boundary (uses moto DynamoDB)
- lambda_handler: mock fallback when no env vars, real data from DynamoDB,
  days param validation (default, max, min, invalid), response shape
"""
import json
import os
import sys
import unittest
from datetime import date as date_cls

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_handler

history = load_handler("history")

TABLE_NAME = "test-energy-readings"
SYSTEM_ID = "sys-abc"
DEVICE_ID = f"enphase-{SYSTEM_ID}"


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


def _item(ts_utc_iso: str, energy_wh: int, summary_date: str, power_w: int = None):
    return {
        "deviceId": DEVICE_ID,
        "timestamp": ts_utc_iso,
        "energy_wh": energy_wh,
        "power_w": power_w if power_w is not None else energy_wh // 10,
        "summary_date": summary_date,
        "source": "enphase",
    }


# ---------------------------------------------------------------------------
# _compute_daily_totals
# ---------------------------------------------------------------------------

class TestComputeDailyTotals(unittest.TestCase):
    """Pure logic tests — no AWS needed."""

    def test_returns_entry_for_every_day_in_range(self):
        result = history._compute_daily_totals([], "2026-03-10", "2026-03-16")
        self.assertEqual(len(result), 7)
        dates = [d["date"] for d in result]
        self.assertEqual(dates[0], "2026-03-10")
        self.assertEqual(dates[-1], "2026-03-16")

    def test_missing_day_gets_zero_production(self):
        result = history._compute_daily_totals([], "2026-03-10", "2026-03-10")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["total_production_kwh"], 0.0)
        self.assertEqual(result[0]["peak_power_w"], 0)
        self.assertEqual(result[0]["data_source"], "no_data")

    def test_max_energy_wh_used_as_daily_total(self):
        # Three items for the same day — max wins
        items = [
            _item("2026-03-10T15:00:00Z", 5000, "2026-03-10"),
            _item("2026-03-10T20:00:00Z", 18000, "2026-03-10"),  # highest
            _item("2026-03-10T22:00:00Z", 17000, "2026-03-10"),
        ]
        result = history._compute_daily_totals(items, "2026-03-10", "2026-03-10")
        self.assertEqual(result[0]["total_production_kwh"], 18.0)

    def test_peak_power_is_max_power_w(self):
        items = [
            _item("2026-03-10T15:00:00Z", 5000, "2026-03-10", power_w=2000),
            _item("2026-03-10T19:00:00Z", 10000, "2026-03-10", power_w=4500),
            _item("2026-03-10T20:00:00Z", 15000, "2026-03-10", power_w=3200),
        ]
        result = history._compute_daily_totals(items, "2026-03-10", "2026-03-10")
        self.assertEqual(result[0]["peak_power_w"], 4500)

    def test_data_source_enphase_for_day_with_items(self):
        items = [_item("2026-03-10T15:00:00Z", 10000, "2026-03-10")]
        result = history._compute_daily_totals(items, "2026-03-10", "2026-03-10")
        self.assertEqual(result[0]["data_source"], "enphase")

    def test_items_with_summary_date_outside_range_ignored(self):
        # summary_date is outside the requested range — should not appear
        items = [_item("2026-03-09T15:00:00Z", 20000, "2026-03-09")]
        result = history._compute_daily_totals(items, "2026-03-10", "2026-03-11")
        for d in result:
            self.assertEqual(d["total_production_kwh"], 0.0)

    def test_single_day_range(self):
        items = [_item("2026-03-10T18:00:00Z", 24000, "2026-03-10")]
        result = history._compute_daily_totals(items, "2026-03-10", "2026-03-10")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["total_production_kwh"], 24.0)

    def test_multiple_days_each_computed_independently(self):
        items = [
            _item("2026-03-10T18:00:00Z", 20000, "2026-03-10"),
            _item("2026-03-11T18:00:00Z", 30000, "2026-03-11"),
        ]
        result = history._compute_daily_totals(items, "2026-03-10", "2026-03-11")
        self.assertEqual(result[0]["total_production_kwh"], 20.0)
        self.assertEqual(result[1]["total_production_kwh"], 30.0)

    def test_output_dates_are_chronological(self):
        result = history._compute_daily_totals([], "2026-03-10", "2026-03-16")
        dates = [d["date"] for d in result]
        self.assertEqual(dates, sorted(dates))

    def test_mixed_days_some_with_data_some_without(self):
        items = [_item("2026-03-11T18:00:00Z", 15000, "2026-03-11")]
        result = history._compute_daily_totals(items, "2026-03-10", "2026-03-12")
        self.assertEqual(result[0]["data_source"], "no_data")   # Mar 10 — no data
        self.assertEqual(result[1]["data_source"], "enphase")   # Mar 11 — has data
        self.assertEqual(result[2]["data_source"], "no_data")   # Mar 12 — no data


# ---------------------------------------------------------------------------
# _query_dynamo_range  (moto)
# ---------------------------------------------------------------------------

@mock_aws
class TestQueryDynamoRange(unittest.TestCase):
    """_query_dynamo_range uses between query covering the correct UTC range."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        history._dynamo = ddb
        self.table = _make_table(ddb)

    def _put(self, item):
        self.table.put_item(Item=item)

    def test_returns_items_within_date_range(self):
        self._put(_item("2026-03-10T15:00:00Z", 5000, "2026-03-10"))
        self._put(_item("2026-03-14T15:00:00Z", 8000, "2026-03-14"))
        result = history._query_dynamo_range(TABLE_NAME, SYSTEM_ID, "2026-03-10", "2026-03-14")
        self.assertEqual(len(result), 2)

    def test_excludes_items_before_range(self):
        self._put(_item("2026-03-09T15:00:00Z", 5000, "2026-03-09"))
        result = history._query_dynamo_range(TABLE_NAME, SYSTEM_ID, "2026-03-10", "2026-03-14")
        self.assertEqual(len(result), 0)

    def test_covers_pacific_utc_boundary_on_last_day(self):
        # 07:30 UTC on end_date+1 = late evening Pacific on end_date (PDT, UTC-7)
        self._put(_item("2026-03-15T07:30:00Z", 5000, "2026-03-14"))
        result = history._query_dynamo_range(TABLE_NAME, SYSTEM_ID, "2026-03-10", "2026-03-14")
        self.assertEqual(len(result), 1)

    def test_multiple_days_all_returned(self):
        for day in range(10, 15):  # Mar 10–14
            self._put(_item(f"2026-03-{day:02d}T15:00:00Z", 5000 * day, f"2026-03-{day:02d}"))
        result = history._query_dynamo_range(TABLE_NAME, SYSTEM_ID, "2026-03-10", "2026-03-14")
        self.assertEqual(len(result), 5)

    def test_empty_table_returns_empty_list(self):
        result = history._query_dynamo_range(TABLE_NAME, SYSTEM_ID, "2026-03-10", "2026-03-14")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# lambda_handler  (moto)
# ---------------------------------------------------------------------------

@mock_aws
class TestHistoryLambdaHandler(unittest.TestCase):

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        history._dynamo = ddb
        self.table = _make_table(ddb)

    def tearDown(self):
        for key in ("ENERGY_TABLE", "ENPHASE_SYSTEM_ID"):
            os.environ.pop(key, None)

    def _call(self, days=None):
        params = {}
        if days is not None:
            params["days"] = str(days)
        event = {"queryStringParameters": params if params else None}
        return history.lambda_handler(event, None)

    def test_mock_fallback_when_no_env_vars(self):
        result = self._call(days=7)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["data_source"], "mock")
        self.assertEqual(len(body["days"]), 7)

    def test_default_14_days_when_no_param(self):
        result = self._call()
        body = json.loads(result["body"])
        self.assertEqual(body["days_requested"], 14)
        self.assertEqual(len(body["days"]), 14)

    def test_days_clamped_to_90_max(self):
        result = self._call(days=200)
        body = json.loads(result["body"])
        self.assertEqual(body["days_requested"], 90)
        self.assertEqual(len(body["days"]), 90)

    def test_days_clamped_to_1_min(self):
        result = self._call(days=0)
        body = json.loads(result["body"])
        self.assertEqual(body["days_requested"], 1)
        self.assertEqual(len(body["days"]), 1)

    def test_invalid_days_param_returns_400(self):
        event = {"queryStringParameters": {"days": "not-a-number"}}
        result = history.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 400)

    def test_enphase_source_when_dynamo_has_data(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        # Insert an item for today (handler computes today in Pacific time)
        from datetime import date
        today = date.today().isoformat()
        self.table.put_item(Item=_item(f"{today}T15:00:00Z", 20000, today))
        result = self._call(days=1)
        body = json.loads(result["body"])
        self.assertEqual(body["data_source"], "enphase")

    def test_mock_fallback_when_table_is_empty(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        result = self._call(days=7)
        body = json.loads(result["body"])
        self.assertEqual(body["data_source"], "mock")

    def test_response_contains_all_required_fields(self):
        result = self._call(days=7)
        body = json.loads(result["body"])
        for key in ("start_date", "end_date", "days_requested", "days", "avg_production_kwh", "data_source"):
            self.assertIn(key, body)

    def test_each_day_has_required_fields(self):
        result = self._call(days=3)
        body = json.loads(result["body"])
        for day in body["days"]:
            for key in ("date", "total_production_kwh", "peak_power_w", "data_source"):
                self.assertIn(key, day)

    def test_avg_production_matches_days_data(self):
        result = self._call(days=7)
        body = json.loads(result["body"])
        computed_avg = sum(d["total_production_kwh"] for d in body["days"]) / len(body["days"])
        self.assertAlmostEqual(body["avg_production_kwh"], computed_avg, places=2)

    def test_days_list_is_chronological(self):
        result = self._call(days=14)
        body = json.loads(result["body"])
        dates = [d["date"] for d in body["days"]]
        self.assertEqual(dates, sorted(dates))

    def test_start_end_date_span_matches_days_requested(self):
        result = self._call(days=7)
        body = json.loads(result["body"])
        start = date_cls.fromisoformat(body["start_date"])
        end = date_cls.fromisoformat(body["end_date"])
        self.assertEqual((end - start).days + 1, 7)

    def test_no_query_string_params(self):
        # event with no queryStringParameters at all
        result = history.lambda_handler({}, None)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["days_requested"], 14)

    def test_mock_days_have_nonzero_production(self):
        # Mock data should look realistic — not all zeros
        result = self._call(days=14)
        body = json.loads(result["body"])
        self.assertEqual(body["data_source"], "mock")
        total_kwh = sum(d["total_production_kwh"] for d in body["days"])
        self.assertGreater(total_kwh, 0)


if __name__ == "__main__":
    unittest.main()
