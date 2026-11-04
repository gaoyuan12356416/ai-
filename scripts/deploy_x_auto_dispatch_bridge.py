#!/usr/bin/env python3
"""Supplemental, phased deployment of the local read-only dispatch bridge."""
from __future__ import annotations
import argparse
import ast
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import deploy_x_run446 as base

BRIDGE = "x-auto-dispatch-state.service"
UNIT = Path("/etc/systemd/system") / BRIDGE
DROPIN = Path("/etc/systemd/system/x-auto-post-service.service.d/30-dispatch-state.conf")
SOCKET = Path("/run/x-auto-dispatch-state/state.sock")
FILES = ("features/x_auto_posts/publisher.py", "features/x_auto_posts/service.py",
         "features/x_auto_posts/dispatch_reader.py", "scripts/x_auto_dispatch_state_bridge.py",
         "static/x-auto-publish-runs.js", "static/x-auto-publish-runs.html")
KIND = "x-auto-dispatch-bridge-deployment-v1"


def file_record(path, root, index):
    record = {"path": str(path), "exists": path.exists()}
    if path.exists():
        target = root / ("before-" + str(index) + "-" + path.name)
        shutil.copy2(path, target)
        stat = path.stat()
        record.update(backup=str(target), sha256=base.sha(path), mode=stat.st_mode & 0o777,
                      uid=stat.st_uid, gid=stat.st_gid)
    return record


def prepare(root, source, commit, original_acl, timeout):
    base.require(not root.exists(), "new_backup_root_required")
    base.verify_source(source, commit)
    base.require(original_acl.is_file(), "original_pre_run446_acl_backup_required")
    for name in FILES:
        base.require((source/name).is_file(), "source_file_missing:" + name)
        if name.endswith(".py"):
            ast.parse((source/name).read_text(encoding="utf-8"))
    base.require((source/"deploy"/BRIDGE).is_file(), "bridge_unit_missing")
    old = base.CURRENT.resolve(strict=True)
    base.require(base.within(old, base.DATA) and base.CURRENT.is_symlink(), "current_release_invalid")
    root.mkdir(parents=True, mode=0o700)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    release = base.RELEASES / ("run446-dispatch-bridge-" + timestamp + "-" + commit[:12])
    paths = [UNIT, DROPIN] + [parent/name for parent in (base.MAIN/"static", base.PUBLIC)
                             for name in ("x-auto-publish-runs.js", "x-auto-publish-runs.html")]
    manifest = {"kind": KIND, "phase": "preparing", "source": str(source), "commit": commit,
                "old_release": str(old), "new_release": str(release),
                "original_acl": str(original_acl), "original_acl_sha256": base.sha(original_acl),
                "triggers": {unit: base.unit_state(unit) for unit in base.TRIGGERS},
                "bridge_before": base.unit_state(BRIDGE),
                "operator_retry_sha256": base.sha(old/"features/x_auto_posts/operator_retry.py"),
                "files": [file_record(path, root, i) for i, path in enumerate(paths)],
                "source_hashes": {name: base.sha(source/name) for name in FILES}}
    acl_paths = [base.DISPATCH_DB.parent, base.DISPATCH_DB]
    acl_paths += [Path(str(base.DISPATCH_DB)+suffix) for suffix in ("-wal", "-shm")
                 if Path(str(base.DISPATCH_DB)+suffix).exists()]
    base.write_atomic(root/"acl.before-bridge", (base.command(["getfacl", "-p", *acl_paths])+"\n").encode(), mode=0o600)
    base.snapshot_db(base.AUTO_DB, root/"auto.before-bridge.sqlite3")
    shutil.copytree(old, release, symlinks=True)
    for name in FILES:
        target = release/name
        base.require(base.within(target.parent.resolve(), release) and not target.is_symlink(), "overlay_symlink_requires_review")
        shutil.copy2(source/name, target)
    base.require(base.sha(release/"features/x_auto_posts/operator_retry.py") == manifest["operator_retry_sha256"],
                 "operator_retry_support_changed")
    shutil.copy2(source/"deploy"/BRIDGE, root/BRIDGE)
    base.write_atomic(root/"30-dispatch-state.conf", ("[Unit]\nWants="+BRIDGE+"\nAfter="+BRIDGE+"\n").encode(), mode=0o600)
    for item in manifest["files"]:
        path = Path(item["path"])
        staged = root/path.name if path in (UNIT, DROPIN) else release/"static"/path.name
        item["deployed_sha256"] = base.sha(staged)
    base.save(root, manifest)
    for unit in base.TRIGGERS:
        if base.unit_state(unit).get("ActiveState") in {"active", "activating"}:
            base.command(["systemctl", "stop", unit])
    manifest["phase"] = "draining"
    base.save(root, manifest)
    base.drain_runner(timeout)
    base.check_paused(manifest)
    manifest["phase"] = "prepared"
    base.save(root, manifest)
    return {"phase": "prepared", "manifest": str(root/"manifest.json")}


