"""
Unit tests for the recommendation Lambda handler.

Covers:
- _score_window: cost calculation, solar coverage, score ordering
- _charging_source: all four source branches
- _get_hourly_solar_wh: mock fallback, real data from DynamoDB (moto),
  battery SOC extracted from latest matching item
- lambda_handler: response shape, energy calculation, SOC edge cases,
  battery_soc_pct in response
"""
import json
import os
import sys
import unittest

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.dirname(__file__))
from conftest import load_handler

rec = load_handler("recommendation")

TABLE_NAME = "test-energy-readings"
SYSTEM_ID = "sys-rec"
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


def _item(ts: str, energy_wh: int, summary_date: str, **extra):
    d = {
        "deviceId": DEVICE_ID,
        "timestamp": ts,
        "energy_wh": energy_wh,
        "power_w": energy_wh // 10,
        "summary_date": summary_date,
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# _score_window
# ---------------------------------------------------------------------------

class TestScoreWindow(unittest.TestCase):
    """_score_window produces correct cost and solar coverage."""

    _NO_SOLAR = [0] * 24

    def test_pure_grid_off_peak_cost(self):
        # 1-hour window at hour 2 (off-peak, $0.28/kWh), no solar, 7.2 kW charge rate
        result = rec._score_window(2, 1, self._NO_SOLAR)
        expected = round(0.28 * rec.EV_CHARGE_RATE_KW, 2)
        self.assertEqual(result["estimated_cost_usd"], expected)
        self.assertEqual(result["solar_coverage_pct"], 0.0)

    def test_pure_grid_super_offpeak_cheaper_than_offpeak(self):
        off_peak = rec._score_window(2, 1, self._NO_SOLAR)
        super_off = rec._score_window(9, 1, self._NO_SOLAR)  # super off-peak 09–14
        self.assertLess(super_off["estimated_cost_usd"], off_peak["estimated_cost_usd"])

    def test_peak_hour_most_expensive(self):
        peak = rec._score_window(17, 1, self._NO_SOLAR)
        off_peak = rec._score_window(2, 1, self._NO_SOLAR)
        self.assertGreater(peak["estimated_cost_usd"], off_peak["estimated_cost_usd"])

    def test_solar_coverage_reduces_score(self):
        # Enough solar in hours 9–11 to get high coverage
        high_solar = [0] * 24
        high_solar[9] = high_solar[10] = high_solar[11] = 10_000  # 10 kWh each
        with_solar = rec._score_window(9, 3, high_solar)
        without_solar = rec._score_window(9, 3, self._NO_SOLAR)
        self.assertLess(with_solar["score"], without_solar["score"])

    def test_solar_coverage_capped_at_100pct(self):
        massive_solar = [50_000] * 24
        result = rec._score_window(9, 3, massive_solar)
        self.assertEqual(result["solar_coverage_pct"], 100.0)

    def test_duration_spans_correct_hours(self):
        result = rec._score_window(10, 3, self._NO_SOLAR)
        self.assertEqual(result["start_hour"], 10)
        self.assertEqual(result["end_hour"], 13)
        self.assertEqual(result["duration_hours"], 3)


# ---------------------------------------------------------------------------
# _charging_source
# ---------------------------------------------------------------------------

class TestChargingSource(unittest.TestCase):
    """All four charging source branches."""

    def test_solar_direct_high_solar_low_battery(self):
        source, label = rec._charging_source(
            battery_soc_pct=30,
            solar_coverage_pct=85.0,
            rate_label="super off-peak",
        )
        self.assertEqual(source, "solar_direct")
        self.assertEqual(label, "Direct Solar")

    def test_solar_direct_high_solar_no_battery_info(self):
        source, label = rec._charging_source(
            battery_soc_pct=None,
            solar_coverage_pct=70.0,
            rate_label="off-peak",
        )
        self.assertEqual(source, "solar_direct")

    def test_solar_plus_battery_high_solar_high_battery(self):
        source, label = rec._charging_source(
            battery_soc_pct=75,
            solar_coverage_pct=80.0,
            rate_label="super off-peak",
        )
        self.assertEqual(source, "solar_plus_battery")
        self.assertIn("Battery", label)

    def test_home_battery_to_avoid_peak(self):
        # Low solar, battery ≥ 60, peak rate
        source, label = rec._charging_source(
            battery_soc_pct=65,
            solar_coverage_pct=20.0,
            rate_label="peak",
        )
        self.assertEqual(source, "home_battery")
        self.assertIn("peak", label.lower())

    def test_solar_plus_battery_partial_solar_high_battery(self):
        # battery ≥ 80 and solar ≥ 30 → solar_plus_battery
        source, label = rec._charging_source(
            battery_soc_pct=85,
            solar_coverage_pct=35.0,
            rate_label="off-peak",
        )
        self.assertEqual(source, "solar_plus_battery")

    def test_grid_fallback_low_solar_low_battery(self):
        source, label = rec._charging_source(
            battery_soc_pct=20,
            solar_coverage_pct=10.0,
            rate_label="off-peak",
        )
        self.assertEqual(source, "grid")

    def test_grid_fallback_no_battery_info_low_solar(self):
        source, label = rec._charging_source(
            battery_soc_pct=None,
            solar_coverage_pct=0.0,
            rate_label="super off-peak",
        )
        self.assertEqual(source, "grid")


# ---------------------------------------------------------------------------
# _get_hourly_solar_wh  (moto)
# ---------------------------------------------------------------------------

@mock_aws
class TestGetHourlySolarWh(unittest.TestCase):

    def setUp(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        rec._dynamo = ddb
        self.table = _make_table(ddb)
        os.environ["ENERGY_TABLE"] = TABLE_NAME
        os.environ["ENPHASE_SYSTEM_ID"] = SYSTEM_ID

    def tearDown(self):
        os.environ.pop("ENERGY_TABLE", None)
        os.environ.pop("ENPHASE_SYSTEM_ID", None)

    def test_mock_fallback_when_no_config(self):
        hourly, source, battery = rec._get_hourly_solar_wh("", "", "2026-03-10")
        self.assertEqual(source, "mock")
        self.assertEqual(len(hourly), 24)
        self.assertIsNone(battery)

    def test_mock_fallback_when_table_empty(self):
        hourly, source, battery = rec._get_hourly_solar_wh(
            TABLE_NAME, SYSTEM_ID, "2026-03-10"
        )
        self.assertEqual(source, "mock")

    def test_enphase_source_when_data_present(self):
        self.table.put_item(Item=_item(
            "2026-03-10T18:00:00+00:00", 5000, "2026-03-10"
        ))
        hourly, source, battery = rec._get_hourly_solar_wh(
            TABLE_NAME, SYSTEM_ID, "2026-03-10"
        )
        self.assertEqual(source, "enphase")
        self.assertEqual(len(hourly), 24)

    def test_hourly_production_diff_computed_correctly(self):
        self.table.put_item(Item=_item("2026-03-10T17:00:00+00:00", 1000, "2026-03-10"))
        self.table.put_item(Item=_item("2026-03-10T18:00:00+00:00", 3000, "2026-03-10"))
        hourly, _, _ = rec._get_hourly_solar_wh(TABLE_NAME, SYSTEM_ID, "2026-03-10")
        # UTC 17:00 = Pacific 10:00, UTC 18:00 = Pacific 11:00
        self.assertEqual(hourly[10], 1000)
        self.assertEqual(hourly[11], 2000)

    def test_battery_soc_from_latest_item(self):
        self.table.put_item(Item=_item(
            "2026-03-10T17:00:00+00:00", 2000, "2026-03-10"
        ))
        self.table.put_item(Item={
            **_item("2026-03-10T19:00:00+00:00", 4000, "2026-03-10"),
            "battery_soc_pct": 77,
        })
        _, _, battery = rec._get_hourly_solar_wh(TABLE_NAME, SYSTEM_ID, "2026-03-10")
        self.assertEqual(battery, 77)

    def test_battery_soc_none_when_field_absent(self):
        self.table.put_item(Item=_item(
            "2026-03-10T18:00:00+00:00", 3000, "2026-03-10"
        ))
        _, _, battery = rec._get_hourly_solar_wh(TABLE_NAME, SYSTEM_ID, "2026-03-10")
        self.assertIsNone(battery)


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------

class TestRecommendationLambdaHandler(unittest.TestCase):
    """lambda_handler response shape and calculation correctness."""

    def _call(self, current_soc=0.3, target_soc=0.8, date="2026-03-10"):
        event = {"queryStringParameters": {
            "current_soc": str(current_soc),
            "target_soc": str(target_soc),
            "date": date,
        }}
        return rec.lambda_handler(event, None)

    def test_returns_200_with_required_fields(self):
        result = self._call()
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        for field in ("date", "ev_model", "battery_kwh", "charge_rate_kw",
                      "current_soc_pct", "target_soc_pct", "energy_needed_kwh",
                      "hours_needed", "best_window", "summary", "data_source"):
            self.assertIn(field, body)

    def test_energy_needed_calculation(self):
        # 30% → 80% of 76.6 kWh = 38.3 kWh
        result = self._call(current_soc=0.3, target_soc=0.8)
        body = json.loads(result["body"])
        expected = round(76.6 * (0.8 - 0.3), 1)
        self.assertAlmostEqual(body["energy_needed_kwh"], expected, places=1)

    def test_current_soc_above_target_gives_zero_energy(self):
        result = self._call(current_soc=0.9, target_soc=0.8)
        body = json.loads(result["body"])
        self.assertEqual(body["energy_needed_kwh"], 0.0)
        self.assertEqual(body["hours_needed"], 1)  # max(1, ...) floor

    def test_soc_percentages_in_response(self):
        result = self._call(current_soc=0.25, target_soc=0.90)
        body = json.loads(result["body"])
        self.assertEqual(body["current_soc_pct"], 25)
        self.assertEqual(body["target_soc_pct"], 90)

    def test_best_window_fields_present(self):
        result = self._call()
        body = json.loads(result["body"])
        window = body["best_window"]
        for field in ("start", "end", "rate_period",
                      "estimated_cost_usd", "solar_coverage_pct"):
            self.assertIn(field, window)

    def test_best_window_start_time_format(self):
        result = self._call()
        body = json.loads(result["body"])
        start = body["best_window"]["start"]
        # Should be HH:MM format
        self.assertRegex(start, r"^\d{2}:\d{2}$")

    def test_all_candidates_limited_to_five(self):
        result = self._call()
        body = json.loads(result["body"])
        self.assertLessEqual(len(body["all_candidates"]), 5)

    def test_super_offpeak_preferred_over_peak_no_solar(self):
        # With no solar data (mock will be used), super off-peak should score better
        result = self._call()
        body = json.loads(result["body"])
        # Best window should not be during peak hours (16–20) with no solar advantage
        best_start = int(body["best_window"]["start"].split(":")[0])
        # Super off-peak is 9–14, off-peak is cheaper than peak — shouldn't pick peak
        self.assertNotIn(body["best_window"]["rate_period"], ["peak"])

    def test_battery_soc_pct_in_response(self):
        # Mock solar data path returns None for battery_soc; field should still be present
        result = self._call()
        body = json.loads(result["body"])
        self.assertIn("home_battery_soc_pct", body)

    def test_uses_mock_data_when_no_dynamo_config(self):
        result = self._call()
        body = json.loads(result["body"])
        self.assertEqual(body["data_source"], "mock")


if __name__ == "__main__":
    unittest.main()
