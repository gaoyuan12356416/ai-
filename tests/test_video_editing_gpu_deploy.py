import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import deploy_video_editing_gpu as deploy


class DeployTests(unittest.TestCase):
    def test_environment_overrides_preserve_legacy_and_close_intake(self):
        old = {"COS_BUCKET": "advertising-12345", "COS_REGION": "ap-hongkong",
               "COS_SECRET_KEY": "never-output", "DRAMA_FFMPEG": "/opt/ffmpeg"}
        values = deploy.new_environment(old, "a" * 40)
        self.assertNotIn("COS_SECRET_KEY", values)
        self.assertNotIn("DRAMA_FFMPEG", values)
        self.assertEqual(values["VIDEO_EDITING_FFMPEG"], "/opt/ffmpeg")
        self.assertEqual(values["VIDEO_EDITING_DISABLED_FILE"], "/etc/video-editing/intake.disabled")
        self.assertEqual(values["VIDEO_EDITING_SOURCE_HOSTS"], "img.tianmai.cn")
        merged = deploy.merge_environment(b"# retained\nCUSTOM=value\nVIDEO_EDITING_GPU_ENABLED=0\n", values).decode()
        self.assertIn("CUSTOM=value\n", merged)
        self.assertEqual(merged.count("VIDEO_EDITING_GPU_ENABLED="), 1)
        self.assertNotIn("never-output", merged)
        with self.assertRaises(deploy.DeployError):
            deploy.new_environment(dict(old, DRAMA_GPU_MAX_CONCURRENCY="2"), "a" * 40)

    def test_unsafe_environment_cannot_inject_new_keys(self):
        with self.assertRaises(deploy.DeployError):
            deploy.merge_environment(b"", {"VIDEO_EDITING_SOURCE_HOSTS": "img.tianmai.cn\nEVIL=1"})

    def test_ledger_blocks_every_unfinished_or_process_unknown_record(self):
        with tempfile.TemporaryDirectory() as root:
            jobs = Path(root) / ".runtime/jobs"; jobs.mkdir(parents=True)
            for index, (status, extra) in enumerate([
                ("completed", {}), ("failed", {}), ("queued", {}),
                ("recovery_required", {}), ("failed", {"_children": {"1": {"pid": 1}}}),
                ("completed", {"_launches": {"pending": {}}})]):
                (jobs / (str(index) + ".json")).write_text(json.dumps(
                    {"job_id": str(index), "version": 1, "status": status, **extra}))
            value = deploy.ledger_snapshot(root)
            self.assertEqual(value["busy"], 4); self.assertFalse(value["mixed"])
            self.assertEqual(len(value["hashes"]), 6)
            job = "ve_" + "a" * 32
            (jobs / (job + ".json")).write_text(json.dumps({"job_id": job, "version": 1, "status": "completed"}))
            self.assertTrue(deploy.ledger_snapshot(root)["mixed"])
            (jobs / "broken.json").write_text("not json")
            with self.assertRaises(deploy.DeployError): deploy.ledger_snapshot(root)

    def test_health_requires_closed_editor_and_identical_legacy_profiles(self):
        old = {"ok": True, "release_sha": deploy.BASELINE, "render_concurrency": 1, "source_overlay_version": "v1"}
        new = dict(old, release_sha="a" * 40, video_editing_enabled=False, video_editing_profile="video-editing-720-v1")
        deploy.check_health(new, "a" * 40, old)
        with self.assertRaises(deploy.DeployError):
            deploy.check_health(dict(new, render_concurrency=2), "a" * 40, old)
        with self.assertRaises(deploy.DeployError):
            deploy.check_health(dict(new, video_editing_enabled=True), "a" * 40, old)

    def test_apply_preflight_failure_never_stops_service_or_writes_configuration(self):
        old = {"DRAMA_WORK_ROOT": "/work"}
        identity = mock.Mock(return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001))
        with mock.patch.object(deploy, "wait_for_idle"), mock.patch.dict("sys.modules", {"pwd": SimpleNamespace(getpwnam=identity)}), \
             mock.patch.object(deploy, "run", side_effect=deploy.DeployError("command_failed")) as run, \
             mock.patch.object(deploy, "backup_configuration") as backup, mock.patch.object(deploy, "atomic") as atomic:
            with self.assertRaises(deploy.DeployError):
                deploy.apply(Path("/candidate"), old, {}, {}, drain_timeout=0)
            self.assertEqual(run.call_count, 1)
            self.assertIn("check_drama_synthesis_gpu_runtime.py", run.call_args.args[1])
            backup.assert_not_called(); atomic.assert_not_called()

    def test_partial_configuration_write_restores_old_release_without_ledger_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            snapshot = {"hashes": {"old.json": "frozen"}, "counts": {"completed": 1}, "busy": 0, "mixed": False}
            identity = mock.Mock(return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001))
            def write(path, *args):
                if path == base / "gpu.env": raise deploy.DeployError("simulated_disk_write_failure")
            with mock.patch.dict("sys.modules", {"pwd": SimpleNamespace(getpwnam=identity)}), \
                 mock.patch.multiple(deploy, BASE=base, CONFIG=base / "config", ENV_FILE=base / "gpu.env", MARKER=base / "closed"), \
                 mock.patch.object(deploy, "run", return_value='{"ok": true}') as run, \
                 mock.patch.object(deploy, "wait_for_idle", return_value=snapshot), \
                 mock.patch.object(deploy, "wait_worker_stopped"), \
                 mock.patch.object(deploy, "ledger_snapshot", return_value=snapshot), \
                 mock.patch.object(deploy, "backup_configuration", return_value=[]), \
                 mock.patch.object(deploy, "atomic", side_effect=write), \
                 mock.patch.object(deploy, "restore_changed_configuration") as restore, \
                 mock.patch.object(deploy, "switch_link") as switch, \
                 mock.patch.object(deploy, "health", return_value={"ok": True, "release_sha": deploy.BASELINE}):
                with self.assertRaisesRegex(deploy.DeployError, "simulated_disk_write_failure"):
                    deploy.apply(base / "candidate", {"DRAMA_WORK_ROOT": "/work"}, {}, {}, drain_timeout=0)
                restore.assert_called_once()
                switch.assert_called_once_with(base / "releases" / deploy.BASELINE)
                self.assertEqual(snapshot["hashes"], {"old.json": "frozen"})
                self.assertTrue(any(call.args == ("systemctl", "start", deploy.TUNNEL) for call in run.call_args_list))


if __name__ == "__main__": unittest.main()
