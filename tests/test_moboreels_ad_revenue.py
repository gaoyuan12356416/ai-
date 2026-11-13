"""Financial units, source dates and the documented read-only contract."""

from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.moboreels_library.ad_revenue import (
    AdRevenueError, get_ad_revenue_page, normalize_ad_revenue,
)


def row(**changes):
    result = {"ad_time": "2026-11-12", "project_type": 2, "code": "SAMPLE", "amount": "100.0000"}
    result.update(changes)
    return result


def provider_for(body, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = body.encode("utf-8")
    return mock.Mock(_post=mock.Mock(return_value=response))


class AdRevenueTests(unittest.TestCase):
    def test_fractional_cents_are_not_truncated_or_treated_as_dollars(self):
        normalized = normalize_ad_revenue(row(amount="0.0001"), "2026-11-12")
        self.assertEqual(normalized["amount_cents"], "0.0001")
        self.assertEqual(normalized["amount_usd"], "0.000001")
        self.assertEqual(normalized["ad_time"], "2026-11-12")
        self.assertEqual(normalized["code"], "SAMPLE")
        self.assertNotIn("promotion_id", normalized)

    def test_large_decimal_amount_keeps_every_source_digit(self):
        cents = "123456789012345678901234567890.1234"
        normalized = normalize_ad_revenue(row(amount=cents))
        self.assertEqual(normalized["amount_cents"], cents)
        self.assertEqual(normalized["amount_usd"], "1234567890123456789012345678.901234")

    def test_zero_and_negative_adjustments_are_preserved(self):
        for cents, dollars in (("0", "0"), ("-12.34", "-0.1234")):
            with self.subTest(cents=cents):
                self.assertEqual(normalize_ad_revenue(row(amount=cents))["amount_usd"], dollars)

    def test_invalid_money_cannot_become_zero_income(self):
        for amount in (None, "", "NaN", "Infinity", "-Infinity", True, {}, "three"):
            with self.subTest(amount=amount), self.assertRaises(AdRevenueError):
                normalize_ad_revenue(row(amount=amount))

    def test_wrong_date_project_or_missing_code_is_rejected(self):
        for changes in ({"ad_time": "2026-11-13"}, {"ad_time": "20261112"},
                        {"ad_time": "2026-02-30"}, {"project_type": 1},
                        {"project_type": True}, {"code": ""}, {"code": None}):
            with self.subTest(changes=changes), self.assertRaises(AdRevenueError):
                normalize_ad_revenue(row(**changes), "2026-11-12")

    def test_raw_fields_are_retained_without_mutating_input(self):
        raw = row(amount=Decimal("100.0000"), extra={"value": "original"})
        normalized = normalize_ad_revenue(raw)
        self.assertEqual(normalized["raw_json"]["amount"], "100.0000")
        json.dumps(normalized)
        normalized["raw_json"]["extra"]["value"] = "changed"
        self.assertEqual(raw["extra"]["value"], "original")

    def test_exact_documented_request_preserves_json_decimal_tokens(self):
        provider = provider_for('{"code":200,"data":{"total":1,"list":['
                                '{"ad_time":"2026-11-12","project_type":2,"code":"A",'
                                '"amount":12345678901234567890.012345}]}}')
        data = get_ad_revenue_page(provider, "2026-11-12", 1)
        provider._post.assert_called_once_with("/api/story/getadlist", {
            "date": "2026-11-12", "project_type": 2, "page_index": 1, "page_size": 100,
        })
        self.assertEqual(data["list"][0]["amount"], "12345678901234567890.012345")
        self.assertEqual(normalize_ad_revenue(data["list"][0])["amount_usd"],
                         "123456789012345678.90012345")

    def test_successful_empty_day_is_distinct_from_failure(self):
        provider = provider_for('{"code":200,"data":{"total":0,"list":[]}}')
        self.assertEqual(get_ad_revenue_page(provider, "2026-11-12", 1), {"total": 0, "list": []})
        for body, status in (("not-json", 200), ('{"code":500,"message":"SECRET"}', 200),
                             ('{"code":200,"data":{"total":0,"list":[]}}', 503)):
            with self.subTest(body=body), self.assertRaises(AdRevenueError) as caught:
                get_ad_revenue_page(provider_for(body, status), "2026-11-12", 1)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_invalid_pagination_does_not_call_provider(self):
        provider = mock.Mock()
        for number, size in ((0, 100), (True, 100), (1, 101), (1, 0), (1, True)):
            with self.subTest(number=number, size=size), self.assertRaises(AdRevenueError):
                get_ad_revenue_page(provider, "2026-11-12", number, size)
        provider._post.assert_not_called()

    def test_malformed_page_is_not_reported_as_empty(self):
        for data in ({"total": True, "list": []}, {"total": -1, "list": []},
                     {"total": 0, "list": [row()]}, {"total": 1, "list": [None]},
                     {"total": 1, "list": None}):
            with self.subTest(data=data), self.assertRaises(AdRevenueError):
                get_ad_revenue_page(provider_for(json.dumps({"code": 200, "data": data})), "2026-11-12", 1)

    def test_transport_failure_is_safe_and_not_retried(self):
        provider = mock.Mock(_post=mock.Mock(side_effect=requests.Timeout("SECRET")))
        with self.assertRaises(AdRevenueError) as caught:
            get_ad_revenue_page(provider, "2026-11-12", 1)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(provider._post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
