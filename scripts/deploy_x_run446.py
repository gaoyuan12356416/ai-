#!/usr/bin/env python3
"""Phased GitHub-first Run 446 rollout. Never alter publishing task state."""
from __future__ import annotations
import argparse
import ast
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import uuid

DATA = Path("/mnt/data-disk")
DATA_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
CURRENT = Path("/opt/x-post-automation/current")
RELEASES = DATA / "x-post-automation/releases"
AUTO_DB = DATA / "x-auto-post-publisher/x-auto-post.sqlite3"
DISPATCH_DB = DATA / "pipeline-dispatch/dispatch.sqlite3"
MAIN = Path("/root/drama_material_service")
PUBLIC = Path("/usr/share/nginx/html")
SERVICE = "x-auto-post-service.service"
RUNNER = "x-auto-post-runner.service"
TRIGGERS = ("x-auto-post-runner.timer", "x-auto-post-runner.path")
OVERLAYS = ("features/x_auto_posts/publisher.py", "features/x_auto_posts/service.py",
            "features/x_auto_posts/operator_retry.py", "static/x-auto-publish-runs.js",
            "static/x-auto-publish-runs.html")
OLD_RUN_ITEM_SHA = "aa29498bf529fecf240f19d4d46e4da799781c4f2021a3c1052d7c2012027642"


def require(value, message):
    if not value:
        raise RuntimeError(message)


def within(path, parent):
    try:
        Path(path).relative_to(parent)
        return True
    except ValueError:
        return False


def command(args, *, check=True):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError("command_failed: " + " ".join(str(arg) for arg in args[:3])
                           + ": " + result.stderr.strip()[-800:])
    return result.stdout.strip()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_atomic(path, content, *, mode=None):
    path = Path(path)
    original = path.stat() if path.exists() else None
    temp = path.with_name(path.name + ".run446-" + uuid.uuid4().hex)
    with temp.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, mode if mode is not None else (original.st_mode & 0o777 if original else 0o600))
    if original:
        os.chown(temp, original.st_uid, original.st_gid)
    os.replace(temp, path)


def save(root, manifest):
    write_atomic(root / "manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(), mode=0o600)


def unit_state(unit):
    output = command(["systemctl", "show", unit, "--property=ActiveState,SubState,UnitFileState"])
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def verify_mount():
    require(os.geteuid() == 0, "root_required")
    require(command(["findmnt", "-n", "-o", "UUID", "--target", str(DATA)]) == DATA_UUID, "data_disk_mount_identity_changed")
    require(shutil.disk_usage(DATA).free > 1024 ** 3, "data_disk_free_space_below_1GiB")


def verify_source(source, commit):
    require(re.fullmatch(r"[0-9a-f]{40}", commit or ""), "exact_commit_required")
    require(command(["git", "-C", source, "rev-parse", "HEAD"]) == commit, "source_head_changed")
    require(not command(["git", "-C", source, "status", "--porcelain", "--untracked-files=no"]), "source_has_tracked_changes")
    for name in OVERLAYS:
        require((source / name).is_file(), "source_overlay_missing:" + name)
        if name.endswith(".py"):
            ast.parse((source / name).read_text(encoding="utf-8"))


def run_item_block(text):
    cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == "XAutoPostService")
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_run_item")
    first = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
    return first, node.end_lineno, "".join(text.splitlines(keepends=True)[first:node.end_lineno])


def staged_main(source):
    path = MAIN / "features/x_auto_posts/service.py"
    original = path.read_text(encoding="utf-8")
    start, end, block = run_item_block(original)
    require(hashlib.sha256(block.encode()).hexdigest() == OLD_RUN_ITEM_SHA, "main_run_item_changed_review_required")
    replacement = run_item_block((source / "features/x_auto_posts/service.py").read_text(encoding="utf-8"))[2]
    lines = original.splitlines(keepends=True)
    result = "".join(lines[:start]) + replacement + "".join(lines[end:])
    ast.parse(result)
    return result.encode()


def snapshot_db(source, target):
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            require(dst.execute("PRAGMA quick_check").fetchone()[0] == "ok", "database_backup_failed")
    os.chmod(target, 0o600)


