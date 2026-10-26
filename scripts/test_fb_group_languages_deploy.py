"""Isolated deployment guards; no remote services or publishing calls."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import deploy_fb_group_page_languages as release


class LanguageDeploymentTests(unittest.TestCase):
    def test_projection_preserves_old_group_columns_and_ignores_metric_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "facts.sqlite3"
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript("CREATE TABLE fb_group_pool(tenant_key TEXT PRIMARY KEY,status TEXT);"
                                   "INSERT INTO fb_group_pool VALUES('t','enabled');"
                                   "CREATE TABLE metrics(id INTEGER PRIMARY KEY,value TEXT);")
                conn.commit()
            columns, before = release.facts(db)
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript("ALTER TABLE fb_group_pool ADD COLUMN routing_policy_version TEXT DEFAULT 'group_pool_v1';"
                                   "INSERT INTO metrics VALUES(1,'unrelated');")
                conn.commit()
            release.check_facts(db, columns, before)
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("UPDATE fb_group_pool SET status='paused'")
                conn.commit()
            with self.assertRaisesRegex(RuntimeError, "facts changed"):
                release.check_facts(db, columns, before)

    def test_stage_rejects_unrelated_runtime(self):
        with patch.object(release.base, "run", return_value="app.py"):
            with self.assertRaisesRegex(RuntimeError, "outside language scope"):
                release.stage()

    def test_group_scheduler_stops_before_other_producers(self):
        calls = []
        with patch.object(release.subprocess, "run", side_effect=lambda *a, **kw: calls.append("stop_group")), \
             patch.object(release.pool, "quiesce", side_effect=lambda *a: calls.append("drain_others")):
            release.quiesce({release.GROUP_UNIT: {"active": "active"}})
        self.assertEqual(calls, ["stop_group", "drain_others"])

    def test_rollback_backend_drift_stops_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"files": [{"target": str(Path(directory) / "worker.py"), "after": "expected"}]}))
            with patch.object(release.subprocess, "run") as command:
                with self.assertRaisesRegex(RuntimeError, "Compatible backend drift"):
                    release.rollback(manifest)
                command.assert_not_called()

    def test_rollback_drain_failure_restores_original_services_before_gate_change(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"files": []}))
            saved = {release.GROUP_UNIT: {"active": "active"}}
            with patch.object(release, "saved_states", return_value=saved), \
                 patch.object(release, "assert_window"), \
                 patch.object(release, "quiesce", side_effect=RuntimeError("still processing")), \
                 patch.object(release, "resume") as restore, \
                 patch.object(release.subprocess, "run") as command:
                with self.assertRaisesRegex(RuntimeError, "still processing"):
                    release.rollback(manifest)
                restore.assert_called_once_with(saved, main=False)
                command.assert_not_called()
            self.assertEqual(json.loads(manifest.read_text())["rollback_phase"], "failed_original_services_restored")


if __name__ == "__main__":
    unittest.main()
