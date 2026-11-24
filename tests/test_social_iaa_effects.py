import unittest

from features.social_analytics.bill_income import merge_settled


class BillIncomeTest(unittest.TestCase):
    def test_settlement_replaces_money_and_includes_ad_only_identities(self):
        insight = dict(campaign_id="a", revenue_cents=180, clicks=7)
        bills = [dict(date="2026-11-23", site_id="2116", campaign_id=key, platform="x",
                      iap_cents=iap, iaa_cents=iaa, ad_events=1)
                 for key, iap, iaa in (("a", 100, 80), ("b", 0, 15))]
        rows, covered = merge_settled([insight], bills, ("campaign_id",), dict(clicks=0, revenue_cents=0))
        self.assertTrue(covered)
        self.assertEqual([(r["clicks"], r["revenue_cents"], r["iaa_cents"]) for r in rows],
                         [(7, 180, 80), (0, 15, 15)])
        self.assertEqual(merge_settled([insight], [], ("campaign_id",), {}), ([insight], False))
