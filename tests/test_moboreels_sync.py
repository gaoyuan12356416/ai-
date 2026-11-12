"""Behavioral sync tests using only temporary files and in-memory adapters."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.moboreels_library.catalog import normalize_drama
from features.moboreels_library.settings import Settings
from features.moboreels_library.sync import SyncManager


_REAL_SLEEP = time.sleep


def source_row(story_id, **changes):
    row = {
        "story_id": str(story_id), "story_name": "Drama %s" % story_id,
        "is_show": 1, "introduce": "Original synopsis", "lang_id": 3,
        "img_src": "https://example.test/%s.jpg" % story_id,
        "baiduLink": "Original share", "baiduLinkCode": "a123",
        "publish_time": "2026-11-12 12:00:00", "local_type": 2,
        "audio_type": 1, "holder_type": 1, "payepisfrom": 9,
    }
    row.update(changes)
    return row


class FakeProvider:
    def __init__(self, rows, overrides=None, blocked=False):
        self.rows = copy.deepcopy(rows)
        self.overrides = overrides or {}
        self.calls = []
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocked = blocked

    def get_page(self, page, page_size=100):
        with self.lock:
            self.calls.append(page)
        self.entered.set()
        if self.blocked and not self.release.wait(5):
            raise RuntimeError("test provider was not released")
        if page in self.overrides:
            return copy.deepcopy(self.overrides[page])
        begin = (page - 1) * page_size
        return {"total": len(self.rows), "list": copy.deepcopy(self.rows[begin:begin + page_size])}


class FakeDatabase:
    def __init__(self, rows=()):
        self.lock = threading.RLock()
        self.rows = {}
        for index, raw in enumerate(rows, 1):
            row = normalize_drama(raw)
            row.update(id=index, institution_user_id="test-institution", project_type=2,
                       first_seen_at="2026-11-01T00:00:00Z", last_seen_at="2026-11-01T00:00:00Z",
                       last_sync_run_id="previous")
            self.rows[row["story_id"]] = row
        self.apply_calls = 0
        self.touch_calls = 0
        self.read_calls = 0
        self.events = []
        self.applied_batches = []
        self.touched_ids = []
        self.commit_then_fail_once = False
        self.fail_before_once = False
        self.always_fail = False
        self.fail_existing_read = False

    def iter_catalog_rows(self, batch_size=500):
        with self.lock:
            if self.fail_existing_read:
                raise RuntimeError("simulated catalog read failure")
            rows = copy.deepcopy(list(self.rows.values()))
            self.events.append("iter")
        return iter(rows)

    def read_catalog_ids(self, ids):
        with self.lock:
            self.read_calls += 1
            self.events.append("read")
            return {str(key): copy.deepcopy(self.rows[str(key)]) for key in ids if str(key) in self.rows}

    def apply_catalog_batch(self, records, run_id, seen_at):
        records = list(records)
        if len(records) > 20:
            raise AssertionError("catalog change transaction exceeds 20 rows")
        with self.lock:
            self.apply_calls += 1
            self.events.append("apply")
            if self.always_fail:
                raise RuntimeError("simulated persistent write failure")
            if self.fail_before_once:
                self.fail_before_once = False
                raise RuntimeError("simulated connection loss before commit")
            self.applied_batches.append([str(record["story_id"]) for record in records])
            for record in records:
                key = str(record["story_id"])
                old = self.rows.get(key, {})
                row = copy.deepcopy(record)
                row.update(
                    id=old.get("id", len(self.rows) + 1),
                    institution_user_id="test-institution", project_type=2,
                    first_seen_at=old.get("first_seen_at", seen_at),
                    last_seen_at=seen_at, last_sync_run_id=run_id,
                )
                self.rows[key] = row
            if self.commit_then_fail_once:
                self.commit_then_fail_once = False
                raise RuntimeError("simulated lost acknowledgement after commit")

    def touch_catalog(self, ids, run_id, seen_at):
        ids = list(ids)
        if len(ids) > 500:
            raise AssertionError("catalog metadata transaction exceeds 500 rows")
        with self.lock:
            self.touch_calls += 1
            self.events.append("touch")
            self.touched_ids.extend(str(key) for key in ids)
            for key in ids:
                self.rows[str(key)].update(last_seen_at=seen_at, last_sync_run_id=run_id)

    def close_writer(self):
        with self.lock:
            self.events.append("close")


class SyncBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=Path(self.temp.name), institution_user_id="test-institution",
            token="test-only", internal_token="test-only", db_user="unused", db_password="unused",
        )
        self.sleep_patch = mock.patch(
            "features.moboreels_library.sync.time.sleep",
            side_effect=lambda seconds: _REAL_SLEEP(min(seconds, 0.001)),
        )
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        self.temp.cleanup()

    def complete(self, manager):
        job = manager.start("manual", {"user_id": "operator", "tenant_key": "test"})
        result = manager.wait(job["run_id"], timeout=10)
        self.assertIn(result["status"], {"succeeded", "failed"}, result)
        return result

    def test_retry_during_failed_worker_cleanup_is_not_stranded(self):
        db = FakeDatabase()
        db.always_fail = True
        provider = FakeProvider([source_row("1")])
        manager = SyncManager(self.settings, db, provider)
        failed_saved = threading.Event()
        release_cleanup = threading.Event()
        original_update = manager._update

        def pause_after_failure(run_id, **changes):
            result = original_update(run_id, **changes)
            if changes.get("status") == "failed":
                failed_saved.set()
                release_cleanup.wait(5)
            return result

        manager._update = pause_after_failure
        first = manager.start("manual")
        try:
            self.assertTrue(failed_saved.wait(5))
            db.always_fail = False
            resumed = manager.start("manual")
            self.assertEqual(first["run_id"], resumed["run_id"])
        finally:
            release_cleanup.set()
        result = manager.wait(first["run_id"], timeout=10)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(provider.calls, [1])

    def test_last_success_time_remains_available_after_later_failure(self):
        provider = FakeProvider([source_row("1")])
        manager = SyncManager(self.settings, FakeDatabase(), provider)
        succeeded = self.complete(manager)
        self.assertEqual(manager.last_success_at(), succeeded["finished_at"])
        provider.overrides[1] = {"total": 2, "list": []}
        failed = self.complete(manager)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(manager.last_success_at(), succeeded["finished_at"])

    def test_full_sync_changes_status_preserves_hidden_info_and_does_not_delete_missing(self):
        initial = [source_row("1"), source_row("2"), source_row("3"),
                   source_row("4", is_show=0), source_row("5")]
        current = [
            source_row("1", story_name="Changed title"),
            source_row("2", is_show=0, story_name="-", introduce="-", lang_id=-1,
                       img_src="-", baiduLink="-", baiduLinkCode="-", payepisfrom=-1),
            source_row("3"), source_row("4", is_show=1), source_row("6"),
        ]
        db = FakeDatabase(initial)
        result = self.complete(SyncManager(self.settings, db, FakeProvider(current)))
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["processed"], 5)
        self.assertEqual(result["verified"], 5)
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["updated_count"], 3)
        self.assertEqual(result["listed_count"], 1)
        self.assertEqual(result["delisted_count"], 1)
        self.assertEqual(db.rows["1"]["story_name"], "Changed title")
        self.assertEqual(db.rows["2"]["story_name"], "Drama 2")
        self.assertEqual(db.rows["2"]["lang_id"], 3)
        self.assertEqual(db.rows["2"]["raw_json"]["lang_id"], -1)
        self.assertEqual(db.rows["2"]["is_show"], 0)
        self.assertEqual(db.rows["2"]["first_seen_at"], "2026-11-01T00:00:00Z")
        self.assertEqual(db.rows["4"]["is_show"], 1)
        self.assertEqual(db.rows["5"]["is_show"], 1)
        self.assertEqual(db.rows["5"]["last_sync_run_id"], "previous")
        self.assertNotIn("3", sum(db.applied_batches, []))
        self.assertIn("3", db.touched_ids)
        self.assertEqual(set(db.rows), {"1", "2", "3", "4", "5", "6"})

    def test_incomplete_source_page_makes_no_database_changes(self):
        rows = [source_row(str(index)) for index in range(101)]
        provider = FakeProvider(rows, {2: {"total": 101, "list": []}})
        db = FakeDatabase()
        result = self.complete(SyncManager(self.settings, db, provider))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(db.apply_calls + db.touch_calls, 0)
        self.assertEqual(db.rows, {})

    def test_duplicate_source_ids_make_no_database_changes(self):
        rows = [source_row(str(index)) for index in range(101)]
        provider = FakeProvider(rows, {2: {"total": 101, "list": [rows[0]]}})
        db = FakeDatabase()
        result = self.complete(SyncManager(self.settings, db, provider))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(db.apply_calls + db.touch_calls, 0)

    def test_source_total_drift_makes_no_database_changes(self):
        rows = [source_row(str(index)) for index in range(101)]
        provider = FakeProvider(rows, {2: {"total": 102, "list": [rows[-1]]}})
        db = FakeDatabase()
        result = self.complete(SyncManager(self.settings, db, provider))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(db.apply_calls + db.touch_calls, 0)

    def test_parallel_manual_and_scheduled_start_reuse_active_job(self):
        provider = FakeProvider([source_row("1")], blocked=True)
        manager = SyncManager(self.settings, FakeDatabase(), provider)
        first = manager.start("scheduled", None)
        try:
            self.assertTrue(provider.entered.wait(3))
            with ThreadPoolExecutor(max_workers=8) as pool:
                jobs = list(pool.map(lambda _: manager.start("manual", {"user_id": "u"}), range(8)))
            self.assertEqual({job["run_id"] for job in jobs}, {first["run_id"]})
        finally:
            provider.release.set()
        self.assertEqual(manager.wait(first["run_id"], timeout=10)["status"], "succeeded")
        self.assertEqual(provider.calls, [1])

    def test_committed_write_with_lost_response_is_read_back_not_repeated(self):
        db = FakeDatabase()
        db.commit_then_fail_once = True
        result = self.complete(SyncManager(self.settings, db, FakeProvider([source_row("1")])))
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(db.apply_calls, 1)
        self.assertEqual(len(db.rows), 1)
        self.assertIn("read", db.events[db.events.index("apply") + 1:])

    def test_two_managers_share_the_active_run_without_duplicate_fetching(self):
        provider = FakeProvider([source_row("1")], blocked=True)
        db = FakeDatabase()
        first_manager = SyncManager(self.settings, db, provider)
        first = first_manager.start("scheduled", None)
        try:
            self.assertTrue(provider.entered.wait(3))
            second_manager = SyncManager(self.settings, db, provider, recover=False)
            second = second_manager.start("manual", {"user_id": "u"})
            self.assertEqual(first["run_id"], second["run_id"])
        finally:
            provider.release.set()
        result = first_manager.wait(first["run_id"], timeout=10)
        self.assertEqual(result["status"], "succeeded", result)
        second_manager.wait(first["run_id"], timeout=10)
        self.assertEqual(provider.calls, [1])

    def test_uncommitted_write_is_reconciled_before_retry(self):
        db = FakeDatabase()
        db.fail_before_once = True
        result = self.complete(SyncManager(self.settings, db, FakeProvider([source_row("1")])))
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(db.apply_calls, 2)
        positions = [index for index, event in enumerate(db.events) if event == "apply"]
        self.assertIn("read", db.events[positions[0] + 1:positions[1]])

    def test_retry_failed_write_reuses_complete_snapshot_and_same_run(self):
        db = FakeDatabase()
        db.always_fail = True
        provider = FakeProvider([source_row("1")])
        manager = SyncManager(self.settings, db, provider)
        first = self.complete(manager)
        self.assertEqual(first["status"], "failed", first)
        initial_calls = list(provider.calls)
        db.always_fail = False
        resumed = self.complete(manager)
        self.assertEqual(resumed["run_id"], first["run_id"])
        self.assertEqual(resumed["status"], "succeeded", resumed)
        self.assertGreaterEqual(resumed["resume_count"], 1)
        self.assertEqual(provider.calls, initial_calls)

    def test_restart_recovers_running_job_from_complete_snapshot(self):
        db = FakeDatabase()
        db.always_fail = True
        provider = FakeProvider([source_row("1")])
        original = SyncManager(self.settings, db, provider)
        first = self.complete(original)
        self.assertEqual(first["status"], "failed", first)
        initial_calls = list(provider.calls)
        with sqlite3.connect(str(self.settings.data_dir / "sync.sqlite3")) as connection:
            row = connection.execute("SELECT payload FROM sync_run WHERE run_id=?", (first["run_id"],)).fetchone()
            payload = json.loads(row[0])
            payload.update(status="running", stage="writing", finished_at=None)
            connection.execute("UPDATE sync_run SET payload=? WHERE run_id=?", (json.dumps(payload), first["run_id"]))
        connection.close()
        db.always_fail = False
        recovered = SyncManager(self.settings, db, provider, recover=True)
        result = recovered.wait(first["run_id"], timeout=10)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(provider.calls, initial_calls)
        self.assertEqual(len(db.rows), 1)

    def test_database_read_failure_reuses_already_complete_source_snapshot(self):
        db = FakeDatabase()
        db.fail_existing_read = True
        provider = FakeProvider([source_row("1")])
        manager = SyncManager(self.settings, db, provider)
        first = self.complete(manager)
        self.assertEqual(first["status"], "failed", first)
        self.assertEqual(provider.calls, [1])
        db.fail_existing_read = False
        resumed = self.complete(manager)
        self.assertEqual(resumed["run_id"], first["run_id"])
        self.assertEqual(resumed["status"], "succeeded", resumed)
        self.assertEqual(provider.calls, [1])


if __name__ == "__main__":
    unittest.main()
