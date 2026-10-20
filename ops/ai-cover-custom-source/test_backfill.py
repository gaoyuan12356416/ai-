import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import types
import unittest

spec = importlib.util.spec_from_file_location("cover_backfill", Path(__file__).with_name("backfill.py"))
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


@contextlib.contextmanager
def writing(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "upload.sqlite3"
        with writing(self.ledger) as connection:
            connection.execute("CREATE TABLE uploads(key TEXT PRIMARY KEY,state TEXT,attempts INTEGER,final_id INTEGER)")
        self.calls = []

    def manifest(self, count, name="manifest.json", start=0, **extra):
        sources = [dict(source_kind="screenshot", source_key="source:" + str(i),
                        app_id=1479, sha256=hashlib.sha256(str(i).encode()).hexdigest())
                   for i in range(start, start + count)]
        body = dict(cutoff="2026-10-20T12:00:00+08:00", window_hours=168,
                    sources=sources, issues=[],
                    sha256=hashlib.sha256(backfill.encoded(sources).encode()).hexdigest(), **extra)
        path = self.root / name
        backfill.write_json(path, body)
        return path, body

    def put(self, key, state="confirmed", attempts=1):
        with writing(self.ledger) as connection:
            connection.execute("INSERT OR REPLACE INTO uploads VALUES(?,?,?,?)",
                               (key, state, attempts, 123 if state == "confirmed" else None))

    def invoke(self, command, **kwargs):
        keys = command[command.index("--keys") + 1:]
        self.calls.append(keys)
        self.assertLessEqual(len(keys), 50)
        self.assertEqual(command[command.index("--max-new") + 1], "50")
        self.assertGreater(kwargs["timeout"], 0)
        for key in keys:
            self.put(key)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def progress(self):
        return json.loads((self.root / "backfill-progress.json").read_text())

    def test_serial_rounds_keep_original_order_and_skip_confirmed(self):
        path, body = self.manifest(123)
        keys = [backfill.identity(source) for source in body["sources"]]
        for key in keys[:3]:
            self.put(key)
        result = backfill.run_backfill(path, root=self.root, invoke=self.invoke)
        self.assertEqual([len(call) for call in self.calls], [50, 50, 20])
        self.assertEqual([key for call in self.calls for key in call], keys[3:])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["summary"]["confirmed"], 123)
        self.assertEqual(len(result["rounds"]), 3)
        self.assertEqual(result["cutoff"], body["cutoff"])

    def test_unknown_stops_before_first_child(self):
        path, body = self.manifest(2)
        self.put(backfill.identity(body["sources"][0]), "unknown")
        with self.assertRaisesRegex(backfill.BackfillStopped, "unresolved_ledger"):
            backfill.run_backfill(path, root=self.root, invoke=self.invoke)
        self.assertFalse(self.calls)
        self.assertEqual(self.progress()["status"], "stopped")

    def test_rejected_blocked_and_accepted_also_stop_without_sending(self):
        path, body = self.manifest(1)
        key = backfill.identity(body["sources"][0])
        for state in ("rejected", "blocked", "accepted"):
            with self.subTest(state=state):
                self.put(key, state)
                with self.assertRaisesRegex(backfill.BackfillStopped, "unresolved_ledger"):
                    backfill.run_backfill(path, root=self.root, resume=True, invoke=self.invoke)
        self.assertFalse(self.calls)

    def test_parallel_runner_is_rejected_without_waiting(self):
        with backfill.runner_lock(self.root):
            with self.assertRaisesRegex(backfill.BackfillStopped, "already_running"):
                with backfill.runner_lock(self.root):
                    self.fail("second runner must not acquire the lock")

    def test_partial_failure_never_automatically_reposts(self):
        path, _ = self.manifest(60)

        def partial(command, **kwargs):
            keys = command[command.index("--keys") + 1:]
            self.calls.append(keys)
            self.put(keys[0])
            self.put(keys[1], "unknown")
            return types.SimpleNamespace(returncode=1)

        with self.assertRaisesRegex(backfill.BackfillStopped, "manage_round_failed"):
            backfill.run_backfill(path, root=self.root, invoke=partial)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(backfill.BackfillStopped, "explicit_resume"):
            backfill.run_backfill(path, root=self.root, invoke=self.invoke)
        with self.assertRaisesRegex(backfill.BackfillStopped, "unresolved_ledger"):
            backfill.run_backfill(path, root=self.root, resume=True, invoke=self.invoke)
        self.assertEqual(len(self.calls), 1)

    def test_timeout_stops_without_second_call(self):
        path, _ = self.manifest(60)

        def timeout(command, **kwargs):
            self.calls.append(command)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.assertRaisesRegex(backfill.BackfillStopped, "timeout_no_retry"):
            backfill.run_backfill(path, root=self.root, invoke=timeout)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.progress()["status"], "stopped")

    def test_child_success_without_confirmed_rows_is_not_completion(self):
        path, _ = self.manifest(1)
        with self.assertRaisesRegex(backfill.BackfillStopped, "did_not_confirm"):
            backfill.run_backfill(path, root=self.root,
                                  invoke=lambda *args, **kwargs: types.SimpleNamespace(returncode=0))
        self.assertEqual(self.progress()["status"], "stopped")

    def test_manifest_change_between_rounds_stops(self):
        path, _ = self.manifest(60)

        def replace_manifest(command, **kwargs):
            result = self.invoke(command, **kwargs)
            altered = json.loads(path.read_text())
            altered["created_at"] = "changed-after-first-batch"
            backfill.write_json(path, altered)
            return result

        with self.assertRaisesRegex(backfill.BackfillStopped, "manifest_changed"):
            backfill.run_backfill(path, root=self.root, invoke=replace_manifest)
        self.assertEqual(len(self.calls), 1)

    def test_main_and_supplement_have_same_parent_and_cutoff(self):
        main, body = self.manifest(2)
        candidate = dict(source_kind="screenshot", source_key="source:2", app_id=1479,
                         sha256=hashlib.sha256(b"2").hexdigest())
        body["issues"] = [dict(reason="legacy_final_pending", candidate=candidate)]
        backfill.write_json(main, body)
        parent = dict(path=str(main), sha256=body["sha256"],
                      file_sha256=hashlib.sha256(main.read_bytes()).hexdigest())
        supplement, _ = self.manifest(1, "supplement.json", start=2,
                                      kind="legacy_final_supplement", parent_manifest=parent)
        result = backfill.run_backfill(main, [supplement], root=self.root, invoke=self.invoke)
        self.assertEqual([len(x) for x in self.calls], [2, 1])
        self.assertEqual(result["summary"]["confirmed"], 3)
        self.assertEqual(result["status"], "complete")

    def test_unresolved_parent_candidate_prevents_false_completion(self):
        main, body = self.manifest(1)
        candidate = dict(source_kind="screenshot", source_key="pending-other",
                         app_id=1479, sha256="a" * 64)
        body["issues"] = [dict(reason="legacy_final_pending", candidate=candidate)]
        backfill.write_json(main, body)
        with self.assertRaisesRegex(backfill.BackfillStopped, "need_supplement"):
            backfill.run_backfill(main, root=self.root, invoke=self.invoke)
        self.assertEqual(self.progress()["status"], "stopped")

    def test_other_unresolved_issue_prevents_false_completion(self):
        main, body = self.manifest(1)
        body["issues"] = [dict(source_kind="youtube", source_key="unverified-source",
                              reason="accepted_time_evidence_missing")]
        backfill.write_json(main, body)
        with self.assertRaisesRegex(backfill.BackfillStopped, "manifest_unresolved_issues"):
            backfill.run_backfill(main, root=self.root, invoke=self.invoke)
        self.assertEqual(self.progress()["summary"]["confirmed"], 1)
        self.assertEqual(self.progress()["status"], "stopped")
        self.assertEqual(self.progress()["unresolved_issues"][0]["reason"], "accepted_time_evidence_missing")

    def test_explicitly_excluded_issue_does_not_block_completion(self):
        main, body = self.manifest(1)
        body["issues"] = [dict(source_kind="youtube", source_key="never-uploaded",
                              reason="legacy_pending_never_uploaded", excluded=True)]
        backfill.write_json(main, body)
        result = backfill.run_backfill(main, root=self.root, invoke=self.invoke)
        self.assertEqual(result["status"], "complete")

    def test_finite_round_limit_and_all_confirmed_noop(self):
        path, _ = self.manifest(60)
        with self.assertRaisesRegex(backfill.BackfillStopped, "finite_round_limit"):
            backfill.run_backfill(path, root=self.root, max_rounds=1, invoke=self.invoke)
        self.assertEqual(len(self.calls), 1)
        result = backfill.run_backfill(path, root=self.root, resume=True, invoke=self.invoke)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.calls), 2)
        backfill.run_backfill(path, root=self.root, invoke=self.invoke)
        self.assertEqual(len(self.calls), 2)

    def test_exception_and_invalid_supplement_fail_closed(self):
        path, body = self.manifest(1)

        def failure(*args, **kwargs):
            raise OSError("private details must not be persisted")

        with self.assertRaisesRegex(backfill.BackfillStopped, "runner_exception:OSError"):
            backfill.run_backfill(path, root=self.root, invoke=failure)
        self.assertNotIn("private details", (self.root / "backfill-progress.json").read_text())
        supplement, _ = self.manifest(1, "bad.json", start=1,
                                      kind="legacy_final_supplement",
                                      parent_manifest=dict(sha256=body["sha256"], file_sha256="wrong"))
        with self.assertRaisesRegex(ValueError, "supplement_parent_mismatch"):
            backfill.load_bundle(path, [supplement])


if __name__ == "__main__":
    unittest.main()
