#!/usr/bin/env python3
"""The superseded legacy-table workflow must never connect or mutate again."""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import drama_youtube_three_table_rehearsal as rehearsal
from scripts import migrate_drama_youtube_unified_schema as migration


class RetiredLegacyWorkflowTests(unittest.TestCase):
    def test_all_former_migration_entrypoints_refuse_before_network(self):
        names = ("load_backup_evidence_file", "_connect", "_inspect", "_validate_existing",
                 "_run_migration", "migrate", "verify_runtime_writer", "main")
        with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")) as network:
            for name in names:
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "retired"):
                    getattr(migration, name)({"host": "101.32.56.53", "database": "kunlunads_dev"}, apply=True)
            network.assert_not_called()

    def test_all_former_snapshot_entrypoints_refuse_before_network(self):
        names = ("export_snapshot", "_connect", "_load_snapshot", "_validate_table_snapshot_evidence",
                 "validate_table_snapshot_evidence", "rehearse_loopback", "main")
        with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")) as network:
            for name in names:
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "retired"):
                    getattr(rehearsal, name)({"schema": "kunlunads_dev", "candidate_git_sha": "a" * 40})
            network.assert_not_called()

    def test_tombstones_cannot_import_a_database_or_load_historical_evidence(self):
        for module in (migration, rehearsal):
            self.assertIs(module.RETIRED, True)
            self.assertEqual(module.LEGACY_SCHEMA, "kunlunads_dev")
            source = Path(module.__file__).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add((node.module or "").split(".")[0])
            self.assertLessEqual(imports, {"__future__", "json", "sys"})
            self.assertNotIn("pymysql", source)
            self.assertNotIn("MIGRATIONS =", source)
            self.assertIn("bootstrap_drama_youtube_ads_ai.py", source)

    def test_every_cli_mode_fails_with_safe_retirement_message(self):
        for module in (migration, rehearsal):
            for args in ([], ["--help"], ["--apply", "--credential-file", "PRIVATE_FAKE_CONFIG"],
                         ["--verify-runtime-writer"], ["--rehearsal"]):
                with self.subTest(script=Path(module.__file__).name, args=args):
                    result = subprocess.run([sys.executable, module.__file__, *args], capture_output=True,
                                            text=True, timeout=10)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("legacy_youtube_workflow_retired", result.stderr)
                    self.assertNotIn("PRIVATE_FAKE_CONFIG", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