def drain_runner(timeout):
    deadline = time.monotonic() + timeout
    while unit_state(RUNNER).get("ActiveState") not in {"inactive", "failed"}:
        require(time.monotonic() < deadline, "runner_still_active_not_interrupted; keep_triggers_paused_and_retry")
        print(json.dumps({"stage": "waiting_existing_runner", "seconds_remaining": int(deadline-time.monotonic())}), flush=True)
        time.sleep(5)


def check_paused(manifest):
    require(all(unit_state(unit).get("ActiveState") not in {"active", "activating"} for unit in TRIGGERS), "runner_trigger_active")
    require(unit_state(RUNNER).get("ActiveState") in {"inactive", "failed"}, "runner_not_drained")
    for unit, state in manifest["triggers"].items():
        require(unit_state(unit).get("UnitFileState") == state.get("UnitFileState"), "trigger_enablement_changed:" + unit)


def prepare(root, source, commit, timeout):
    require(not (root / "manifest.json").exists(), "deployment_manifest_exists_use_next_phase")
    verify_source(source, commit)
    require(CURRENT.is_symlink(), "current_must_be_release_symlink")
    current = CURRENT.resolve(strict=True)
    require(within(current, DATA), "runtime_not_on_data_disk")
    require(within(root, DATA) and not root.exists(), "new_data_disk_backup_root_required")
    main_payload = staged_main(source)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = RELEASES / ("run446-" + timestamp + "-" + commit[:12])
    require(not release.exists(), "release_already_exists")
    root.mkdir(parents=True, mode=0o700)
    manifest = {"kind": "x-run446-deployment-v1", "phase": "preparing", "commit": commit,
                "source": str(source), "old_release": str(current), "new_release": str(release),
                "created_at": timestamp, "triggers": {unit: unit_state(unit) for unit in TRIGGERS},
                "service_before": unit_state(SERVICE), "files": [], "source_hashes": {name: sha(source/name) for name in OVERLAYS},
                "old_runtime_hashes": {name: sha(current/name) if (current/name).exists() else None for name in OVERLAYS}}
    save(root, manifest)
    files = [MAIN / "features/x_auto_posts/service.py"]
    files += [MAIN / "static" / name for name in ("x-auto-publish-runs.js", "x-auto-publish-runs.html")]
    files += [PUBLIC / name for name in ("x-auto-publish-runs.js", "x-auto-publish-runs.html")]
    for index, path in enumerate(files):
        require(path.is_file(), "existing_target_missing:" + str(path))
        copy = root / ("before-" + str(index) + "-" + path.name)
        shutil.copy2(path, copy)
        st = path.stat()
        manifest["files"].append({"path": str(path), "backup": str(copy), "sha256": sha(path),
                                  "mode": st.st_mode & 0o777, "uid": st.st_uid, "gid": st.st_gid})
    write_atomic(root / "main-service.staged.py", main_payload, mode=0o600)
    acl = command(["getfacl", "-p", DISPATCH_DB.parent, DISPATCH_DB])
    write_atomic(root / "dispatch.acl.before", (acl + "\n").encode(), mode=0o600)
    save(root, manifest)
    snapshot_db(AUTO_DB, root / "auto.before.sqlite3")
    manifest["auto_backup_sha256"] = sha(root / "auto.before.sqlite3")
    shutil.copytree(current, release, symlinks=True)
    for name in OVERLAYS:
        require(within((release/name).parent.resolve(), release)
                and not (release/name).is_symlink(), "overlay_symlink_requires_review:" + name)
        shutil.copy2(source / name, release / name)
    manifest["new_runtime_hashes"] = {name: sha(release/name) for name in OVERLAYS}
    save(root, manifest)
    for unit in TRIGGERS:
        if unit_state(unit).get("ActiveState") in {"active", "activating"}:
            command(["systemctl", "stop", unit])
    manifest["phase"] = "draining"
    save(root, manifest)
    drain_runner(timeout)
    check_paused(manifest)
    manifest["phase"] = "prepared"
    save(root, manifest)
    return {"phase": "prepared", "manifest": str(root/"manifest.json"), "next": "apply two prepare recoveries, then switch"}


