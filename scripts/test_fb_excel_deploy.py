"""Isolated rollout/rollback failure contracts; no server or publisher calls."""
import copy
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch

import deploy_fb_excel_publish as release


class DeploymentFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for obj, key, value in ((release, "ROOT", self.root / "release"),
                                (release.base, "BASE", self.root / "data"),
                                (release.base, "MAIN", self.root / "main"),
                                (release.base, "FB", self.root / "fb"),
                                (release.base, "STATIC", self.root / "public"),
                                (release.base, "DB", self.root / "operational.sqlite3"),
                                (release, "DROPIN", self.root / "systemd/fb.service.d/zz-fb-video-excel.conf")):
            self.stack.enter_context(patch.object(obj, key, value))
        self.commit = "a" * 40
        self.head, self.old = {}, {}
        new_files = {"features/fb_video_publish/excel.py", "static/fb-excel-publish.js",
                     "static/fb-excel-template.xlsx", release.GATE_SOURCE}
        for relative in release.ALLOWED:
            data = ("VALUE = " + repr("new " + relative) + "\n").encode() if relative.endswith(".py") else ("new " + relative).encode()
            self.head[relative] = release.GATE_ON if relative == release.GATE_SOURCE else data
            self.old[relative] = None if relative in new_files else ("old " + relative).encode()
            source = release.ROOT / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(self.head[relative])
            for target, _ in release.destinations(relative):
                target.parent.mkdir(parents=True, exist_ok=True)
                if self.old[relative] is not None:
                    target.write_bytes(self.old[relative])
        (release.base.MAIN / "app.py").write_bytes(b"unrelated main integration")
        with closing(sqlite3.connect(release.base.DB)) as conn:
            conn.executescript("CREATE TABLE retained(id INTEGER PRIMARY KEY,status TEXT,url TEXT);"
                               "INSERT INTO retained VALUES(1,'unknown','https://fixture.test/1');"
                               "CREATE TABLE fb_video_batch(request_json TEXT);")
        self.db_before = release.base.DB.read_bytes()
        self.link = self.root / "immutable-link.html"
        self.link.write_bytes(b"existing immutable link")
        self.saved = {u: {"active": "active", "enabled": "enabled"} for u in release.STATE_UNITS}
        self.saved[release.pool_release.TIMERS[0]] = {"active": "inactive", "enabled": "disabled"}
        self.stack.enter_context(patch.object(release, "git_bytes", side_effect=lambda ref, rel:
                                             (self.head if ref == "HEAD" else self.old)[rel]))
        self.stack.enter_context(patch.object(release.base, "run", side_effect=lambda *args:
                                             "\n".join(sorted(release.ALLOWED)) if "diff" in args else self.commit))
        self.stack.enter_context(patch.object(release.pool_release, "saved_states", return_value=self.saved))
        self.stack.enter_context(patch.object(release.base, "state", return_value={"active": "inactive", "enabled": "enabled"}))
        self.stop = self.stack.enter_context(patch.object(release.pool_release, "quiesce"))
        self.idle = self.stack.enter_context(patch.object(release.pool_release, "in_flight", return_value=([], 0)))
        self.window = self.stack.enter_context(patch.object(release, "assert_pool_window", return_value=1))
        self.resume = self.stack.enter_context(patch.object(release, "resume"))
        self.commands = self.stack.enter_context(patch.object(release.subprocess, "run"))

    def manifest(self):
        path = next((release.base.BASE / "backups").glob("*/manifest.json"))
        return path, json.loads(path.read_text(encoding="utf-8"))

    def deploy(self):
        targets = release.stage_targets()
        release.apply_release(targets, self.commit)
        return self.manifest()

    def assert_facts_preserved(self):
        self.assertEqual(self.db_before, release.base.DB.read_bytes())
        self.assertEqual(b"existing immutable link", self.link.read_bytes())

    def assert_runtime(self, *, compatible, gate=None):
        for relative in release.ALLOWED:
            expected = self.head[relative] if compatible and relative in release.VIDEO_FILES | release.AUTO_FILES else self.old[relative]
            if relative == release.GATE_SOURCE and gate is not None:
                expected = gate
            for target, _ in release.destinations(relative):
                self.assertEqual(release.sha_bytes(expected), release.base.sha(target), str(target))


