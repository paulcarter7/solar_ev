"""
Unit tests for shared/utils.py.

Covers:
- api_response: status code, body serialisation, CORS headers present/absent
- utc_now: returns timezone-aware UTC datetime
- today_iso: returns YYYY-MM-DD string matching UTC today
"""
import json
import re
import sys
import os
import unittest
from datetime import date, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
import utils


class TestApiResponse(unittest.TestCase):
    """api_response builds a valid Lambda proxy response dict."""

    def test_status_code_is_set(self):
        resp = utils.api_response(200, {"ok": True})
        self.assertEqual(resp["statusCode"], 200)

    def test_body_is_json_string(self):
        resp = utils.api_response(200, {"key": "value"})
        body = json.loads(resp["body"])
        self.assertEqual(body["key"], "value")

    def test_body_serialises_non_json_types_via_default_str(self):
        from datetime import date
        resp = utils.api_response(200, {"d": date(2026, 3, 11)})
        body = json.loads(resp["body"])
        self.assertEqual(body["d"], "2026-03-11")

    def test_cors_headers_present_by_default(self):
        resp = utils.api_response(200, {})
        self.assertEqual(resp["headers"]["Access-Control-Allow-Origin"], "*")
        self.assertIn("Access-Control-Allow-Headers", resp["headers"])
        self.assertIn("Access-Control-Allow-Methods", resp["headers"])

    def test_cors_headers_absent_when_disabled(self):
        resp = utils.api_response(200, {}, cors=False)
        self.assertNotIn("Access-Control-Allow-Origin", resp["headers"])

    def test_content_type_always_set(self):
        resp = utils.api_response(200, {}, cors=False)
        self.assertEqual(resp["headers"]["Content-Type"], "application/json")

    def test_error_status_codes_pass_through(self):
        resp = utils.api_response(404, {"error": "not found"})
        self.assertEqual(resp["statusCode"], 404)

    def test_empty_body_serialises(self):
        resp = utils.api_response(204, {})
        self.assertEqual(json.loads(resp["body"]), {})

    def test_list_body_serialises(self):
        resp = utils.api_response(200, [1, 2, 3])
        self.assertEqual(json.loads(resp["body"]), [1, 2, 3])


class TestUtcNow(unittest.TestCase):
    """utc_now returns an aware UTC datetime."""

    def test_returns_utc_aware_datetime(self):
        dt = utils.utc_now()
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_is_recent(self):
        from datetime import datetime
        dt = utils.utc_now()
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        self.assertLess(abs(age), 5)


class TestTodayIso(unittest.TestCase):
    """today_iso returns today's UTC date in YYYY-MM-DD format."""

    def test_matches_iso_format(self):
        s = utils.today_iso()
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}$")

    def test_matches_utc_date(self):
        from datetime import datetime
        expected = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(utils.today_iso(), expected)


if __name__ == "__main__":
    unittest.main()
