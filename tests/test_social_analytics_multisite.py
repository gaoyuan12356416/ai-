import csv
import io
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from features.social_analytics import adapters, report, routes


DAY = "2026-10-19"
SYSTEM = {"role": "admin", "_system_report": True}


def entry(site="2049", campaign="same", owner="alice", **changes):
    value = dict(site_id=site, key=(campaign, "frozen name"), link_id=campaign, tenant="tenant",
                 owner=owner, owner_label=owner, drama="42", drama_label="Frozen drama",
                 channel="page", channel_label="Frozen Page", language="en", language_label="en",
                 link_type="auto_template" if site == "2049" else "manual", link_type_label="发布",
                 date=DAY, publications=[], excluded=False, sub_user_id="248")
    value.update(changes)
    return adapters.normalized("facebook", value)


def metric(site="2049", campaign="same", cents=100, **changes):
    value = dict(platform="facebook", site_id=site, campaign_id=campaign, campaign="renamed",
                 date=DAY, revenue_cents=cents, refund_cents=4, clicks=8, views=10,
                 installs=2, conversions=1)
    value.update(changes)
    return value


def build(catalog, metrics, actor=SYSTEM, **kwargs):
    query = kwargs.pop("query", {})
    params = report.parameters({"start": [DAY], "end": [DAY], "platform": ["facebook"], **query})
    return report.summarize(catalog, metrics, actor, params, "snapshot", **kwargs)


