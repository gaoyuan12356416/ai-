import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from features.social_analytics.adapters import CatalogErrors, normalized
from features.social_analytics.backend_scope import restrict
from features.social_analytics.report import parameters, summarize
from features.social_daily_report import billing
from features.social_daily_report.collector import collect, present
from features.social_daily_report.report import build_cards, require_complete

DAY = "2026-10-25"


def link(platform="x", cid="one", owner="a", *, site=None, manual=False, published=True):
    site = site or {"x": "2116", "youtube": "2284", "facebook": "2049"}[platform]
    return normalized(platform, dict(
        key=(cid, "name is not attribution"), link_id=cid, site_id=site, owner=owner,
        owner_label=owner, tenant="tenant", channel="page", drama="drama", language="en",
        date=DAY, excluded=False, link_type="manual" if manual else "auto",
        effect_source=dict(adset_id="page", ad_id="material"),
        publications=[dict(id=platform + site + cid, date=DAY, kind="post")] if published else []))


def bill(number=1, platform="x", cid="one", cents=100, user="payer", **updates):
    value = dict(id=number, site_id={"x": "2116", "youtube": "2284", "facebook": "2049"}[platform],
                 campaign_id=cid, adset_id="page", ad_id="material", event_name="af_purchase",
                 event_time=billing.window(DAY)[0], revenue_cents=cents,
                 user_key=hashlib.sha256(user.encode()).hexdigest())
    value.update(updates)
    return value


def baseline(catalog):
    values = [dict(platform=row["platform"], site_id=row["site_id"], date=DAY,
                   campaign_id=row["key"][0], adset_id="page", ad_id="material",
                   revenue_cents=0, conversions=0, refund_cents=17, clicks=10, views=20, installs=3)
              for row in catalog]
    selected, values, coverage, audit = restrict(catalog, values, DAY)
    # A source can have real traffic yet no eligible publications.
    for site, platform in billing.SITES.items():
        coverage[platform + ":" + site] = {DAY}
    shared = summarize(selected, values, {"_system_report": True}, parameters({
        "start": [DAY], "end": [DAY], "group_by": ["owner,platform"], "page_size": ["100"]}),
        "snapshot", metric_coverage=coverage)
    shared["meta"]["content_scope"] = "ai_backend_confirmed_publications_and_manual_shortlinks"
    shared["scope_audit"] = audit
    return present(shared, DAY)


