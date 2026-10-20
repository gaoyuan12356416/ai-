import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from features.social_analytics import adapters, report, routes


DAY = "2026-10-19"
ACTOR = {"tenant_key": "t", "user_id": "admin", "role": "admin"}
SYSTEM = {"role": "admin", "_system_report": True}


def catalog(platform="youtube", campaign="campaign", owner="alice", **changes):
    value = {
        "key": (campaign, "saved name"), "link_id": campaign, "tenant": "t", "owner": owner,
        "owner_label": owner, "channel": "account", "channel_label": "Account", "drama": "42",
        "drama_label": "A drama", "language": "en", "language_label": "en",
        "link_type": "auto" if platform == "youtube" else "system", "link_type_label": "自动发布",
        "date": DAY, "excluded": False, "publications": [],
    }
    value.update(changes)
    return adapters.normalized(platform, value)


def metric(platform="youtube", campaign="campaign", cents=100, **changes):
    value = dict(date=DAY, platform=platform, campaign_id=campaign, campaign="different current name",
                 clicks=4, views=5, installs=2, conversions=1, revenue_cents=cents, refund_cents=3)
    value.update(changes)
    return value


def params(**changes):
    query = {"start": [DAY], "end": [DAY]}
    query.update(changes)
    return report.parameters(query)


def summarize(catalogs, metrics, actor=SYSTEM, **query):
    query.setdefault("platform", ["youtube", "x"])
    return report.summarize(catalogs, metrics, actor, params(**query), "snapshot")


