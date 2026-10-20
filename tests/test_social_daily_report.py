from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.post_daily_report.delivery import DefiniteFailure, DeliveryStore, DeliveryUnknown
from features.social_daily_report.collector import METRICS, metrics, present
from features.social_daily_report.delivery import NAMESPACE, SocialDeliveryStore, deliver
from features.social_daily_report.report import build_cards, money, payload_size, preview_html


DAY, CHAT = "2026-10-19", "test-chat"


def metric(revenue=0, published=0, **changes):
    value = dict.fromkeys(METRICS, 0)
    value.update(revenue_cents=revenue, published_count=published)
    value.update(changes)
    return value


def fixture():
    youtube = metric(91037, 21, installs=499, clicks=4047, views=5420)
    x = metric(78042, 5, installs=10, post_count=4, repost_count=1)
    total = {key: youtube[key] + x[key] for key in METRICS}
    return {
        "all_rows": [
            dict(owner="tenant:gaoyuan", owner_label="郜远", platform="x", **x),
            dict(owner="tenant:alice", owner_label="小明", platform="youtube", **youtube),
            dict(owner="tenant:historic", owner_label="历史内容发布人", platform="youtube",
                 **metric(0, 0)),
        ],
        "totals": total, "platform_totals": {"youtube": youtube, "x": x},
        "quality": {"platforms": {
            platform: dict(state="complete", source_totals=value, attributed_totals=value,
                           unmatched_totals=metric(), excluded_totals=metric())
            for platform, value in (("youtube", youtube), ("x", x))
        }},
    }