class DailyBillingTests(unittest.TestCase):
    def test_stale_insight_money_replaced_without_changing_other_metrics_or_layout(self):
        catalog = [link(), link("youtube", "yt", "b", manual=True, published=False)]
        report = baseline(catalog)
        before = copy.deepcopy(report)
        billing.apply(report, billing.aggregate(catalog, [bill(cents=1234),
            bill(2, "youtube", "yt", cents=777, user="second")], DAY))
        require_complete(report)
        self.assertEqual(report["totals"]["revenue_cents"], 2011)
        self.assertEqual(report["totals"]["conversions"], 2)
        for field in ("refund_cents", "installs", "clicks", "views", "published_count", "post_count", "repost_count"):
            self.assertEqual(before["totals"][field], report["totals"][field], field)
            for old, new in zip(before["rows"], report["rows"]):
                self.assertEqual(old[field], new[field], field)
        self.assertEqual(report["shared_report"]["totals"]["revenue_cents"], 2011)
        self.assertEqual(report["shared_report"]["trend"][0]["conversions"], 2)
        old_card, new_card = build_cards(before, "chat"), build_cards(report, "chat")
        self.assertEqual(old_card[0]["header"], new_card[0]["header"])
        self.assertEqual([e["tag"] for e in old_card[0]["elements"]],
                         [e["tag"] for e in new_card[0]["elements"]])
        self.assertIn("$20.11", json.dumps(new_card))

    def test_payers_are_deduplicated_across_orders_campaigns_channels_and_owners(self):
        catalog = [link(), link("youtube", "yt", "a"), link("facebook", "fb", "b")]
        report = baseline(catalog)
        data = [bill(), bill(2), bill(3, "youtube", "yt"), bill(4, "facebook", "fb")]
        billing.apply(report, billing.aggregate(catalog, data, DAY))
        self.assertEqual(report["totals"]["revenue_cents"], 400)
        self.assertEqual(report["totals"]["conversions"], 1)
        people = {row["owner"]: row for row in report["rows"]}
        self.assertEqual(people["a"]["conversions"], 1)
        self.assertEqual(people["b"]["conversions"], 1)
        self.assertEqual(people["a"]["platforms"]["x"]["conversions"], 1)
        self.assertEqual(people["a"]["platforms"]["youtube"]["conversions"], 1)
        self.assertEqual(report["quality"]["billing"]["included"]["payment_orders"], 4)

    def test_facebook_legacy_collision_is_excluded_even_when_campaign_matches(self):
        catalog = [link("facebook", "6")]
        result = billing.aggregate(catalog, [bill(platform="facebook", cid="6", cents=30588),
            bill(2, "facebook", "6", cents=900000, adset_id="legacy-page")], DAY)
        self.assertEqual(result["total"].revenue_cents, 30588)
        self.assertEqual(sum(x.revenue_cents for x in result["outside"].values()), 900000)

    def test_manual_youtube_kept_and_unpublished_or_future_posts_excluded(self):
        manual = link("youtube", "yt", manual=True, published=False)
        failed = link("x", "failed", published=False)
        future = link("facebook", "future")
        future["publications"][0]["date"] = "2026-10-26"
        result = billing.aggregate([manual, failed, future], [
            bill(platform="youtube", cid="yt", cents=500), bill(2, cid="failed", cents=700),
            bill(3, "facebook", "future", cents=900)], DAY)
        self.assertEqual(result["total"].revenue_cents, 500)

    def test_global_campaign_conflict_and_missing_id_are_not_guessed(self):
        catalog = [link(), link(owner="other")]
        result = billing.aggregate(catalog, [bill(), bill(2, cid="", cents=200)], DAY)
        self.assertEqual(result["total"].revenue_cents, 0)
        self.assertEqual(sum(x.revenue_cents for x in result["outside"].values()), 300)

    def test_youtube_full_id_and_unique_storage_alias_share_frozen_identity(self):
        cid = "yt_" + "x" * 40
        catalog = [link("youtube", cid)]
        result = billing.aggregate(catalog, [bill(platform="youtube", cid=cid),
            bill(2, "youtube", cid[:32])], DAY)
        self.assertEqual(result["total"].revenue_cents, 200)
        self.assertEqual(len(result["total"].users), 1)

    def test_utc_boundary_and_sql_gate_contract(self):
        with patch.object(billing, "query_source_rows", return_value=[]) as query:
            billing.read_bills({"safe": True}, DAY)
        sql = query.call_args.args[1]
        for value in ("idx_site_event_name_time", "event_name='af_purchase'",
                      "event_time >= 1792886400", "event_time < 1792972800", "SHA2(user_id,256)"):
            self.assertIn(value, sql)
        self.assertEqual(query.call_args.args[2], 50000)
        for at in (1792886400, 1792972799):
            self.assertEqual(billing.aggregate([link()], [bill(event_time=at)], DAY)["total"].orders, 1)
        for at in (1792886399, 1792972800):
            with self.assertRaises(ValueError):
                billing.aggregate([link()], [bill(event_time=at)], DAY)

    def test_invalid_and_duplicate_financial_records_are_rejected(self):
        for data in ([], [bill(user_key=None)], [bill(), bill()],
                     [bill(revenue_cents=None)], [bill(revenue_cents=1.5)],
                     [bill(event_name="refund")], [bill(site_id="unexpected")]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                billing.aggregate([link()], data, DAY)

    def test_real_outside_payments_can_establish_zero_backend_income(self):
        report = baseline([link()])
        billing.apply(report, billing.aggregate([link()], [bill(cid="outside")], DAY))
        require_complete(report)
        self.assertEqual(report["totals"]["revenue_cents"], 0)

    def test_read_failure_or_empty_feed_blocks_performance_card_and_preserves_other_fields(self):
        for response in (RuntimeError("sensitive-driver-message"), []):
            report = baseline([link()])
            original_refunds = report["totals"]["refund_cents"]
            kwargs = dict(side_effect=response) if isinstance(response, Exception) else dict(return_value=response)
            with patch.object(billing, "read_bills", **kwargs):
                billing.collect_financials(report, {}, [link()])
            self.assertIsNone(report["totals"]["revenue_cents"])
            self.assertIsNone(report["totals"]["conversions"])
            self.assertEqual(report["totals"]["refund_cents"], original_refunds)
            with self.assertRaises(ValueError):
                require_complete(report)
            card = json.dumps(build_cards(report, "chat"), ensure_ascii=False)
            self.assertIn("数据采集未完成", card)
            self.assertNotIn("sensitive-driver-message", card)
            self.assertNotIn("$0.00", card)

    def test_collector_uses_one_catalog_snapshot_for_both_sources(self):
        catalog = [link()]
        snapshot = (catalog, CatalogErrors())
        shared = baseline(catalog)["shared_report"]
        with patch("features.social_daily_report.collector.read_settings", return_value={}), \
             patch("features.social_analytics.adapters.read_catalogs", return_value=snapshot) as read, \
             patch("features.social_analytics.routes.build_report", return_value=shared) as build, \
             patch.object(billing, "read_bills", return_value=[bill(cents=215988)]):
            result = collect(DAY)
        read.assert_called_once()
        self.assertIs(build.call_args.kwargs["catalog_snapshot"], snapshot)
        self.assertEqual(result["totals"]["revenue_cents"], 215988)


if __name__ == "__main__":
    unittest.main()
