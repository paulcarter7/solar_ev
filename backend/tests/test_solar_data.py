"""
Unit tests for the solar_data Lambda handler.

Covers:
- _readings_from_dynamo: 24 hourly slots, Pacific-hour bucketing, cumulative
  energy diffs, multiple snapshots same hour, bad timestamp skipped, UTC-crossing
- _query_dynamo: queries both the requested date and next UTC date, filters
  by summary_date (uses moto DynamoDB)
- lambda_handler: mock fallback when no config, real Enphase data from DynamoDB,
  battery_soc_pct surfaced from latest item with that field
"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_handler

solar = load_handler("solar_data")

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


def _item(ts_utc_iso: str, energy_wh: int, summary_date: str, **extra):
    item = {
        "deviceId": DEVICE_ID,
        "timestamp": ts_utc_iso,
        "energy_wh": energy_wh,
        "power_w": energy_wh // 10,
        "summary_date": summary_date,
        "source": "enphase",
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# _readings_from_dynamo
# ---------------------------------------------------------------------------

class TestReadingsFromDynamo(unittest.TestCase):
    """Pure logic tests — no AWS needed."""

    def test_always_returns_24_slots(self):
        result = solar._readings_from_dynamo([], "2026-03-10")
        self.assertEqual(len(result), 24)

    def test_empty_input_all_zero_no_data(self):
        result = solar._readings_from_dynamo([], "2026-03-10")
        for r in result:
            self.assertEqual(r["production_wh"], 0)
            self.assertEqual(r["source"], "no_data")

    def test_correct_pacific_hour_bucketing(self):
        # 2026-03-10T18:00:00Z = 11:00 AM Pacific (UTC-7 in March, PDT starts Mar 8)
        items = [_item("2026-03-10T18:00:00+00:00", 3000, "2026-03-10")]
        result = solar._readings_from_dynamo(items, "2026-03-10")
        # UTC-7 in March (PDT), so 18:00 UTC = 11:00 Pacific
        self.assertEqual(result[11]["production_wh"], 3000)
        self.assertEqual(result[11]["source"], "enphase")
        # Other hours should be zero
        for r in result:
            if r["hour"] != 11:
                self.assertEqual(r["production_wh"], 0)

    def test_cumulative_diff_computed_correctly(self):
        # Three hourly snapshots with cumulative energy_wh values
        items = [
            _item("2026-03-10T17:00:00+00:00", 1000, "2026-03-10"),  # hour 10 Pacific
            _item("2026-03-10T18:00:00+00:00", 3000, "2026-03-10"),  # hour 11 Pacific
            _item("2026-03-10T19:00:00+00:00", 5500, "2026-03-10"),  # hour 12 Pacific
        ]
        result = solar._readings_from_dynamo(items, "2026-03-10")
        self.assertEqual(result[10]["production_wh"], 1000)  # 1000 - 0
        self.assertEqual(result[11]["production_wh"], 2000)  # 3000 - 1000
        self.assertEqual(result[12]["production_wh"], 2500)  # 5500 - 3000

    def test_multiple_snapshots_same_hour_accumulate(self):
        # Two snapshots landing in the same Pacific hour
        items = [
            _item("2026-03-10T18:00:00+00:00", 1000, "2026-03-10"),  # 11:00 Pacific
            _item("2026-03-10T18:30:00+00:00", 1500, "2026-03-10"),  # 11:30 Pacific
        ]
        result = solar._readings_from_dynamo(items, "2026-03-10")
        # First diff = 1000; second diff = 500; accumulated = 1500
        self.assertEqual(result[11]["production_wh"], 1500)

    def test_bad_timestamp_skipped_gracefully(self):
        items = [
            {"deviceId": DEVICE_ID, "timestamp": "not-a-timestamp",
             "energy_wh": 999, "power_w": 0, "summary_date": "2026-03-10"},
            _item("2026-03-10T18:00:00+00:00", 2000, "2026-03-10"),
        ]
        # Should not raise; the bad row is skipped
        result = solar._readings_from_dynamo(items, "2026-03-10")
        self.assertEqual(len(result), 24)
        self.assertEqual(result[11]["production_wh"], 2000)

    def test_utc_crossing_pacific_night_hour(self):
        # 2026-03-11T06:00:00Z = 11 PM Pacific on Mar 10 (23:00 PDT)
        items = [_item("2026-03-11T06:00:00+00:00", 500, "2026-03-10")]
        result = solar._readings_from_dynamo(items, "2026-03-10")
        self.assertEqual(result[23]["production_wh"], 500)

    def test_slot_timestamps_use_local_time_format(self):
        result = solar._readings_from_dynamo([], "2026-03-10")
        # Local-time format (no Z): browser getHours() works correctly
        self.assertEqual(result[0]["timestamp"], "2026-03-10T00:00:00")
        self.assertEqual(result[13]["timestamp"], "2026-03-10T13:00:00")


# ---------------------------------------------------------------------------
# _query_dynamo  (moto)
# ---------------------------------------------------------------------------

@mock_aws
class TestQueryDynamo(unittest.TestCase):
    """_query_dynamo queries both dates and filters by summary_date."""

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        solar._dynamo = ddb
        self.table = _make_table(ddb)

    def _put(self, item):
        self.table.put_item(Item=item)

    def test_returns_items_for_requested_local_date(self):
        # Row whose UTC date matches the requested date
        self._put(_item("2026-03-10T15:00:00+00:00", 1000, "2026-03-10"))
        result = solar._query_dynamo(TABLE_NAME, SYSTEM_ID, "2026-03-10")
        self.assertEqual(len(result), 1)

    def test_also_fetches_next_utc_date_for_pacific_evening(self):
        # Row on next UTC date but same Pacific local date (evening)
        self._put(_item("2026-03-11T05:00:00+00:00", 2000, "2026-03-10"))
        result = solar._query_dynamo(TABLE_NAME, SYSTEM_ID, "2026-03-10")
        self.assertEqual(len(result), 1)
        self.assertEqual(int(result[0]["energy_wh"]), 2000)

    def test_excludes_rows_from_different_local_date(self):
        # Row with UTC timestamp in range but summary_date for a different day
        self._put(_item("2026-03-10T07:00:00+00:00", 5000, "2026-03-09"))
        result = solar._query_dynamo(TABLE_NAME, SYSTEM_ID, "2026-03-10")
        self.assertEqual(len(result), 0)

    def test_results_sorted_by_timestamp(self):
        self._put(_item("2026-03-10T20:00:00+00:00", 3000, "2026-03-10"))
        self._put(_item("2026-03-10T16:00:00+00:00", 1000, "2026-03-10"))
        self._put(_item("2026-03-10T18:00:00+00:00", 2000, "2026-03-10"))
        result = solar._query_dynamo(TABLE_NAME, SYSTEM_ID, "2026-03-10")
        timestamps = [r["timestamp"] for r in result]
        self.assertEqual(timestamps, sorted(timestamps))


# ---------------------------------------------------------------------------
# lambda_handler  (moto)
# ---------------------------------------------------------------------------

@mock_aws
class TestSolarDataLambdaHandler(unittest.TestCase):

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        solar._dynamo = ddb
        self.table = _make_table(ddb)

    def tearDown(self):
        for key in ("ENERGY_TABLE", "ENPHASE_SYSTEM_ID"):
            os.environ.pop(key, None)

    def _call(self, date="2026-03-10"):
        event = {"queryStringParameters": {"date": date}}
        return solar.lambda_handler(event, None)

    def test_mock_fallback_when_no_env_vars(self):
        result = self._call()
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["data_source"], "mock")
        self.assertEqual(len(body["hourly_readings"]), 24)

    def test_enphase_source_when_dynamo_has_data(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        self.table.put_item(Item=_item(
            "2026-03-10T18:00:00+00:00", 5000, "2026-03-10"
        ))
        result = self._call()
        body = json.loads(result["body"])
        self.assertEqual(body["data_source"], "enphase")

    def test_mock_fallback_when_dynamo_table_empty(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        result = self._call()
        body = json.loads(result["body"])
        self.assertEqual(body["data_source"], "mock")

    def test_total_production_matches_sum_of_hourly(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        self.table.put_item(Item=_item(
            "2026-03-10T18:00:00+00:00", 4000, "2026-03-10"
        ))
        result = self._call()
        body = json.loads(result["body"])
        total = sum(r["production_wh"] for r in body["hourly_readings"])
        self.assertEqual(body["total_production_wh"], total)

    def test_battery_soc_from_latest_item(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        self.table.put_item(Item=_item(
            "2026-03-10T17:00:00+00:00", 2000, "2026-03-10"
        ))
        self.table.put_item(Item={
            **_item("2026-03-10T19:00:00+00:00", 4000, "2026-03-10"),
            "battery_soc_pct": 65,
        })
        result = self._call()
        body = json.loads(result["body"])
        self.assertEqual(body["home_battery_soc_pct"], 65)

    def test_battery_soc_none_when_no_items_have_it(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        self.table.put_item(Item=_item(
            "2026-03-10T18:00:00+00:00", 3000, "2026-03-10"
        ))
        result = self._call()
        body = json.loads(result["body"])
        self.assertIsNone(body["home_battery_soc_pct"])

    def test_tou_schedule_included(self):
        result = self._call()
        body = json.loads(result["body"])
        labels = {s["label"] for s in body["tou_schedule"]}
        self.assertIn("Peak", labels)
        self.assertIn("Super Off-Peak", labels)

    def test_battery_capacity_always_20kwh(self):
        result = self._call()
        body = json.loads(result["body"])
        self.assertEqual(body["home_battery_capacity_wh"], 20000)

    def test_weather_from_latest_item(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        self.table.put_item(Item=_item(
            "2026-03-10T17:00:00+00:00", 2000, "2026-03-10"
        ))
        self.table.put_item(Item={
            **_item("2026-03-10T19:00:00+00:00", 4000, "2026-03-10"),
            "cloud_cover_pct":   10,
            "temp_c":            18,
            "weather_condition": "Clear",
        })
        result = self._call()
        body = json.loads(result["body"])
        self.assertEqual(body["weather"]["cloud_cover_pct"], 10)
        self.assertEqual(body["weather"]["temp_c"], 18)
        self.assertEqual(body["weather"]["weather_condition"], "Clear")

    def test_weather_none_when_no_items_have_it(self):
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        self.table.put_item(Item=_item(
            "2026-03-10T18:00:00+00:00", 3000, "2026-03-10"
        ))
        result = self._call()
        body = json.loads(result["body"])
        self.assertIsNone(body["weather"])


if __name__ == "__main__":
    unittest.main()
