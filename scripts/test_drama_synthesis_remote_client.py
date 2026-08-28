"""CPU-only reconnect/fencing tests: no application import, video or network."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import requests

from features.drama_synthesis.async_runtime import render_fingerprint
from features.drama_synthesis.core import DramaSynthesisError, DramaSynthesisStore, RECIPE_PROFILE, RECIPE_VERSION
from features.drama_synthesis import cpu_runtime as runtime
from features.drama_synthesis.remote_client import (
    RemoteJobConflict, RemoteJobFailed, RemotePollingInterrupted, RemoteRecoveryRequired,
    wait_for_gpu_job,
)


ROOT = Path(__file__).resolve().parents[1]
JOB = "0123456789abcdef0123456789abcdef"
URL = "https://media.example.test/material.mp4"
BASE = "http://127.0.0.1:18788"
PAYLOAD = {
    "job_id": JOB, "content_id": "test-drama", "episode_start": 1, "episode_end": 1,
    "outputs": {"concat_video": True, "no_bgm_video": False, "random_template_video": False},
    "episodes": [{"episode_number": 1, "episode_url": "https://source.example.test/one.mp4"}],
}


@contextmanager
def database(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def snapshot(status="running", generation=1, **changes):
    value = {
        "job_id": JOB, "fingerprint": render_fingerprint(PAYLOAD), "generation": generation,
        "status": status, "stage": "downloading" if status == "running" else status,
        "progress": {"bytes_done": 10, "bytes_total": 100},
        "started_at": "2000-01-01T00:00:00Z", "heartbeat_at": "2000-01-01T00:00:20Z",
        "last_progress_at": "2000-01-01T00:00:10Z",
    }
    if status == "completed":
        value["result"] = {"job_id": JOB, "output_video_url": URL}
    if status == "failed":
        value["error"] = {"code": "gpu_render_failed", "message": "unsafe URL/token must not be forwarded"}
    value.update(changes)
    return value


class FakeStop:
    def __init__(self, stop_after=None):
        self.seconds = 0
        self.waits = 0
        self.stop_after = stop_after

    def is_set(self):
        return self.stop_after is not None and self.waits >= self.stop_after

    def wait(self, seconds):
        self.seconds += seconds
        self.waits += 1
        return self.is_set()


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self.data = status, data
        self.closed = False

    def json(self):
        return deepcopy(self.data)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []
        self.responses = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        if not self.rows:
            raise AssertionError("unexpected extra request")
        expected, result = self.rows.pop(0)
        if expected != method:
            raise AssertionError("expected %s, got %s" % (expected, method))
        if isinstance(result, Exception):
            raise result
        response = FakeResponse(*result)
        self.responses.append(response)
        return response


class ClientTests(unittest.TestCase):
    def run_client(self, rows, **kwargs):
        session = FakeSession(rows)
        result = wait_for_gpu_job(BASE, "synthetic-token", PAYLOAD, session=session,
                                  stop_event=kwargs.pop("stop_event", FakeStop()), **kwargs)
        self.assertFalse(session.rows)
        self.assertTrue(all(response.closed for response in session.responses))
        for _, _, params in session.calls:
            self.assertEqual(params["timeout"], (3, 15))
            self.assertFalse(params["allow_redirects"])
        return result, session

    def test_more_than_four_hours_never_resubmits_or_fails_a_running_job(self):
        stop = FakeStop()
        rows = [("GET", (404, {"code": "gpu_job_not_found"})), ("POST", (202, snapshot()))]
        rows.extend(("GET", (200, snapshot())) for _ in range(1500))
        rows.append(("GET", (200, snapshot("completed"))))
        result, session = self.run_client(rows, stop_event=stop)
        self.assertGreater(stop.seconds, 14400)
        self.assertEqual(result["output_video_url"], URL)
        self.assertEqual(sum(method == "POST" for method, _, _ in session.calls), 1)

    def test_lost_submit_response_queries_before_same_payload_retry(self):
        result, session = self.run_client([
            ("GET", (404, {"code": "gpu_job_not_found"})),
            ("POST", requests.ReadTimeout("must stay private")),
            ("GET", (503, {"code": "gpu_runtime_unavailable"})),
            ("GET", requests.ConnectionError("must stay private")),
            ("GET", (404, {"code": "gpu_job_not_found"})),
            ("POST", (202, snapshot("queued"))),
            ("GET", (200, snapshot("completed"))),
        ])
        bodies = [params["json"] for method, _, params in session.calls if method == "POST"]
        self.assertEqual(bodies, [PAYLOAD, PAYLOAD])
        self.assertEqual(result["job_id"], JOB)

    def test_lost_response_of_accepted_submit_never_submits_again(self):
        _, session = self.run_client([
            ("GET", (404, {"code": "gpu_job_not_found"})),
            ("POST", requests.ReadTimeout()), ("GET", (200, snapshot())),
            ("GET", (200, snapshot("completed"))),
        ])
        self.assertEqual(sum(method == "POST" for method, _, _ in session.calls), 1)

    def test_known_record_disappearance_requires_recovery(self):
        for known, rows in (
            (True, [("GET", (404, {"code": "gpu_job_not_found"}))]),
            (False, [("GET", (200, snapshot())), ("GET", (404, {"code": "gpu_job_not_found"}))]),
        ):
            with self.subTest(known=known):
                session = FakeSession(rows)
                with self.assertRaises(RemoteRecoveryRequired):
                    wait_for_gpu_job(BASE, "test", PAYLOAD, session=session, stop_event=FakeStop(), known_remote=known)
                self.assertTrue(all(method == "GET" for method, _, _ in session.calls))

    def test_connection_status_preserves_actual_progress_and_timestamps(self):
        seen = []
        self.run_client([
            ("GET", (200, snapshot())), ("GET", requests.ReadTimeout("https://secret.example/token")),
            ("GET", (200, snapshot("completed"))),
        ], on_status=seen.append)
        self.assertEqual(seen[1]["connection_state"], "reconnecting")
        for key in ("status", "stage", "metrics", "last_progress_at", "heartbeat_at"):
            self.assertEqual(seen[0][key], seen[1][key])
        self.assertTrue(seen[1]["stalled"])
        self.assertNotIn("secret.example", json.dumps(seen))

    def test_conflicting_fingerprint_and_late_generation_stop(self):
        for records, error in (
            ([snapshot(fingerprint="f" * 64)], RemoteJobConflict),
            ([snapshot(generation=2), snapshot(generation=1)], RemoteRecoveryRequired),
        ):
            session = FakeSession([("GET", (200, item)) for item in records])
            with self.assertRaises(error):
                wait_for_gpu_job(BASE, "test", PAYLOAD, session=session, stop_event=FakeStop())

    def test_stop_only_stops_observation_and_does_not_emit_failure(self):
        seen = []
        session = FakeSession([("GET", (200, snapshot()))])
        with self.assertRaises(RemotePollingInterrupted):
            wait_for_gpu_job(BASE, "test", PAYLOAD, session=session, stop_event=FakeStop(stop_after=1), on_status=seen.append)
        self.assertEqual([item["status"] for item in seen], ["running"])

    def test_terminal_failure_uses_safe_message_and_never_resubmits(self):
        session = FakeSession([("GET", (200, snapshot("failed")))])
        with self.assertRaises(RemoteJobFailed) as raised:
            wait_for_gpu_job(BASE, "test", PAYLOAD, session=session, stop_event=FakeStop())
        self.assertNotIn("unsafe", str(raised.exception))
        self.assertEqual(len(session.calls), 1)

    def test_unverified_cache_is_not_relabelled_as_a_network_retry(self):
        session = FakeSession([("GET", (503, {"code": "gpu_result_cache_unverified"}))])
        with self.assertRaises(RemoteRecoveryRequired) as raised:
            wait_for_gpu_job(BASE, "test", PAYLOAD, session=session, stop_event=FakeStop())
        self.assertEqual(raised.exception.code, "gpu_result_cache_unverified")
        self.assertEqual(len(session.calls), 1)

    def test_resume_intent_after_new_generation_only_reconnects(self):
        _, session = self.run_client([
            ("GET", (200, snapshot("running", 2))),
            ("GET", (200, snapshot("completed", 2))),
        ], expected_generation=1, previous_status=snapshot("failed", 1))
        self.assertTrue(all(method == "GET" for method, _, _ in session.calls))

    def test_explicit_resume_is_bounded_to_one_expected_generation(self):
        session = FakeSession([
            ("GET", (200, snapshot("failed", 1))), ("POST", requests.ReadTimeout()),
            ("GET", (200, snapshot("failed", 1))), ("POST", (202, snapshot("queued", 2))),
            ("GET", (200, snapshot("failed", 2))),
        ])
        with self.assertRaises(RemoteJobFailed):
            wait_for_gpu_job(BASE, "test", PAYLOAD, session=session, stop_event=FakeStop(), expected_generation=1)
        posts = [(url, params["json"]) for method, url, params in session.calls if method == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(url.endswith("/resume") and body["expected_generation"] == 1 for url, body in posts))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.sqlite3"
        self.store = DramaSynthesisStore(self.db)
        self.store.ensure_storage()
        with database(self.db) as conn:
            conn.executescript("""
                CREATE TABLE drama_material_job(
                  job_id TEXT PRIMARY KEY,status TEXT NOT NULL,progress INTEGER NOT NULL,
                  progress_detail TEXT NOT NULL DEFAULT '',error_message TEXT NOT NULL DEFAULT '',
                  outputs_json TEXT NOT NULL,advanced_options_json TEXT NOT NULL DEFAULT '{}',
                  output_video_url TEXT NOT NULL DEFAULT '',output_video_no_bgm_url TEXT NOT NULL DEFAULT '',
                  cover_16x9_url TEXT NOT NULL DEFAULT '',finished_at TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL DEFAULT '2000-01-01 00:00:00',
                  completion_notified_at TEXT NOT NULL DEFAULT '',completion_notification_error TEXT NOT NULL DEFAULT '');
                CREATE TABLE drama_material_job_worker_lease(
                  job_id TEXT PRIMARY KEY,worker_id TEXT NOT NULL,pid INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,claimed_at TEXT NOT NULL DEFAULT '',heartbeat_at TEXT NOT NULL DEFAULT '',
                  released_at TEXT NOT NULL DEFAULT '',attempt INTEGER NOT NULL,last_error TEXT NOT NULL DEFAULT '');
            """)
            conn.execute("INSERT INTO drama_material_job(job_id,status,progress,outputs_json) VALUES(?,?,?,?)",
                         (JOB, "rendering", 46, json.dumps({"concat_video": True})))
            conn.execute("INSERT INTO drama_material_job_worker_lease(job_id,worker_id,status,attempt) VALUES(?,?,?,?)",
                         (JOB, "worker-a", "running", 1))
        self.lease = runtime.LeaseIdentity("worker-a", 1)
        self.result = {"job_id": JOB, "output_video_url": URL}

    def tearDown(self):
        self.tmp.cleanup()

    def job(self):
        with database(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM drama_material_job WHERE job_id=?", (JOB,)).fetchone())

    def sql(self, query, args=()):
        with database(self.db) as conn:
            conn.execute(query, args)

    def add_recipe(self):
        value = {"version": RECIPE_VERSION, "profile": RECIPE_PROFILE, "source": "concat_video", "assets": {}}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        value["recipe_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        self.store.freeze_recipe(JOB, value)
        self.sql("UPDATE drama_material_job SET outputs_json=? WHERE job_id=?",
                 (json.dumps({"concat_video": True, "random_template_video": True}), JOB))
        self.result.update(output_random_template_url="https://media.example.test/random.mp4",
                           random_template_output_sha256="1" * 64, random_template_output_profile=RECIPE_PROFILE,
                           random_template_recipe_sha256=value["recipe_sha256"])
        return value

    def test_old_owner_or_attempt_cannot_write_progress_or_complete(self):
        for worker, attempt in (("worker-b", 1), ("worker-a", 2)):
            with self.subTest(worker=worker, attempt=attempt):
                self.sql("UPDATE drama_material_job_worker_lease SET worker_id=?,attempt=?", (worker, attempt))
                with self.assertRaises(runtime.LeaseLostError):
                    runtime.fenced_update_job(self.db, JOB, {"progress": 50}, self.lease)
                with self.assertRaises(runtime.LeaseLostError):
                    runtime.atomic_complete_job(self.db, JOB, self.result, self.lease)
                self.assertEqual(self.job()["status"], "rendering")

    def test_none_lease_cannot_take_over_a_running_worker(self):
        with self.assertRaises(runtime.LeaseLostError):
            runtime.atomic_complete_job(self.db, JOB, self.result, None)
        self.sql("UPDATE drama_material_job_worker_lease SET status='failed'")
        self.assertEqual(runtime.atomic_complete_job(self.db, JOB, self.result, None)["status"], "done")

    def test_guard_must_share_the_write_transaction(self):
        with database(self.db) as conn:
            with self.assertRaises(RuntimeError):
                runtime.guard_current_lease(conn, JOB, self.lease)

    def test_replayed_completion_preserves_first_times_and_notifications(self):
        self.add_recipe()
        with mock.patch("features.drama_synthesis.core.utc_now", return_value="2001-01-01T00:00:00Z"), \
             mock.patch.object(runtime, "_now", return_value="2001-01-01 00:00:00"):
            runtime.atomic_complete_job(self.db, JOB, self.result, self.lease)
        self.sql("UPDATE drama_material_job SET completion_notified_at='already-notified'")
        first = self.job()
        with mock.patch("features.drama_synthesis.core.utc_now", return_value="2002-01-01T00:00:00Z"), \
             mock.patch.object(runtime, "_now", return_value="2002-01-01 00:00:00"):
            runtime.atomic_complete_job(self.db, JOB, self.result, self.lease)
        self.assertEqual(self.job(), first)
        self.assertEqual(self.store.recipe(JOB)["completed_at_utc"], "2001-01-01T00:00:00Z")
        with self.assertRaises(runtime.LeaseLostError):
            runtime.fenced_update_job(self.db, JOB, {"status": "failed"}, self.lease)

    def test_first_success_supersedes_failure_notice_but_success_replay_does_not(self):
        runtime.remember_remote_submission(self.db, JOB, PAYLOAD, self.lease)
        runtime.record_remote_status(self.db, JOB, snapshot("completed"), self.lease)
        self.sql("UPDATE drama_material_job SET completion_notified_at='prior-failure',completion_notification_error='old delivery error'")
        completed = runtime.atomic_complete_job(self.db, JOB, self.result, self.lease)
        self.assertEqual(completed["completion_notified_at"], "")
        self.assertEqual(completed["completion_notification_error"], "")
        self.assertEqual(runtime.get_remote_status(self.db, JOB)["prior_failure_notified_at"], "prior-failure")
        self.sql("UPDATE drama_material_job SET completion_notified_at='successful-notice'")
        replayed = runtime.atomic_complete_job(self.db, JOB, self.result, self.lease)
        self.assertEqual(replayed["completion_notified_at"], "successful-notice")
        self.assertEqual(replayed["finished_at"], completed["finished_at"])

    def test_recipe_and_job_completion_roll_back_together(self):
        self.add_recipe()
        self.sql("CREATE TRIGGER reject_done BEFORE UPDATE ON drama_material_job WHEN NEW.status='done' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            runtime.atomic_complete_job(self.db, JOB, self.result, self.lease)
        self.assertEqual(self.job()["status"], "rendering")
        self.assertEqual(self.store.recipe(JOB)["completed_at_utc"], "")

    def test_recipe_result_mismatch_does_not_mark_done(self):
        self.add_recipe()
        with self.assertRaises(runtime.RemoteStateConflict):
            runtime.atomic_complete_job(self.db, JOB, self.result, self.lease, expected_recipe_sha256="f" * 64)
        self.assertEqual(self.job()["status"], "rendering")

    def test_conflicting_replay_cannot_replace_a_completed_artifact(self):
        runtime.atomic_complete_job(self.db, JOB, self.result, self.lease)
        before = self.job()
        with self.assertRaises(runtime.RemoteStateConflict):
            runtime.atomic_complete_job(self.db, JOB, {**self.result, "output_video_url": URL + "?different"}, self.lease)
        self.assertEqual(self.job(), before)

    def test_deleted_job_cannot_be_recreated_by_late_result(self):
        self.sql("DELETE FROM drama_material_job WHERE job_id=?", (JOB,))
        with self.assertRaises(runtime.LeaseLostError):
            runtime.atomic_complete_job(self.db, JOB, self.result, self.lease)

    def test_frozen_payload_survives_restart_and_conflicts_fail_closed(self):
        original = {**deepcopy(PAYLOAD), "await_cover_16x9": True}
        runtime.remember_remote_submission(self.db, JOB, original, self.lease)
        delivered_cover = {**original, "cover_16x9_url": "https://media.example.test/cover.jpg"}
        self.assertEqual(runtime.remember_remote_submission(self.db, JOB, delivered_cover, self.lease), original)
        self.assertEqual(runtime.get_remote_payload(self.db, JOB), original)
        changed = {**original, "episode_end": 2}
        with self.assertRaises(runtime.RemoteStateConflict):
            runtime.remember_remote_submission(self.db, JOB, changed, self.lease)
        self.assertNotIn("payload", json.dumps(runtime.get_remote_status(self.db, JOB)))
        self.assertNotIn("source.example", json.dumps(runtime.get_remote_status(self.db, JOB)))

    def test_status_first_start_and_completed_state_cannot_regress(self):
        runtime.remember_remote_submission(self.db, JOB, PAYLOAD, self.lease)
        runtime.record_remote_status(self.db, JOB, snapshot(), self.lease)
        runtime.record_remote_status(self.db, JOB, snapshot(started_at="2001-01-01T00:00:00Z"), self.lease)
        self.assertEqual(runtime.get_remote_status(self.db, JOB)["first_started_at"], "2000-01-01T00:00:00Z")
        runtime.record_remote_status(self.db, JOB, snapshot("completed"), self.lease)
        runtime.record_remote_status(self.db, JOB, snapshot(), self.lease)
        self.assertEqual(runtime.get_remote_status(self.db, JOB)["status"], "completed")

    def test_explicit_resume_intent_is_cas_cleared_by_new_generation_only(self):
        runtime.remember_remote_submission(self.db, JOB, PAYLOAD, self.lease)
        runtime.record_remote_status(self.db, JOB, snapshot("failed", connection_state="connected"), self.lease)
        self.sql("UPDATE drama_material_job SET status='failed'")
        self.sql("UPDATE drama_material_job_worker_lease SET status='failed'")
        self.assertEqual(runtime.request_remote_resume(self.db, JOB, 1), 1)
        self.assertEqual(runtime.request_remote_resume(self.db, JOB, 1), 1)
        self.assertEqual(runtime.get_remote_resume_intent(self.db, JOB), 1)
        self.sql("UPDATE drama_material_job_worker_lease SET status='running',attempt=2")
        next_lease = runtime.LeaseIdentity("worker-a", 2)
        runtime.record_remote_status(self.db, JOB, snapshot("failed"), next_lease)
        self.assertEqual(runtime.get_remote_resume_intent(self.db, JOB), 1)
        runtime.record_remote_status(self.db, JOB, snapshot("running", 2), next_lease)
        self.assertIsNone(runtime.get_remote_resume_intent(self.db, JOB))

    def fake_worker(self):
        fake = SimpleNamespace(
            JOB_DB_PATH=str(self.db), JOB_AUTO_RETRY_ATTEMPTS=2,
            fetch_job_row=mock.Mock(side_effect=lambda _job_id: self.job()),
            selected_job_outputs_ready=mock.Mock(return_value=False),
            clear_job_deleted_marker=mock.Mock(), upsert_job_record=mock.Mock(),
            notify_job_creator_on_completion=mock.Mock(), clamp_progress=lambda value: int(value),
            should_auto_retry_job=mock.Mock(return_value=True), is_job_deleted=mock.Mock(return_value=False),
            set_job_progress=mock.Mock(), process_job=mock.Mock(),
        )
        spec = importlib.util.spec_from_file_location("synthetic_drama_worker", ROOT / "scripts/drama_job_worker.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"app": fake}), mock.patch.dict("os.environ", {"DRAMA_JOB_DB_PATH": str(self.db), "DRAMA_JOB_WORKER_ID": "worker-a"}):
            spec.loader.exec_module(module)
        return module, fake

    def test_worker_heartbeat_and_release_are_attempt_fenced(self):
        worker, _ = self.fake_worker()
        self.sql("UPDATE drama_material_job_worker_lease SET attempt=2,heartbeat_at='original'")
        self.assertFalse(worker.update_heartbeat(JOB, 1))
        worker.release_lease(JOB, "failed", attempt=1)
        with database(self.db) as conn:
            row = conn.execute("SELECT status,heartbeat_at FROM drama_material_job_worker_lease").fetchone()
        self.assertEqual(row, ("running", "original"))

    def test_worker_remote_prepare_preserves_notification_markers(self):
        runtime.remember_remote_submission(self.db, JOB, PAYLOAD, self.lease)
        worker, fake = self.fake_worker()
        job = self.job()
        job.update(completion_notified_at="existing", completion_notification_error="pending retry")
        self.assertTrue(worker.prepare_job_for_run(job))
        saved = fake.upsert_job_record.call_args.args[0]
        self.assertEqual(saved["completion_notified_at"], "existing")
        self.assertEqual(saved["completion_notification_error"], "pending retry")

    def test_worker_legacy_prepare_clears_prior_failure_markers(self):
        for outputs_ready in (False, True):
            with self.subTest(outputs_ready=outputs_ready):
                worker, fake = self.fake_worker()
                fake.selected_job_outputs_ready.return_value = outputs_ready
                job = self.job()
                job.update(status="failed", completion_notified_at="failure-notified",
                           completion_notification_error="old delivery error")
                self.assertEqual(worker.prepare_job_for_run(job), not outputs_ready)
                saved = fake.upsert_job_record.call_args.args[0]
                self.assertEqual(saved["completion_notified_at"], "")
                self.assertEqual(saved["completion_notification_error"], "")
                if outputs_ready:
                    self.assertEqual(saved["status"], "done")
                    fake.notify_job_creator_on_completion.assert_called_once_with(job)
                else:
                    fake.notify_job_creator_on_completion.assert_not_called()

    def test_worker_remote_prepare_does_not_infer_done_from_existing_urls(self):
        runtime.remember_remote_submission(self.db, JOB, PAYLOAD, self.lease)
        worker, fake = self.fake_worker()
        fake.selected_job_outputs_ready.return_value = True
        job = self.job()
        job.update(output_video_url=URL, cover_16x9_url="https://media.example.test/cover.jpg")
        self.assertTrue(worker.prepare_job_for_run(job))
        self.assertEqual(job["status"], "queued")
        fake.selected_job_outputs_ready.assert_not_called()
        fake.notify_job_creator_on_completion.assert_not_called()

    def test_worker_does_not_retry_media_after_completion_notification_error(self):
        worker, fake = self.fake_worker()

        def complete_then_fail(_job):
            self.sql("UPDATE drama_material_job SET status='done'")
            raise RuntimeError("synthetic notification failure")

        fake.process_job.side_effect = complete_then_fail
        with self.assertLogs(level="ERROR"):
            worker.run_claimed_job(JOB)
        self.assertEqual(fake.process_job.call_count, 1)
        self.assertEqual(self.job()["status"], "done")
        self.assertEqual(fake.notify_job_creator_on_completion.call_count, 0)

    def test_worker_stop_keeps_job_active_for_takeover(self):
        worker, fake = self.fake_worker()
        fake.process_job.side_effect = RemotePollingInterrupted()
        worker.run_claimed_job(JOB)
        self.assertEqual(self.job()["status"], "rendering")
        self.assertEqual(fake.process_job.call_count, 1)
        with database(self.db) as conn:
            self.assertEqual(conn.execute("SELECT status FROM drama_material_job_worker_lease").fetchone()[0], "interrupted")
        fake.should_auto_retry_job.assert_not_called()
        self.assertEqual(worker.claim_next_job(), JOB)
        self.assertEqual(worker.owned_lease(JOB).attempt, 2)

    def test_worker_does_not_auto_retry_remote_terminal_errors(self):
        worker, fake = self.fake_worker()
        fake.process_job.side_effect = RemoteJobFailed("gpu_render_failed", "媒体制作失败", 502)
        with self.assertLogs(level="ERROR"):
            worker.run_claimed_job(JOB)
        self.assertEqual(fake.process_job.call_count, 1)
        fake.should_auto_retry_job.assert_not_called()
        self.assertEqual(fake.upsert_job_record.call_args.args[0]["error_message"], "媒体制作失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)