def probe():
    for _ in range(15):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(SOCKET))
                client.sendall(b'{"queue_id":5753}\n')
                data = b""
                while b"\n" not in data and len(data) <= 16384:
                    block = client.recv(4096)
                    if not block:
                        break
                    data += block
            result = json.loads(data)
            base.require(result.get("ok") is True and result.get("state", {}).get("source_key") == "x:queue:5753",
                         "bridge_response_invalid")
            return
        except (OSError, ValueError, RuntimeError):
            time.sleep(1)
    raise RuntimeError("bridge_probe_failed_triggers_remain_paused")


def restore_acl_existing(acl_path, root):
    blocks = []
    for block in acl_path.read_text(encoding="utf-8").split("\n\n"):
        names = [line[len("# file: "):] for line in block.splitlines() if line.startswith("# file: ")]
        if names and Path(names[0]).exists():
            blocks.append(block)
    filtered = root/"acl.restore-existing"
    base.write_atomic(filtered, ("\n\n".join(blocks)+"\n").encode(), mode=0o600)
    base.command(["setfacl", "-k", base.DISPATCH_DB.parent])
    base.command(["setfacl", "--restore="+str(filtered)])


def remove_direct_reader_acls(root, manifest):
    original = Path(manifest["original_acl"])
    base.require(base.sha(original) == manifest["original_acl_sha256"], "original_acl_changed")
    restore_acl_existing(original, root)
    sidecar_before = original.parent/"dispatch.sidecars.before.json"
    records = json.loads(sidecar_before.read_text(encoding="utf-8")) if sidecar_before.exists() else []
    by_path = {item["path"]: item.get("acl") for item in records}
    for suffix in ("-wal", "-shm"):
        path = Path(str(base.DISPATCH_DB)+suffix)
        if not path.exists():
            continue
        acl = by_path.get(str(path))
        if acl:
            saved = root/"sidecar.restore"
            base.write_atomic(saved, (acl+"\n").encode(), mode=0o600)
            base.command(["setfacl", "--restore="+str(saved)])
        else:
            base.command(["setfacl", "-x", "u:x-post-daily", path], check=False)


