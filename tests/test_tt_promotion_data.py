import json
import sqlite3
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

from features.post_daily_report import tt as daily
from features.post_daily_report.tt_promotion import collect_promotion
from features.social_analytics.report import summarize, parameters, csv_export
from features.social_analytics.tiktok import AUTO_QUEUE_ID_NAMESPACE, campaign_key, read_tiktok
from features.tt_promotion.profiles import build_snapshot, profiles_for_accounts, rank_candidates
from features.x_tag_revenue.taxonomy import classify
from tests import test_social_tiktok_attribution as fixtures
from tests.test_social_analytics import metric


DAY = "2026-11-24"
TS = DAY + "T04:00:00+00:00"
ACTOR = dict(role="admin", tenant_key="tenant", user_id="admin")


class PromotionDataTest(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.TikTokAttributionTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.post, self.auto, self.app = (
            fixture.root,
            fixture.post,
            fixture.auto,
            dict(fixture.app),
        )
        self.promo = self.root / "promotion.db"
        self.app["SOCIAL_ANALYTICS_TT_PROMOTION_DB_PATH"] = str(self.promo)
        fixture.sql(
            self.promo,
            """
            CREATE TABLE tt_promotion_report_task (id INTEGER PRIMARY KEY,template_id,template_creator_user_id,
             account_id,account_username,account_display_name,drama_language,drama_name,content_id,material_id,
             scheduled_at_utc,published_at_utc,status,privacy_level,publish_id,unknown_outcome,code,route_queue_id,
             trigger_type,error_code,video_template,template_tenant);
            CREATE TABLE tt_promotion_report_plan (slot_key PRIMARY KEY,task_id,template_id,template_name,account_id,
             scheduled_at_utc,video_template,trigger_type,slot_state DEFAULT 'planned',slot_reason DEFAULT '');
        """,
        )
        fixture.sql(self.post, "CREATE TABLE tt_post_direct_test(publish_id,status)")
        with closing(sqlite3.connect(self.auto)) as db, db:
            for tid in (43, 44):
                db.execute(
                    "INSERT INTO tt_auto_task SELECT ?,account_id,account_username,account_display_name,?,?,?,?,?,?,NULL,run_id,template_version,status,unknown_outcome,account_settings_json FROM tt_auto_task WHERE id=42",
                    (tid, str(tid), "auto", "en", "receipt-" + str(tid), TS, 1),
                )
                db.execute(
                    "INSERT INTO tt_auto_material_ledger VALUES(?,?,?,?,?)", (str(tid), "auto", tid, 1, 100)
                )
        for tid in [700, 701]:
            route_id = AUTO_QUEUE_ID_NAMESPACE + tid
            with closing(sqlite3.connect(self.promo)) as db, db:
                db.execute(
                    "INSERT INTO tt_promotion_report_task VALUES(" + ",".join("?" * 22) + ")",
                    (
                        tid,
                        1,
                        "creator",
                        "9",
                        "handle",
                        "Channel",
                        "en",
                        "新剧",
                        "auto",
                        str(tid),
                        TS,
                        TS,
                        "published",
                        "PUBLIC_TO_EVERYONE",
                        f"receipt-{tid}",
                        0,
                        "C" + str(tid),
                        route_id,
                        "auto",
                        "",
                        "random_overlay",
                        "tenant",
                    ),
                )
                db.execute(
                    "INSERT INTO tt_promotion_report_plan(slot_key,task_id,template_id,template_name,account_id,scheduled_at_utc,video_template,trigger_type) VALUES(?,?,?,?,?,?,?,?)",
                    (str(tid), tid, 1, "模板", "9", TS, "random_overlay", "auto"),
                )
            name = f"TT-{tid}"
            query = urlencode(
                dict(c=name, af_c_id=tid, af_adset_id="9", af_ad_id=tid, af_dp="auto", af_channel="TT")
            )
            with closing(sqlite3.connect(self.post)) as db, db:
                db.execute(
                    "INSERT INTO tt_post_code_route VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        route_id,
                        "C" + str(tid),
                        "TT",
                        str(tid),
                        name,
                        "9",
                        str(tid),
                        "auto",
                        "https://www.dramawavew2a.com/ads/101/2250/view?" + query,
                        TS,
                    ),
                )

    def test_three_old_two_new_same_category_dedup_and_cents(self):
        catalog = read_tiktok(self.app)
        facts = [
            metric("tiktok", campaign_key(str(n), "TT-" + str(n)), n, date=DAY, site_id="2250", iaa_cents=1)
            for n in (700, 701)
        ]
        query = parameters(
            dict(platform=["tiktok"], start=[DAY], end=[DAY], group_by=["channel,link_type"])
        )

        def report(rows):
            return summarize(
                rows,
                facts,
                ACTOR,
                query,
                TS,
                metric_coverage={"tiktok:2250": [DAY]},
                iaa_coverage={"tiktok:2250": [DAY]},
            )

        result = report(catalog)
        row = next(r for r in result["all_rows"] if r["channel"] == "tiktok:9")
        self.assertEqual(
            (row["link_type"], row["published_count"], row["revenue"], row["iap"], row["iaa"]),
            ("auto_template", 5, 14.01, 13.99, 0.02),
        )
        row = next(r for r in report(catalog + catalog)["all_rows"] if r["channel"] == "tiktok:9")
        self.assertEqual((row["published_count"], row["revenue"]), (5, 14.01))
        self.assertIn("14.01", str(csv_export(result)))
        with closing(sqlite3.connect(self.promo)) as db, db:
            db.execute("UPDATE tt_promotion_report_task SET unknown_outcome=1 WHERE id=701")
        current = report(read_tiktok(self.app))
        self.assertEqual(
            next(r for r in current["all_rows"] if r["channel"] == "tiktok:9")["published_count"], 4
        )

    def test_day_plan_merge_and_cross_source_duplicate(self):
        moment = datetime.fromisoformat(TS).replace(hour=0)
        window = SimpleNamespace(
            start=moment,
            end=moment + timedelta(days=1),
            cutoff=moment + timedelta(days=1, hours=11),
            date=DAY,
        )
        paths = dict(tt=self.post, tt_auto=self.auto, tt_promotion=self.promo)

        def count():
            old = daily._row("auto_template:random_overlay", "自动模板·随机叠加", "随机叠加")
            old.update(expected=3, published=3)
            out = dict(rows=[old], warnings=[], evidence=[], prior_completed=0)
            collect_promotion(paths, window, out)
            self.assertFalse(out["warnings"])
            self.assertEqual(len(out["rows"]), 1)
            return old

        row = count()
        self.assertEqual((row["expected"], row["published"]), (5, 5))
        with closing(sqlite3.connect(self.promo)) as db, db:
            db.execute("UPDATE tt_promotion_report_task SET publish_id='receipt-A' WHERE id=700")
            db.execute(
                "INSERT INTO tt_promotion_report_plan VALUES('empty',NULL,1,'模板','9',?,'random_overlay','auto','planned','')",
                (TS,),
            )
        row = count()
        self.assertEqual((row["expected"], row["published"], row["unknown"]), (6, 4, 1))

    def test_scoped_windows_conserve_and_rank(self):
        catalog = read_tiktok(self.app)
        facts = [
            metric("tiktok", campaign_key("700", "TT-700"), 701, date=DAY, site_id="2250", iaa_cents=11)
        ]
        days = [(date.fromisoformat(DAY) - timedelta(days=i)).isoformat() for i in range(28)]
        m = dict(
            version="v",
            start=days[-1],
            end=DAY,
            coverage={"tiktok:2250": days},
            iaa_coverage={"tiktok:2250": days},
            generated_at=TS,
        )
        audience = dict(status="matched", audience="female", audience_labels=["女性"], languages=["en"])
        tags = classify(["revenge", "ceo"], "local", audience_record=audience, expected_language="en")
        data = build_snapshot(catalog, facts, {("auto", "en"): tags}, m, date.fromisoformat(DAY))
        (self.root / "current.json").write_text(json.dumps(data), encoding="utf-8")
        full = profiles_for_accounts([9], ACTOR, root=self.root, detail=True)["accounts"]["9"]
        for win in full["windows"].values():
            self.assertEqual(
                (win["revenue_cents"], win["iap_cents"], win["iaa_cents"], win["published_count"]),
                (701, 690, 11, 5),
            )
            audience = win["audiences"][0]
            self.assertEqual(len(audience["children"]), 2)
            for key in ["revenue_cents", "iaa_cents", "iap_cents"]:
                self.assertEqual(sum(t[key] for t in audience["children"]), win[key])
        candidates = [
            dict(
                content_id="bad", language="en", classification={"audience": "male", "themes": ["revenge"]}
            ),
            dict(content_id="auto", language="en", classification=tags),
        ]
        ordered, info = rank_candidates(candidates, full, "slot", root=self.root)
        self.assertEqual(ordered[0]["content_id"], "auto")
        self.assertEqual(info["reason"], "preferred_match")
        for actor in [
            dict(role="admin", tenant_key="other", user_id="a"),
            dict(role="user", tenant_key="tenant", user_id="other"),
        ]:
            result = profiles_for_accounts([9], actor, root=self.root)
            self.assertEqual(result["accounts"]["9"]["windows"]["all"]["revenue_cents"], 0)

    def test_unavailable_profile_is_not_zero(self):
        state = profiles_for_accounts([9], ACTOR, root=self.root)["accounts"]["9"]
        self.assertIsNone(state["windows"]["7d"]["revenue_cents"])

    def test_initial_iaa_delay_keeps_known_recharge_without_zero_ad_income(self):
        catalog = read_tiktok(self.app)
        facts = [
            metric("tiktok", campaign_key("700", "TT-700"), 500, date=DAY, site_id="2250", iaa_cents=0)
        ]
        manifest = dict(
            version="initial", end=DAY, coverage={"tiktok:2250": [DAY]}, iaa_coverage={}, generated_at=TS
        )
        data = build_snapshot(catalog, facts, {}, manifest, date.fromisoformat(DAY))
        (self.root / "current.json").write_text(json.dumps(data), encoding="utf-8")
        state = profiles_for_accounts([9], ACTOR, root=self.root, detail=True)["accounts"]["9"]
        window = state["windows"]["all"]
        self.assertEqual(
            (state["status"], window["iap_cents"], window["revenue_cents"]), ("partial", 500, 500)
        )
        self.assertEqual(window["iaa_pending_dates"], [DAY])
        self.assertIsNone(window["iaa_cents"])
        for audience in window["audiences"]:
            self.assertIsNone(audience["iaa_cents"])
            for theme in audience["children"]:
                self.assertIsNone(theme["iaa_cents"])
                for drama in theme["dramas"]:
                    self.assertIsNone(drama["iaa_cents"])
