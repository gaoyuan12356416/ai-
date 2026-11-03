import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from features.socialkit_gap_alert import collector
from features.socialkit_gap_alert.cards import split_cards, payload_size, MAX_BYTES
from features.socialkit_gap_alert.delivery import Store, deliver
from features.socialkit_gap_alert.runner import run_alerts, CHAT_ID
from features.post_daily_report.delivery import DefiniteFailure, DeliveryUnknown
from features.social_analytics.adapters import normalized

NOW = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
DAY = "2026-11-03"


def event(drama="drama-1", platform="facebook", day=DAY):
    return dict(date=day, app_id=1479, platform=platform, drama=drama, drama_name="测试剧",
                socialkit_revenue_cents=1, own_revenue_cents=0, history_success_count=0,
                last_published_date=None, state="no_confirmed_publication")


def report(events=None, day=DAY):
    return dict(date=day, collected_at=NOW.isoformat(), socialkit={"version": "v1", "source_updated_at": NOW.isoformat()},
                own_source_updated_at={"facebook": {"2049": NOW.isoformat(), "2310": NOW.isoformat()}},
                events=[event(day=day)] if events is None else events, pending=[], errors={})


def candidate(drama="drama-1", platform="facebook", income=1, app=1479):
    return dict(platform=platform, drama_app_id=app, drama=drama, drama_name="同名剧", revenue_cents=income)


def own(amounts=None, history=None, blocked=()):
    return dict(amounts=amounts or {}, history=history or {}, blocked=set(blocked))


