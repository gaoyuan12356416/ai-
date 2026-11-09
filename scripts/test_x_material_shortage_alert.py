import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

from features.x_posts.material_shortage import EVENT, snapshot, emit, observe_rejections
from features.x_material_alert import AlertStore, build_card, event_key, valid_event
from features.post_daily_report.delivery import DefiniteFailure, DeliveryUnknown


def example(time="03:21"):
    return {"event": EVENT, "run_date": "2026-11-08", "publish_time": time, "config_version": 35,
            "scanned": 1000, "scan_limit_reached": True,
            "languages": [{"language": "en", "required": 10, "matched": 3, "missing": 7,
                           "rejections": {"x_long_video_requires_premium": 20}}]}


def config():
    return {"start_at": "2026-01-01T00:00:00+00:00", "chat_id": "test_chat", "mention_id": "ou_test", "feishu_config": "unused"}


def entry(event, cursor="c1"):
    return {"MESSAGE": json.dumps(event), "__CURSOR": cursor, "__REALTIME_TIMESTAMP": "1794100000000000"}


class ShortageTests(unittest.TestCase):
    def test_count_usable_accounts_and_frozen_selection(self):
        accounts = [{"id": 1, "drama_language": "en"}, {"id": 2, "drama_language": "en"},
                    {"id": 3, "drama_language": "jp"}]
        result = snapshot(accounts, [{"account_id": 1}], scanned=1000)
        self.assertEqual([(r["language"], r["missing"]) for r in result["languages"]], [("en", 1), ("ja", 1)])
        self.assertTrue(result["scan_limit_reached"])
        self.assertEqual(snapshot(accounts, [{"account_id": a["id"]} for a in accounts])["languages"], [])

    def test_empty_pool_and_language_specific_reason(self):
        totals = {}
        observe_rejections(totals, [{"material_id": "a", "material_language": "ja"}],
                           [{"material_id": "a", "error_code": "x_long_video_requires_premium"}])
        result = snapshot([{"id": 1, "drama_language": "ja"}], [], rejections=totals)
        self.assertEqual(result["languages"][0]["missing"], 1)
        self.assertEqual(result["languages"][0]["rejections"]["x_long_video_requires_premium"], 1)

    def test_emit_is_material_only_and_never_blocks(self):
        identity = {"source_type": "material", "run_date": "2026-11-08", "publish_time": "03:21", "version": 35}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            emit(identity, {"languages": []})
            emit({**identity, "source_type": "drama"}, example())
            self.assertEqual(output.getvalue(), "")
            emit(identity, {"languages": example()["languages"]})
        self.assertTrue(valid_event(json.loads(output.getvalue())))
        with mock.patch("builtins.print", side_effect=OSError("closed")):
            emit(identity, example())

    def test_card_exact_counts_mention_and_duration_guidance(self):
        text = json.dumps(build_card(example(), "ou_test"), ensure_ascii=False)
        self.assertIn("英语", text)
        self.assertIn("缺 7 条", text)
        self.assertIn("优先补充20分钟内视频", text)
        self.assertIn("ou_test", text)
        self.assertNotIn("551", text)
        bad = example(); bad["languages"][0]["missing"] = 8
        self.assertFalse(valid_event(bad))

    def test_replayed_journal_and_repeated_send_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as cleanup:
            store = AlertStore(Path(folder) / "alerts.sqlite3")
            cleanup.callback(store.close)
            self.assertEqual(store.ingest([entry(example())], config())["queued"], 1)
            self.assertEqual(store.ingest([entry(example())], config())["queued"], 0)
            self.assertEqual(store.cursor(), "c1")
            send = mock.Mock(return_value={"message_id": "message1"})
            self.assertEqual(store.deliver(config(), send)[0]["status"], "sent")
            self.assertEqual(store.deliver(config(), send), [])
            self.assertEqual(send.call_count, 1)

    def test_unknown_is_not_retried_and_does_not_block_other_event(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as cleanup:
            store = AlertStore(Path(folder) / "alerts.sqlite3"); cleanup.callback(store.close)
            store.ingest([entry(example()), entry(example("05:21"), "c2")], config())
            send = mock.Mock(side_effect=[DeliveryUnknown("timeout"), {"message_id": "message2"}])
            self.assertEqual([r["status"] for r in store.deliver(config(), send)], ["needs_reconciliation", "sent"])
            store.deliver(config(), send)
            self.assertEqual(send.call_count, 2)

    def test_explicit_rejection_retries_with_original_uuid(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as cleanup:
            store = AlertStore(Path(folder) / "alerts.sqlite3"); cleanup.callback(store.close)
            store.ingest([entry(example())], config())
            send = mock.Mock(side_effect=[DefiniteFailure("feishu_code_1"), {"message_id": "message1"}])
            store.deliver(config(), send); store.deliver(config(), send)
            self.assertEqual(send.call_args_list[0].args[-1], send.call_args_list[1].args[-1])

    def test_historical_events_are_not_backfilled(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as cleanup:
            store = AlertStore(Path(folder) / "alerts.sqlite3"); cleanup.callback(store.close)
            later = {**config(), "start_at": "2026-11-10T00:00:00+00:00"}
            self.assertEqual(store.ingest([entry(example())], later)["queued"], 0)

    def test_real_empty_pool_loader_preserves_exception_and_observation(self):
        from scripts.x_post_schedule_runner import _material_candidates, ScheduleRunError
        from scripts.test_x_post_schedule_runner import make_config
        sidecar = mock.Mock(); sidecar.available_pool_items.return_value = []
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as cleanup:
            with self.assertRaises(ScheduleRunError) as caught:
                _material_candidates(make_config(folder), sidecar, [{"id": 1, "drama_language": "en"}],
                                     source_date="2026-11-07", connection_factory=None,
                                     downloader=None, prober=None, repair_client=None, timestamp=1)
        self.assertEqual(caught.exception.code, "x_post_schedule_material_shortage")
        self.assertEqual(caught.exception.shortage_snapshot["languages"][0]["missing"], 1)

    def test_runner_announces_only_after_confirmed_partial_reservation(self):
        from scripts.x_post_schedule_runner import execute_schedule_tick, MaterialScheduleCandidates
        from scripts.x_post_daily_runner import SidecarError
        from scripts.test_x_post_schedule_runner import make_config, FakeSidecar, due_item
        def loader(_config, _sidecar, accounts, *, source_date, **kwargs):
            selected = [{"account_id": accounts[0]["id"], "source_date": source_date, "material_id": "9001"}]
            return MaterialScheduleCandidates(selected, shortage_snapshot=snapshot(accounts, selected))
        for rejected in (False, True):
            with self.subTest(rejected=rejected), tempfile.TemporaryDirectory() as folder:
                sidecar = FakeSidecar([due_item()])
                if rejected:
                    sidecar.create_error = SidecarError("x_post_pool_fifo_conflict", "conflict", status=409)
                with mock.patch("scripts.x_post_schedule_runner.DEFAULT_WORK_DIR", str(Path(folder).resolve())), contextlib.redirect_stdout(io.StringIO()) as output:
                    execute_schedule_tick(make_config(folder), sidecar=sidecar, material_candidate_loader=loader,
                                          now=datetime(2026, 7, 27, 10, 0, 30, tzinfo=timezone(timedelta(hours=8))))
                if rejected:
                    self.assertEqual(output.getvalue(), "")
                else:
                    self.assertEqual(json.loads(output.getvalue())["languages"][0]["missing"], 1)
                    self.assertEqual(len([c for c in sidecar.calls if c[0] == "publish"]), 1)

    def test_runner_zero_candidate_failure_still_announces_without_publication(self):
        from scripts.x_post_schedule_runner import execute_schedule_tick, ScheduleRunError
        from scripts.test_x_post_schedule_runner import make_config, FakeSidecar, due_item
        def loader(_config, _sidecar, accounts, **kwargs):
            exc = ScheduleRunError("empty", "x_post_schedule_material_shortage")
            exc.shortage_snapshot = snapshot(accounts, [])
            raise exc
        with tempfile.TemporaryDirectory() as folder:
            sidecar = FakeSidecar([due_item()])
            with mock.patch("scripts.x_post_schedule_runner.DEFAULT_WORK_DIR", str(Path(folder).resolve())), contextlib.redirect_stdout(io.StringIO()) as output:
                execute_schedule_tick(make_config(folder), sidecar=sidecar, material_candidate_loader=loader,
                                      now=datetime(2026, 7, 27, 10, 0, 30, tzinfo=timezone(timedelta(hours=8))))
            self.assertEqual(json.loads(output.getvalue())["languages"][0]["missing"], 2)
            self.assertFalse(any(c[0] in ("create", "publish") for c in sidecar.calls))


if __name__ == "__main__":
    unittest.main()
