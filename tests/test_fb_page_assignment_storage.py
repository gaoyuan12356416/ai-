import copy
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from features.fb_page_assignment.storage import ENV_PREFIX, PayloadConflictError, Store, Writer


def sample_rows():
    return [{"drama_id": "drama_1", "page_id": "12345678901234567890", "status": "assigned",
             "selection_stage": "language_profile", "error_code": "", "error_message": "",
             "diagnostics": {"language": "en", "profile_snapshot": "20261110T102316Z"}}]


class FakeDatabase:
    """Small transactional fixture; validates the writer's fixed SQL shape."""
    def __init__(self):
        self.rows = {}
        self.connections = []
        self.lose_commit_receipt = False
        self.commits = 0
        self.statements = []

    def connect(self, **kwargs):
        connection = FakeConnection(self, kwargs)
        self.connections.append(connection)
        return connection


class FakeConnection:
    def __init__(self, database, config):
        self.database, self.config = database, config
        self.closed = False
        self.rolled_back = False

    def begin(self):
        self.rows = copy.deepcopy(self.database.rows)
        self.changed = set()

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.database.commits += 1
        self.database.rows.update({key: copy.deepcopy(self.rows[key]) for key in self.changed})
        if self.database.lose_commit_receipt:
            self.database.lose_commit_receipt = False
            raise TimeoutError("commit receipt lost")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def fetchall(self):
        return self.result

    def execute(self, sql, args):
        normalized = " ".join(sql.split())
        self.assert_target(normalized)
        self.connection.database.statements.append(normalized)
        if normalized.startswith("SELECT item_index,payload_hash,revision"):
            if not re.search(r"WHERE task_id=%s AND item_index IN \(%s(?:,%s)*\) ORDER BY item_index FOR UPDATE$", normalized):
                raise AssertionError("only precise primary-key transactional reads are allowed")
            self.result = [(i, row["payload_hash"], row["revision"]) for i in args[1:]
                           if (row := self.connection.rows.get((args[0], i))) is not None]
        elif normalized.startswith("INSERT INTO"):
            columns = re.search(r"\((.*?)\) VALUES", normalized).group(1).replace("`", "").split(",")
            self.assert_value_lengths(columns, args, normalized)
            for start in range(0, len(args), len(columns)):
                row = dict(zip(columns, args[start:start + len(columns)]))
                key = row["task_id"], row["item_index"]
                if key in self.connection.rows:
                    raise AssertionError("duplicate insert")
                self.connection.rows[key] = row
                self.connection.changed.add(key)
        elif normalized.startswith("UPDATE"):
            revision, status, reasons, message_id, error_code, updated_at, task_id, *indices, ceiling = args
            for index in indices:
                row = self.connection.rows[(task_id, index)]
                if row["revision"] < ceiling:
                    self.connection.changed.add((task_id, index))
                    row.update(revision=revision, alert_status=status, alert_reasons_json=reasons,
                               feishu_message_id=message_id, alert_error_code=error_code, updated_at=updated_at)
        else:
            raise AssertionError("unexpected SQL: " + normalized)

    @staticmethod
    def assert_value_lengths(columns, args, sql):
        if len(args) % len(columns) or len(args) != sql.count("%s"):
            raise AssertionError("VALUES placeholders do not match the bound rows")

    @staticmethod
    def assert_target(sql):
        if "`ads_ai`.`fb_drama_page_assignment_log`" not in sql:
            raise AssertionError("wrong target")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "outbox.sqlite3")
        self.database = FakeDatabase()
        self.writer = Writer({ENV_PREFIX + "USER": "fixture", ENV_PREFIX + "PASSWORD": "fixture"},
                             connection_factory=self.database.connect)

    def save(self, task_id="task_1", rows=None, reasons=None):
        return self.store.save(task_id, "external_1", sample_rows() if rows is None else rows,
                               200, {"task_id": task_id}, reasons or [])

    def test_freezes_duplicate_input_rows_and_external_id_is_not_unique(self):
        rows = sample_rows() * 2
        self.save(rows=rows)
        rows[0]["page_id"] = "changed"
        result = self.store.get("task_1")
        self.assertEqual([row["item_index"] for row in result["rows"]], [1, 2])
        self.assertEqual(result["rows"][0]["page_id"], "12345678901234567890")
        self.save("task_2")
        self.assertEqual(len(self.store.pending_sync()), 2)

    def test_duplicate_save_is_noop_and_changed_payload_is_rejected(self):
        first = self.save()
        self.assertEqual(self.save()["created_at"], first["created_at"])
        rows = sample_rows()
        rows[0]["page_id"] = "different"
        with self.assertRaises(PayloadConflictError):
            self.save(rows=rows)
        self.assertEqual(self.store.get("task_1")["rows"][0]["page_id"], "12345678901234567890")

    def test_empty_request_has_error_sentinel(self):
        result = self.store.save("bad", "external", [], 400,
                                 {"code": "invalid_drama_ids", "message": "missing ids"}, ["request_error"])
        self.assertEqual(result["rows"][0]["item_index"], 0)
        self.assertIsNone(result["rows"][0]["drama_id"])
        self.assertTrue(self.store.sync_task("bad", self.writer))
        self.assertEqual(self.database.rows[("bad", 0)]["error_code"], "invalid_drama_ids")

    def test_explicit_request_error_uses_zero_index_and_preserves_details(self):
        row = {"drama_id": None, "page_id": None, "status": "failed",
               "error_code": "invalid_json_or_body", "error_message": "bad JSON",
               "diagnostics": {"stage": "parse"}}
        result = self.store.save("bad", "caller", [row], 400,
                                 {"task_id": "bad", "results": []}, ["invalid_json_or_body"])
        self.assertEqual(result["rows"][0], dict(row, item_index=0))
        self.assertTrue(self.store.sync_task("bad", self.writer))
        self.assertEqual(self.database.rows[("bad", 0)]["error_code"], "invalid_json_or_body")

    def test_defer_coordination_failure_retains_frozen_response(self):
        before = self.save()
        result = self.store.defer_sync("task_1", RuntimeError("secret=do-not-log"))
        self.assertEqual(result["response"], before["response"])
        self.assertEqual(result["rows"], before["rows"])
        self.assertEqual(result["alert_status"], "pending")
        self.assertEqual(result["last_sync_error"], "RuntimeError")

    def test_unknown_commit_is_retried_with_same_rows_and_backoff(self):
        self.save()
        self.database.lose_commit_receipt = True
        self.assertFalse(self.store.sync_task("task_1", self.writer))
        first = self.store.get("task_1")
        self.assertFalse(first["details_cleared"])
        self.assertEqual(first["rows"][0]["diagnostics"], sample_rows()[0]["diagnostics"])
        self.assertIn("log_sync_pending", first["alert_reasons"])
        self.assertEqual(first["alert_status"], "pending")
        self.assertEqual(len(self.database.rows), 1)
        self.assertFalse(self.store.sync_task("task_1", self.writer))
        self.assertEqual(len(self.database.connections), 1)
        with patch("features.fb_page_assignment.storage.time.time", return_value=first["sync_next_at"] + 1):
            self.assertTrue(self.store.sync_task("task_1", self.writer))
        self.assertEqual(len(self.database.rows), 1)
        self.assertEqual(self.store.get("task_1")["sync_status"], "synced")

    def test_transaction_rolls_back_all_rows_on_hash_conflict(self):
        self.save(rows=sample_rows() * 2)
        request = self.store.get("task_1")
        self.writer.write_task(request)
        before = copy.deepcopy(self.database.rows)
        self.database.rows[("task_1", 2)]["payload_hash"] = "conflict"
        request["revision"] = 2
        request["alert_status"] = "pending"
        with self.assertRaises(PayloadConflictError):
            self.writer.write_task(request)
        self.assertEqual(self.database.rows[("task_1", 1)], before[("task_1", 1)])
        self.assertTrue(self.database.connections[-1].rolled_back)
        self.assertTrue(self.database.connections[-1].closed)

    def test_old_snapshot_never_overwrites_sent_receipt(self):
        old = self.save(reasons=["no_page"])
        self.writer.write_task(old)
        self.assertIsNotNone(self.store.claim_alert("task_1"))
        current = self.store.mark_alert("task_1", "sent", message_id="om_receipt")
        self.writer.write_task(current)
        self.writer.write_task(old)
        row = self.database.rows[("task_1", 1)]
        self.assertEqual((row["alert_status"], row["feishu_message_id"]), ("sent", "om_receipt"))
        self.assertEqual(row["page_id"], "12345678901234567890")

    def test_compact_receipt_updates_original_key_and_never_inserts_partial_facts(self):
        original = self.save(rows=sample_rows() * 2)
        hashes = self.writer.write_task(original)
        compact = [dict(item_index=i, payload_hash=h, drama_id="drama_1", page_id="12345678901234567890")
                   for i, h in hashes.items()]
        with self.store._connect() as db:
            db.execute("UPDATE fb_page_assignment_requests SET rows_json=? WHERE task_id=?",
                       (json.dumps({"confirmed_items":compact}), "task_1"))
        saved = self.store.mark_alert("task_1", "sent", message_id="receipt")
        self.assertTrue(saved["details_cleared"])
        self.writer.write_task(saved)
        self.assertEqual(self.database.rows["task_1", 1]["feishu_message_id"], "receipt")
        self.assertEqual(self.database.rows["task_1", 1]["diagnostics_json"],
                         self.writer._record(original, original["rows"][0])["diagnostics_json"])
        del self.database.rows["task_1", 2]
        with self.assertRaises(PayloadConflictError):
            self.writer.write_task(saved)
        self.assertNotIn(("task_1", 2), self.database.rows)

    def test_alert_claim_is_exclusive_and_recovery_does_not_resend(self):
        self.save(reasons=["no_page"])
        self.assertIsNotNone(self.store.claim_alert("task_1"))
        self.assertIsNone(self.store.claim_alert("task_1"))
        self.assertEqual(self.store.recover_inflight_alerts(), 1)
        self.assertEqual(self.store.get("task_1")["alert_status"], "unknown")
        self.assertEqual(self.store.pending_alerts(), [])
        with self.assertRaises(ValueError):
            self.store.mark_alert("task_1", "pending")

    def test_sync_failure_after_sent_alert_does_not_create_notification_loop(self):
        self.save(reasons=["profile_relaxed"])
        self.store.claim_alert("task_1")
        self.store.mark_alert("task_1", "sent", message_id="om_receipt")
        failure = Mock()
        failure.write_task.side_effect = RuntimeError("password=must-not-be-logged")
        self.assertFalse(self.store.sync_task("task_1", failure))
        request = self.store.get("task_1")
        self.assertEqual(request["alert_status"], "sent")
        self.assertEqual(request["last_sync_error"], "RuntimeError")
        self.assertEqual(self.store.pending_alerts(), [])
        with self.assertRaises(ValueError):
            self.store.mark_alert("task_1", "failed", error_code="late_error")
        self.assertEqual(self.store.get("task_1")["feishu_message_id"], "om_receipt")

    def test_notification_changed_during_sync_stays_pending_for_new_revision(self):
        self.save(reasons=["no_page"])
        writer = Mock()
        writer.write_task.side_effect = lambda request: self.store.claim_alert("task_1")
        self.assertTrue(self.store.sync_task("task_1", writer))
        result = self.store.get("task_1")
        self.assertLess(result["synced_revision"], result["revision"])
        self.assertEqual(result["sync_status"], "pending")
        self.assertTrue(self.store.sync_task("task_1", self.writer))

    def test_writer_target_credentials_and_timeouts(self):
        for key, value in (("HOST", "localhost"), ("PORT", "63350"), ("DATABASE", "kunlunads_dev")):
            with self.assertRaises(ValueError):
                Writer({ENV_PREFIX + "USER": "fixture", ENV_PREFIX + "PASSWORD": "fixture", ENV_PREFIX + key: value})
        with self.assertRaises(ValueError):
            Writer({})
        self.save()
        self.store.sync_task("task_1", self.writer)
        config = self.database.connections[-1].config
        self.assertEqual((config["connect_timeout"], config["read_timeout"], config["write_timeout"]), (3, 5, 5))
        self.assertFalse(config["autocommit"])

    def test_wave_reuses_connection_with_independent_commits_and_bounded_inserts(self):
        self.save("many", rows=sample_rows() * 17)
        self.save("large", rows=[dict(sample_rows()[0], diagnostics={"trace": "a" * 300000})] * 2)
        with self.writer.batch() as session:
            self.assertTrue(self.store.sync_task("many", session))
            self.assertTrue(self.store.sync_task("large", session))
        self.assertEqual((len(self.database.connections), self.database.commits), (1, 2))
        self.assertEqual(sum(s.startswith("SELECT") for s in self.database.statements), 2)
        self.assertEqual(sum(s.startswith("INSERT") for s in self.database.statements), 5)
        self.assertEqual(len(self.database.rows), 19)
        self.assertEqual(self.store.pending_sync_ids(16), [])

    def test_lost_commit_discards_connection_and_only_defers_its_task(self):
        self.save("first")
        self.save("second")
        self.database.lose_commit_receipt = True
        with self.writer.batch() as session:
            self.assertFalse(self.store.sync_task("first", session))
            self.assertTrue(self.store.sync_task("second", session))
        self.assertEqual(self.store.get("first")["sync_status"], "pending")
        retry_at = self.store.get("first")["sync_next_at"]
        self.store.claim_alert("first")
        self.store.mark_alert("first", "sent", message_id="qa-receipt")
        self.assertEqual(self.store.get("first")["sync_next_at"], retry_at)
        self.assertEqual(len(self.database.connections), 2)
        self.assertTrue(all(c.closed for c in self.database.connections))
        self.assertEqual(len(self.database.rows), 2)

    def test_ack_clears_full_payload_reclaims_disk_and_keeps_original_duplicate_receipt(self):
        rows = sample_rows()
        rows[0]["diagnostics"]["large"] = "d" * 2000000
        original = self.save(rows=rows)
        before = self.store.path.stat().st_size
        with self.store._connect() as db:
            self.assertEqual(db.execute("PRAGMA auto_vacuum").fetchone()[0], 1)
        self.assertTrue(self.store.sync_task("task_1", self.writer))
        after = self.save(rows=rows)
        self.assertTrue(after["details_cleared"])
        self.assertNotIn("diagnostics", after["rows"][0])
        self.assertEqual(after["initial_hash"], original["initial_hash"])
        self.assertEqual(after["response"], original["response"])
        self.assertEqual(after["rows"][0]["payload_hash"], self.database.rows["task_1", 1]["payload_hash"])
        self.assertIn('"large":', self.database.rows["task_1", 1]["diagnostics_json"])
        self.assertLess(self.store.path.stat().st_size, before // 4)
        with self.assertRaises(PayloadConflictError):
            self.save(rows=sample_rows())

    def test_legacy_cleanup_requires_matching_committed_revision_and_every_original_item(self):
        original = self.save(rows=sample_rows()*2)
        hashes = self.writer.write_task(original)
        def clean(rev=1, fingerprint=original["initial_hash"], items=hashes):
            return self.store.compact_verified("task_1", rev, fingerprint, items)
        self.assertFalse(clean())
        with self.store._connect() as db:
            db.execute("UPDATE fb_page_assignment_requests SET synced_revision=revision")
        self.assertFalse(clean(rev=2))
        self.assertFalse(clean(fingerprint="wrong"))
        self.assertFalse(clean(items={1:hashes[1]}))
        self.assertFalse(clean(items={1:hashes[1], 2:"wrong"}))
        self.assertEqual(self.store.get("task_1")["rows"], original["rows"])
        self.assertTrue(clean())
        self.assertFalse(clean())

    def test_pending_alert_keeps_details_until_final_receipt_is_acknowledged(self):
        self.save(reasons=["no_page"])
        self.assertTrue(self.store.sync_task("task_1", self.writer))
        self.assertFalse(self.store.get("task_1")["details_cleared"])
        self.store.claim_alert("task_1")
        self.store.mark_alert("task_1", "sent", message_id="test-receipt")
        self.assertTrue(self.store.sync_task("task_1", self.writer))
        self.assertTrue(self.store.get("task_1")["details_cleared"])
        self.assertEqual(self.database.rows["task_1", 1]["feishu_message_id"], "test-receipt")

    def test_revision_race_with_successful_remote_commit_preserves_full_pending_payload(self):
        original = self.save()
        def write_and_advance(request):
            hashes = self.writer.write_task(request)
            self.store.mark_alert("task_1", "sent", message_id="concurrent-receipt")
            return hashes
        writer = Mock()
        writer.write_task.side_effect = write_and_advance
        self.assertTrue(self.store.sync_task("task_1", writer))
        pending = self.store.get("task_1")
        self.assertEqual(pending["rows"], original["rows"])
        self.assertLess(pending["synced_revision"], pending["revision"])
        self.assertTrue(self.store.sync_task("task_1", self.writer))
        self.assertTrue(self.store.get("task_1")["details_cleared"])

    def test_two_request_threads_do_not_write_the_same_task_twice(self):
        self.save()
        calls = []
        writer = Mock()
        writer.write_task.side_effect = lambda request: calls.append(request["task_id"])
        threads = [threading.Thread(target=self.store.sync_task, args=("task_1", writer)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(calls, ["task_1"])


if __name__ == "__main__":
    unittest.main()
