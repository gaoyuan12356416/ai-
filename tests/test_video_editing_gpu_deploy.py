import errno
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error

from scripts import deploy_video_editing_gpu as deploy


class DeployTests(unittest.TestCase):
    def test_release_allowlist_admits_exact_recovery_validator_only(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root).resolve(); candidate = base / "releases" / ("a" * 40)
            candidate.mkdir(parents=True)
            changed = ["features/video_editing/" + name + ".py" for name in ("gpu_integration", "recipe", "render", "media")]
            changed.append("scripts/validate_video_editing_recovery.py")
            def git(*args):
                if "rev-parse" in args: return candidate.name
                if "diff" in args: return "\n".join(changed)
                return ""
            original_resolve = Path.resolve
            def resolve(path, *args, **kwargs):
                if path == base / "current": return base / "releases" / deploy.BASELINE
                return original_resolve(path, *args, **kwargs)
            with mock.patch.object(Path, "is_symlink", lambda path: path == base / "current"), \
                 mock.patch.object(Path, "resolve", resolve):
                self.assertEqual(deploy.release_check(candidate, base=base, git=git), changed)
                changed[-1] = "scripts/validate_video_editing_recovery_extra.py"
                with self.assertRaisesRegex(deploy.DeployError, "candidate_contains_unrelated_changes"):
                    deploy.release_check(candidate, base=base, git=git)

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

    def test_startup_health_waits_for_refused_listener_then_checks_identity(self):
        old = {"ok": True, "release_sha": deploy.BASELINE, "render_concurrency": 1}
        new = dict(old, release_sha="a" * 40, video_editing_enabled=False,
                   video_editing_profile="video-editing-720-v1")
        refused = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
        with mock.patch.object(deploy, "health", side_effect=[refused, new]) as health, \
             mock.patch.object(deploy.time, "sleep") as sleep:
            self.assertEqual(deploy.wait_for_health("a" * 40, old), new)
            self.assertEqual(health.call_count, 2)
            sleep.assert_called_once()

    def test_startup_health_refused_listener_has_finite_deadline(self):
        refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        with mock.patch.object(deploy, "health", side_effect=refused) as health, \
             mock.patch.object(deploy.time, "monotonic", side_effect=[0, 0, 1]), \
             mock.patch.object(deploy.time, "sleep") as sleep:
            with self.assertRaisesRegex(deploy.DeployError, "worker_health_start_timeout"):
                deploy.wait_for_health("a" * 40, timeout=1)
            self.assertEqual(health.call_count, 2)
            sleep.assert_called_once_with(.5)

    def test_startup_health_never_waits_through_identity_or_configuration_mismatch(self):
        old = {"ok": True, "release_sha": deploy.BASELINE, "render_concurrency": 1}
        new = dict(old, release_sha="a" * 40, video_editing_enabled=False,
                   video_editing_profile="video-editing-720-v1")
        for bad, code in [
            (dict(new, release_sha=deploy.BASELINE), "worker_health_release_mismatch"),
            (dict(new, ok=False), "worker_health_release_mismatch"),
            (dict(new, render_concurrency=2), "legacy_health_changed"),
            (dict(new, video_editing_enabled=True), "editing_intake_not_closed"),
        ]:
            with self.subTest(code=code), mock.patch.object(deploy, "health", return_value=bad) as health, \
                 mock.patch.object(deploy.time, "sleep") as sleep:
                with self.assertRaisesRegex(deploy.DeployError, code):
                    deploy.wait_for_health("a" * 40, old)
                health.assert_called_once(); sleep.assert_not_called()

    def test_startup_health_does_not_retry_http_timeout_or_invalid_json(self):
        failures = [urllib.error.HTTPError("http://127.0.0.1/healthz", 503, "unavailable", {}, None),
                    urllib.error.URLError(TimeoutError("timeout")),
                    json.JSONDecodeError("invalid", "", 0)]
        for failure in failures:
            try:
                with self.subTest(error=type(failure).__name__), \
                     mock.patch.object(deploy, "health", side_effect=failure) as health, \
                     mock.patch.object(deploy.time, "sleep") as sleep:
                    with self.assertRaises((deploy.DeployError, json.JSONDecodeError)):
                        deploy.wait_for_health("a" * 40)
                    health.assert_called_once(); sleep.assert_not_called()
            finally:
                if isinstance(failure, urllib.error.HTTPError): failure.close()

    def test_apply_preflight_failure_never_stops_service_or_writes_configuration(self):
        old = {"DRAMA_WORK_ROOT": "/work"}
        identity = mock.Mock(return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001))
        with mock.patch.object(deploy, "wait_for_idle"), mock.patch.object(deploy, "wait_for_media_idle"), \
             mock.patch.dict("sys.modules", {"pwd": SimpleNamespace(getpwnam=identity)}), \
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
                 mock.patch.object(deploy, "wait_for_media_idle"), \
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

    def test_media_guard_sees_python_upload_socket_without_child_or_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder); process = proc / "123"
            (process / "fd").mkdir(parents=True); (process / "net").mkdir()
            (process / "task/123").mkdir(parents=True)
            (process / "task/123/children").write_text("")
            (process / "fd/3").touch(); (process / "fd/4").touch()
            listener = "0: 0100007F:2253 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1001 0 100\n"
            upload = "1: 0100007F:2253 0100007F:D001 01 00000000:00000000 00:00000000 00000000 1001 0 101\n"
            (process / "net/tcp").write_text("header\n" + listener + upload)
            (process / "net/tcp6").write_text("header\n")
            def target(fd): return {"3": "socket:[100]", "4": "socket:[101]"}[fd.name]
            with mock.patch.object(deploy.os, "readlink", side_effect=target):
                value = deploy.worker_media_snapshot({}, pid=123, proc_root=proc)
                self.assertTrue(value["busy"]); self.assertEqual(value["connections"], 1)
                self.assertEqual(value["children"], 0)
                (process / "net/tcp").write_text("header\n" + listener)
                self.assertFalse(deploy.worker_media_snapshot({}, pid=123, proc_root=proc)["busy"])
                (process / "task/123/children").write_text("456")
                self.assertTrue(deploy.worker_media_snapshot({}, pid=123, proc_root=proc)["busy"])

    def test_media_guard_blocks_pending_connections_and_open_media(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder); process = proc / "123"; media = proc / "media"
            (process / "fd").mkdir(parents=True); (process / "net").mkdir()
            (process / "task/123").mkdir(parents=True)
            (process / "task/123/children").write_text("")
            (process / "fd/3").touch(); (process / "fd/4").touch()
            (process / "net/tcp").write_text("header\n0: 0100007F:2253 00000000:0000 0A 00000000:00000001 00:00000000 00000000 1001 0 100\n")
            (process / "net/tcp6").write_text("header\n")
            with mock.patch.object(deploy.os, "readlink", side_effect=lambda fd: "socket:[100]" if fd.name == "3" else str(media) + "/task-1/source.mp4"):
                value = deploy.worker_media_snapshot({"DRAMA_YOUTUBE_MEDIA_ROOT": str(media)}, pid=123, proc_root=proc)
                self.assertTrue(value["busy"])
                self.assertEqual(value["listen_pending"], 1); self.assertEqual(value["media_handles"], 1)
            (process / "net/tcp").unlink()
            with mock.patch.object(deploy.os, "readlink", return_value="socket:[100]"):
                with self.assertRaises(deploy.DeployError): deploy.worker_media_snapshot({}, pid=123, proc_root=proc)

    def test_media_wait_never_treats_unknown_or_active_as_idle(self):
        with mock.patch.object(deploy, "worker_media_snapshot", return_value={"busy": True}):
            with self.assertRaisesRegex(deploy.DeployError, "youtube_media_not_drained"):
                deploy.wait_for_media_idle({}, 0, quiet_seconds=0)
        with mock.patch.object(deploy, "worker_media_snapshot", side_effect=deploy.DeployError("media_activity_unverified")):
            with self.assertRaisesRegex(deploy.DeployError, "media_activity_unverified"):
                deploy.wait_for_media_idle({}, 0, quiet_seconds=0)


if __name__ == "__main__": unittest.main()