class AttributionTests(unittest.TestCase):
    def test_campaign_name_does_not_affect_join_and_channels_are_isolated(self):
        result = summarize([catalog(), catalog("x")], [metric(cents=123), metric("x", cents=456)])
        self.assertEqual(result["totals"]["revenue_cents"], 579)
        self.assertEqual(result["platform_totals"]["youtube"]["revenue_cents"], 123)
        self.assertEqual(result["platform_totals"]["x"]["revenue_cents"], 456)
        self.assertTrue(result["quality"]["complete"])

    def test_different_case_and_unrelated_campaign_are_unmatched(self):
        result = summarize([catalog(campaign="ABC")], [metric(campaign="abc"), metric(campaign="another")])
        self.assertEqual(result["totals"]["revenue_cents"], 0)
        self.assertEqual(result["quality"]["unmatched_totals"]["revenue_cents"], 200)

    def test_one_to_many_same_identity_does_not_multiply_effect(self):
        original = catalog()
        duplicate = dict(original, link_id="youtube:another-link")
        result = summarize([original, duplicate], [metric()], platform=["youtube"])
        self.assertEqual(result["totals"]["revenue_cents"], 100)
        self.assertEqual(result["totals"]["links"], 2)

    def test_owner_filter_cannot_resolve_conflicting_campaign(self):
        result = summarize([catalog(owner="alice"), catalog(owner="bob")], [metric()],
                           owner=["alice"], platform=["youtube"])
        self.assertEqual(result["totals"]["revenue_cents"], 0)
        self.assertEqual(result["quality"]["unmatched_totals"]["revenue_cents"], 100)
        self.assertEqual(result["all_rows"], [])

    def test_youtube_known_alias_only_and_collision(self):
        campaign = "a" * 32 + "-tail"
        result = summarize([catalog(campaign=campaign)], [metric(campaign="a" * 32)], platform=["youtube"])
        self.assertEqual(result["totals"]["revenue_cents"], 100)
        self.assertEqual(result["quality"]["corrected_campaigns"], 1)
        collision = summarize([catalog(campaign=campaign), catalog(campaign="a" * 32 + "-other")],
                              [metric(campaign="a" * 32)], platform=["youtube"])
        self.assertEqual(collision["quality"]["unmatched_totals"]["revenue_cents"], 100)
        arbitrary = summarize([catalog(campaign=campaign)], [metric(campaign="a" * 31)], platform=["youtube"])
        self.assertEqual(arbitrary["quality"]["unmatched_campaigns"], 1)
        x = summarize([catalog("x", campaign)], [metric("x", "a" * 32)], platform=["x"])
        self.assertEqual(x["quality"]["unmatched_campaigns"], 1)

    def test_full_id_colliding_with_alias_is_also_unresolved(self):
        result = summarize([catalog(campaign="b" * 32), catalog(campaign="b" * 33)],
                           [metric(campaign="b" * 32)], platform=["youtube"])
        self.assertEqual(result["quality"]["unmatched_campaigns"], 1)

    def test_unknown_excluded_and_attributed_reconcile_without_filters(self):
        result = summarize([catalog(), catalog(campaign="canary", excluded=True)],
                           [metric(cents=10), metric(campaign="canary", cents=20), metric(campaign="unknown", cents=30)],
                           platform=["youtube"], owner=["nobody"])
        q = result["quality"]["platforms"]["youtube"]
        self.assertEqual(q["source_totals"]["revenue_cents"], 60)
        self.assertEqual(q["attributed_totals"]["revenue_cents"], 10)
        self.assertEqual(q["excluded_totals"]["revenue_cents"], 20)
        self.assertEqual(q["unmatched_totals"]["revenue_cents"], 30)
        self.assertEqual(result["totals"]["revenue_cents"], 0)

    def test_tenant_and_owner_acl_hide_foreign_and_unknown_money(self):
        own = {"tenant_key": "t", "user_id": "alice", "role": "user"}
        values = [catalog(), catalog(campaign="bob", owner="bob"),
                  catalog(campaign="foreign", tenant="other")]
        metrics = [metric(), metric(campaign="bob", cents=200),
                   metric(campaign="foreign", cents=900), metric(campaign="unmatched", cents=800)]
        own_report = summarize(values, metrics, own, platform=["youtube"])
        self.assertEqual(own_report["totals"]["revenue_cents"], 100)
        self.assertEqual(own_report["quality"]["unmatched_totals"]["revenue_cents"], 0)
        self.assertEqual(own_report["quality"]["platforms"]["youtube"]["source_totals"]["revenue_cents"], 0)
        admin_report = summarize(values, metrics, ACTOR, platform=["youtube"])
        self.assertEqual(admin_report["totals"]["revenue_cents"], 300)
        self.assertEqual(admin_report["quality"]["platforms"]["youtube"]["source_totals"]["revenue_cents"], 300)
        options = report.options(values, own)["options"]
        self.assertEqual([row["value"] for row in options["owner"]], ["alice"])

    def test_cross_tenant_conflict_cannot_be_filtered_away(self):
        result = summarize([catalog(), catalog(tenant="other")], [metric()], ACTOR, platform=["youtube"])
        self.assertEqual(result["totals"]["revenue_cents"], 0)
        self.assertEqual(result["quality"]["unmatched_totals"]["revenue_cents"], 0)

    def test_system_owner_group_keeps_tenants_separate(self):
        result = summarize([catalog(), catalog(campaign="foreign", tenant="other")],
                           [metric(), metric(campaign="foreign")], platform=["youtube"])
        self.assertEqual(len(result["all_rows"]), 2)
        self.assertEqual({row["owner_tenant"] for row in result["all_rows"]}, {"t", "other"})

    def test_missing_channel_is_null_and_combined_total_is_partial(self):
        result = summarize([catalog(), catalog("x")], [metric()])
        self.assertEqual(result["totals"]["revenue_cents"], 100)
        self.assertFalse(result["quality"]["complete"])
        self.assertIsNone(result["platform_totals"]["x"]["revenue"])
        self.assertEqual(result["platform_totals"]["x"]["published_count"], 0)
        self.assertEqual(result["platform_totals"]["x"]["data_status"], "unavailable")
        self.assertEqual(result["trend"][0]["data_status"], "partial")

    def test_missing_days_do_not_erase_available_days_or_become_zero(self):
        result = summarize([catalog()], [metric()], platform=["youtube"], end=["2026-10-20"])
        self.assertEqual(result["totals"]["revenue_cents"], 100)
        self.assertIsNone(result["trend"][1]["revenue"])
        self.assertEqual(result["quality"]["missing_dates"], ["2026-10-20"])

    def test_publications_dedupe_and_manual_link_never_inflates_count(self):
        fact = {"id": "youtube:video", "date": DAY, "kind": "video"}
        first = catalog(publications=[fact])
        duplicate = dict(first, link_id="youtube:another")
        manual = catalog(campaign="manual", link_type="manual")
        result = summarize([first, duplicate, manual], [metric(campaign="manual")], platform=["youtube"])
        self.assertEqual(result["totals"]["published_count"], 1)
        self.assertIsNone(result["totals"]["post_count"])
        self.assertEqual(result["totals"]["revenue_cents"], 100)

    def test_conflicting_publication_identity_does_not_pick_first(self):
        fact = {"id": "same", "date": DAY, "kind": "video"}
        result = summarize([catalog(publications=[fact]), catalog(campaign="two", owner="bob", publications=[fact])],
                           [metric()], platform=["youtube"])
        self.assertEqual(result["totals"]["published_count"], 0)
        self.assertEqual(result["quality"]["publication_conflicts"], 1)

    def test_csv_uses_same_rows_and_escapes_formulas(self):
        result = summarize([catalog(owner_label="=HYPERLINK('bad')")], [metric()], platform=["youtube"])
        text = report.csv_export(result).decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("成功发布数", text)
        self.assertIn("YouTube", text)

    def test_parameters_defaults_and_validation(self):
        value = report.parameters({})
        self.assertEqual(value["groups"], ["owner", "platform"])
        self.assertEqual(value["platforms"], ["youtube", "x", "facebook"])
        for query in ({"platform": ["tiktok"]}, {"group_by": ["owner,owner"]},
                      {"group_by": ["owner,drama,channel,date"]}, {"surprise": ["yes"]}):
            with self.assertRaises(report.ReportError):
                report.parameters(query)

    def test_validation_rejects_duplicates_partial_fields_and_negative_count(self):
        for values in ([metric(), metric()], [metric(clicks=None)], [metric(installs=-1)]):
            with self.assertRaises(report.ReportError):
                report.validate_metrics(values, "youtube", {DAY})

    def test_source_queries_are_site_scoped_binary_id_and_readonly(self):
        statement = routes.metric_sql(DAY, DAY, "x")
        self.assertIn("site_id='2116'", statement)
        self.assertIn("FORCE INDEX(sd)", statement)
        self.assertIn("GROUP BY dt,BINARY campaign_id LIMIT", statement)
        self.assertNotIn("BINARY campaign LIMIT", statement)
        with self.assertRaises(ValueError):
            routes.metric_sql("2026-10-19' OR 1=1", DAY, "x")


class XCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "x.sqlite3"
        with closing(sqlite3.connect(self.path)) as c, c:
            c.executescript("""
                CREATE TABLE x_post_link_attribution(log_id INTEGER PRIMARY KEY,queue_id INTEGER UNIQUE,
                  version TEXT,context_json TEXT);
                CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id INTEGER,long_url TEXT,
                  created_at TEXT,status TEXT,x_post_id TEXT,published_at TEXT,unknown_outcome INTEGER);
                CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,status TEXT,account_id INTEGER);
                CREATE TABLE x_post_repost_ledger(queue_id INTEGER,status TEXT,target_account_id INTEGER,
                  source_post_id TEXT,reposted_at TEXT,unknown_outcome INTEGER);
            """)

    def tearDown(self):
        self.temporary.cleanup()

    def insert(self, queue, *, repost=False, unknown=0, status="published", owner="gaoyuan"):
        context = dict(version="x-attribution-v2", af_c_id=str(queue), tenant="t", owner=owner, owner_name="郜远",
                       sub_user_id=248, channel_id="platform-account", channel_name="Account",
                       content_id="42", drama_name="Frozen", language="en", link_type="system", excluded=False)
        url = "https://www.dramawavew2a.com/ads/101/2116/view?" + urlencode({"af_c_id": queue, "c": "name"})
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("INSERT INTO x_post_link_attribution VALUES(?,?,?,?)",
                      (queue, queue, "x-attribution-v2", json.dumps(context)))
            c.execute("INSERT INTO x_post_publish_log VALUES(?,?,?,?,?,?,?,?)",
                      (queue, queue, url, DAY + "T01:00:00Z", status, "source" if repost else str(queue),
                       DAY + "T02:00:00Z", unknown))
            c.execute("INSERT INTO x_post_queue VALUES(?,?,?)", (queue, status, 7))
            if repost:
                c.execute("INSERT INTO x_post_repost_ledger VALUES(?,?,?,?,?,?)",
                          (queue, "reposted", 7, "source", DAY + "T03:00:00Z", unknown))

    def test_real_post_and_repost_counts_exclude_unknown_and_source(self):
        self.insert(1)
        self.insert(2, repost=True)
        self.insert(3, unknown=1)
        values = adapters.read_x(self.path)
        self.assertEqual(len(values), 3)
        result = summarize(values, [metric("x", "1")], platform=["x"])
        self.assertEqual(result["totals"]["published_count"], 2)
        self.assertEqual(result["totals"]["post_count"], 1)
        self.assertEqual(result["totals"]["repost_count"], 1)
        self.assertEqual(result["all_rows"][0]["owner_label"], "郜远")

    def test_repost_time_converts_to_utc(self):
        self.insert(1, repost=True)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE x_post_repost_ledger SET reposted_at='2026-10-20T01:00:00+08:00'")
        self.assertEqual(adapters.read_x(self.path)[0]["publications"][0]["date"], DAY)

    def test_missing_or_mismatched_frozen_mapping_fails_channel(self):
        self.insert(1)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE x_post_publish_log SET long_url=replace(long_url,'af_c_id=1','af_c_id=2')")
        with self.assertRaises(report.ReportError):
            adapters.read_x(self.path)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("DELETE FROM x_post_link_attribution")
        with self.assertRaises(report.ReportError):
            adapters.read_x(self.path)

    def test_readonly_catalog_does_not_create_missing_database(self):
        missing = self.path.parent / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            adapters.read_x(missing)
        self.assertFalse(missing.exists())

    def test_unknown_x_frozen_version_is_unavailable(self):
        self.insert(1)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE x_post_link_attribution SET version='x-attribution-v9'")
        with self.assertRaises(report.ReportError):
            adapters.read_x(self.path)


