import copy
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "ops/moboreels-attribution-20261113/fb_metric_startup_fix.py"
SPEC = importlib.util.spec_from_file_location("fb_metric_fix_under_test", SOURCE)
fix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fix)
ROLLBACK_SPEC = importlib.util.spec_from_file_location("fb_fix_rollback_under_test", SOURCE.with_name("rollback.py"))
rollback = importlib.util.module_from_spec(ROLLBACK_SPEC)
ROLLBACK_SPEC.loader.exec_module(rollback)


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


class FBMetricFixTests(unittest.TestCase):
    def test_only_reviewed_business_and_runtime_changes(self):
        self.assertTrue(fix.approved_changes([
            *fix.MODULES, "features/moboreels_library/orders.py",
            fix.OPS + "/runtime/fb/" + fix.MODULES[0],
            fix.OPS + "/fb_metric_startup_fix.py", "tests/test_fb_metric_startup.py"]))
        self.assertFalse(fix.approved_changes(["features/fb_auto_posts/publisher.py"]))
        self.assertFalse(fix.approved_changes([fix.OPS + "/runtime/tt/features/tt_posts/service.py"]))

    def test_pending_main_overrides_keep_original_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.json"
            original = {"target": "/main/core.py", "phase": "main", "before": "live-sha",
                        "after": "b53-sha", "source": "/b53/core.py", "staged": "/staged/old",
                        "backup": "/private/original", "mode": 420}
            replacement = dict(original, after="fix-sha", source="/fix/core.py", staged="/staged/fix")
            manifest = {"commit": fix.BUSINESS_SHA, "phases": {"fb": {"status": "activated"}},
                        "candidates": {"fb": "/b53/candidate"}, "candidate_trees": {"fb": "original-tree"},
                        "files": [copy.deepcopy(original)], "hotfixes": {"report-query-date": {"status": "activated"}}}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            record = {"id": "fix-1", "commit": "new-sha", "release": "/fix", "status": "intent",
                      "receipt": str(Path(folder) / "receipt.json"),
                      "main_overrides": [{"target": original["target"], "original": original, "replacement": replacement}]}
            def save(target, payload):
                Path(target).write_text(json.dumps(payload), encoding="utf-8")
            fix.persist(path, record, save, require)
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["files"], [replacement])
            self.assertEqual(result["hotfixes"][fix.NAME], record)
            self.assertEqual(json.loads(Path(record["receipt"]).read_text()), record)
            self.assertEqual(result["candidates"], manifest["candidates"])
            self.assertEqual(result["candidate_trees"], manifest["candidate_trees"])
            self.assertEqual(record["main_overrides"][0]["original"], original)
            self.assertEqual(result["prepared_hotfixes"][fix.NAME]["main_overrides"], record["main_overrides"])
            # Repeated status persistence does not lose the old entry or other hotfixes.
            record["status"] = "activated"
            fix.persist(path, record, save, require)
            result = json.loads(path.read_text())
            self.assertEqual(result["hotfixes"]["report-query-date"], {"status": "activated"})
            result["phases"]["main"] = {"status": "installing"}
            save(path, result)
            with self.assertRaisesRegex(RuntimeError, "Main phase"):
                fix.persist(path, record, save, require)

    def test_pause_requires_matching_version_and_disabled_fb(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "dispatch.sqlite3"
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE pipeline_config(id INTEGER,version INTEGER,config_json TEXT)")
                conn.execute("INSERT INTO pipeline_config VALUES(1,193,?)", (json.dumps({"enabled_channels": ["x", "tt"]}),))
                conn.commit()
            receipt = {"status": "committed", "lane": "fb", "target_enabled": False,
                       "changed": True, "after_version": 193}
            with patch.object(fix, "COORDINATOR", db):
                fix.check_paused(receipt, require)
                with self.assertRaisesRegex(RuntimeError, "admission/version"):
                    fix.check_paused(dict(receipt, after_version=192), require)
                with closing(sqlite3.connect(db)) as conn:
                    conn.execute("UPDATE pipeline_config SET config_json=?", (json.dumps({"enabled_channels": ["fb"]}),))
                    conn.commit()
                with self.assertRaisesRegex(RuntimeError, "admission/version"):
                    fix.check_paused(receipt, require)

    def test_rollback_recognizes_only_activated_exact_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            deployment = root / "deployment-1"
            private = deployment / "private" / "fix-1"
            private.mkdir(parents=True)
            release = root / "new-release"
            candidates = root / "candidates"
            candidate = str(candidates / "new-sha" / deployment.name / "fb-metric-fix")
            files, entries, hashes = [], [], {}
            after = hashlib.sha256(b"reviewed fix").hexdigest()
            for relative in fix.MODULES:
                target = str(root / "logical" / relative)
                source = str(release / fix.OPS / "runtime/fb" / relative)
                backup = str(private / Path(relative).name)
                hashes[source], hashes[backup] = after, "b53-before"
                files.append(dict(target=target, relative=relative, source=source,
                                  backup=backup, before="b53-before", after=after))
                entries.append(dict(target=target, before="prebusiness", after="b53-before"))
            record = dict(status="activated", business_sha=fix.BUSINESS_SHA, commit="new-sha",
                          release=str(release), candidate=candidate, current=candidate,
                          previous_current="old-candidate", previous_tree="old-tree", candidate_tree="new-tree",
                          receipt=str(private / "receipt.json"), files=files)
            Path(record["receipt"]).write_text(json.dumps(record), encoding="utf-8")
            manifest = dict(commit=fix.BUSINESS_SHA, candidates={"fb": "old-candidate"},
                            candidate_trees={"fb": "old-tree"}, roots={"fb": {"logical": str(root / "logical")}},
                            hotfixes={"fb-metric-startup": record})
            deploy = Mock(require=require, CANDIDATES=candidates)
            deploy.tree_digest.side_effect = lambda path: {candidate: "new-tree", "old-candidate": "old-tree"}[str(path)]
            deploy.digest.side_effect = lambda path: hashes[str(path)]
            with patch.object(rollback.subprocess, "check_output", return_value=b"reviewed fix"):
                active, adjusted = rollback.fb_hotfix_candidate(manifest, deployment, deploy, entries)
            self.assertEqual(active, candidate)
            self.assertTrue(all(row["after"] == after for row in adjusted))
            self.assertTrue(all(row["after"] == "b53-before" for row in entries))
            deploy.verify.assert_called_once_with(release, "new-sha")
            record["status"] = "needs_reconciliation"
            with self.assertRaisesRegex(RuntimeError, "requires reconciliation"):
                rollback.fb_hotfix_candidate(manifest, deployment, deploy, entries)


if __name__ == "__main__":
    unittest.main()