class ComparisonTests(unittest.TestCase):
    def test_exact_money_product_channel_and_ids(self):
        candidates = [candidate(), candidate("zero", income=0), candidate("ours"),
                      candidate("negative"), candidate("other-product", app=3302),
                      candidate("drama-1", "x"), candidate("different-id")]
        channels = {"facebook": own({"ours": 1, "negative": -1}, {"drama-1": {"count": 2, "last_date": "2026-10-01"}}), "x": own()}
        events, pending = collector.compare(candidates, channels, DAY)
        self.assertFalse(pending)
        self.assertEqual({(e["platform"], e["drama"]) for e in events},
                         {("facebook", "drama-1"), ("facebook", "different-id"), ("x", "drama-1")})
        self.assertEqual(events[0]["state"], "no_confirmed_publication")  # stable drama ID sort
        published = next(e for e in events if e["platform"] == "facebook" and e["drama"] == "drama-1")
        self.assertEqual(published["history_success_count"], 2)
        self.assertEqual(published["state"], "published_zero_income")

    def test_unknown_never_zero_and_other_platform_continues(self):
        events, pending = collector.compare([candidate(), candidate("bad"), candidate("x", "x")],
                                             {"facebook": own(blocked=["bad"]), "x": {"error": "timeout"}}, DAY)
        self.assertEqual([e["drama"] for e in events], ["drama-1"])
        self.assertEqual(len(pending), 2)

    def test_history_deduplicates_all_dates_and_rejects_conflicts(self):
        base = dict(platform="facebook", site_id="2310", key=["1", "n"], tenant="t", owner="o",
                    drama="a", excluded=False, publications=[{"id": "post:1", "date": "2020-01-01"}])
        histories, blocked = collector.history([base, copy.deepcopy(base)])
        self.assertEqual(histories["a"], {"count": 1, "last_date": "2020-01-01"})
        self.assertFalse(blocked)
        other = dict(base, key=["2", "n"], drama="b")
        histories, blocked = collector.history([base, other])
        self.assertEqual(blocked, {"a", "b"})
        self.assertFalse(histories)

    def test_facebook_both_sources_must_be_fresh(self):
        shared = {"quality": {"platforms": {"facebook": {"state": "complete", "publication_status": "complete"}}}, "all_rows": []}
        with patch.object(collector, "read_catalogs", return_value=([], {})), patch.object(collector, "build_report", return_value=shared), patch.object(collector, "source_times", return_value={"2049": (NOW - timedelta(hours=3)).isoformat(), "2310": NOW.isoformat()}):
            with self.assertRaisesRegex(collector.ComparisonUnavailable, "stale"):
                collector.own_channel({}, DAY, "facebook", NOW)

    def test_missing_drama_is_zero_only_with_complete_source(self):
        complete = {"state": "complete", "publication_status": "complete"}
        shared = {"quality": {"platforms": {"x": complete}}, "all_rows": []}
        with patch.object(collector, "read_catalogs", return_value=([], {})), patch.object(collector, "build_report", return_value=shared), patch.object(collector, "source_times", return_value={"2116": NOW.isoformat()}):
            channel = collector.own_channel({}, DAY, "x", NOW)
            events, pending = collector.compare([candidate(platform="x")], {"x": channel}, DAY)
            self.assertEqual(len(events), 1)
            self.assertFalse(pending)
            complete["state"] = "partial"
            with self.assertRaisesRegex(collector.ComparisonUnavailable, "incomplete"):
                collector.own_channel({}, DAY, "x", NOW)

    def test_unrefunded_positive_revenue_remains_candidate(self):
        row = candidate(income=1)
        row["refund_cents"] = 100
        events, _ = collector.compare([row], {"facebook": own()}, DAY)
        self.assertEqual(events[0]["socialkit_revenue_cents"], 1)

    def test_unconfirmed_publication_raw_nonzero_income_never_becomes_zero(self):
        record = normalized("x", dict(key=("q-unknown", "saved"), link_id="q-unknown", tenant="t", owner="o",
                            owner_label="o", channel="a", channel_label="a", drama="drama-1", drama_label="name",
                            language="en", language_label="en", link_type="manual", link_type_label="manual",
                            date=DAY, excluded=False, publications=[]))
        for amount in (100, -100, 0):
            metric = dict(date=DAY, platform="x", site_id="2116", campaign_id="q-unknown", campaign="new name",
                          clicks=0, views=0, installs=0, conversions=0, revenue_cents=amount, refund_cents=0,
                          updated_at=NOW.isoformat())
            with self.subTest(amount=amount), patch.object(collector, "read_catalogs", return_value=([record], {})), patch("features.social_analytics.routes.source", return_value=([metric], NOW.isoformat())), patch.object(collector, "source", return_value=([metric], NOW.isoformat())):
                channel = collector.own_channel({}, DAY, "x", NOW)
            events, pending = collector.compare([candidate(platform="x")], {"x": channel}, DAY)
            self.assertEqual(len(events), int(amount == 0))
            self.assertEqual(len(pending), int(amount != 0))

    def test_facebook_unconfirmed_income_requires_frozen_source_pair(self):
        record = dict(platform="facebook", site_id="2310", key=("q", "n"), drama="d", tenant="t", owner="o",
                      publications=[], excluded=False, effect_source={"adset_id": "page", "ad_id": "material"})
        metric = dict(platform="facebook", campaign_id="q", revenue_cents=100, adset_id="other-page", ad_id="material")
        self.assertFalse(collector.unconfirmed_income_drama([record], {"2310": [metric]}, DAY))
        metric["adset_id"] = "page"
        self.assertEqual(collector.unconfirmed_income_drama([record], {"2310": [metric]}, DAY), {"d"})
        record["excluded"] = True
        self.assertFalse(collector.unconfirmed_income_drama([record], {"2310": [metric]}, DAY))

    def test_failed_platform_does_not_block_healthy(self):
        candidates = [candidate(), candidate("x", "x")]
        def own_channel(app, day, platform, now, **kwargs):
            if platform == "facebook":
                raise RuntimeError("private connector info")
            return own()
        with patch.object(collector, "socialkit_candidates", return_value=(candidates, {"version": "v1"})), patch.object(collector, "own_channel", side_effect=own_channel):
            result = collector.collect({}, "unused", now=NOW)
        self.assertEqual([e["platform"] for e in result["events"]], ["x"])
        self.assertEqual(result["errors"], {"facebook": "RuntimeError"})
        self.assertNotIn("private", json.dumps(result))

    def test_real_snapshot_sum_accounts_and_excludes_unmatched_hotdrama(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            conn = sqlite3.connect(root / "v1.sqlite3")
            conn.execute("CREATE TABLE metadata(key TEXT,value TEXT)")
            conn.execute("CREATE TABLE effects(date TEXT,platform TEXT,drama_app_id INTEGER,drama_content_id TEXT,drama_name TEXT,revenue_cents INTEGER,matched INTEGER)")
            conn.executemany("INSERT INTO effects VALUES(?,?,?,?,?,?,?)", [
                (DAY, "facebook", 1479, "a", "name", 100, 1),
                (DAY, "facebook", 1479, "a", "name", 200, 1),
                (DAY, "facebook", 3302, "a", "name", 400, 1),
                (DAY, "facebook", 1479, "bad", "name", 500, 0),
                (DAY, "tiktok", 1479, "a", "name", 100, 1)])
            conn.commit(); conn.close()
            manifest = {"version": "v1", "database": "v1.sqlite3", "date_manifest": {DAY: {"state": "complete", "refreshed_at": NOW.isoformat(), "source_updated_at": NOW.isoformat()}}}
            (root / "current.json").write_text(json.dumps(manifest), encoding="utf-8")
            rows, meta = collector.socialkit_candidates(root, "v1", DAY, NOW)
            self.assertEqual([(r["drama"], r["revenue_cents"]) for r in rows], [("a", 300)])
            with self.assertRaises(collector.ComparisonUnavailable):
                collector.socialkit_candidates(root, "v1", DAY, NOW + timedelta(hours=3))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "delivery.sqlite3"
        self.store = Store(self.path)
        self.sent = []

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def sender(self, card, request_uuid):
        self.sent.append((card, request_uuid))
        return {"message_id": "m" + str(len(self.sent))}

    def test_same_day_dedupe_restart_new_hits_and_next_day(self):
        first = deliver(self.store, report(), CHAT_ID, self.sender)
        self.assertEqual(first["sent_cards"], 1)
        self.store.close(); self.store = Store(self.path)
        changed = event(); changed["history_success_count"] = 1
        second = deliver(self.store, report([changed, event("new")]), CHAT_ID, self.sender)
        self.assertEqual(second["new_events"], 1)
        self.assertEqual(second["sent_cards"], 1)
        next_day = "2026-11-04"
        third = deliver(self.store, report([event(day=next_day)], next_day), CHAT_ID, self.sender)
        self.assertEqual(third["sent_cards"], 1)
        self.assertEqual(len(self.sent), 3)

    def test_unknown_is_held_but_new_events_continue(self):
        def unknown(*args):
            raise DeliveryUnknown("transport")
        result = deliver(self.store, report(), CHAT_ID, unknown)
        self.assertEqual(result["held_cards"], 1)
        result = deliver(self.store, report([event(), event("new")]), CHAT_ID, self.sender)
        self.assertEqual(result["sent_cards"], 1)
        self.assertEqual(result["held_cards"], 1)
        self.assertEqual(len(self.sent), 1)

    def test_interrupted_send_does_not_retry(self):
        self.store.prepare(report(), CHAT_ID)
        card = self.store.pending(DAY, CHAT_ID)[0]
        self.store.claim(card["id"])
        self.store.close(); self.store = Store(self.path)
        result = deliver(self.store, report(), CHAT_ID, self.sender)
        self.assertEqual(result["held_cards"], 1)
        self.assertFalse(self.sent)

    def test_definite_failure_retries_same_uuid_only_while_eligible(self):
        def fail(*args):
            raise DefiniteFailure("not accepted")
        deliver(self.store, report(), CHAT_ID, fail)
        uuid = self.store.receipts(DAY, CHAT_ID)[0]["request_uuid"]
        result = deliver(self.store, report(), CHAT_ID, self.sender)
        self.assertEqual(result["sent_cards"], 1)
        self.assertEqual(self.sent[0][1], uuid)
        deliver(self.store, report([event("no-longer-zero")]), CHAT_ID, fail)
        result = deliver(self.store, report([]), CHAT_ID, self.sender)
        self.assertEqual(result["sent_cards"], 0)
        self.assertIn("cancelled", [r["status"] for r in result["receipts"]])

    def test_definite_retry_limit_does_not_block_new_events(self):
        def fail(*args):
            raise DefiniteFailure("not accepted")
        for _ in range(4):
            deliver(self.store, report(), CHAT_ID, fail)
        self.assertEqual(self.store.receipts(DAY, CHAT_ID)[0]["attempts"], 3)
        result = deliver(self.store, report([event(), event("new")]), CHAT_ID, self.sender)
        self.assertEqual(result["sent_cards"], 1)
        self.assertEqual(result["failed_cards"], 1)

    def test_definite_failure_rebuilds_changed_publication_evidence(self):
        def fail(*args):
            raise DefiniteFailure("not accepted")
        deliver(self.store, report(), CHAT_ID, fail)
        changed = event(); changed.update(history_success_count=1, last_published_date=DAY,
                                         state="published_zero_income")
        result = deliver(self.store, report([changed]), CHAT_ID, self.sender)
        self.assertEqual(result["sent_cards"], 1)
        self.assertEqual([r["status"] for r in result["receipts"]].count("cancelled"), 1)
        self.assertIn("已发布，今日未出收入", json.dumps(self.sent[0][0], ensure_ascii=False))

    def test_success_without_receipt_is_unknown(self):
        result = deliver(self.store, report(), CHAT_ID, lambda *args: {})
        self.assertEqual(result["held_cards"], 1)
        self.assertEqual(result["sent_cards"], 0)

    def test_multipart_preserves_all_events_and_receipts(self):
        events = [event(str(i)) for i in range(25)]
        result = deliver(self.store, report(events), CHAT_ID, self.sender)
        self.assertEqual(result["sent_cards"], 3)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0], 25)
        self.assertTrue(all(payload_size(card, CHAT_ID) <= MAX_BYTES for card, _ in self.sent))

    def test_multipart_sends_in_rank_order_not_uuid_order(self):
        events = [event(str(i)) for i in range(25)]
        with patch("features.socialkit_gap_alert.delivery.uuid.uuid4", side_effect=[uuid.UUID(int=n) for n in (3, 2, 1)]):
            deliver(self.store, report(events), CHAT_ID, self.sender)
        first_ids = []
        for card, _ in self.sent:
            content = card["elements"][2]["text"]["content"]
            first_ids.append(content.split("剧目 ID：", 1)[1].split("\n", 1)[0])
        self.assertEqual(first_ids, ["0", "10", "20"])

    def test_empty_is_silent(self):
        result = deliver(self.store, report([]), CHAT_ID, self.sender)
        self.assertEqual(result["new_events"], 0)
        self.assertFalse(self.sent)

    def test_untrusted_names_are_plain_text(self):
        malicious = event(); malicious["drama_name"] = '<at id="all"></at> [link](https://bad.example)'
        card = split_cards(report([malicious]), [malicious], CHAT_ID)[0][1]
        self.assertTrue(all(e.get("text", {}).get("tag") != "lark_md" for e in card["elements"]))