class ExcelDeploymentTests(DeploymentFixture):
    def test_all_41_destinations_and_missing_baseline_are_exact(self):
        targets = release.stage_targets()
        self.assertEqual(41, len(targets))
        self.assertEqual(9, sum(not target.exists() for _, target, _ in targets))
        for relative in release.AUTO_FILES:
            self.assertEqual([(release.base.FB / relative, release.base.FB)], release.destinations(relative))
        with patch.object(release.base, "run", return_value="app.py"):
            with self.assertRaisesRegex(RuntimeError, "outside Excel"):
                release.stage_targets()
        with patch.object(release.base, "run", return_value="features/fb_video_publish/core.py"):
            with self.assertRaisesRegex(RuntimeError, "Incomplete committed"):
                release.stage_targets()

    def test_source_or_live_drift_blocks_before_service_stop(self):
        targets = release.stage_targets()
        source, target, _ = targets[0]
        original = source.read_bytes()
        source.write_bytes(b"uncommitted change")
        with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
            release.apply_release(targets, self.commit)
        source.write_bytes(original)
        target.write_bytes(b"unreviewed live change")
        with self.assertRaisesRegex(RuntimeError, "Live baseline drift"):
            release.stage_targets()
        self.stop.assert_not_called()
        self.assert_facts_preserved()

    def test_apply_backups_and_preserves_original_states_and_facts(self):
        path, manifest = self.deploy()
        self.assertEqual("deployed", manifest["phase"])
        self.assertTrue(manifest["activation_attempted"])
        self.assertTrue(manifest["compatible_backend_installed"])
        self.assertEqual(self.saved, manifest["states"])
        self.assertTrue(Path(manifest["database_backup"]["path"]).is_file())
        self.assertEqual(manifest["database_backup"]["sha256"], release.base.sha(Path(manifest["database_backup"]["path"])))
        self.resume.assert_called_once_with(self.saved, manifest["main_app_sha256"], restart_main=True, gate_enabled=True)
        release.validate_manifest(manifest, path, compatible=True)
        self.assert_facts_preserved()

    def test_drain_failure_resumes_without_runtime_changes(self):
        self.stop.side_effect = RuntimeError("publisher still busy")
        with self.assertRaisesRegex(RuntimeError, "still busy"):
            release.apply_release(release.stage_targets(), self.commit)
        self.assert_runtime(compatible=False)
        self.assertEqual("failed_before_install", self.manifest()[1]["phase"])
        self.resume.assert_called_once()
        self.assert_facts_preserved()

    def test_partial_copy_failure_before_activation_restores_new_and_old_files(self):
        target = release.base.FB / "features/fb_video_publish/worker.py"
        atomic = release.base.atomic_bytes
        def write(data, path, mode=0o644):
            if path == target and data == self.head["features/fb_video_publish/worker.py"]:
                raise OSError("copy interrupted")
            atomic(data, path, mode)
        with patch.object(release.base, "atomic_bytes", side_effect=write):
            with self.assertRaisesRegex(OSError, "copy interrupted"):
                release.apply_release(release.stage_targets(), self.commit)
        self.assert_runtime(compatible=False)
        self.assertEqual("failed_restored_before_activation", self.manifest()[1]["phase"])
        self.assertFalse(self.manifest()[1]["activation_attempted"])
        self.resume.assert_called_once()
        self.assert_facts_preserved()

    def test_reload_failure_before_activation_can_restore_old_worker(self):
        self.commands.side_effect = [OSError("daemon reload failed"), None]
        with self.assertRaisesRegex(OSError, "daemon reload"):
            release.apply_release(release.stage_targets(), self.commit)
        self.assert_runtime(compatible=False)
        self.assertFalse(self.manifest()[1]["activation_attempted"])

    def test_uncertain_first_restart_never_restores_old_worker(self):
        first = []
        def resume(saved, app_hash, **kwargs):
            if not first:
                first.append(True)
                self.assertEqual("activation_attempted", self.manifest()[1]["phase"])
                raise RuntimeError("health failed after start")
        self.resume.side_effect = resume
        with self.assertRaisesRegex(RuntimeError, "health failed"):
            release.apply_release(release.stage_targets(), self.commit)
        self.assert_runtime(compatible=True, gate=release.GATE_OFF)
        self.assertEqual("failed_closed_compatible", self.manifest()[1]["phase"])
        self.assertEqual(False, self.resume.call_args.kwargs["gate_enabled"])
        self.assertTrue(self.resume.call_args.kwargs["restart_main"])
        self.assert_facts_preserved()

    def test_emergency_recovery_closes_gate_after_failed_state_without_new_window_block(self):
        current = copy.deepcopy(self.saved)
        current[release.base.FB_UNIT]["active"] = "failed"
        self.resume.side_effect = [RuntimeError("sidecar start failed"), None]
        # The third check precedes activation. No fourth window check is allowed
        # to prevent emergency gate closure after an uncertain service restart.
        self.window.side_effect = [1, 1, 1, AssertionError("emergency window must not delay closure")]
        with patch.object(release.pool_release, "saved_states", side_effect=[self.saved, current]):
            with self.assertRaisesRegex(RuntimeError, "sidecar start failed"):
                release.apply_release(release.stage_targets(), self.commit)
        self.assert_runtime(compatible=True, gate=release.GATE_OFF)
        self.assertEqual("failed_closed_compatible", self.manifest()[1]["phase"])

    def test_failed_restore_keeps_producers_stopped_and_manifest_recoverable(self):
        target = release.base.FB / "features/fb_video_publish/worker.py"
        backup_target = release.base.MAIN / "features/fb_video_publish/client.py"
        atomic = release.base.atomic_bytes
        def write(data, path, mode=0o644):
            if path == target and data == self.head["features/fb_video_publish/worker.py"]:
                raise OSError("copy failed")
            if path == backup_target and data == self.old["features/fb_video_publish/client.py"]:
                raise OSError("restore failed")
            atomic(data, path, mode)
        with patch.object(release.base, "atomic_bytes", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "recovery incomplete"):
                release.apply_release(release.stage_targets(), self.commit)
        self.resume.assert_not_called()
        self.assertEqual("recovery_required", self.manifest()[1]["phase"])
        self.assert_facts_preserved()

    def test_functional_rollback_only_closes_excel_and_restores_static(self):
        path, _ = self.deploy()
        self.resume.reset_mock()
        release.rollback(path)
        self.assert_runtime(compatible=True, gate=release.GATE_OFF)
        self.assertFalse(self.resume.call_args.kwargs["restart_main"])
        self.assertFalse(self.resume.call_args.kwargs["gate_enabled"])
        self.assertEqual("functional_rollback", self.manifest()[1]["phase"])
        release.rollback(path)  # Same verified manifest remains safe to replay.
        self.assert_facts_preserved()

    def test_corrupt_backup_or_manifest_path_rejected_before_stopping(self):
        path, manifest = self.deploy()
        self.stop.reset_mock()
        backed = next(item for item in manifest["files"] if item["backup"])
        Path(backed["backup"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "backup drift"):
            release.rollback(path)
        self.stop.assert_not_called()

    def test_manifest_cannot_target_other_files_or_backup_sources(self):
        path, manifest = self.deploy()
        for field in ("target", "source", "backup"):
            altered = copy.deepcopy(manifest)
            item = next(item for item in altered["files"] if item["backup"])
            item[field] = str(self.root / "outside")
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                release.validate_manifest(altered, path)
        altered = copy.deepcopy(manifest)
        altered["files"].append(copy.deepcopy(altered["files"][0]))
        with self.assertRaisesRegex(RuntimeError, "duplicated"):
            release.validate_manifest(altered, path)

    def test_manual_rollback_refuses_incomplete_compatible_backend(self):
        path, manifest = self.deploy()
        worker = release.base.FB / "features/fb_video_publish/worker.py"
        worker.write_bytes(self.old["features/fb_video_publish/worker.py"])
        self.stop.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "Compatible backend incomplete"):
            release.rollback(path)
        self.stop.assert_not_called()

    def test_pending_work_prevents_old_file_restore_after_copy_failure(self):
        self.idle.side_effect = [([], 0), (["busy.service"], 1)]
        target = release.base.FB / "features/fb_video_publish/worker.py"
        atomic = release.base.atomic_bytes
        def write(data, path, mode=0o644):
            if path == target and data == self.head["features/fb_video_publish/worker.py"]:
                raise OSError("copy failed")
            atomic(data, path, mode)
        with patch.object(release.base, "atomic_bytes", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "recovery incomplete"):
                release.apply_release(release.stage_targets(), self.commit)
        self.resume.assert_not_called()
        self.assertEqual("recovery_required", self.manifest()[1]["phase"])

    def test_external_acceptance_before_our_restart_keeps_compatible_backend(self):
        def command(*args, **kwargs):
            if self.commands.call_count == 1:
                with closing(sqlite3.connect(release.base.DB)) as conn:
                    conn.execute("INSERT INTO fb_video_batch VALUES(?)", ('{"schema_version":3}',))
                    conn.commit()
                raise OSError("daemon reload failed after external acceptance")
        self.commands.side_effect = command
        with self.assertRaisesRegex(OSError, "external acceptance"):
            release.apply_release(release.stage_targets(), self.commit)
        self.assert_runtime(compatible=True, gate=release.GATE_OFF)
        self.assertTrue(self.manifest()[1]["accepted_new_protocol_observed"])
        self.assertEqual("failed_closed_compatible", self.manifest()[1]["phase"])
        self.assertTrue(release.accepted_new_protocol())

    def test_new_protocol_during_partial_copy_keeps_mixed_code_stopped(self):
        with closing(sqlite3.connect(release.base.DB)) as conn:
            conn.execute("INSERT INTO fb_video_batch VALUES(?)", ('{"schema_version":3}',))
            conn.commit()
        target = release.base.FB / "features/fb_video_publish/worker.py"
        atomic = release.base.atomic_bytes
        def write(data, path, mode=0o644):
            if path == target and data == self.head["features/fb_video_publish/worker.py"]:
                raise OSError("copy failed")
            atomic(data, path, mode)
        with patch.object(release.base, "atomic_bytes", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "recovery incomplete"):
                release.apply_release(release.stage_targets(), self.commit)
        self.resume.assert_not_called()
        self.assertEqual("recovery_required", self.manifest()[1]["phase"])

    def test_database_read_failure_never_authorizes_old_backend_restore(self):
        self.commands.side_effect = OSError("daemon reload failed")
        with patch.object(release, "accepted_new_protocol", side_effect=sqlite3.OperationalError("database unavailable")):
            with self.assertRaisesRegex(RuntimeError, "recovery incomplete"):
                release.apply_release(release.stage_targets(), self.commit)
        self.resume.assert_not_called()
        self.assertTrue(self.manifest()[1]["compatible_backend_installed"])
        self.assertEqual("recovery_required", self.manifest()[1]["phase"])

    def test_drift_after_reopen_still_closes_admission_and_holds_services(self):
        def resume(*args, **kwargs):
            path, manifest = self.manifest()
            original = next(item for item in manifest["files"] if item["backup"])
            Path(original["backup"]).write_bytes(b"unexpected backup drift")
            raise RuntimeError("probe failed after reopening")
        self.resume.side_effect = resume
        with self.assertRaisesRegex(RuntimeError, "recovery incomplete") as raised:
            release.apply_release(release.stage_targets(), self.commit)
        manifest = self.manifest()[1]
        self.assertEqual("recovery_required", manifest["phase"])
        self.assertTrue(manifest["emergency_hold_attempted"])
        self.assertTrue(manifest["emergency_hold_completed"])
        self.assertEqual(release.GATE_OFF, release.DROPIN.read_bytes())
        self.assertEqual(self.head["features/fb_video_publish/worker.py"],
                         (release.base.FB / "features/fb_video_publish/worker.py").read_bytes())
        self.assertIn("probe failed", str(raised.exception.__cause__))
        self.resume.assert_called_once()  # No producer restart after failed recovery.

    def test_emergency_drain_failure_does_not_claim_runtime_closed_or_resume(self):
        def resume(*args, **kwargs):
            path, manifest = self.manifest()
            original = next(item for item in manifest["files"] if item["backup"])
            Path(original["backup"]).write_bytes(b"unexpected drift")
            raise RuntimeError("original health failure")
        self.resume.side_effect = resume
        self.stop.side_effect = [None, RuntimeError("platform request still in flight")]
        with self.assertRaisesRegex(RuntimeError, "recovery incomplete") as raised:
            release.apply_release(release.stage_targets(), self.commit)
        manifest = self.manifest()[1]
        self.assertTrue(manifest["emergency_hold_attempted"])
        self.assertFalse(manifest["emergency_hold_completed"])
        self.assertEqual("RuntimeError", manifest["emergency_hold_failure_type"])
        self.assertEqual(release.GATE_OFF, release.DROPIN.read_bytes())
        self.assertIn("original health failure", str(raised.exception.__cause__))
        self.resume.assert_called_once()


