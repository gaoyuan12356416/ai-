"""GitHub-first Excel rollout; retain compatible executors after any reopen.

No schema migration, database restore, immutable-link change, or GPU deployment.
Run from the exact pushed release checkout on the CPU server. --rollback closes
Excel admission and restores old static assets, while keeping the v3 backend.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

import deploy_fb_page_list_performance as previous

base, pool_release = previous.base, previous.pool_release
ROOT = Path(__file__).resolve().parents[1]
BASELINE = "3e6b75e"
REMOTE_REF = "refs/heads/codex/fb-page-excel-publish-20261021"
SCOPE = "fb_page_excel_publish_v3"
VIDEO_FILES = {"features/fb_video_publish/" + name + ".py" for name in
               ("client", "core", "excel", "pool", "query_broker", "service", "sources", "worker")}
AUTO_FILES = {"features/fb_auto_posts/" + name + ".py" for name in ("service", "repositories", "publisher")}
STATIC_FILES = {"static/" + name for name in ("fb-pages.html", "fb-video-publish.html",
                "fb-video-publish-records.html", "fb-video-publish.js", "fb-video-publish.css",
                "fb-excel-publish.js", "fb-excel-template.xlsx")}
GATE_SOURCE = "deploy/fb-video-excel.conf"
DROPIN = Path("/etc/systemd/system/fb-auto-post-service.service.d/zz-fb-video-excel.conf")
GATE_ON = b"[Service]\nEnvironment=FB_VIDEO_EXCEL_ENABLED=1\n"
GATE_OFF = b"[Service]\nEnvironment=FB_VIDEO_EXCEL_ENABLED=0\n"
ALLOWED = VIDEO_FILES | AUTO_FILES | STATIC_FILES | {GATE_SOURCE}
PRODUCERS = pool_release.TIMERS + [pool_release.WORKER, pool_release.POOL_UNIT]
STATE_UNITS = PRODUCERS + [base.FB_UNIT, base.API_UNIT]
assert_pool_window = previous.assert_pool_window


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def safe_path(path, root):
    """The fixed runtime root may be a release symlink; its children may not."""
    path, root = Path(path), Path(root)
    base.require(path.is_absolute() and root.is_absolute(), "Absolute deployment paths required")
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise RuntimeError("Deployment path outside permitted root") from None
    base.require(relative.parts and ".." not in relative.parts, "Invalid deployment path")
    current = root
    for part in relative.parts:
        current = current / part
        base.require(not current.is_symlink(), "Symlink inside deployment path: " + str(current))
    base.require(path.resolve().is_relative_to(root.resolve()), "Resolved deployment path escapes root")
    base.require(not path.exists() or path.is_file(), "Deployment target is not a regular file")
    return path


def destinations(relative):
    if relative in VIDEO_FILES:
        return [(base.MAIN / relative, base.MAIN), (base.FB / relative, base.FB)]
    if relative in AUTO_FILES:
        # The main API does not load these executors and has a different baseline.
        return [(base.FB / relative, base.FB)]
    if relative in STATIC_FILES:
        return [(base.MAIN / relative, base.MAIN), (base.FB / relative, base.FB),
                (base.STATIC / Path(relative).name, base.STATIC)]
    if relative == GATE_SOURCE:
        return [(DROPIN, DROPIN.parent.parent)]
    raise RuntimeError("Runtime file outside Excel patch: " + relative)


def target_map():
    return {str(target): (relative, root) for relative in ALLOWED for target, root in destinations(relative)}


def git_bytes(ref, relative):
    """A missing baseline blob is legitimate; Git failures are not absence."""
    base.run("git", "-C", str(ROOT), "rev-parse", "--verify", ref + "^{commit}")
    listed = subprocess.check_output(["git", "-C", str(ROOT), "ls-tree", "-z", "--full-tree", ref, "--", relative])
    if not listed:
        return None
    records = listed.decode("utf-8").rstrip("\0").split("\0")
    base.require(len(records) == 1, "Ambiguous Git source")
    metadata, filename = records[0].split("\t", 1)
    base.require(filename == relative and re.fullmatch(r"100(?:644|755) blob [0-9a-f]{40}", metadata),
                 "Git source is not a regular blob")
    return subprocess.check_output(["git", "-C", str(ROOT), "show", ref + ":" + relative])


def verified_commit(remote_ref=REMOTE_REF):
    base.require(remote_ref == REMOTE_REF, "Unexpected deployment branch")
    commit = base.run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    base.require(re.fullmatch(r"[0-9a-f]{40}", commit), "Invalid release commit")
    base.require(not base.run("git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"),
                 "Dirty release checkout")
    remote = base.run("git", "-C", str(ROOT), "remote", "get-url", "origin")
    base.require(remote in {"git@github.com:gaoyuan12356416/ai-.git",
                           "https://github.com/gaoyuan12356416/ai-.git"}, "Unexpected GitHub repository")
    advertised = base.run("git", "-C", str(ROOT), "ls-remote", "--exit-code", "origin", remote_ref).split()
    base.require(advertised == [commit, remote_ref], "Release is not the exact pushed GitHub ref")
    return commit


def stage_targets():
    changed = base.run("git", "-C", str(ROOT), "diff", "--name-only", BASELINE, "HEAD").splitlines()
    runtime = set()
    for relative in changed:
        if relative in ALLOWED:
            runtime.add(relative)
        else:
            base.require(not relative.startswith(("features/", "static/", "deploy/")) and relative != "app.py",
                         "Runtime file outside Excel patch: " + relative)
    base.require(runtime == ALLOWED, "Incomplete committed Excel runtime release")
    targets = []
    for relative in sorted(runtime):
        source = safe_path(ROOT / relative, ROOT)
        base.require(source.is_file(), "Deletion is not supported by this patch")
        current = source.read_bytes()
        base.require(current == git_bytes("HEAD", relative), "Source differs from committed HEAD: " + relative)
        if source.suffix == ".py":
            compile(current, str(source), "exec")
        if relative == GATE_SOURCE:
            base.require(current == GATE_ON, "Excel drop-in must contain only its admission flag")
        before = sha_bytes(git_bytes(BASELINE, relative))
        for target, root in destinations(relative):
            safe_path(target, root)
            base.require(base.sha(target) == before, "Live baseline drift: " + str(target))
            targets.append((source, target, target.stat().st_mode & 0o777 if target.exists() else 0o644))
    base.require(len(targets) == 41, "Expected exactly 41 runtime copies")
    return targets


def validate_states(saved, *, recovery=False):
    base.require(isinstance(saved, dict) and set(saved) == set(STATE_UNITS), "Invalid saved service scope")
    for state in saved.values():
        base.require(isinstance(state, dict) and set(state) == {"active", "enabled"}
                     and state["active"] in ({"active", "inactive", "failed", "activating", "deactivating"}
                                              if recovery else {"active", "inactive"}) and isinstance(state["enabled"], str)
                     and re.fullmatch(r"[a-z-]+", state["enabled"]), "Unstable saved service state")


def validate_manifest(manifest, manifest_path, *, compatible=False):
    """Validate all paths and hashes before a service stop or any restore write."""
    manifest_path = safe_path(manifest_path, base.BASE)
    base.require(manifest_path.name == "manifest.json" and manifest_path.parent.parent == base.BASE / "backups"
                 and re.fullmatch(r"excel-\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}", manifest_path.parent.name),
                 "Invalid Excel backup directory")
    base.require(manifest.get("scope") == SCOPE and manifest.get("baseline") == BASELINE
                 and manifest.get("remote_ref") == REMOTE_REF and manifest.get("format_version") == 1
                 and re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("commit", ""))), "Use the original deployer for this manifest")
    validate_states(manifest["states"])
    base.require(base.sha(base.MAIN / "app.py") == manifest.get("main_app_sha256"), "Main app.py drift")
    allowed, seen = target_map(), set()
    base.require(isinstance(manifest.get("files"), list), "Invalid manifest files")
    for index, item in enumerate(manifest["files"]):
        target_text = item.get("target")
        base.require(target_text in allowed and target_text not in seen, "Rollback path outside patch or duplicated")
        seen.add(target_text)
        relative, root = allowed[target_text]
        base.require(item.get("relative") == relative and item.get("source") == str(ROOT / relative),
                     "Manifest source outside release")
        target = safe_path(Path(target_text), root)
        before, after = item.get("before"), item.get("after")
        base.require((before is None or re.fullmatch(r"[0-9a-f]{64}", str(before)))
                     and re.fullmatch(r"[0-9a-f]{64}", str(after)), "Invalid manifest file hash")
        base.require(type(item.get("mode_before")) is int and 0 <= item["mode_before"] <= 0o777
                     and type(item.get("mode")) is int and 0 <= item["mode"] <= 0o777, "Invalid saved file mode")
        if before is None:
            base.require(item.get("backup") is None, "Unexpected backup for newly added file")
        else:
            backup = manifest_path.parent / ("file-" + str(index))
            base.require(item.get("backup") == str(backup), "Rollback backup outside manifest directory")
            safe_path(backup, manifest_path.parent)
            base.require(base.sha(backup) == before, "Rollback backup drift: " + str(backup))
        permitted = {before, after}
        if relative == GATE_SOURCE:
            permitted.add(sha_bytes(GATE_OFF))
        base.require(base.sha(target) in permitted, "Rollback target drift: " + str(target))
        if compatible and relative in VIDEO_FILES | AUTO_FILES:
            base.require(base.sha(target) == after, "Compatible backend incomplete; retain stopped services")
    base.require(seen == set(allowed), "Incomplete runtime manifest")


def restore_files(manifest, manifest_path, *, static_only=False):
    validate_manifest(manifest, manifest_path, compatible=static_only)
    for item in manifest["files"]:
        if static_only and item["relative"] not in STATIC_FILES:
            continue
        target = Path(item["target"])
        if item["before"] is None:
            target.unlink(missing_ok=True)
        else:
            base.atomic_bytes(Path(item["backup"]).read_bytes(), target, item["mode_before"])
        base.require(base.sha(target) == item["before"], "Restored file hash mismatch")


def verify_gate(enabled):
    # Read only this flag from the live process; never print its environment.
    pid = base.run("systemctl", "show", base.FB_UNIT, "-p", "MainPID", "--value")
    base.require(re.fullmatch(r"[1-9][0-9]*", pid), "Sidecar PID unavailable")
    selected = [value for value in (Path("/proc") / pid / "environ").read_bytes().split(b"\0")
                if value.startswith(b"FB_VIDEO_EXCEL_ENABLED=")]
    base.require(selected == [b"FB_VIDEO_EXCEL_ENABLED=" + (b"1" if enabled else b"0")],
                 "Live Excel admission flag differs from the release")


def resume(saved, app_hash, *, restart_main=False, gate_enabled=None):
    base.require(base.sha(base.MAIN / "app.py") == app_hash, "Main app.py changed during deployment")
    if restart_main:
        base.require(saved[base.API_UNIT]["active"] == "active", "Main API was not active")
        subprocess.run(["systemctl", "restart", base.API_UNIT], check=True)
    if saved[base.API_UNIT]["active"] == "active":
        base.health("http://127.0.0.1:8787/api/ui/topbar")
    pool_release.restore_states(saved, [base.FB_UNIT])
    if saved[base.FB_UNIT]["active"] == "active":
        base.health("http://127.0.0.1:18835/health")
        if gate_enabled is not None:
            verify_gate(gate_enabled)
    pool_release.restore_states(saved, PRODUCERS)
    for unit in (base.API_UNIT, base.FB_UNIT):
        base.require(base.state(unit) == saved[unit], "Service state changed: " + unit)


def functional_rollback(manifest, manifest_path, *, desired_states=None, restart_main=False):
    """Always retain v3 execution, receipts and reconciliation, including unknowns."""
    validate_manifest(manifest, manifest_path, compatible=True)
    if desired_states is None:
        assert_pool_window()
    current = pool_release.saved_states()
    desired = desired_states or current
    validate_states(current, recovery=desired_states is not None)
    validate_states(desired)
    quiesced = False
    try:
        pool_release.quiesce(current)
        quiesced = True
        if desired_states is None:
            assert_pool_window()
        # Emergency recovery after an uncertain restart must close admission even
        # if the failure consumed the originally safe scheduling window.
        validate_manifest(manifest, manifest_path, compatible=True)
        base.atomic_bytes(GATE_OFF, DROPIN, 0o644)
        restore_files(manifest, manifest_path, static_only=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        resume(desired, manifest["main_app_sha256"], restart_main=restart_main, gate_enabled=False)
        manifest["phase"] = "functional_rollback"
        manifest["functional_rollback_at"] = datetime.now(timezone.utc).isoformat()
        pool_release.json_save(manifest_path, manifest)
    except Exception:
        # Once any rollback write begins, never resume mixed/incomplete files.
        if not quiesced:
            resume(current, manifest["main_app_sha256"])
        raise


def accepted_new_protocol():
    """Durable fallback if another operator reopened the sidecar during a copy.

    Fail closed on malformed requests, missing tables, SQL errors or excessive
    history. A successful/failed v3 batch still requires compatible record reads.
    """
    with base.database() as conn:
        for index, row in enumerate(conn.execute("SELECT request_json FROM fb_video_batch LIMIT 10001"), 1):
            base.require(index <= 10000, "Batch history exceeds safe full-restore audit; retain compatible backend")
            request = json.loads(row[0])
            base.require(isinstance(request, dict), "Cannot verify accepted batch protocol")
            version = request.get("schema_version", 1)
            if type(version) is not int or version not in (1, 2):
                return True
    return False


def emergency_hold(manifest):
    """Best effort fixed-scope hold after recovery fails; never restore code."""
    manifest["emergency_hold_attempted"] = True
    manifest["emergency_hold_completed"] = False
    try:
        gate = [item for item in manifest["files"] if item.get("relative") == GATE_SOURCE]
        base.require(len(gate) == 1 and gate[0].get("target") == str(DROPIN)
                     and gate[0].get("source") == str(ROOT / GATE_SOURCE)
                     and gate[0].get("before") is None and gate[0].get("after") == sha_bytes(GATE_ON),
                     "Emergency admission path outside fixed release")
        safe_path(DROPIN, DROPIN.parent.parent)
        base.require(base.sha(DROPIN) in {None, sha_bytes(GATE_ON), sha_bytes(GATE_OFF)},
                     "Emergency admission file drift")
        base.atomic_bytes(GATE_OFF, DROPIN, 0o644)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        current = pool_release.saved_states()
        validate_states(current, recovery=True)
        # Existing quiesce waits for Graph/prepare leases; it never kills a busy
        # sidecar to make an emergency hold appear successful.
        pool_release.quiesce(current)
        base.require(base.state(base.FB_UNIT)["active"] == "inactive"
                     and pool_release.in_flight() == ([], 0), "Runtime admission hold is not confirmed")
        manifest["emergency_hold_completed"] = True
    except Exception as error:
        manifest["emergency_hold_failure_type"] = type(error).__name__


def apply_release(targets, commit):
    base.require(re.fullmatch(r"[0-9a-f]{40}", commit), "Invalid release commit")
    base.require(base.run("git", "-C", str(ROOT), "rev-parse", "HEAD") == commit, "Release HEAD changed after preflight")
    saved = pool_release.saved_states()
    validate_states(saved)
    base.require(all(saved[u]["active"] == "active" for u in (base.API_UNIT, base.FB_UNIT)),
                 "Main API and FB sidecar must be active before deployment")
    app_hash = base.sha(base.MAIN / "app.py")
    base.require(bool(app_hash), "Main app.py missing")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = base.BASE / "backups" / ("excel-" + stamp + "-" + commit[:8])
    safe_path(backup / "manifest.json", base.BASE)
    backup.mkdir(parents=True, mode=0o700)
    manifest_path = backup / "manifest.json"
    manifest = {"format_version": 1, "scope": SCOPE, "commit": commit, "baseline": BASELINE,
                "remote_ref": REMOTE_REF, "states": saved, "main_app_sha256": app_hash,
                "phase": "prepared", "activation_attempted": False, "compatible_backend_installed": False, "files": []}
    allowed = target_map()
    base.require(len(targets) == len(allowed) and {str(target) for _, target, _ in targets} == set(allowed),
                 "Incomplete deployment target set")
    git_hashes = {relative: (sha_bytes(git_bytes("HEAD", relative)), sha_bytes(git_bytes(BASELINE, relative)))
                  for relative in ALLOWED}
    for index, (source, target, mode) in enumerate(targets):
        relative, root = allowed[str(target)]
        safe_path(target, root)
        base.require(source == ROOT / relative, "Deployment source outside release")
        safe_path(source, ROOT)
        before, after = base.sha(target), base.sha(source)
        base.require((after, before) == git_hashes[relative], "Source or baseline changed after preflight: " + relative)
        destination = backup / ("file-" + str(index)) if before is not None else None
        if destination:
            shutil.copy2(target, destination)
            base.require(base.sha(destination) == before, "Backup hash mismatch")
        manifest["files"].append({"relative": relative, "source": str(source), "target": str(target),
                                  "backup": str(destination) if destination else None, "before": before,
                                  "after": after, "mode": mode,
                                  "mode_before": target.stat().st_mode & 0o777 if target.exists() else mode})
    pool_release.json_save(manifest_path, manifest)
    validate_manifest(manifest, manifest_path)
    modified = False
    try:
        assert_pool_window()
        pool_release.quiesce(saved)
        assert_pool_window()
        base.require(pool_release.in_flight() == ([], 0), "Publisher not fully drained")
        snapshot = backup / "operational.sqlite3"
        base.snapshot_db(snapshot)
        with closing(sqlite3.connect("file:" + str(snapshot) + "?mode=ro", uri=True)) as conn:
            base.require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "SQLite backup integrity failure")
        manifest["database_backup"] = {"path": str(snapshot), "sha256": base.sha(snapshot)}
        validate_manifest(manifest, manifest_path)
        for (source, target, _), item in zip(targets, manifest["files"]):
            base.require(base.sha(source) == item["after"], "Release source changed during drain")
            base.require(base.sha(target) == item["before"], "Target changed during drain")
        modified = True
        for source, target, mode in targets:
            base.atomic_bytes(source.read_bytes(), target, mode)
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["after"], "Installed file hash mismatch")
        manifest["compatible_backend_installed"] = True
        manifest["phase"] = "files_installed"
        pool_release.json_save(manifest_path, manifest)
        assert_pool_window()
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        # Persist BEFORE the first restart: it can succeed even if the command or
        # health probe fails. Any later recovery must retain compatible Python.
        manifest["activation_attempted"] = True
        manifest["phase"] = "activation_attempted"
        pool_release.json_save(manifest_path, manifest)
        resume(saved, app_hash, restart_main=True, gate_enabled=True)
        validate_manifest(manifest, manifest_path, compatible=True)
        manifest["phase"] = "deployed"
        pool_release.json_save(manifest_path, manifest)
        print(json.dumps({"deployed": True, "commit": commit, "manifest": str(manifest_path), "files": len(targets),
                          "database_migrated": False, "main_api_restarted": True, "service_states_preserved": True}), flush=True)
    except Exception as error:
        manifest["failure_type"] = type(error).__name__
        try:
            if manifest["activation_attempted"]:
                functional_rollback(manifest, manifest_path, desired_states=saved, restart_main=True)
                manifest["phase"] = "failed_closed_compatible"
            elif modified:
                pool_release.quiesce(pool_release.saved_states())
                base.require(pool_release.in_flight() == ([], 0), "Cannot restore while publisher is active")
                if accepted_new_protocol():
                    manifest["accepted_new_protocol_observed"] = True
                    base.require(manifest["compatible_backend_installed"],
                                 "Accepted v3 batch with incomplete compatible backend; retain stopped services")
                    functional_rollback(manifest, manifest_path, desired_states=saved, restart_main=True)
                    manifest["phase"] = "failed_closed_compatible"
                else:
                    restore_files(manifest, manifest_path)
                    subprocess.run(["systemctl", "daemon-reload"], check=True)
                    resume(saved, app_hash)
                    manifest["phase"] = "failed_restored_before_activation"
            else:
                resume(saved, app_hash)
                manifest["phase"] = "failed_before_install"
        except Exception as recovery:
            manifest["phase"] = "recovery_required"
            manifest["recovery_failure_type"] = type(recovery).__name__
            if modified:
                emergency_hold(manifest)
            try:
                pool_release.json_save(manifest_path, manifest)
            except Exception as persistence:
                print(json.dumps({"recovery_manifest_write_failed": type(persistence).__name__,
                                  "manifest": str(manifest_path),
                                  "emergency_hold_completed": manifest.get("emergency_hold_completed", False)}), flush=True)
            raise RuntimeError("Deployment recovery incomplete; inspect manifest, retain compatible backend: "
                               + str(manifest_path)) from error
        pool_release.json_save(manifest_path, manifest)
        raise


def rollback(manifest_path):
    safe_path(manifest_path, base.BASE)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    functional_rollback(manifest, manifest_path)
    print(json.dumps({"rolled_back": True, "manifest": str(manifest_path), "excel_admission_enabled": False,
                      "compatible_backend_retained": True, "database_and_links_untouched": True,
                      "service_states_preserved": True}), flush=True)


@contextmanager
def deployment_lock():
    import fcntl
    path = safe_path(base.BASE / "excel-deploy.lock", base.BASE)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", type=Path)
    parser.add_argument("--remote-ref", default=REMOTE_REF)
    args = parser.parse_args()
    base.require(os.name == "posix" and os.geteuid() == 0, "Run on the CPU server as root")
    base.check_mount()
    with deployment_lock() if args.apply or args.rollback else nullcontext():
        if args.rollback:
            rollback(args.rollback)
            return
        commit = verified_commit(args.remote_ref)
        targets = stage_targets()
        enabled = assert_pool_window()
        print(json.dumps({"preflight_ok": True, "commit": commit, "files": len(targets),
                          "enabled_pools": enabled, "in_flight": pool_release.in_flight(),
                          "restart_scope": [base.API_UNIT, base.FB_UNIT], "database_migrated": False}), flush=True)
        if args.apply:
            apply_release(targets, commit)


if __name__ == "__main__":
    main()
