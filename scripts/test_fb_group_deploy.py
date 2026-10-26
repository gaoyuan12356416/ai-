"""Deployment rehearsal primitives against isolated files/SQLite, no services."""
import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy_fb_group_material_pool as release
from features.fb_video_publish.core import Store


class DeploymentTests(unittest.TestCase):
    def test_additive_group_schema_preserves_all_existing_video_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "isolated.sqlite"
            Store(db)
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE fb_video_preservation_probe(id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO fb_video_preservation_probe VALUES(1,'immutable')")
                conn.commit()
            before = release.immutable_facts(db)
            Store(db)
            release.check_frozen_facts(before, db)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fb_group_%'").fetchall()
                self.assertEqual(len(tables), 6)
                conn.execute("UPDATE fb_video_preservation_probe SET value='changed'")
                conn.commit()
            with self.assertRaisesRegex(RuntimeError, "Existing FB records changed"):
                release.check_frozen_facts(before, db)

    def test_runtime_allowlist_rejects_unrelated_app_patch(self):
        with patch.object(release.base, "run", return_value="app.py"):
            with self.assertRaisesRegex(RuntimeError, "outside reviewed scope"):
                release.stage(Path("unused"))

    def test_drifted_backend_prevents_capability_rollback_before_services(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.json"
            path.write_text(json.dumps({"files": [{"target": str(Path(folder) / "worker.py"), "after": "expected"}]}))
            with patch.object(release.subprocess, "run") as mutate:
                with self.assertRaisesRegex(RuntimeError, "Compatible backend drift"):
                    release.rollback(path)
                mutate.assert_not_called()

    def test_collector_exclusion_requires_exact_reviewed_source_hashes(self):
        with patch.object(release, "_active_work", return_value=(["fb-auto-post-effects.service"], 0)):
            with patch.object(release.base, "sha", side_effect=["reviewed", "drifted"]):
                with self.assertRaisesRegex(RuntimeError, "collector drift"):
                    release.active_work()
            with patch.object(release.base, "sha", return_value="reviewed"):
                self.assertEqual(release.active_work(), ([], 0))


if __name__ == "__main__":
    unittest.main()
