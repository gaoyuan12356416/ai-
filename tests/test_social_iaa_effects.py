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

    def test_late_iaa_only_defers_the_affected_revenue_comparison(self):
        from features.socialkit_gap_alert.collector import compare
        from features.socialkit_gap_alert.digest import ad_amount
        def candidate(drama, platform, complete, ad):
            return dict(platform=platform, drama=drama, drama_app_id=1479,
                        revenue_cents=2400, iaa_cents=ad, iaa_complete=complete)
        own = {"facebook": dict(amounts={}, iaa=None, iaa_complete=False, history={}, blocked=set()),
               "x": dict(amounts={}, iaa={}, iaa_complete=True, history={}, blocked=set())}
        candidates = [candidate("late", "facebook", True, 20), candidate("ready", "x", True, 30),
                      candidate("cash-only", "x", False, None)]
        events, pending = compare(candidates, own, "2026-11-24")
        result = {e["drama"]: e for e in events}
        self.assertEqual(result["late"]["reasons"], ["no_confirmed_publication"])
        self.assertEqual(result["cash-only"]["reasons"], ["no_confirmed_publication"])
        self.assertIn("large_revenue_gap", result["ready"]["reasons"])
        self.assertEqual({p["drama"] for p in pending}, {"late", "cash-only"})
        self.assertEqual(ad_amount([result["late"]], "own"), "待回传")
        self.assertEqual(ad_amount([result["ready"]], "own"), "$0.00")
        self.assertIn("部分已回传", ad_amount(events, "socialkit"))