class PresentationTests(unittest.TestCase):
    def test_shared_values_preserved_in_cents(self):
        report = present(fixture(), DAY)
        self.assertEqual(report["totals"]["revenue_cents"], 169079)
        self.assertEqual(report["platforms"]["youtube"]["totals"]["revenue_cents"], 91037)
        self.assertEqual(report["rows"][0]["name"], "小明")
        self.assertEqual(len(report["rows"]), 3)  # keep zero-new-publication owners

    def test_person_rollup_uses_identity_not_name(self):
        shared = fixture()
        shared["all_rows"] = [
            dict(owner="a", owner_label="同名", platform="youtube", **metric(100, 0)),
            dict(owner="b", owner_label="同名", platform="x", **metric(200, 2)),
            dict(owner="a", owner_label="同名", platform="x", **metric(300, 1)),
        ]
        result = present(shared, DAY)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["revenue_cents"], 400)
        self.assertEqual(set(result["rows"][0]["platforms"]), {"youtube", "x"})

    def test_duplicate_group_is_rejected(self):
        shared = fixture()
        shared["all_rows"].append(shared["all_rows"][0])
        with self.assertRaisesRegex(ValueError, "duplicate_owner_platform"):
            present(shared, DAY)

    def test_same_user_id_in_different_tenants_stays_separate(self):
        shared = fixture()
        shared["all_rows"] = [
            dict(owner="user-1", owner_tenant="a", owner_label="同名", platform="youtube", **metric(100)),
            dict(owner="user-1", owner_tenant="b", owner_label="同名", platform="youtube", **metric(200)),
        ]
        report = present(shared, DAY)
        self.assertEqual(len(report["rows"]), 2)
        self.assertEqual({row["owner_tenant"] for row in report["rows"]}, {"a", "b"})

    def test_currency_is_exact_and_nonintegral_cents_rejected(self):
        self.assertEqual(metrics({"revenue": "12.34", "refunds": "0.29"})["revenue_cents"], 1234)
        self.assertEqual(money(-1), "-$0.01")
        self.assertEqual(money(169079), "$1,690.79")
        with self.assertRaises(ValueError):
            metrics({"revenue_cents": 1.5})

    def test_card_has_channels_attribution_unmatched_refunds_and_report_link(self):
        shared = fixture()
        shared["quality"]["platforms"]["x"]["unmatched_totals"] = metric(100, refund_cents=5)
        report = present(shared, DAY)
        cards = build_cards(report, CHAT)
        text = json.dumps(cards, ensure_ascii=False)
        for expected in ("社媒发布人日报", "YouTube", "X 渠道小计", "Post 4", "Repost 1", "campaign ID",
                         "待归属", "手动短链贡献效果", "social-analytics.html", "历史内容发布人"):
            self.assertIn(expected, text)
        self.assertLess(payload_size(cards[0], CHAT), 29000)
        self.assertIn("<!doctype html>", preview_html(cards))

    def test_missing_channel_is_not_zero(self):
        shared = fixture()
        shared["quality"]["platforms"]["x"]["state"] = "error"
        shared["platform_totals"]["x"] = dict.fromkeys(METRICS, None)
        shared["all_rows"][0].update(dict.fromkeys(METRICS, None))
        cards = build_cards(present(shared, DAY), CHAT)
        text = json.dumps(cards, ensure_ascii=False)
        self.assertEqual(cards[0]["header"]["template"], "orange")
        self.assertIn("部分渠道数据不完整", text)
        self.assertIn("X 渠道小计｜待核实／未同步", text)
        self.assertIn("收入 **待核实**", text)

    def test_partial_owner_totals_equal_shared_owner_group(self):
        from features.social_analytics.adapters import normalized
        from features.social_analytics.report import parameters, summarize
        catalog = [
            normalized(platform, dict(key=(platform, ""), link_id=platform, tenant="t", owner="a",
                       owner_label="Alice", channel="account", channel_label="Account", drama="1",
                       drama_label="Drama", language="en", link_type="auto", excluded=False, date=DAY))
            for platform in ("youtube", "x")
        ]
        values = [dict(platform="youtube", date=DAY, campaign_id="youtube", campaign="",
                       clicks=1, views=2, installs=1, conversions=0, revenue_cents=12345, refund_cents=0)]
        actor = {"role": "admin", "_system_report": True}
        base = {"start": [DAY], "end": [DAY]}
        shared = summarize(catalog, values, actor, parameters(dict(base, group_by=["owner,platform"])),
                           "snapshot", errors={"x": "unavailable"})
        webpage = summarize(catalog, values, actor, parameters(dict(base, group_by=["owner"])),
                            "snapshot", errors={"x": "unavailable"})
        daily = present(shared, DAY)
        self.assertEqual(daily["rows"][0]["revenue_cents"], 12345)
        for field in METRICS:
            self.assertEqual(daily["rows"][0][field], webpage["all_rows"][0][field], field)
        self.assertEqual(daily["rows"][0]["data_status"], "partial")
        self.assertIn("数据不完整，以下为已知部分", json.dumps(build_cards(daily, CHAT), ensure_ascii=False))

    def test_split_is_measured_using_actual_utf8_envelope(self):
        shared = fixture()
        shared["all_rows"] = [
            dict(owner="person-%d" % i, owner_label="多字节发布人😀" * 10,
                 platform="youtube", **metric(100))
            for i in range(180)
        ]
        cards = build_cards(present(shared, DAY), CHAT, limit=8000)
        self.assertGreater(len(cards), 1)
        self.assertTrue(all(payload_size(card, CHAT) <= 8000 for card in cards))
        self.assertTrue(all(("（%d/%d）" % (i, len(cards))) in card["header"]["title"]["content"]
                            for i, card in enumerate(cards, 1)))
        self.assertEqual(sum("多字节发布人" in item["text"]["content"]
                             for card in cards for item in card["elements"]), 180)

    def test_collector_calls_shared_campaign_report(self):
        import sys
        import types
        from features.social_daily_report.collector import collect
        captured = []
        def build_report(app, actor, query, **kwargs):
            captured.append((app, actor, query, kwargs))
            return fixture()
        routes = types.ModuleType("features.social_analytics.routes")
        routes.build_report = build_report
        with patch.dict(sys.modules, {"features.social_analytics.routes": routes}), \
             patch("features.social_daily_report.collector.read_settings", return_value={}):
            result = collect(DAY, "/main.sqlite", "/not-read.env", "/x.sqlite")
        self.assertEqual(result["totals"]["revenue_cents"], 169079)
        app, actor, query, options = captured[0]
        self.assertEqual(app["SOCIAL_ANALYTICS_X_DB_PATH"], "/x.sqlite")
        self.assertTrue(actor["_system_report"])
        self.assertEqual(query["group_by"], ["owner,platform"])
        self.assertEqual(query["start"], [DAY])
        self.assertFalse(options["include_titles"])

    def test_shared_backend_and_daily_reconcile_same_campaign_snapshot(self):
        from features.social_analytics.adapters import normalized
        from features.social_analytics.routes import build_report

        def catalog(platform, owner):
            return normalized(platform, {
                "key": ("same-campaign-id", "frozen old name"), "link_id": "link",
                "tenant": "tenant", "owner": owner, "owner_label": owner,
                "channel": "account", "channel_label": "Account", "drama": "42",
                "drama_label": "Drama", "language": "en", "language_label": "en",
                "link_type": "manual", "link_type_label": "手动短链", "date": DAY,
                "excluded": False, "publications": [],
            })
        catalogs = [catalog("youtube", "alice"), catalog("x", "gaoyuan")]
        snapshots = {
            platform: [dict(
                date=DAY, platform=platform, campaign_id="same-campaign-id",
                campaign="current name changed", clicks=10, views=15, installs=3,
                conversions=1, revenue_cents=cents, refund_cents=0,
            )] for platform, cents in (("youtube", 91037), ("x", 78042))
        }
        snapshots["x"].append(dict(snapshots["x"][0], campaign_id="UNKNOWN", revenue_cents=99))
        with patch("features.social_analytics.routes.read_catalogs", return_value=(catalogs, {})), \
             patch("features.social_analytics.routes.source",
                   side_effect=lambda app, start, end, platform: (snapshots[platform], "snapshot")):
            shared = build_report(
                {}, {"role": "admin", "_system_report": True},
                {"start": [DAY], "end": [DAY], "group_by": ["owner,platform"]},
            )
        report = present(shared, DAY)
        self.assertEqual(report["totals"]["revenue_cents"], 169079)
        self.assertEqual(report["totals"]["published_count"], 0)  # shortlinks never imply a post
        self.assertEqual(report["platforms"]["x"]["unmatched"]["revenue_cents"], 99)
        for platform, quality in shared["quality"]["platforms"].items():
            for field in ("revenue_cents", "refund_cents", "installs", "views", "clicks"):
                self.assertEqual(
                    quality["source_totals"][field],
                    report["platforms"][platform]["totals"][field]
                    + report["platforms"][platform]["unmatched"][field]
                    + report["platforms"][platform]["excluded"][field],
                )
        self.assertEqual({person["name"] for person in report["rows"]}, {"alice", "gaoyuan"})
        self.assertIn("$1,690.79", json.dumps(build_cards(report, CHAT), ensure_ascii=False))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "delivery.sqlite3"
        self.store = SocialDeliveryStore(self.path)
        self.cards = [{"card": 1}, {"card": 2}, {"card": 3}]

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def freeze(self):
        return self.store.freeze(DAY, CHAT, {"immutable": "original"}, self.cards)

    def test_legacy_sent_row_blocks_new_batch_without_mutation(self):
        legacy = DeliveryStore(self.path, namespace=NAMESPACE)
        legacy.claim(DAY, CHAT)
        legacy.finish(DAY, CHAT, "sent", "old-message")
        old = legacy.get(DAY, CHAT)
        legacy.close()
        sent = []
        result = deliver(self.store, DAY, CHAT, lambda *args: sent.append(args))
        self.assertEqual(result["status"], "already_sent")
        self.assertEqual(self.store.get(DAY, CHAT), old)
        self.assertFalse(sent)
        with self.assertRaises(DeliveryUnknown):
            self.freeze()

    def test_unknown_legacy_row_is_not_retried(self):
        self.store.claim(DAY, CHAT)
        self.store.finish(DAY, CHAT, "unknown")
        with self.assertRaisesRegex(DeliveryUnknown, "prior_delivery_unconfirmed"):
            deliver(self.store, DAY, CHAT, lambda *args: self.fail("must not send"))

    def test_frozen_batch_detects_changed_cards(self):
        self.freeze()
        with self.assertRaisesRegex(DeliveryUnknown, "payload_changed"):
            self.store.freeze(DAY, CHAT, {}, [{"different": True}])
        self.assertEqual(self.store.batch(DAY, CHAT)["report"], {"immutable": "original"})

    def test_all_fragments_have_stable_unique_uuids_and_receipts(self):
        self.freeze()
        requests = []
        def send(card, request_uuid):
            requests.append(request_uuid)
            return {"message_id": "msg-%d" % card["card"]}
        result = deliver(self.store, DAY, CHAT, send, sleep=lambda _: None)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(set(requests)), 3)
        self.assertTrue(all(part["status"] == "sent" for part in result["parts"]))
        deliver(self.store, DAY, CHAT, lambda *args: self.fail("no repeat"))

    def test_definite_failure_retries_only_that_fragment_same_uuid(self):
        self.freeze()
        calls = []
        def send(card, request_uuid):
            calls.append((card["card"], request_uuid))
            if card["card"] == 2 and sum(c[0] == 2 for c in calls) == 1:
                raise DefiniteFailure("known_rejection")
            return {"message_id": "msg-%d" % card["card"]}
        deliver(self.store, DAY, CHAT, send, sleep=lambda _: None)
        self.assertEqual([i for i, _ in calls], [1, 2, 2, 3])
        self.assertEqual(calls[1][1], calls[2][1])
        self.assertEqual(self.store.part(DAY, CHAT, 2)["attempts"], 2)

    def test_resume_skips_previously_sent_parts_and_reuses_frozen_report(self):
        self.freeze()
        self.store.claim(DAY, CHAT)
        self.store.claim_part(DAY, CHAT, 1, self.cards[0])
        self.store.finish_part(DAY, CHAT, 1, "sent", "already-confirmed")
        self.store.claim_part(DAY, CHAT, 2, self.cards[1])
        self.store.finish_part(DAY, CHAT, 2, "failed", detail="known_rejection")
        self.store.finish(DAY, CHAT, "failed", detail="known_rejection")
        calls = []
        def send(card, request_uuid):
            calls.append(card["card"])
            return {"message_id": "new-%d" % card["card"]}
        deliver(self.store, DAY, CHAT, send, sleep=lambda _: None)
        self.assertEqual(calls, [2, 3])
        self.assertEqual(self.store.part(DAY, CHAT, 1)["message_id"], "already-confirmed")
        self.assertEqual(self.store.batch(DAY, CHAT)["report"], {"immutable": "original"})

    def test_unknown_fragment_blocks_whole_batch_and_remaining_parts(self):
        self.freeze()
        calls = []
        def send(card, request_uuid):
            calls.append(card["card"])
            if card["card"] == 2:
                raise TimeoutError()
            return {"message_id": "first-confirmed"}
        with self.assertRaises(DeliveryUnknown):
            deliver(self.store, DAY, CHAT, send, sleep=lambda _: None)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(self.store.get(DAY, CHAT)["status"], "unknown")
        self.assertEqual(self.store.part(DAY, CHAT, 2)["status"], "unknown")
        self.assertIsNone(self.store.part(DAY, CHAT, 3))
        with self.assertRaises(DeliveryUnknown):
            deliver(self.store, DAY, CHAT, lambda *args: self.fail("must not retry"))

    def test_crash_after_claim_keeps_sending_hold(self):
        self.freeze()
        with self.assertRaises(KeyboardInterrupt):
            deliver(self.store, DAY, CHAT, lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(self.store.get(DAY, CHAT)["status"], "sending")
        self.assertEqual(self.store.part(DAY, CHAT, 1)["status"], "sending")
        with self.assertRaises(DeliveryUnknown):
            deliver(self.store, DAY, CHAT, lambda *args: self.fail("must not retry"))


class RunnerTests(unittest.TestCase):
    def test_old_sent_archive_remains_unchanged_and_collection_never_runs(self):
        from scripts.social_publisher_daily_report import CHAT_ID, main
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            archive = path / "reports" / "2020-01-01"
            archive.mkdir(parents=True)
            saved = archive / "report.json"
            saved.write_text('{"old":"immutable"}', encoding="utf-8")
            store = DeliveryStore(path / "delivery.sqlite3", namespace=NAMESPACE)
            store.claim("2020-01-01", CHAT_ID)
            store.finish("2020-01-01", CHAT_ID, "sent", "old-report")
            prior = store.get("2020-01-01", CHAT_ID)
            store.close()
            with patch("scripts.social_publisher_daily_report.collect", side_effect=AssertionError("no collect")), \
                 patch("scripts.social_publisher_daily_report.send_card", side_effect=AssertionError("no send")):
                self.assertEqual(main(["--date", "2020-01-01", "--send", "--state-dir", folder]), 0)
            self.assertEqual(saved.read_text(encoding="utf-8"), '{"old":"immutable"}')
            store = SocialDeliveryStore(path / "delivery.sqlite3")
            self.assertEqual(store.get("2020-01-01", CHAT_ID), prior)
            store.close()

    def test_historical_missing_report_cannot_be_sent(self):
        from scripts.social_publisher_daily_report import main
        with tempfile.TemporaryDirectory() as folder, \
             patch("scripts.social_publisher_daily_report.collect", side_effect=AssertionError("no collect")):
            with self.assertRaisesRegex(RuntimeError, "historical_delivery_not_enabled"):
                main(["--date", "2020-01-01", "--send", "--state-dir", folder])

    def test_preview_never_creates_delivery_ledger(self):
        from scripts.social_publisher_daily_report import main
        with tempfile.TemporaryDirectory() as folder, \
             patch("scripts.social_publisher_daily_report.collect", return_value=present(fixture(), DAY)), \
             patch("scripts.social_publisher_daily_report.send_card", side_effect=AssertionError("no send")):
            main(["--date", DAY, "--preview", "--state-dir", folder])
            self.assertFalse((Path(folder) / "delivery.sqlite3").exists())
            self.assertTrue((Path(folder) / "previews" / DAY / "preview.html").exists())


if __name__ == "__main__":
    unittest.main()