class StandaloneGuards(unittest.TestCase):
    def test_only_exact_pushed_ref_is_accepted(self):
        values = ["a" * 40, "", "git@github.com:gaoyuan12356416/ai-.git",
                  "b" * 40 + "\t" + release.REMOTE_REF]
        with patch.object(release.base, "run", side_effect=values):
            with self.assertRaisesRegex(RuntimeError, "exact pushed"):
                release.verified_commit()
        with self.assertRaisesRegex(RuntimeError, "branch"):
            release.verified_commit("refs/heads/main")

    def test_baseline_absence_is_not_a_git_failure(self):
        with patch.object(release.base, "run", return_value="a" * 40), \
                patch.object(release.subprocess, "check_output", return_value=b""):
            self.assertIsNone(release.git_bytes(release.BASELINE, "features/fb_video_publish/excel.py"))
        with patch.object(release.base, "run", return_value="a" * 40), \
                patch.object(release.subprocess, "check_output", side_effect=subprocess.CalledProcessError(128, "git")):
            with self.assertRaises(subprocess.CalledProcessError):
                release.git_bytes(release.BASELINE, "features/fb_video_publish/excel.py")

    def test_git_symlink_source_is_rejected(self):
        listing = b"120000 blob " + b"a" * 40 + b"\tfeatures/fb_video_publish/excel.py\0"
        with patch.object(release.base, "run", return_value="a" * 40), \
                patch.object(release.subprocess, "check_output", return_value=listing):
            with self.assertRaisesRegex(RuntimeError, "regular blob"):
                release.git_bytes("HEAD", "features/fb_video_publish/excel.py")

    def test_gate_readback_precedes_resuming_producers(self):
        saved = {u: {"active": "active", "enabled": "enabled"} for u in release.STATE_UNITS}
        order = []
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory)
            (main / "app.py").write_bytes(b"main")
            with patch.object(release.base, "MAIN", main), patch.object(release.base, "health"), \
                    patch.object(release.base, "state", side_effect=lambda unit: saved[unit]), \
                    patch.object(release.subprocess, "run") as commands, \
                    patch.object(release.pool_release, "restore_states", side_effect=lambda _, units: order.append(units)), \
                    patch.object(release, "verify_gate", side_effect=lambda value: order.append(value)):
                release.resume(saved, release.base.sha(main / "app.py"), restart_main=True, gate_enabled=True)
            commands.assert_called_once_with(["systemctl", "restart", release.base.API_UNIT], check=True)
        self.assertEqual([[release.base.FB_UNIT], True, release.PRODUCERS], order)

    def test_gate_mismatch_keeps_producers_stopped(self):
        saved = {u: {"active": "active", "enabled": "enabled"} for u in release.STATE_UNITS}
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory)
            (main / "app.py").write_bytes(b"main")
            with patch.object(release.base, "MAIN", main), patch.object(release.base, "health"), \
                    patch.object(release.pool_release, "restore_states") as restore, \
                    patch.object(release, "verify_gate", side_effect=RuntimeError("gate mismatch")):
                with self.assertRaisesRegex(RuntimeError, "gate mismatch"):
                    release.resume(saved, release.base.sha(main / "app.py"), gate_enabled=False)
            restore.assert_called_once_with(saved, [release.base.FB_UNIT])


if __name__ == "__main__":
    unittest.main()
