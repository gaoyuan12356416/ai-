#!/usr/bin/env python3
"""Narrow GitHub-first main API attribution fix; never mutate task databases."""
import argparse
import ast
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import deploy_x_run446 as base

SERVICE = "drama-material-api.service"
FILES = ("features/x_posts/attribution.py", "features/x_auto_posts/operator_retry.py")
EXPECTED = {FILES[0]: "5fc5f026eed9421cf32560aa8d5cb3303a43e56c1301a631a4cbbbfa5c70e567", FILES[1]: None}
KIND = "run446-audited-retry-attribution-v1"


def probe():
    pid = base.command(["systemctl", "show", SERVICE, "-p", "MainPID", "--value"])
    env = dict(item.split(b"=", 1) for item in Path("/proc", pid, "environ").read_bytes().split(b"\0") if b"=" in item)
    token = env.get(b"X_POST_AUTOMATION_INTERNAL_TOKEN", b"").decode()
    base.require(len(token) >= 32, "internal_probe_token_missing")
    request = urllib.request.Request("http://127.0.0.1:8787/internal/x-posts/attribution/resolve",
        data=b'{"queue_id":5908,"log_id":5853}', method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read(65536))
    context = result["context"]
    base.require(context["af_c_id"] == "5908" and context["template_id"] == 4
                 and context["link_type"] == "auto_template", "attribution_probe_identity_mismatch")
    # Deliberately omit identity details and credentials from the output.
    return {"queue_id": 5908, "log_id": 5853, "attribution_verified": True}


def prepare(root, source, commit):
    base.require(not root.exists(), "new_backup_root_required")
    base.verify_source(source, commit)
    for name in FILES:
        ast.parse((source/name).read_text(encoding="utf-8"))
        target = base.MAIN/name
        base.require((base.sha(target) if target.exists() else None) == EXPECTED[name], "runtime_drift:"+name)
    root.mkdir(parents=True, mode=0o700)
    records = []
    for name in FILES:
        target = base.MAIN/name
        item = {"relative": name, "path": str(target), "exists": target.exists(),
                "before_sha256": EXPECTED[name], "after_sha256": base.sha(source/name)}
        if target.exists():
            saved = root/Path(name).name
            shutil.copy2(target, saved)
            st = target.stat()
            item.update(backup=str(saved), mode=st.st_mode & 0o777, uid=st.st_uid, gid=st.st_gid)
        records.append(item)
    manifest = {"kind": KIND, "phase": "prepared", "commit": commit, "source": str(source),
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "files": records,
                "service_before": base.unit_state(SERVICE)}
    base.save(root, manifest)
    return {"phase": "prepared", "manifest": str(root/"manifest.json")}


def apply(root, manifest):
    base.require(manifest["phase"] == "prepared", "prepared_phase_required")
    source = Path(manifest["source"])
    base.verify_source(source, manifest["commit"])
    for item in manifest["files"]:
        target = Path(item["path"])
        base.require((base.sha(target) if target.exists() else None) == item["before_sha256"], "runtime_changed")
        base.require(base.sha(source/item["relative"]) == item["after_sha256"], "source_changed")
    manifest["phase"] = "installing"
    base.save(root, manifest)
    # Install the dependency before the importing resolver.
    for item in reversed(manifest["files"]):
        base.write_atomic(item["path"], (source/item["relative"]).read_bytes(), mode=item.get("mode", 0o644))
    manifest["phase"] = "installed"
    base.save(root, manifest)
    base.command(["systemctl", "restart", SERVICE])
    for attempt in range(10):
        try:
            result = probe()
            manifest.update(phase="verified", verification=result, service_after=base.unit_state(SERVICE))
            base.save(root, manifest)
            return {"phase": "verified", **result}
        except Exception:
            if attempt == 9:
                raise RuntimeError("attribution_probe_failed_see_manifest_no_database_changes") from None
            time.sleep(1)


def rollback(root, manifest):
    for item in manifest["files"]:
        path = Path(item["path"])
        current = base.sha(path) if path.exists() else None
        base.require(current in (item["after_sha256"], item["before_sha256"]), "runtime_changed")
        if item["exists"]:
            base.require(base.sha(Path(item["backup"])) == item["before_sha256"], "backup_changed")
    for item in manifest["files"]:
        path = Path(item["path"])
        if item["exists"]:
            base.write_atomic(path, Path(item["backup"]).read_bytes(), mode=item["mode"])
            os.chown(path, item["uid"], item["gid"])
        elif path.exists():
            path.unlink()
    base.command(["systemctl", "restart", SERVICE])
    manifest["phase"] = "rolled_back"
    base.save(root, manifest)
    return {"phase": "rolled_back", "task_and_publication_state_preserved": True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase", required=True, choices=("prepare", "apply", "probe", "rollback"))
    p.add_argument("--backup-root", type=Path, required=True)
    p.add_argument("--source", type=Path)
    p.add_argument("--commit")
    args = p.parse_args()
    base.verify_mount()
    root = args.backup_root.resolve()
    base.require(base.within(root, base.DATA), "backup_root_must_be_on_data_disk")
    if args.phase == "prepare":
        base.require(args.source and args.commit, "source_commit_required")
        result = prepare(root, args.source.resolve(), args.commit)
    elif args.phase == "probe":
        result = probe()
    else:
        manifest = json.loads((root/"manifest.json").read_text())
        base.require(manifest["kind"] == KIND, "invalid_manifest")
        result = apply(root, manifest) if args.phase == "apply" else rollback(root, manifest)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