class RunnerTests(unittest.TestCase):
    def test_preview_does_not_create_delivery_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            value = run_alerts(root=root, socialkit_root=root, now=NOW,
                               collector=lambda *args, **kwargs: report())
            self.assertEqual(value["mode"], "preview")
            self.assertFalse((Path(root) / "delivery.sqlite3").exists())
            self.assertTrue((Path(value["path"]) / "cards.json").exists())

    def test_historical_send_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "historical"):
            run_alerts(now=NOW, report_date="2026-11-02", send=True, clock=lambda: NOW)

    def test_midnight_uses_utc_day_not_beijing_day(self):
        bjt = timezone(timedelta(hours=8))
        before_cutover = datetime(2026, 11, 4, 7, 59, tzinfo=bjt)
        seen = []
        def read(*args, **kwargs):
            seen.append(args[3])
            return report([], args[3])
        with tempfile.TemporaryDirectory() as root:
            result = run_alerts(root=root, now=before_cutover, send=True, collector=read, clock=lambda: before_cutover)
        self.assertEqual(seen, [DAY])
        self.assertEqual(result["sent_cards"], 0)

    def test_day_rollover_during_collection_prevents_sending(self):
        wall = [NOW]
        def read(*args, **kwargs):
            wall[0] = NOW + timedelta(days=1)
            return report()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(collector.ComparisonUnavailable, "statistics_day_changed"):
                run_alerts(root=root, send=True, collector=read, clock=lambda: wall[0])
            self.assertFalse((Path(root) / "delivery.sqlite3").exists())

    def test_day_rollover_during_multipart_stops_next_card(self):
        wall = [NOW]
        sent = []
        def sender(*args):
            sent.append(args)
            wall[0] = NOW + timedelta(days=1)
            return {"message_id": "first"}
        with tempfile.TemporaryDirectory() as root:
            result = run_alerts(root=root, send=True, collector=lambda *args, **kwargs: report([event(str(i)) for i in range(11)]),
                                clock=lambda: wall[0], sender=sender)
        self.assertEqual(len(sent), 1)
        self.assertEqual(result["sent_cards"], 1)
        self.assertEqual(result["failed_cards"], 1)


if __name__ == "__main__":
    unittest.main()