def switch(root, manifest):
    base.require(manifest["phase"] in {"prepared", "switching"}, "prepared_phase_required")
    base.check_paused(manifest)
    source, release = Path(manifest["source"]), Path(manifest["new_release"])
    base.verify_source(source, manifest["commit"])
    base.require(str(base.CURRENT.resolve()) in {manifest["old_release"], manifest["new_release"]}, "current_release_changed")
    base.require({name: base.sha(release/name) for name in FILES} == manifest["source_hashes"], "staged_source_changed")
    base.require(base.sha(release/"features/x_auto_posts/operator_retry.py") == manifest["operator_retry_sha256"], "retry_support_changed")
    for item in manifest["files"]:
        path = Path(item["path"])
        current = base.sha(path) if path.exists() else None
        base.require(current in {item.get("sha256"), item["deployed_sha256"]}, "target_changed:"+str(path))
    manifest["phase"] = "switching"
    base.save(root, manifest)
    for item in manifest["files"]:
        path = Path(item["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = root/path.name if path in (UNIT, DROPIN) else release/"static"/path.name
        base.write_atomic(path, staged.read_bytes(), mode=item.get("mode", 0o644))
    base.link_release(release)
    base.command(["systemctl", "daemon-reload"])
    base.command(["systemctl", "enable", BRIDGE])
    base.command(["systemctl", "restart", BRIDGE])
    probe()
    # Only after the root bridge has proved it can serve the fixed query do we
    # remove the temporary direct-reader ACLs from the unprivileged Auto user.
    remove_direct_reader_acls(root, manifest)
    code = ("import sys; sys.path.insert(0,"+repr(str(release))+"); "
            "from features.x_auto_posts.dispatch_reader import read_dispatch_state; "
            "r=read_dispatch_state(5753); assert r['source_key']=='x:queue:5753'; print('socket_read_ok')")
    base.require(base.command(["runuser", "-u", "x-post-daily", "--", "python3", "-c", code]) == "socket_read_ok", "service_user_socket_probe_failed")
    base.command(["systemctl", "restart", base.SERVICE])
    base.health()
    manifest["phase"] = "switched"
    manifest["bridge_after"] = base.unit_state(BRIDGE)
    base.save(root, manifest)
    return {"phase": "switched", "release": str(release), "direct_reader_acls_removed": True}


def rollback(root, manifest, timeout):
    for unit in base.TRIGGERS:
        base.command(["systemctl", "stop", unit])
    base.drain_runner(timeout)
    base.require(str(base.CURRENT.resolve()) in {manifest["old_release"], manifest["new_release"]}, "unrelated_release_active")
    for item in manifest["files"]:
        path = Path(item["path"])
        if path.exists():
            base.require(base.sha(path) in {item.get("sha256"), item["deployed_sha256"]}, "target_drifted:"+str(path))
    base.command(["systemctl", "stop", BRIDGE])
    if manifest["bridge_before"].get("UnitFileState") != "enabled":
        base.command(["systemctl", "disable", BRIDGE])
    for item in manifest["files"]:
        path = Path(item["path"])
        if item["exists"]:
            base.write_atomic(path, Path(item["backup"]).read_bytes(), mode=item["mode"])
            os.chown(path, item["uid"], item["gid"])
        elif path.exists():
            path.unlink()
    base.link_release(Path(manifest["old_release"]))
    restore_acl_existing(root/"acl.before-bridge", root)
    base.command(["systemctl", "daemon-reload"])
    if manifest["bridge_before"].get("ActiveState") == "active":
        base.command(["systemctl", "start", BRIDGE])
    base.command(["systemctl", "restart", base.SERVICE])
    base.health()
    manifest["phase"] = "rolled_back"
    base.save(root, manifest)
    return {"phase": "rolled_back", "next": "resume; all task and publication state preserved"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "drain", "switch", "resume", "rollback"), required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--original-acl-backup", type=Path)
    parser.add_argument("--drain-seconds", type=int, default=180)
    args = parser.parse_args()
    base.verify_mount()
    root = args.backup_root.resolve()
    base.require(base.within(root, base.DATA), "backup_root_must_be_on_data_disk")
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent/".run446-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.phase == "prepare":
            base.require(args.source and args.commit and args.original_acl_backup, "source_commit_original_acl_required")
            result = prepare(root, args.source.resolve(), args.commit, args.original_acl_backup.resolve(), args.drain_seconds)
        else:
            manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
            base.require(manifest["kind"] == KIND, "manifest_kind_invalid")
            if args.phase == "drain":
                base.require(manifest["phase"] == "draining", "draining_phase_required")
                base.drain_runner(args.drain_seconds); base.check_paused(manifest)
                manifest["phase"] = "prepared"; base.save(root, manifest)
                result = {"phase": "prepared"}
            elif args.phase == "switch":
                result = switch(root, manifest)
            elif args.phase == "resume":
                result = base.resume(root, manifest)
            else:
                result = rollback(root, manifest, args.drain_seconds)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
