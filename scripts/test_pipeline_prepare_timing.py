"""Preparation admission preserves each channel's original scheduling rules."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.pipeline_dispatch.adapters import AdapterError, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore, stamp
from features.pipeline_dispatch.service import build_registry
from features.pipeline_dispatch.tt_native import TTNativeBridge
from scripts import test_tt_auto_post_publisher as fixtures


class PreparationTimingTests(unittest.TestCase):
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)

    def adapter(self, channel, family, **policy):
        return LedgerAdapter(LedgerSpec(channel, family, "unused", "unused"),
                             driver=object(), preparation_policy=policy)

    def row(self, **values):
        return dict(id=1, status="planned", account_id="1", page_id="1",
                    scheduled_at="2026-11-03T12:00:00Z", created_at="2026-11-02T10:00:00Z", **values)

    def test_fb_frozen_future_work_prepares_now_but_publishes_only_at_due_time(self):
        for family in ("auto", "video"):
            adapter = self.adapter("fb", family)
            row = self.row()
            if family == "video":
                row.update(status="queued", prepare_state="planned", processing_mode="random_overlay")
            prep = adapter.project(row, self.now)
            self.assertEqual(prep["stage"], "prepare")
            self.assertEqual(prep["scheduled_at"], "2026-11-02T10:00:00Z")
            self.assertEqual(prep["cutover_due_at"], row["scheduled_at"])
            row.update(status="ready", prepare_state="ready", prepared_media_url="https://media.example/done.mp4", link_state="ready")
            publication = adapter.project(row, self.now)
            self.assertEqual(publication["stage"], "publish")
            self.assertEqual(publication["scheduled_at"], row["scheduled_at"])

    def test_retry_times_and_tt_four_hour_selection_window_are_preserved(self):
        row = self.row()
        row.update(status="pending", scheduled_at="2026-11-02T15:00:00Z")
        adapter = self.adapter("tt", "auto", tt_auto_prepare_ahead_seconds=14400)
        prep = adapter.project(row, self.now)
        self.assertEqual(prep["scheduled_at"], "2026-11-02T11:00:00Z")
        self.assertEqual(prep["cutover_due_at"], "2026-11-02T15:00:00Z")
        row.update(status="reserved", next_attempt_at_utc="2026-11-02T12:05:00Z")
        self.assertEqual(adapter.project(row, self.now)["scheduled_at"], row["next_attempt_at_utc"])
        for family, key in (("auto", "next_prepare_at_utc"), ("video", "prepare_retry_at")):
            item = self.row(**{key: "2026-11-02T12:10:00Z"})
            self.assertEqual(self.adapter("fb", family).project(item, self.now)["scheduled_at"], item[key])

    def test_x_and_tt_ready_publication_are_never_advanced(self):
        row = self.row()
        row["status"] = "pending"
        self.assertEqual(self.adapter("x", "auto").project(row, self.now)["scheduled_at"], row["scheduled_at"])
        row.update(status="ready", prepared_media_url="https://media.example/done.mp4")
        offered = self.adapter("tt", "auto", tt_auto_prepare_ahead_seconds=14400).project(row, self.now)
        self.assertEqual(offered["stage"], "publish")
        self.assertEqual(offered["scheduled_at"], row["scheduled_at"])

    def test_explicit_tt_policy_is_required_for_live_bridge_and_validated(self):
        config = {"paths": {"tt_auto": "unused"}, "drivers": [{"channel": "tt", "families": ["auto"],
                  "port": 18831, "prefix": "/internal/pipeline-dispatch/tt", "token_env": "TEST_TT_TOKEN"}]}
        with patch.dict("os.environ", {"TEST_TT_TOKEN": "x" * 32}):
            with self.assertRaisesRegex(ValueError, "explicit TT auto"):
                build_registry(config)
            config["preparation_policy"] = {"tt_auto_prepare_ahead_seconds": 14400}
            self.assertEqual(build_registry(config).adapters[("tt", "auto")].preparation_policy, config["preparation_policy"])
            config["preparation_policy"]["tt_auto_prepare_ahead_seconds"] = 43201
            with self.assertRaisesRegex(AdapterError, "tt_auto_prepare_ahead_invalid"):
                build_registry(config)

    def test_existing_unclaimed_preparation_advances_without_pausing_future_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DispatchStore(Path(directory) / "dispatch.sqlite3", clock=lambda: self.now)
            store.register_node("cpu-main", ["prepare:fb", "publish:fb"])
            store.worker_heartbeat("cpu-main", "prepare", "prepare")
            store.worker_heartbeat("cpu-main", "publish", "publish")
            store.channel_enabled("fb", True)
            offer = self.adapter("fb", "auto").project(self.row(), self.now)
            old = dict(offer, scheduled_at=offer["cutover_due_at"])
            prior = store.offer(old)
            store.cutover("fb", stamp(self.now))
            self.assertEqual(store.get_job(prior["id"])["status"], "queued")
            fresh = store.offer(offer)
            self.assertLess(fresh["scheduled_at"], stamp(self.now))
            claim = store.claim("cpu-main", "prepare", "prepare")["job"]
            self.assertEqual(claim["id"], prior["id"])
            # Once claimed, an observation cannot move or reset its generation.
            self.assertEqual(store.offer(dict(offer, scheduled_at="2026-11-01T00:00:00Z"))["scheduled_at"], fresh["scheduled_at"])
            store.complete(claim, "prepared", {"artifact": {
                "url": "https://media.example/done.mp4", "sha256": "a" * 64, "size": 100}}, canonical_verified=True)
            publish = dict(offer, stage="publish", scheduled_at=offer["cutover_due_at"], capabilities=["publish:fb"])
            store.offer(publish)
            self.assertIsNone(store.claim("cpu-main", "publish", "publish")["job"])
            # A held historical task can never be reopened by eligibility refresh.
            held = dict(offer, source_key="fb:auto:2", source_id="2", status="backlog_paused")
            saved = store.offer(held)
            self.assertEqual(store.offer(dict(held, status="queued", scheduled_at="2026-11-01T00:00:00Z"))["status"], "backlog_paused")
            self.assertEqual(store.get_job(saved["id"])["scheduled_at"], stamp(held["scheduled_at"]))

    def test_tt_native_uses_actual_executor_window_but_keeps_publish_due_guard(self):
        fixture = fixtures.TTAutoPostPublisherIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task = fixture.reserved_task(suffix="ahead")
        gpu = fixtures.FakeGPU()
        executor = fixture.executor(gpu)
        executor.prepare_ahead_seconds = 14400
        bridge = TTNativeBridge(SimpleNamespace(store=fixture.store, executor=executor), automatic=True)
        job = {"source_key": "tt:auto:" + str(task.id), "source_id": str(task.id), "channel": "tt", "family": "auto", "stage": "prepare", "generation": 1}
        with bridge.connect() as conn:
            conn.execute("UPDATE tt_auto_task SET status='pending',scheduled_at_utc=? WHERE id=?", ((fixture.clock() + timedelta(hours=2)).isoformat(), task.id))
        with patch.object(executor, "_select", side_effect=RuntimeError("selection_boundary")) as select:
            with self.assertRaisesRegex(RuntimeError, "selection_boundary"):
                bridge.reserve(job, {"claim_token": "a" * 32})
            select.assert_called_once()
        with bridge.connect() as conn:
            conn.execute("UPDATE tt_auto_task SET status='ready',claim_token='',claim_phase='',lease_expires_at_utc='' WHERE id=?", (task.id,))
        with self.assertRaisesRegex(AdapterError, "canonical_task_not_claimable"):
            bridge.reserve(dict(job, stage="publish", generation=2), {"claim_token": "b" * 32})
        self.assertEqual(gpu.prepare_calls, [])
        self.assertEqual(gpu.publish_calls, [])


if __name__ == "__main__":
    unittest.main()
