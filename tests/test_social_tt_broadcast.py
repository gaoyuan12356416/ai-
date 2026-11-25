import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from features.social_analytics.adapters import normalized
from features.social_analytics.backend_scope import restrict
from features.social_analytics.report import parameters, summarize
from features.social_analytics.tiktok import campaign_key
from features.social_analytics.bill_income import window

DAY = "2026-11-24"
NOW = datetime(2026, 11, 25, 3, 30, tzinfo=timezone.utc)


def post(cid, name, owner="u", drama="drama-1"):
    return normalized("tiktok", dict(site_id="2250", key=[campaign_key(cid, name), name],
        link_id=cid, tenant="t" if owner else "", owner=owner, owner_label="发布人" if owner else "",
        drama=drama, drama_label=drama, channel="account", language="en", link_type="auto_template",
        date=DAY, excluded=False, publications=[dict(id="post:" + cid, date=DAY, kind="video")]))


def metric(cid, name, cents, **kwargs):
    return dict(platform="tiktok", site_id="2250", campaign_id=campaign_key(cid, name),
                date=DAY, clicks=1, installs=1, views=1, conversions=0,
                revenue_cents=cents, refund_cents=0, **kwargs)


def payment(idx, name, cents):
    return dict(id=idx, site_id="2250", campaign_id="1", campaign=name,
                event_name="af_purchase", event_time=window(DAY)[0], user_key="a" * 64,
                revenue_cents=cents, positive_purchase=True)


def group(name, iap, iaa):
    return dict(site_id="2250", date=DAY, campaign_id="1", campaign=name,
                purchase_events=1, ad_events=2, iap_cents=iap, iaa_cents=iaa)