class MultisiteAttributionTests(unittest.TestCase):
    def test_same_id_is_separated_by_site_before_one_facebook_rollup(self):
        result = build([entry(), entry("2310", owner="bob")], [metric(cents=123), metric("2310", cents=456)])
        self.assertEqual({row["owner"]: row["revenue_cents"] for row in result["all_rows"]},
                         {"alice": 123, "bob": 456})
        self.assertEqual(result["platform_totals"]["facebook"]["revenue_cents"], 579)
        self.assertEqual(set(result["platform_totals"]), {"facebook"})
        self.assertTrue(result["quality"]["complete"])
        for site, expected in (("2049", 123), ("2310", 456)):
            quality = result["quality"]["platforms"]["facebook"]["sources"][site]
            self.assertEqual(quality["source_totals"]["revenue_cents"], expected)
            self.assertEqual(quality["source_key"], "facebook:" + site)

    def test_link_namespace_separates_sources_but_page_group_is_shared(self):
        automatic, manual = entry(), entry("2310")
        self.assertNotEqual(automatic["link_id"], manual["link_id"])
        self.assertEqual(automatic["channel"], manual["channel"])
        result = build([automatic, manual], [metric(), metric("2310")], query={"group_by": ["channel"]})
        self.assertEqual(len(result["all_rows"]), 1)
        self.assertEqual(result["totals"]["links"], 2)

    def test_site_failure_keeps_other_site_amount_and_marks_partial(self):
        result = build([entry(), entry("2310")], [metric(cents=345), metric("2310", cents=999)],
                       errors={"facebook:2310": "效果暂不可用"})
        self.assertEqual(result["totals"]["revenue_cents"], 345)
        quality = result["quality"]["platforms"]["facebook"]
        self.assertEqual(quality["state"], "partial")
        self.assertEqual(quality["sources"]["2049"]["state"], "complete")
        self.assertIsNone(quality["sources"]["2310"]["source_totals"]["revenue_cents"])

    def test_catalog_failure_preserves_other_catalog_and_raw_source_reconciliation(self):
        result = build([entry(), entry("2310")], [metric(), metric("2310", cents=300)],
                       catalog_errors={"facebook:2310": "归属账本不可用"})
        self.assertEqual(result["totals"]["revenue_cents"], 100)
        quality = result["quality"]["platforms"]["facebook"]
        self.assertEqual(quality["source_totals"]["revenue_cents"], 400)
        self.assertEqual(quality["unmatched_totals"]["revenue_cents"], 300)
        self.assertEqual(quality["state"], "partial")
        self.assertIsNone(quality["sources"]["2310"]["totals"]["revenue_cents"])

    def test_empty_manual_source_is_unsynced_even_if_collector_is_fresh(self):
        result = build([entry()], [metric()], source_metadata={
            "facebook:2049": {"state": "complete", "freshness": "fresh"},
            "facebook:2310": {"state": "complete", "freshness": "fresh"},
        })
        quality = result["quality"]["platforms"]["facebook"]
        self.assertFalse(result["quality"]["complete"])
        self.assertEqual(quality["sources"]["2310"]["missing_dates"], [DAY])
        self.assertIsNone(quality["sources"]["2310"]["source_totals"]["revenue"])
        self.assertEqual(quality["sources"]["2310"]["state"], "unavailable")

    def test_owner_in_only_unsynced_source_does_not_borrow_other_owners_source(self):
        result = build([entry(), entry("2310", owner="bob", date="2026-10-20")], [metric()])
        people = {row["owner"]: row for row in result["all_rows"]}
        self.assertEqual(people["alice"]["revenue_cents"], 100)
        self.assertEqual(people["alice"]["data_status"], "complete")
        self.assertIsNone(people["bob"]["revenue_cents"])
        self.assertEqual(people["bob"]["data_status"], "unavailable")
        self.assertEqual(people["bob"]["source_keys"], ["facebook:2310"])
        self.assertEqual(result["platform_totals"]["facebook"]["data_status"], "partial")
        self.assertEqual(result["totals"]["data_status"], "partial")
        exported = list(csv.DictReader(io.StringIO(report.csv_export(result).decode("utf-8-sig"))))
        self.assertEqual(next(row for row in exported if row["owner ID"] == "bob")["收入（USD）"], "未同步")

    def test_owner_with_both_sites_keeps_known_partial_total(self):
        result = build([entry(), entry("2310", date="2026-10-20")], [metric()])
        person = result["all_rows"][0]
        self.assertEqual(person["revenue_cents"], 100)
        self.assertEqual(person["source_keys"], ["facebook:2049", "facebook:2310"])
        self.assertEqual(person["data_status"], "partial")

    def test_stale_collector_preserves_frozen_money_but_invalidates_completeness(self):
        result = build([entry(), entry("2310")], [metric(), metric("2310")], source_metadata={
            "facebook:2049": {"state": "partial", "freshness": "stale", "age_seconds": 900,
                              "last_success_at": DAY + "T01:00:00Z", "private_path": "DO NOT EXPOSE"},
        })
        self.assertEqual(result["totals"]["revenue_cents"], 200)
        quality = result["quality"]["platforms"]["facebook"]
        self.assertEqual(quality["sources"]["2049"]["state"], "partial")
        self.assertEqual(quality["sources"]["2049"]["collector"]["age_seconds"], 900)
        self.assertNotIn("private_path", quality["sources"]["2049"]["collector"])
        self.assertTrue(any("采集" in text for text in result["warnings"]))

    def test_per_source_conservation_and_conflict_resolution_precede_owner_filter(self):
        values = [entry(), entry(owner="bob"), entry("2310", campaign="ok"),
                  entry("2310", campaign="excluded", excluded=True)]
        metrics = [metric(cents=100), metric("2310", "ok", 200),
                   metric("2310", "excluded", 300), metric("2310", "unmatched", 400)]
        result = build(values, metrics, query={"owner": ["alice"]})
        self.assertEqual(result["totals"]["revenue_cents"], 200)
        for quality in result["quality"]["platforms"]["facebook"]["sources"].values():
            for field in report.FIELDS:
                self.assertEqual(quality["source_totals"][field],
                                 sum(quality[bucket + "_totals"][field]
                                     for bucket in ("attributed", "unmatched", "excluded")))

    def test_facebook_has_no_prefix_alias_and_names_never_resolve(self):
        result = build([entry(campaign="a" * 33)], [metric(campaign="a" * 32)])
        self.assertEqual(result["totals"]["revenue_cents"], 0)
        self.assertEqual(result["quality"]["unmatched_totals"]["revenue_cents"], 100)
        self.assertEqual(result["quality"]["corrected_campaigns"], 0)

    def test_tenant_and_owner_acl_apply_to_both_sources(self):
        actor = {"tenant_key": "tenant", "user_id": "alice", "role": "user"}
        result = build([entry(), entry("2310", owner="bob"), entry("2310", "foreign", tenant="other")],
                       [metric(), metric("2310", cents=200), metric("2310", "foreign", 900)], actor)
        self.assertEqual(result["totals"]["revenue_cents"], 100)
        self.assertEqual([row["owner"] for row in result["all_rows"]], ["alice"])
        self.assertEqual(result["quality"]["platforms"]["facebook"]["source_totals"]["revenue_cents"], 0)

    def test_publications_do_not_increment_x_counts_and_csv_marks_not_applicable(self):
        values = [entry(publications=[{"id": "facebook:page:post", "date": DAY, "kind": "post"}]),
                  entry("2310", publications=[{"id": "facebook:page:another", "date": DAY, "kind": "video"}])]
        result = build(values, [metric(), metric("2310")])
        self.assertEqual(result["totals"]["published_count"], 2)
        self.assertIsNone(result["totals"]["post_count"])
        self.assertIsNone(result["totals"]["repost_count"])
        exported = list(csv.DictReader(io.StringIO(report.csv_export(result).decode("utf-8-sig"))))
        self.assertEqual(exported[0]["X Post 数"], "不适用")
        self.assertEqual(exported[0]["X Repost 数"], "不适用")
        self.assertEqual(result["metric_applicability"]["post_count"], ["x"])
        self.assertEqual(report.options(values, SYSTEM)["platform_metadata"]["facebook"]["label"], "Facebook")

    def test_duplicate_confirmed_object_across_sites_is_not_counted_twice(self):
        fact = {"id": "facebook:page:post", "date": DAY, "kind": "video"}
        result = build([entry(publications=[fact]), entry("2310", publications=[fact])],
                       [metric(), metric("2310")])
        self.assertEqual(result["totals"]["published_count"], 0)
        self.assertEqual(result["quality"]["publication_conflicts"], 1)

    def test_inapplicable_x_metric_has_no_facebook_ranking(self):
        result = build([entry(), entry("2310")], [metric(), metric("2310")],
                       query={"ranking_metric": ["post_count"]})
        self.assertEqual(result["ranking"], [])