class SharedCollectorTests(unittest.TestCase):
    def test_one_channel_failure_does_not_erase_other_channel(self):
        def source(app, start, end, platform):
            if platform == "x":
                raise report.ReportError("待核实", 503)
            return [metric()], "now"
        with patch.object(routes, "read_catalogs", return_value=([catalog(), catalog("x")], {})), \
                patch.object(routes, "source", side_effect=source):
            result = routes.build_report({}, SYSTEM, {"start": [DAY], "end": [DAY]})
        self.assertEqual(result["totals"]["revenue"], 1)
        self.assertEqual(result["quality"]["platforms"]["x"]["state"], "unavailable")
        self.assertIsNone(result["platform_totals"]["x"]["revenue"])

    def test_catalog_failure_does_not_show_false_zero(self):
        with patch.object(routes, "read_catalogs", return_value=([], {"x": "归属待核实"})), \
                patch.object(routes, "source", return_value=([metric("x")], "now")):
            result = routes.build_report({}, SYSTEM, {"start": [DAY], "end": [DAY], "platform": ["x"]})
        self.assertIsNone(result["totals"]["revenue"])
        self.assertIsNone(result["totals"]["published_count"])
        self.assertEqual(result["quality"]["platforms"]["x"]["error"], "归属待核实")

    def test_http_actor_cannot_enable_global_scope(self):
        class Handler:
            command = "GET"
            def _social_analytics_actor(self):
                return dict(ACTOR, _system_report=True)
        captured = {}
        def build(app, actor, query, **kwargs):
            captured.update(actor=actor, query=query)
            return {"all_rows": []}
        with patch.object(routes, "build_report", side_effect=build):
            routes.dispatch(Handler(), urlsplit("/api/youtube-analytics/report?channel=UC123"),
                            {"json_response": lambda *args, **kwargs: None}, legacy=True)
        self.assertNotIn("_system_report", captured["actor"])
        self.assertEqual(captured["query"]["platform"], ["youtube"])
        self.assertEqual(captured["query"]["channel"], ["youtube:UC123"])

    def test_readonly_database_guard_and_secrets_stay_out_of_errors(self):
        app = {"ADMIN_MAPPING_MYSQL_HOST": "101.32.56.53", "ADMIN_MAPPING_MYSQL_PORT": 63350,
               "ADMIN_MAPPING_MYSQL_USER": "reader", "ADMIN_MAPPING_MYSQL_PASSWORD": "test-only-secret"}
        failure = type("Result", (), {"returncode": 0, "stdout": "0\n", "stderr": "test-only-secret"})
        with patch.object(routes.subprocess, "run", return_value=failure) as run:
            with self.assertRaises(report.ReportError) as raised:
                routes.query_rows(app, "SELECT 1")
        self.assertNotIn("test-only-secret", str(raised.exception))
        self.assertNotIn("test-only-secret", " ".join(run.call_args.args[0]))
        self.assertIn("START TRANSACTION READ ONLY; SELECT @@read_only;", run.call_args.kwargs["input"])
        self.assertEqual(run.call_args.kwargs["env"]["SQL_GATE_JOB_NAME"], "social-analytics")


class YouTubeCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "jobs.sqlite3"
        with closing(sqlite3.connect(self.path)) as c, c:
            c.executescript("""
                CREATE TABLE drama_admin_user(user_id TEXT,tenant_key TEXT,name TEXT);
                CREATE TABLE drama_material_short_link(id INTEGER,job_id TEXT,material_kind TEXT,
                    content_id TEXT,long_url TEXT,created_at_utc TEXT,publish_state TEXT);
                CREATE TABLE youtube_link_attribution(task_id TEXT,link_id INTEGER,context TEXT);
                CREATE TABLE youtube_manual_short_link(link_id INTEGER,tenant TEXT,owner TEXT,operation_id TEXT,context_json TEXT);
                CREATE TABLE drama_youtube_publish(id INTEGER,job_id TEXT,source_kind TEXT,
                    operator_user_id TEXT,operator_name TEXT,channel_id TEXT,preparation_id TEXT,
                    content_id TEXT,video_id TEXT,privacy_status TEXT,video_state TEXT,video_published_at_utc TEXT,
                    workflow TEXT,public_status TEXT);
                CREATE TABLE youtube_auto_preparation(id TEXT,tenant TEXT,owner TEXT,body TEXT);
                CREATE TABLE drama_material_job(job_id TEXT,drama_name TEXT,language TEXT);
                INSERT INTO drama_admin_user VALUES('alice','t','Alice');
            """)

    def tearDown(self):
        self.temporary.cleanup()

    def insert(self, number, *, manual=False, privacy="public", frozen=True):
        context = dict(tenant="t", owner="alice", owner_name="Alice", content_id="42", drama_name="Frozen drama",
                       channel_id="UC123", channel_name="Frozen channel", language="en", excluded=False)
        context.update(version="youtube-manual-link-v2" if manual else "youtube-attribution-v1",
                       **({"operation_id": str(number)} if manual else {"task_id": str(number)}))
        url = "https://www.dramawavew2a.com/ads/101/2284/view?" + urlencode({"af_c_id": str(number), "c": "name"})
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("INSERT INTO drama_material_short_link VALUES(?,?,?,?,?,?,?)",
                      (number, str(number), "video", "42", url, DAY + "T00:00:00Z", "published"))
            if manual:
                c.execute("INSERT INTO youtube_manual_short_link VALUES(?,?,?,?,?)",
                          (number, "t", "alice", str(number), json.dumps(context)))
            else:
                if frozen:
                    c.execute("INSERT INTO youtube_link_attribution VALUES(?,?,?)", (str(number), number, json.dumps(context)))
                c.execute("INSERT INTO drama_youtube_publish VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (number, str(number), "video", "alice", "Old name", "UC123", "prep", "42",
                           ("v%010d" % number), privacy, "published", DAY + "T01:00:00Z", "legacy", "pending"))

    def test_manual_effect_and_confirmed_public_video_have_separate_counts(self):
        self.insert(1)
        self.insert(2, manual=True)
        self.insert(3, privacy="unlisted")
        values = adapters.read_youtube(self.path)
        result = summarize(values, [metric(campaign="2")], platform=["youtube"])
        self.assertEqual(result["totals"]["revenue_cents"], 100)
        self.assertEqual(result["totals"]["published_count"], 1)
        self.assertEqual(result["totals"]["links"], 3)
        self.assertEqual(values[0]["owner_label"], "Alice")
        self.assertEqual(values[0]["channel"], "youtube:UC123")

    def test_legacy_owner_tenant_requires_unique_mapping(self):
        self.insert(1, frozen=False)
        self.assertEqual(adapters.read_youtube(self.path)[0]["tenant"], "t")
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("INSERT INTO drama_admin_user VALUES('alice','other','Elsewhere')")
        value = adapters.read_youtube(self.path)[0]
        self.assertEqual(value["tenant"], "")
        result = summarize([value], [metric(campaign="1")], platform=["youtube"])
        self.assertEqual(result["quality"]["unmatched_totals"]["revenue_cents"], 100)

    def test_reviewed_thumbnail_requires_confirmed_public_status(self):
        self.insert(1)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE drama_youtube_publish SET workflow='reviewed_thumbnail'")
        self.assertEqual(adapters.read_youtube(self.path)[0]["publications"], [])
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE drama_youtube_publish SET public_status='succeeded'")
        self.assertEqual(len(adapters.read_youtube(self.path)[0]["publications"]), 1)

    def test_adapters_never_change_source_database(self):
        self.insert(1)
        before = self.path.read_bytes()
        adapters.read_youtube(self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_youtube_frozen_id_must_match_url_and_ledger(self):
        self.insert(1)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE drama_material_short_link SET long_url=replace(long_url,'af_c_id=1','af_c_id=2')")
        with self.assertRaises(report.ReportError):
            adapters.read_youtube(self.path)

    def test_manual_v1_prefix_is_validated_by_frozen_version(self):
        self.insert(1, manual=True)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE youtube_manual_short_link SET context_json=json_remove(context_json,'$.version')")
            c.execute("UPDATE drama_material_short_link SET long_url=replace(long_url,'af_c_id=1','af_c_id=yt_manual_1')")
        self.assertEqual(adapters.read_youtube(self.path)[0]["key"][0], "yt_manual_1")
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("UPDATE youtube_manual_short_link SET operation_id='different'")
        with self.assertRaises(report.ReportError):
            adapters.read_youtube(self.path)


if __name__ == "__main__":
    unittest.main()
