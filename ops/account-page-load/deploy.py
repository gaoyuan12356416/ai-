"""Static files + scoped JSON compression. No application restart or data writes."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ["drama-material-api.service", "x-post-automation.service",
            "fb-auto-post-service.service", "fb-page-portrait.service",
            "x-channel-tag-revenue.service"]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def services():
    return run("systemctl", "show", *SERVICES, "--property=Id,MainPID,ActiveState", "--no-pager")


def atomic_copy(source, target, mode, uid, gid):
    target = Path(target)
    fd, temp = tempfile.mkstemp(prefix=".list-load-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(Path(source).read_bytes())
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, mode)
        os.chown(temp, uid, gid)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_manifest(folder, manifest):
    path = folder / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def restore(manifest):
    for item in reversed(manifest["files"]):
        atomic_copy(item["backup"], item["target"], item["mode"], item["uid"], item["gid"])
    run("nginx", "-t")
    run("systemctl", "reload", "nginx")


def apply(folder):
    if run("findmnt", "-no", "UUID", "--target", "/mnt/data-disk") != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise RuntimeError("Expected data disk is not mounted")
    if not str(folder.resolve()).startswith("/mnt/data-disk/"):
        raise RuntimeError("Deployment backups must use the data disk")
    if (folder / "manifest.json").exists():
        raise RuntimeError("Deployment folder already has a manifest")
    targets = json.loads((ROOT / "ops/account-page-load/targets.json").read_text())
    for item in targets:
        if digest(item["target"]) != item["expected_sha256"]:
            raise RuntimeError("Live baseline changed: " + item["target"])
    run("nginx", "-t")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "backup").mkdir()
    manifest = {"commit": run("git", "-C", str(ROOT), "rev-parse", "HEAD"),
                "at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "status": "backed_up", "services_before": services(), "files": []}
    for index, item in enumerate(targets):
        target = Path(item["target"]).resolve()
        source = ROOT / item["source"]
        stat = target.stat()
        backup = folder / "backup" / (str(index) + "-" + target.name)
        shutil.copy2(target, backup)
        manifest["files"].append({**item, "entry_path": item["target"], "target": str(target), "backup": str(backup),
                                  "installed_sha256": digest(source), "mode": stat.st_mode & 0o7777,
                                  "uid": stat.st_uid, "gid": stat.st_gid})
    write_manifest(folder, manifest)
    installed = []
    try:
        for item in manifest["files"]:
            if Path(item["entry_path"]).resolve() != Path(item["target"]):
                raise RuntimeError("Concurrent release switch: " + item["entry_path"])
            if digest(item["target"]) != item["expected_sha256"]:
                raise RuntimeError("Concurrent file change: " + item["target"])
            atomic_copy(ROOT / item["source"], item["target"], item["mode"], item["uid"], item["gid"])
            installed.append(item)
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
        for item in manifest["files"]:
            if digest(item["target"]) != item["installed_sha256"]:
                raise RuntimeError("Installed checksum mismatch: " + item["target"])
    except Exception:
        # Only undo files actually replaced by this process; retain concurrent edits.
        for item in reversed(installed):
            if digest(item["target"]) == item["installed_sha256"]:
                atomic_copy(item["backup"], item["target"], item["mode"], item["uid"], item["gid"])
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
        manifest["status"] = "failed_restored"
        write_manifest(folder, manifest)
        raise
    manifest.update(status="deployed", services_after=services())
    write_manifest(folder, manifest)
    print(json.dumps({"status": manifest["status"], "commit": manifest["commit"],
                      "files": len(manifest["files"]), "manifest": str(folder / "manifest.json"),
                      "application_pids_unchanged": manifest["services_before"] == manifest["services_after"]}))


def rollback(folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    for item in manifest["files"]:
        if digest(item["target"]) != item["installed_sha256"]:
            raise RuntimeError("Later change exists; reconcile this file before rollback: " + item["target"])
        if digest(item["backup"]) != item["expected_sha256"]:
            raise RuntimeError("Backup checksum mismatch: " + item["backup"])
    restore(manifest)
    manifest["status"] = "rolled_back"
    write_manifest(folder, manifest)
    print("Restored static/config files and reloaded Nginx; business data untouched.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["apply", "rollback"])
    parser.add_argument("--folder", required=True, type=Path)
    args = parser.parse_args()
    globals()[args.action](args.folder.resolve())