class TikTokBroadcastTests(unittest.TestCase):
    def test_daily_waits_for_evidenced_purchases_and_preserves_other_channels(self):
        from features.social_daily_report import billing
        from features.social_daily_report.collector import present
        from features.social_daily_report.report import require_complete

        tt = post("1", "Post")
        x = dict(tt, platform="x", platform_label="X", site_id="2116", owner="x-user", key=["x-post", ""],
                 link_id="x-post", publications=[dict(id="x-post", date=DAY, kind="post")])
        catalogs = [tt, x]
        tt_fact = dict(metric("1", "Post", 500), conversions=1)
        x_fact = dict(tt_fact, platform="x", site_id="2116", campaign_id="x-post", revenue_cents=100)
        selected, facts, coverage, _ = restrict(catalogs, [tt_fact, x_fact], DAY)
        params = parameters({"start": [DAY], "end": [DAY], "platform": ["x", "tiktok"], "group_by": ["owner,platform"]})
        x_bill = dict(payment(3, "", 100), site_id="2116", campaign_id="x-post")
        for path in ("settled", "provisional", "empty", "missing_payer", "unavailable"):
            with self.subTest(path=path):
                shared = summarize(selected, facts, {"_system_report": True}, params, "snapshot", metric_coverage=coverage)
                report = present(shared, DAY)
                tt_bill = dict(payment(3 if path != "settled" else 4, "Post", 500), provisional=path != "settled")
                if path == "missing_payer":
                    tt_bill["user_key"] = None
                def orders(app, day, *, tiktok_provisional=False):
                    if not tiktok_provisional:
                        return [x_bill, tt_bill] if path == "settled" else [x_bill]
                    if path == "unavailable":
                        raise RuntimeError("sensitive driver detail")
                    return [] if path == "empty" else [tt_bill]
                def groups(app, sql, max_rows):
                    if "site_id='2250'" not in sql:
                        return []
                    if path == "settled" or "ads_drama_bills_t0" in sql:
                        return [dict(group("Post", 500, 0), iap_usd_1e4=50000, iaa_usd_1e4=0, ad_events=0)]
                    return []
                with patch.object(billing, "read_bills", side_effect=orders) as lookup, \
                        patch.object(billing, "query_source_rows", side_effect=groups):
                    billing.collect_financials(report, {}, catalogs)
                audit = report["quality"]["billing"]
                self.assertEqual(report["platforms"]["x"]["totals"]["revenue_cents"], 100)
                self.assertEqual(lookup.call_count, 1 if path == "settled" else 2)
                if path in ("settled", "provisional"):
                    require_complete(report)
                    self.assertEqual(report["totals"]["revenue_cents"], 600)
                    self.assertEqual(report["totals"]["conversions"], 1)
                    self.assertEqual("tiktok_purchase_source" in audit, path == "provisional")
                else:
                    with self.assertRaisesRegex(ValueError, "sources_incomplete"):
                        require_complete(report)
                    self.assertIsNone(report["totals"]["revenue_cents"])
                    self.assertIsNone(report["platforms"]["tiktok"]["totals"]["conversions"])
                    self.assertEqual(audit["tiktok_purchase_observation"]["insight_revenue_cents"], 500)
                    if path == "missing_payer":
                        self.assertEqual(audit["tiktok_purchase_observation"]["t0_iap_cents"], 500)
                    self.assertNotIn("sensitive driver", json.dumps(report))

    def test_older_three_platform_preparation_is_recollected_without_changing_it(self):
        from features.social_daily_report import prepared
        from features.social_daily_report.collector import present
        from features.social_daily_report.report import build_cards
        from test_social_daily_report import three_channel_fixture

        old = present(three_channel_fixture(), "2026-11-22")
        old["prepared_at"] = NOW.isoformat()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "prepared" / old["report_date"] / "report.json"
            target.parent.mkdir(parents=True)
            original = json.dumps(dict(chat_id="chat", report=old)).encode()
            target.write_bytes(original)
            self.assertIsNone(prepared.load(folder, old["report_date"], "chat"))
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse((Path(folder) / "delivery.sqlite3").exists())
        text = json.dumps(build_cards(old, "chat"), ensure_ascii=False)
        self.assertIn("已归属收入", text)
        self.assertNotIn("TikTok", text)

    def test_historical_tiktok_recharge_available_before_iaa_reporting(self):
        from features.social_daily_report import billing
        day = "2026-11-22"
        purchase = dict(payment(1, "generic", 511), event_time=window(day)[0])
        money = dict(group("generic", 511, 0), date=day, ad_events=0)
        result = billing.aggregate([], [purchase], day, [money])
        self.assertEqual(result["tt_unassigned"].public()["iap_cents"], 511)
        self.assertEqual(result["total"].revenue_cents, 511)
        self.assertFalse(result["iaa_sites"])

    def test_daily_full_income_and_people_are_reconciled_without_guessed_owner(self):
        from features.social_daily_report import billing
        from features.social_daily_report.collector import present
        from features.social_daily_report.report import build_cards, require_complete
        catalog = [post("1", "Post"), post("2", "legacy", owner="")]
        selected, facts, coverage, _ = restrict(catalog, [
            metric("1", "Post", 6000), metric("1", "post", 906)], DAY)
        params = parameters({"start": [DAY], "end": [DAY], "platform": ["tiktok"], "group_by": ["owner,platform"]})
        shared = summarize(selected, facts, {"_system_report": True}, params, "snapshot", metric_coverage=coverage)
        report = present(shared, DAY)
        finances = billing.aggregate(catalog,
            [payment(1, "Post", 6000), payment(2, "post", 906)], DAY,
            [group("Post", 6000, 100), group("post", 906, 447)])
        billing.apply(report, finances)
        require_complete(report)
        platform = report["platforms"]["tiktok"]
        self.assertEqual((platform["totals"]["iap_cents"], platform["totals"]["iaa_cents"]), (6906, 547))
        self.assertEqual(platform["unassigned_income"]["revenue_cents"], 1353)
        self.assertEqual(report["totals"]["revenue_cents"], 7453)
        self.assertEqual(report["totals"]["conversions"], 1)
        self.assertEqual(report["totals"]["published_count"], 2)
        self.assertEqual([(r["owner"], r["revenue_cents"], r["published_count"]) for r in report["rows"]],
                         [("u", 6100, 1)])
        card = json.dumps(build_cards(report, "chat"), ensure_ascii=False)
        for text in ("TikTok", "$74.53", "$69.06", "$5.47", "$13.53", "1 位发布人"):
            self.assertIn(text, card)
        self.assertNotIn("__tiktok_unassigned__", card)

    def test_daily_subcent_payer_preserved_and_group_snapshot_race_rejected(self):
        from features.social_daily_report import billing
        catalog = [post("1", "Post")]
        result = billing.aggregate(catalog, [payment(1, "Post", 0)], DAY, [group("Post", 0, 0)])
        self.assertEqual(result["total"].public()["conversions"], 1)
        with self.assertRaisesRegex(ValueError, "billing_reconciliation_failed"):
            billing.aggregate(catalog, [payment(1, "Post", 0)], DAY, [group("different", 0, 0)])

    def test_digest_compares_verified_drama_and_leaves_generic_income_pending(self):
        from features.socialkit_gap_alert import collector, digest
        catalog = [post("1", "Post"), post("2", "legacy", owner="", drama="drama-2")]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snap.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE catalog(payload TEXT)")
                db.execute("CREATE TABLE effects(platform TEXT,dt TEXT,payload TEXT)")
                db.executemany("INSERT INTO catalog VALUES(?)", [(json.dumps(row),) for row in catalog])
                facts = [metric("1", "Post", 100, iaa_cents=20),
                         metric("2", "legacy", 120, iaa_cents=30, tiktok_unassigned=True, tiktok_drama="drama-2"),
                         metric("0001", "Generic", 3000, iaa_cents=500, tiktok_unassigned=True)]
                db.executemany("INSERT INTO effects VALUES('tiktok',?,?)",
                               [(DAY, json.dumps(row)) for row in facts])
            manifest = dict(version="v", generated_at=NOW.isoformat(), coverage={"tiktok:2250": [DAY]},
                            iaa_coverage={"tiktok:2250": [DAY]})
            channel = collector.warehouse_channel({}, (manifest, path.as_uri() + "?mode=ro&immutable=1"),
                                                  DAY, "tiktok", NOW, True)
        candidates = [dict(platform="tiktok", drama_app_id=1479, drama=drama, revenue_cents=1000,
                           iaa_cents=25, iaa_complete=True) for drama in ("drama-1", "drama-2", "unmapped")]
        events, pending = collector.compare(candidates, {"tiktok": channel}, "2026-11-25")
        self.assertEqual([(e["drama"], e["own_revenue_cents"], e["history_success_count"]) for e in events],
                         [("drama-1", 100, 1), ("drama-2", 120, 1)])
        self.assertEqual(pending, [dict(platform="tiktok", drama="unmapped", reason="tiktok_drama_income_unverified")])
        data = dict(date="2026-11-25", events=events, pending=pending, errors={}, socialkit={"version": "v"},
                    collected_at=NOW.isoformat())
        self.assertIn('value="tiktok"', digest.render_html(data))
        self.assertIn("TikTok：2 项", digest.summary_card(data, "https://example.org/report")["elements"][0]["text"]["content"])
        channel["iaa_complete"] = False
        events, pending = collector.compare(candidates, {"tiktok": channel}, "2026-11-25")
        self.assertFalse(events)
        self.assertEqual(len(pending), 3)


if __name__ == "__main__":
    unittest.main()
