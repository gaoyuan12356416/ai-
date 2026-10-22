#!/usr/bin/env python3
"""Narrow, dry-run-by-default HK GPU rollout; never fetches or edits a release.

The candidate must be a clean Git checkout in releases/<40-SHA>, descended from
the exact live source-overlay release. Pause CPU claims before --apply. This
helper leaves editing intake CLOSED for a separate canary/activation decision.
After an editing record exists, rollback means keeping the mixed reader and
closing intake, never switching to the old reader or restoring ledgers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

BASELINE = "223577b41466d03fd330625441ab3b7c1d86e843"
BASE = Path("/data/drama-synthesis-gpu")
CONFIG = Path("/etc/video-editing")
WORKER = "drama-synthesis-gpu-worker.service"
TUNNEL = "drama-synthesis-gpu-tunnel.service"
DROPIN = Path("/etc/systemd/system") / (WORKER + ".d") / "zzzz-video-editing.conf"
ENV_FILE = CONFIG / "gpu.env"
MARKER = CONFIG / "intake.disabled"
SAFE_HEALTH = ("render_concurrency", "compositor_backend", "compositor_chunk_seconds",
    "compositor_lanes", "compositor_filter_threads", "asset_cache_enabled", "frame_pipeline",
    "renderer_profile", "kernel_template_sha256", "source_overlay_version",
    "source_overlay_kernel_sha256", "source_overlay_cuda_kernel_sha256", "runtime_identity")


class DeployError(Exception):
    """Only constant, nonsecret error codes may leave the helper."""


def run(*args, timeout=60, env=None, cwd=None, user=None, group=None):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, env=env, cwd=cwd, user=user, group=group,
            extra_groups=[] if user is not None else None)
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError("command_unavailable_or_timed_out") from None
    if result.returncode:
        raise DeployError("command_failed")
    return result.stdout.strip()


def property_value(unit, name):
    return run("systemctl", "show", unit, "--property=" + name, "--value")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600):
    if path.is_symlink():
        raise DeployError("config_symlink_refused")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".ve-new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temporary.exists(): temporary.unlink()


def release_check(candidate, *, base=BASE, git=run):
    candidate = Path(candidate)
    current = base / "current"
    expected_old = base / "releases" / BASELINE
    if not current.is_symlink() or current.resolve() != expected_old.resolve():
        raise DeployError("live_baseline_mismatch")
    if (not re.fullmatch(r"[0-9a-f]{40}", candidate.name) or candidate.is_symlink()
            or candidate.parent != base / "releases" or not candidate.is_dir()
            or candidate.resolve() != candidate):
        raise DeployError("candidate_release_path_invalid")
    if git("git", "-C", str(candidate), "rev-parse", "HEAD") != candidate.name:
        raise DeployError("candidate_commit_mismatch")
    if git("git", "-C", str(candidate), "status", "--porcelain", "--untracked-files=all"):
        raise DeployError("candidate_dirty")
    git("git", "-C", str(candidate), "merge-base", "--is-ancestor", BASELINE, candidate.name)
    changed = git("git", "-C", str(candidate), "diff", "--name-only", BASELINE, candidate.name).splitlines()
    allowed = {"features/drama_synthesis/async_runtime.py", "scripts/drama_synthesis_gpu_worker.py",
               "scripts/deploy_video_editing_gpu.py", "scripts/test_drama_synthesis_gpu_runtime.py"}
    if not changed or any(p not in allowed and not p.startswith(("features/video_editing/", "tests/test_video_editing_",
                                                               "doc/20261022.video-editing/")) for p in changed):
        raise DeployError("candidate_contains_unrelated_changes")
    required = {"features/video_editing/gpu_integration.py", "features/video_editing/recipe.py",
                "features/video_editing/render.py", "features/video_editing/media.py"}
    if not required.issubset(changed):
        raise DeployError("candidate_missing_editor")
    return changed


def ledger_snapshot(work_root):
    directory = Path(work_root) / ".runtime" / "jobs"
    if not directory.is_dir() or directory.is_symlink():
        raise DeployError("runtime_ledger_unavailable")
    hashes, counts, busy, mixed = {}, {}, 0, False
    for path in directory.glob("*.json"):
        if path.is_symlink() or path.stat().st_size > 4 * 1024**2:
            raise DeployError("runtime_record_invalid")
        try:
            data = path.read_bytes(); record = json.loads(data)
            status = record["status"]
            if (record["job_id"] != path.stem or record["version"] != 1
                    or status not in {"completed", "failed", "queued", "running", "recovery_required"}):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise DeployError("runtime_record_invalid") from None
        counts[status] = counts.get(status, 0) + 1
        # Conservative: even a stale process marker requires reconciliation by
        # the running reader before deployment, never deletion by an installer.
        if status not in {"completed", "failed"} or record.get("_children") or record.get("_launches") or record.get("_resource_blocked"):
            busy += 1
        mixed |= path.stem.startswith("ve_") or record.get("_payload", {}).get("task_type") == "video_editing_v1"
        hashes[path.name] = sha(data)
    return {"hashes": hashes, "counts": counts, "busy": busy, "mixed": mixed}


def effective_env():
    pid = property_value(WORKER, "MainPID")
    if not pid.isdigit() or int(pid) <= 0:
        raise DeployError("worker_not_running")
    # Read credentials only into memory, never into arguments, output or logs.
    try:
        return dict(item.decode().split("=", 1) for item in Path("/proc", pid, "environ").read_bytes().split(b"\0") if b"=" in item)
    except (OSError, UnicodeError):
        raise DeployError("worker_environment_unavailable") from None


def new_environment(old, release, *, bucket=None, region=None):
    bucket, region = bucket or old.get("COS_BUCKET", ""), region or old.get("COS_REGION", "")
    if not re.fullmatch(r"[a-z0-9-]+-\d+", bucket) or not re.fullmatch(r"[a-z]+-[a-z]+(?:-[a-z]+)?", region):
        raise DeployError("cos_configuration_invalid")
    if old.get("DRAMA_GPU_MAX_CONCURRENCY", "1") != "1" or old.get("DRAMA_GPU_QUEUE_LIMIT", "8") != "8":
        raise DeployError("shared_scheduler_capacity_mismatch")
    return {"DRAMA_GPU_RELEASE_SHA": release, "VIDEO_EDITING_GPU_ENABLED": "1",
        "VIDEO_EDITING_DISABLED_FILE": MARKER.as_posix(), "VIDEO_EDITING_SOURCE_HOSTS": "img.tianmai.cn",
        "VIDEO_EDITING_COS_BUCKET": bucket, "VIDEO_EDITING_COS_REGION": region,
        "VIDEO_EDITING_COS_PREFIX": "video-editing/v1", "VIDEO_EDITING_ENCODER": "h264_nvenc",
        "VIDEO_EDITING_WORK_ROOT": (BASE / "work/video-editing").as_posix(),
        "VIDEO_EDITING_STATE_ROOT": (BASE / "results/video-editing").as_posix(),
        "VIDEO_EDITING_FFMPEG": old.get("DRAMA_FFMPEG", "/usr/bin/ffmpeg"),
        "VIDEO_EDITING_FFPROBE": old.get("DRAMA_FFPROBE", "/usr/bin/ffprobe"),
        "VIDEO_EDITING_MIN_FREE_BYTES": str(20 * 1024**3)}


def merge_environment(previous, overrides):
    lines = previous.decode("utf-8").splitlines()
    for key, value in overrides.items():
        if not re.fullmatch(r"[A-Z0-9_]+", key) or not re.fullmatch(r"[A-Za-z0-9_./,:-]+", value):
            raise DeployError("unsafe_environment_value")
    lines = [line for line in lines if line.split("=", 1)[0].strip() not in overrides]
    return ("\n".join(lines + [key + "=" + value for key, value in sorted(overrides.items())]) + "\n").encode()


def health():
    # Loopback only; health contains no credentials. Do not follow redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open("http://127.0.0.1:8787/healthz", timeout=5) as response:
        return json.load(response)


def check_health(value, release, previous=None):
    if not value.get("ok") or value.get("release_sha") != release:
        raise DeployError("worker_health_release_mismatch")
    if previous is not None:
        if any(value.get(key) != previous.get(key) for key in SAFE_HEALTH):
            raise DeployError("legacy_health_changed")
        if value.get("video_editing_enabled") is not False or value.get("video_editing_profile") != "video-editing-720-v1":
            raise DeployError("editing_intake_not_closed")


def backup_configuration(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    paths = {ENV_FILE, DROPIN, MARKER}
    for unit in (WORKER, TUNNEL):
        for prop in ("FragmentPath", "DropInPaths"):
            paths.update(Path(item) for item in property_value(unit, prop).split() if item.startswith("/"))
        # systemctl emits '/path (ignore_errors=yes)'; do not read values.
        paths.update(Path(item) for item in re.findall(r"(/[^\s;()]+)", property_value(unit, "EnvironmentFiles")))
    manifest = []
    for index, path in enumerate(sorted(paths)):
        if path.is_symlink(): raise DeployError("config_symlink_refused")
        row = {"path": str(path), "exists": path.exists(), "backup": str(index)}
        if path.exists():
            metadata = path.stat(); data = path.read_bytes()
            row.update(sha256=sha(data), mode=metadata.st_mode & 0o777, uid=metadata.st_uid, gid=metadata.st_gid)
            atomic(directory / str(index), data)
        manifest.append(row)
    atomic(directory / "configuration.json", json.dumps(manifest, indent=2).encode())
    return manifest


def restore_changed_configuration(directory, manifest):
    # Base units and secret files were backed up but never edited by this helper.
    for row in manifest:
        path = Path(row["path"])
        if path not in {ENV_FILE, DROPIN}: continue
        if row["exists"]:
            data = (directory / row["backup"]).read_bytes()
            if sha(data) != row["sha256"]: raise DeployError("backup_hash_mismatch")
            atomic(path, data, row["mode"]); os.chown(path, row["uid"], row["gid"])
        elif path.exists(): path.unlink()
    # Always keep the marker. Rollback never opens editing intake.


def switch_link(target):
    temporary = BASE / "current.video-editing-new"
    if temporary.exists() or temporary.is_symlink(): raise DeployError("release_switch_pending")
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, BASE / "current")
    directory = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def wait_for_idle(work_root, timeout):
    end = time.monotonic() + timeout
    while True:
        snapshot = ledger_snapshot(work_root)
        if not snapshot["busy"]: return snapshot
        if time.monotonic() >= end: raise DeployError("runtime_not_drained")
        time.sleep(2)


def wait_worker_stopped(timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if property_value(WORKER, "ActiveState") in {"inactive", "failed"}:
            if property_value(WORKER, "MainPID") != "0": raise DeployError("worker_process_not_stopped")
            control_group = property_value(WORKER, "ControlGroup")
            if control_group:
                procs = Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cgroup.procs"
                if procs.exists() and procs.read_text().strip(): raise DeployError("worker_children_not_stopped")
            return
        time.sleep(1)
    raise DeployError("worker_stop_timeout_no_force_kill")


def apply(candidate, old_env, overrides, previous_health, *, drain_timeout):
    import pwd
    identity = pwd.getpwnam("drama-synthesis-gpu")
    work_root = old_env["DRAMA_WORK_ROOT"]
    wait_for_idle(work_root, drain_timeout)
    python = str(BASE / "runtime/current/bin/python")
    # Existing preflight exercises the real compositor and guarded import.
    # New code is checked with the effective live environment plus only the
    # editor overrides; failures occur before any service/config/link mutation.
    env = dict(old_env, **overrides, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    preflight = run(python, str(candidate / "scripts/check_drama_synthesis_gpu_runtime.py"),
        "--check-app-import", env=env, cwd=candidate, user=identity.pw_uid, group=identity.pw_gid, timeout=300)
    try:
        if json.loads(preflight).get("ok") is not True: raise ValueError()
    except (ValueError, TypeError): raise DeployError("candidate_preflight_failed") from None
    wait_for_idle(work_root, 0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BASE / "deploy-backups" / ("video-editing-" + stamp)
    manifest = backup_configuration(backup)
    if CONFIG.is_symlink(): raise DeployError("config_symlink_refused")
    CONFIG.mkdir(mode=0o755, parents=True, exist_ok=True)
    # The non-root worker must be able to stat the marker even under a root
    # deployment umask of 0077. Environment contents remain root-only 0600.
    os.chmod(CONFIG, 0o755)
    # No restart can expose editor writes while deployment is being verified.
    atomic(MARKER, b"intake disabled for verified video editing rollout\n", 0o644)
    changed = False
    try:
        run("systemctl", "stop", "--no-block", WORKER)
        wait_worker_stopped()
        before = wait_for_idle(work_root, 0)
        if before["mixed"]: raise DeployError("old_reader_rollback_unsafe")
        atomic(backup / "ledger-hashes.json", json.dumps(before, sort_keys=True).encode())
        previous = ENV_FILE.read_bytes() if ENV_FILE.exists() else b""
        changed = True  # A partial environment/drop-in write also needs rollback.
        atomic(ENV_FILE, merge_environment(previous, overrides))
        atomic(DROPIN, ("[Service]\nEnvironmentFile=" + str(ENV_FILE) + "\n").encode(), 0o644)
        switch_link(candidate)
        run("systemctl", "daemon-reload")
        run("systemctl", "start", WORKER, timeout=360)
        run("systemctl", "start", TUNNEL, timeout=60)
        check_health(health(), candidate.name, previous_health)
        actual = effective_env()
        if any(actual.get(key) != value for key, value in overrides.items()):
            raise DeployError("effective_editor_environment_mismatch")
        for key, value in old_env.items():
            if key.startswith(("DRAMA_", "COS_", "GPU_", "DEMUCS_")) and key not in overrides and actual.get(key) != value:
                raise DeployError("legacy_environment_changed")
        after = ledger_snapshot(work_root)
        if before["hashes"] != after["hashes"]: raise DeployError("ledger_changed_during_rollout")
        if property_value(TUNNEL, "ActiveState") != "active": raise DeployError("tunnel_not_active")
        atomic(backup / "result.json", json.dumps({"ok": True, "release": candidate.name,
            "ledger_records": len(after["hashes"]), "intake_closed": True}, sort_keys=True).encode())
        return {"ok": True, "release": candidate.name, "backup": str(backup), "intake_closed": True,
                "ledger_records": len(after["hashes"])}
    except BaseException:
        # An unexpected editing acceptance forbids rollback to the old reader.
        # Preserve the mixed reader, all records and the closed intake marker.
        if ledger_snapshot(work_root)["mixed"]:
            raise DeployError("mixed_reader_retained_operator_recovery_required") from None
        if changed:
            # A caller violating the claim pause can have started legacy work
            # after our health check. Do not interrupt it to perform rollback.
            if ledger_snapshot(work_root)["busy"]:
                raise DeployError("active_reader_retained_operator_recovery_required") from None
            run("systemctl", "stop", "--no-block", WORKER)
            wait_worker_stopped()
            wait_for_idle(work_root, 0)
            restore_changed_configuration(backup, manifest)
            switch_link(BASE / "releases" / BASELINE)
            run("systemctl", "daemon-reload")
        run("systemctl", "start", WORKER, timeout=360)
        run("systemctl", "start", TUNNEL)
        check_health(health(), BASELINE)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--cpu-claims-paused", action="store_true", help="operator attests all CPU GPU-submit claims are paused")
    parser.add_argument("--drain-timeout", type=int, default=1800)
    parser.add_argument("--bucket")
    parser.add_argument("--region")
    args = parser.parse_args(argv)
    try:
        if os.name != "posix" or os.geteuid() != 0: raise DeployError("run_on_gpu_as_root")
        if args.apply and not args.cpu_claims_paused: raise DeployError("pause_cpu_claims_before_apply")
        if not 0 <= args.drain_timeout <= 14400: raise DeployError("drain_timeout_invalid")
        files = release_check(args.candidate)
        old_env = effective_env()
        if old_env.get("DRAMA_GPU_RELEASE_SHA") != BASELINE: raise DeployError("running_baseline_mismatch")
        overrides = new_environment(old_env, args.candidate.name, bucket=args.bucket, region=args.region)
        old_health = health(); check_health(old_health, BASELINE)
        snapshot = ledger_snapshot(old_env["DRAMA_WORK_ROOT"])
        if snapshot["mixed"]: raise DeployError("baseline_mixed_records_unexpected")
        if not args.apply:
            result = {"ok": True, "dry_run": True, "release": args.candidate.name, "files": files,
                "ledger_counts": snapshot["counts"], "busy_records": snapshot["busy"],
                "preflight": "required_during_apply", "intake_after_apply": "closed",
                "cos_acl": "private_enforced_by_uploader", "service_units": [WORKER, TUNNEL]}
        else:
            import fcntl
            with open("/run/lock/video-editing-gpu-deploy.lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = apply(args.candidate, old_env, overrides, old_health, drain_timeout=args.drain_timeout)
        print(json.dumps(result, sort_keys=True))
        return 0
    except DeployError as exc:
        print(json.dumps({"ok": False, "error": str(exc)})); return 1
    except Exception:
        # Never print subprocess output, URLs, environment or exception details.
        print(json.dumps({"ok": False, "error": "deployment_failed_inspect_private_backup"})); return 1


if __name__ == "__main__":
    raise SystemExit(main())