def grant_reader_acl(root):
    require(command(["systemctl", "show", SERVICE, "--property=User", "--value"]) == "x-post-daily", "unexpected_auto_service_user")
    sidecars = [Path(str(DISPATCH_DB) + suffix) for suffix in ("-wal", "-shm")]
    sidecar_backup = root / "dispatch.sidecars.before.json"
    if not sidecar_backup.exists():
        before = [{"path": str(path), "acl": command(["getfacl", "-p", path]) if path.exists() else None}
                  for path in sidecars]
        write_atomic(sidecar_backup, (json.dumps(before, indent=2) + "\n").encode(), mode=0o600)
    command(["setfacl", "-m", "u:x-post-daily:--x", DISPATCH_DB.parent])
    command(["setfacl", "-m", "u:x-post-daily:r--", DISPATCH_DB])
    # New SQLite WAL/SHM files inherit the reader entry and the DB's effective
    # 0640 mode. The directory remains traversal-only, never writable by Auto.
    command(["setfacl", "-m", "d:u:x-post-daily:r--,d:m::r--", DISPATCH_DB.parent])
    # Pin a root read snapshot during initialization/probe so read-only SQLite
    # can open existing WAL/SHM without needing directory write permission.
    with contextlib.closing(sqlite3.connect(DISPATCH_DB.resolve().as_uri() + "?mode=rw", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        require(conn.execute("PRAGMA journal_mode").fetchone()[0].lower() in {"delete", "wal"}, "dispatch_journal_mode_unsupported")
        conn.execute("BEGIN")
        conn.execute("SELECT id FROM pipeline_job WHERE source_key='x:queue:5753'").fetchall()
        for path in sidecars:
            if path.exists():
                command(["setfacl", "-m", "u:x-post-daily:r--", path])
        code = ("import sqlite3; c=sqlite3.connect('file:" + str(DISPATCH_DB) + "?mode=ro',uri=True,timeout=5); "
                "c.execute('PRAGMA query_only=ON'); "
                "r=c.execute(\"SELECT id,status FROM pipeline_job WHERE source_key='x:queue:5753'\").fetchall(); "
                "assert r; print('dispatch_read_ok'); c.close()")
        result = command(["runuser", "-u", "x-post-daily", "--", "python3", "-c", code])
        require(result == "dispatch_read_ok", "service_user_dispatch_read_failed")
        current = [DISPATCH_DB.parent, DISPATCH_DB] + [path for path in sidecars if path.exists()]
        write_atomic(root / "dispatch.acl.after", (command(["getfacl", "-p", *current]) + "\n").encode(), mode=0o600)


def link_release(release):
    temp = CURRENT.with_name("current.run446-" + uuid.uuid4().hex)
    temp.symlink_to(release)
    os.replace(temp, CURRENT)


def health():
    for _ in range(15):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18833/health", timeout=2) as response:
                result = json.load(response)
            if result.get("ok") is True and result.get("service") == "x-auto-post":
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("x_auto_health_failed_triggers_remain_paused")


def switch(root, manifest):
    require(manifest["phase"] in {"prepared", "switching"}, "prepare_phase_required")
    source = Path(manifest["source"])
    verify_source(source, manifest["commit"])
    check_paused(manifest)
    require(str(CURRENT.resolve()) == manifest["old_release"], "current_release_changed")
    for name, expected in manifest["old_runtime_hashes"].items():
        path = CURRENT/name
        require((sha(path) if path.exists() else None) == expected, "old_runtime_file_changed:" + name)
    for item in manifest["files"]:
        require(sha(item["path"]) in {item["sha256"], item.get("deployed_sha256", "")}, "target_file_changed:" + item["path"])
    release = Path(manifest["new_release"])
    require({name: sha(release/name) for name in OVERLAYS} == manifest["source_hashes"], "staged_release_hash_changed")
    for item in manifest["files"]:
        target = Path(item["path"])
        staged = root / "main-service.staged.py" if target.suffix == ".py" else release / "static" / target.name
        item["deployed_sha256"] = sha(staged)
    manifest["phase"] = "switching"
    save(root, manifest)
    grant_reader_acl(root)
    for item in manifest["files"]:
        target = Path(item["path"])
        payload = (root / "main-service.staged.py").read_bytes() if target.suffix == ".py" else (release / "static" / target.name).read_bytes()
        write_atomic(target, payload)
        item["deployed_sha256"] = sha(target)
    link_release(release)
    command(["systemctl", "restart", SERVICE])
    require(unit_state(SERVICE).get("ActiveState") == "active", "auto_service_not_active")
    health()
    manifest["phase"] = "switched"
    manifest["service_after"] = unit_state(SERVICE)
    save(root, manifest)
    return {"phase": "switched", "release": str(release), "service": SERVICE,
            "next": "apply task2712 audited retry, verify, then resume"}


def resume(root, manifest):
    require(manifest["phase"] in {"switched", "rolled_back"}, "switch_or_rollback_required")
    check_paused(manifest)
    health()
    for unit, before in manifest["triggers"].items():
        if before.get("ActiveState") in {"active", "activating"}:
            command(["systemctl", "start", unit])
    manifest["phase"] = "resumed" if manifest["phase"] == "switched" else "rollback_resumed"
    manifest["triggers_after"] = {unit: unit_state(unit) for unit in TRIGGERS}
    save(root, manifest)
    return {"phase": manifest["phase"], "triggers": manifest["triggers_after"]}


def rollback(root, manifest, timeout):
    with contextlib.closing(sqlite3.connect(AUTO_DB.resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='x_auto_operator_retry'").fetchone()
        require(not exists or not conn.execute("SELECT 1 FROM x_auto_operator_retry WHERE task_id=2712").fetchone(),
                "operator_retry_already_authorized_keep_key_support_forward_fix_required")
    require(str(CURRENT.resolve()) in {manifest["old_release"], manifest["new_release"]}, "unrelated_release_active_do_not_rollback")
    for unit in TRIGGERS:
        command(["systemctl", "stop", unit])
    drain_runner(timeout)
    for item in manifest["files"]:
        target = Path(item["path"])
        allowed = {item["sha256"], item.get("deployed_sha256", "")}
        require(sha(target) in allowed, "target_changed_after_deployment:" + str(target))
    for item in manifest["files"]:
        target = Path(item["path"])
        write_atomic(target, Path(item["backup"]).read_bytes(), mode=item["mode"])
        os.chown(target, item["uid"], item["gid"])
    link_release(Path(manifest["old_release"]))
    command(["setfacl", "-k", DISPATCH_DB.parent])
    command(["setfacl", "--restore=" + str(root/"dispatch.acl.before")])
    sidecar_backup = root / "dispatch.sidecars.before.json"
    if sidecar_backup.exists():
        for item in json.loads(sidecar_backup.read_text(encoding="utf-8")):
            path = Path(item["path"])
            if not path.exists():
                continue
            if item["acl"] is None:
                command(["setfacl", "-x", "u:x-post-daily", path], check=False)
            else:
                restore = root / "dispatch.sidecar.restore.acl"
                write_atomic(restore, (item["acl"] + "\n").encode(), mode=0o600)
                command(["setfacl", "--restore=" + str(restore)])
    command(["systemctl", "restart", SERVICE])
    health()
    manifest["phase"] = "rolled_back"
    save(root, manifest)
    return {"phase": "rolled_back", "next": "resume; databases and publication records preserved"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "drain", "switch", "resume", "rollback"), required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--drain-seconds", type=int, default=180)
    args = parser.parse_args()
    verify_mount()
    root = args.backup_root.resolve()
    require(within(root, DATA), "backup_root_must_be_on_data_disk")
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent / ".run446-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.phase == "prepare":
            require(args.source is not None and args.commit, "prepare_source_and_commit_required")
            result = prepare(root, args.source.resolve(strict=True), args.commit, args.drain_seconds)
        else:
            manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
            require(manifest.get("kind") == "x-run446-deployment-v1", "manifest_kind_invalid")
            if args.phase == "drain":
                require(manifest["phase"] == "draining", "draining_phase_required")
                drain_runner(args.drain_seconds)
                check_paused(manifest)
                manifest["phase"] = "prepared"
                save(root, manifest)
                result = {"phase": "prepared", "next": "apply two prepare recoveries, then switch"}
            elif args.phase == "switch":
                result = switch(root, manifest)
            elif args.phase == "resume":
                result = resume(root, manifest)
            else:
                result = rollback(root, manifest, args.drain_seconds)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