class MultisiteQueryTests(unittest.TestCase):
    def tearDown(self):
        routes._cache.clear()

    def test_site_is_explicit_and_registry_allowlisted(self):
        for site in ("2049", "2310"):
            sql = routes.metric_sql(DAY, DAY, "facebook", site)
            self.assertIn("site_id='" + site + "'", sql)
            self.assertIn("'site_id','" + site + "'", sql)
            self.assertIn("GROUP BY dt,BINARY campaign_id", sql)
        for site in (None, "2284", "2049' OR 1=1"):
            with self.assertRaises(report.ReportError):
                routes.metric_sql(DAY, DAY, "facebook", site)

    def test_validator_does_not_merge_ids_across_sites_or_accept_wrong_site(self):
        report.validate_metrics([metric(), metric("2310")], "facebook", {DAY})
        for rows, expected_site in (([metric(), metric()], None), ([metric("2310")], "2049"),
                                    ([metric(site="2116")], None)):
            with self.assertRaises(report.ReportError):
                report.validate_metrics(rows, "facebook", {DAY}, site_id=expected_site)

    def test_source_cache_key_includes_site(self):
        routes._cache.clear()
        with patch.object(routes, "query_rows", side_effect=[[metric()], [metric("2310", cents=999)]]) as query:
            automatic, _ = routes.source({}, DAY, DAY, "facebook", "2049")
            manual, _ = routes.source({}, DAY, DAY, "facebook", "2310")
            again, _ = routes.source({}, DAY, DAY, "facebook", "2049")
        self.assertEqual(query.call_count, 2)
        self.assertEqual(automatic, again)
        self.assertEqual(manual[0]["revenue_cents"], 999)

    def test_real_build_report_keeps_single_source_failure(self):
        def source(app, start, end, platform, site):
            if site == "2310":
                raise report.ReportError("手动站点未同步", 503)
            return [metric()], "snapshot"
        with patch.object(routes, "read_catalogs", return_value=([entry()], adapters.CatalogErrors())), \
                patch.object(routes, "source", side_effect=source):
            result = routes.build_report({}, SYSTEM, {"start": [DAY], "end": [DAY], "platform": ["facebook"]})
        self.assertEqual(result["totals"]["revenue_cents"], 100)
        self.assertEqual(result["quality"]["platforms"]["facebook"]["sources"]["2310"]["error"], "手动站点未同步")

    def test_options_exposes_safe_source_health_and_legacy_remains_youtube_only(self):
        class Handler:
            command = "GET"
            def _social_analytics_actor(self):
                return {"tenant_key": "tenant", "user_id": "alice", "role": "admin"}
        failures = adapters.CatalogErrors()
        failures.source_metadata = {"facebook:2049": {"state": "partial", "freshness": "stale",
                                                      "private_context": "hidden"}}
        replies = []
        with patch.object(routes, "read_catalogs", return_value=([entry()], failures)):
            routes.dispatch(Handler(), urlsplit("/api/social-analytics/options"),
                            {"json_response": lambda handler, code, value, **kw: replies.append(value)})
        quality = replies[0]["quality"]["platforms"]["facebook"]
        self.assertEqual(quality["state"], "partial")
        self.assertNotIn("private_context", quality["sources"]["2049"]["collector"])
        with patch.object(routes, "read_catalogs", return_value=([], {})) as read:
            routes.dispatch(Handler(), urlsplit("/api/youtube-analytics/options"),
                            {"json_response": lambda handler, code, value, **kw: replies.append(value)}, legacy=True)
        self.assertEqual(read.call_args.args[1], ["youtube"])
        self.assertEqual(replies[-1]["options"]["platform"], [{"value": "youtube", "label": "YouTube"}])


if __name__ == "__main__":
    unittest.main()
