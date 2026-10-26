"""Offline verification of the exact-job reconciliation and concurrent-change guard."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("cover_callback_recovery", Path(__file__).with_name("recover.py"))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.patch("STATE", root)
        self.patch("DB", root / "source.sqlite3")
        self.original = dict(job_id=recovery.JOB_ID, app_id="3360", content_id="132516419",
                             status="failed", error_message="catalog_read_failed_2013\nold traceback",
                             progress=96, progress_detail="callback", assets_json="{}",
                             finished_at="2026-10-26 03:09:51", updated_at="2026-10-26 03:09:51",
                             elapsed_seconds=272, token_total=123)
        self.original.update({size + "_url": "https://example.invalid/" + size for size, _ in recovery.material.SPECS})
        with sqlite3.connect(recovery.DB) as conn:
            conn.execute("CREATE TABLE drama_screenshot_job(" + ",".join(
                key + (" INTEGER" if isinstance(value, int) else " TEXT") for key, value in self.original.items()) + ")")
            conn.execute("INSERT INTO drama_screenshot_job VALUES(" + ",".join("?" for _ in self.original) + ")", list(self.original.values()))
        self.before = [dict(key="a", state="confirmed", attempts=1),
                       dict(key="b", state="confirmed", attempts=1), dict(key="c", state="prepared", attempts=0)]
        self.after = [*self.before[:2], dict(key="c", state="confirmed", attempts=1)]
        self.images = [dict(source_key=size, sha256="a" * 64) for size, _ in recovery.material.SPECS]
        recovery.save("before.json", dict(job=self.original, uploads=self.before, images=self.images))
        self.patch("uploads", mock.Mock(side_effect=[self.before, self.after]))
        self.patch("environment", mock.Mock(return_value={}))
        self.post = mock.Mock(return_value={"confirmed_ids": [1, 2, 3]})
        for name, value in (("upload_screenshot", self.post), ("screenshot_sources", mock.Mock(return_value=self.images))):
            patch = mock.patch.object(recovery.material, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def patch(self, name, value):
        patch = mock.patch.object(recovery, name, value)
        patch.start()
        self.addCleanup(patch.stop)

    def test_confirmed_images_complete_only_exact_failed_row_and_preserve_tokens(self):
        recovery.recover()
        result = recovery.job()
        self.assertEqual((result["status"], result["progress"], result["error_message"], result["token_total"]),
                         ("done", 100, "", 123))
        for size, _ in recovery.material.SPECS:
            self.assertEqual(result[size + "_url"], self.original[size + "_url"])
        self.post.assert_called_once()
        self.assertTrue((recovery.STATE / "recovered.json").is_file())

    def test_preexisting_state_drift_blocks_callback(self):
        with sqlite3.connect(recovery.DB) as conn:
            conn.execute("UPDATE drama_screenshot_job SET token_total=124")
        with self.assertRaisesRegex(RuntimeError, "frozen_state_drifted"):
            recovery.recover()
        self.post.assert_not_called()

    def test_concurrent_operator_change_is_never_overwritten_after_callback(self):
        def callback(*_args):
            with sqlite3.connect(recovery.DB) as conn:
                conn.execute("UPDATE drama_screenshot_job SET status='queued'")
            return {"confirmed_ids": [1, 2, 3]}
        self.post.side_effect = callback
        with self.assertRaisesRegex(RuntimeError, "job_changed_after_callback"):
            recovery.recover()
        self.assertEqual(recovery.job()["status"], "queued")
        self.assertTrue((recovery.STATE / "callback-result.json").is_file())
        self.assertFalse((recovery.STATE / "recovered.json").exists())

    def test_callback_failure_cannot_mark_job_complete(self):
        self.post.side_effect = RuntimeError("material_source_pending:unknown")
        with self.assertRaisesRegex(RuntimeError, "material_source_pending"):
            recovery.recover()
        self.assertEqual(recovery.job(), self.original)
        self.assertFalse((recovery.STATE / "recovered.json").exists())


if __name__ == "__main__":
    unittest.main()
