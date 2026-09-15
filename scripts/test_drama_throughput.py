"""Offline queue recovery, lookahead fencing and verified-download reuse tests."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from features.drama_synthesis import async_runtime, app_support, media_pipeline, prefetch
from scripts import test_drama_synthesis_gpu_runtime as gpu_fixtures
from scripts import test_drama_synthesis_remote_client as cpu_fixtures
from scripts import test_drama_synthesis_media_pipeline as media_fixtures


class ObserverTests(unittest.TestCase):
    setUp = cpu_fixtures.RuntimeTests.setUp
    tearDown = cpu_fixtures.RuntimeTests.tearDown
    fake_worker = cpu_fixtures.RuntimeTests.fake_worker
    job = cpu_fixtures.RuntimeTests.job
    sql = cpu_fixtures.RuntimeTests.sql

    def add_queued(self, job_id):
        self.sql("INSERT INTO drama_material_job(job_id,status,progress,outputs_json) VALUES(?,'queued',2,'{}')", (job_id,))

    def test_interrupted_render_precedes_more_than_fifty_old_queued_jobs(self):
        worker, _ = self.fake_worker()
        self.sql("UPDATE drama_material_job_worker_lease SET status='interrupted'")
        self.sql("UPDATE drama_material_job SET updated_at='2099-01-01 00:00:00'")
        for i in range(60):
            self.add_queued("queued-%02d" % i)
        self.assertEqual(worker.claim_next_job(), cpu_fixtures.JOB)

    def test_fresh_lease_stays_owned_while_next_job_can_be_claimed(self):
        worker, _ = self.fake_worker()
        self.sql("UPDATE drama_material_job_worker_lease SET heartbeat_at=?", (worker.now_text(),))
        self.add_queued("next")
        self.assertEqual(worker.claim_next_job(), "next")
        self.assertIsNone(worker.claim_next_job())

    def test_two_observers_fill_only_two_slots_and_stop_together(self):
        worker, _ = self.fake_worker()
        self.sql("UPDATE drama_material_job_worker_lease SET status='interrupted'")
        self.add_queued("next")
        self.add_queued("third")
        started, ready = [], threading.Event()
        mutex = threading.Lock()

        def observe(job):
            with mutex:
                started.append(job)
                if len(started) == 2:
                    ready.set()
            worker.STOP_EVENT.wait(3)

        with mock.patch.object(worker, "run_claimed_job", side_effect=observe):
            runner = threading.Thread(target=lambda: worker.observe_jobs(observers=2))
            runner.start()
            try:
                self.assertTrue(ready.wait(3))
                self.assertEqual(set(started), {cpu_fixtures.JOB, "next"})
            finally:
                worker.STOP_EVENT.set()
                runner.join(3)
            self.assertFalse(runner.is_alive())

    def test_once_keeps_its_one_job_contract(self):
        worker, _ = self.fake_worker()
        with mock.patch.object(worker, "claim_next_job", return_value="one") as claim, mock.patch.object(worker, "run_claimed_job") as run:
            worker.observe_jobs(once=True, observers=2)
        claim.assert_called_once_with()
        run.assert_called_once_with("one")

    def test_observer_configuration_rejects_unbounded_concurrency(self):
        worker, _ = self.fake_worker()
        for value in ("0", "3", "eight", "2.0"):
            with mock.patch.dict("os.environ", {"DRAMA_JOB_WORKER_OBSERVERS": value}), self.assertRaises(ValueError):
                worker.observer_count()


class PrefetchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def runtime(self, execute, prepare):
        value = async_runtime.AsyncRuntime(self.directory.name, execute, lambda _: None, prefetch=prepare)
        self.addCleanup(value.close, 3)
        return value

    def test_only_next_job_prefetches_during_render_and_never_uses_render_slot(self):
        entered, render, finish = threading.Event(), threading.Event(), threading.Event()
        prepared = []

        def execute(payload):
            if payload["job_id"] == "first":
                async_runtime.emit_progress("downloading")
                entered.set()
                render.wait(3)
                async_runtime.emit_progress("rendering_random")
                finish.wait(3)
            return gpu_fixtures.result_for(payload)

        def prepare(payload, **kwargs):
            prepared.append(payload["job_id"])
            kwargs["progress_callback"](downloaded_bytes=10, completed_episodes=1, total_episodes=2)

        value = self.runtime(execute, prepare)
        for job in ("first", "next", "third"):
            value.submit(gpu_fixtures.render_payload(job))
        self.assertTrue(entered.wait(3))
        self.assertFalse(value._prefetch_allowed())
        self.assertEqual(prepared, [])
        render.set()
        gpu_fixtures.wait_for(lambda: value.get("next")["stage"] == "prefetched")
        self.assertEqual(prepared, ["next"])
        self.assertEqual(value.get("next")["status"], "queued")
        self.assertFalse(value.render_slots.acquire(blocking=False))
        self.assertNotIn("private-source", json.dumps(value.get("next")))
        finish.set()
        gpu_fixtures.wait_for(lambda: value.get("third")["status"] == "completed")

    def test_render_handoff_cancels_prefetch_before_same_job_execution(self):
        prepared, finish = threading.Event(), threading.Event()
        events = []

        def execute(payload):
            events.append("render-" + payload["job_id"])
            if payload["job_id"] == "first":
                async_runtime.emit_progress("concatenating")
                finish.wait(3)
            return gpu_fixtures.result_for(payload)

        def prepare(payload, **kwargs):
            events.append("prefetch-start")
            prepared.set()
            if not kwargs["stop_event"].wait(3):
                raise AssertionError("render handoff did not cancel lookahead")
            events.append("prefetch-stop")

        value = self.runtime(execute, prepare)
        value.submit(gpu_fixtures.render_payload("first"))
        value.submit(gpu_fixtures.render_payload("next"))
        self.assertTrue(prepared.wait(3))
        finish.set()
        gpu_fixtures.wait_for(lambda: value.get("next")["status"] == "completed")
        self.assertLess(events.index("prefetch-stop"), events.index("render-next"))
        self.assertEqual(events.count("render-next"), 1)

    def test_prefetch_failure_is_optional_and_not_retried_or_published(self):
        finish = threading.Event()
        calls = []

        def execute(payload):
            if payload["job_id"] == "first":
                async_runtime.emit_progress("rendering_random")
                finish.wait(3)
            return gpu_fixtures.result_for(payload)

        def prepare(payload, **_kwargs):
            calls.append(payload["job_id"])
            raise ValueError("private source token")

        value = self.runtime(execute, prepare)
        value.submit(gpu_fixtures.render_payload("first"))
        value.submit(gpu_fixtures.render_payload("next"))
        gpu_fixtures.wait_for(lambda: calls == ["next"] and value.get("next")["stage"] == "queued")
        self.assertEqual(value.get("next")["status"], "queued")
        self.assertNotIn("error", value.get("next"))
        self.assertNotIn("private source token", json.dumps(value.get("next")))
        finish.set()
        gpu_fixtures.wait_for(lambda: value.get("next")["status"] == "completed")
        self.assertEqual(calls, ["next"])

    def test_shutdown_keeps_owner_lock_until_prefetch_drains(self):
        prepared, release = threading.Event(), threading.Event()

        def execute(payload):
            async_runtime.emit_progress("rendering_random")
            release.wait(3)
            return gpu_fixtures.result_for(payload)

        def prepare(payload, **kwargs):
            prepared.set()
            kwargs["stop_event"].wait(3)
            release.wait(3)

        value = self.runtime(execute, prepare)
        value.submit(gpu_fixtures.render_payload("first"))
        value.submit(gpu_fixtures.render_payload("next"))
        self.assertTrue(prepared.wait(3))
        self.assertFalse(value.close(timeout=0.01))
        with self.assertRaises(Exception):
            async_runtime.AsyncRuntime(self.directory.name, execute, lambda _: None)
        release.set()
        self.assertTrue(value.close(3))


class PrefetchDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.payload = gpu_fixtures.render_payload("download-reuse")

    def prepare(self, **kwargs):
        return prefetch.prefetch_episodes(self.payload, self.directory.name, stop_event=threading.Event(),
                                         min_free_bytes=0, **kwargs)

    def test_prefetch_checkpoints_are_reused_by_normal_downloader_without_network(self):
        sessions = []

        def downloader(url, path, route, callback, **kwargs):
            session = media_fixtures.Session([media_fixtures.Response([media_fixtures.BODY])])
            sessions.append(session)
            return media_pipeline.download_episode_with_route(
                url, path, route, callback, session_factory=lambda: session, **kwargs)

        result = self.prepare(downloader=downloader, workers=2)
        self.assertEqual(result["completed_episodes"], 2)
        for item in self.payload["episodes"]:
            target = Path(self.directory.name) / self.payload["job_id"] / "downloads" / ("%03d.mp4" % item["episode_number"])
            session = media_fixtures.Session([])
            reused = media_pipeline.download_episode(item["episode_url"], target, session_factory=lambda: session)
            self.assertTrue(reused["reused"])
            self.assertEqual(session.calls, [])

    def test_disk_reserve_rejects_before_network(self):
        downloader = mock.Mock()
        with self.assertRaises(prefetch.PrefetchStopped):
            prefetch.prefetch_episodes(self.payload, self.directory.name, stop_event=threading.Event(),
                                      min_free_bytes=100, disk_usage=lambda _: SimpleNamespace(free=99), downloader=downloader)
        downloader.assert_not_called()

    def test_total_source_size_cap_stops_all_workers_before_body_transfer(self):
        def downloader(url, path, route, callback, **kwargs):
            callback(0, 100)
            raise AssertionError("body must not be requested after budget exceeded")
        with self.assertRaises(prefetch.PrefetchStopped):
            self.prepare(workers=1, max_bytes=99, downloader=downloader)

    def test_prefetch_progress_is_displayed_as_waiting_not_rendering(self):
        for stage in ("prefetching", "prefetched"):
            view = app_support.remote_display({"status": "queued", "stage": stage,
                "metrics": {"completed_episodes": 1, "total_episodes": 2, "downloaded_bytes": 1000}})
            self.assertEqual(view["status"], "queued")
            self.assertEqual(view["stage_percent"], 50.0)
            self.assertIn("已下载 1/2 集", view["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
