"""Deploy only the shared dialog and its cache URLs; no publisher restart."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


SOURCE = Path(__file__).resolve().parents[1]
BASELINE = "52fee7459ea631ae2a7e8fba4d57e7530269aec241490d578f469f931b5c74d8"
ROOTS = (
    Path("/root/drama_material_service/static"),
    Path("/usr/share/nginx/html"),
    Path("/opt/x-tag-publisher/current/static"),
    Path("/opt/fb-auto-post/current/static"),
)
ASSET = "hit-drama-publish.js"
PAGES = ("fb-auto-publish-templates.html", "x-tag-auto-publish-templates.html")


def run(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, metadata):
    temporary = path.with_name(path.name + ".multi-id-" + str(os.getpid()))
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, metadata["mode"])
        os.chown(temporary, metadata["uid"], metadata["gid"])
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def plan():
    data = (SOURCE / "static" / ASSET).read_bytes()
    version = sha(data)[:12]
    changes = {}
    for root in ROOTS:
        asset = (root / ASSET).resolve(strict=True)
        if sha(asset.read_bytes()) != BASELINE:
            raise RuntimeError("Shared dialog changed; reconcile before deployment: " + str(asset))
        changes[asset] = data
        for name in PAGES:
            page = root / name
            if not page.is_file():
                continue
            page = page.resolve(strict=True)
            old = page.read_bytes()
            new, count = re.subn(
                rb'(/hit-drama-publish\.js)(?:\?[^"\']*)?',
                lambda match: match[1] + b"?v=" + version.encode(), old,
            )
            if count != 1:
                raise RuntimeError("Expected one dialog asset URL: " + str(page))
            changes[page] = new
    return changes


def apply(folder):
    commit = run("git", "-C", SOURCE, "rev-parse", "HEAD")
    if run("git", "-C", SOURCE, "status", "--porcelain"):
        raise RuntimeError("Deploy from a clean source checkout")
    if not run("git", "-C", SOURCE, "branch", "-r", "--contains", commit):
        raise RuntimeError("Commit must be fetched from GitHub before deployment")
    uuid = run("findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk")
    if uuid != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise RuntimeError("Expected data disk is unavailable")
    changes = plan()
    folder = folder.resolve()
    if not folder.is_relative_to(Path("/mnt/data-disk")):
        raise RuntimeError("Backups belong on the data disk")
    folder.mkdir(parents=True, exist_ok=False)
    record = {"commit": commit, "created_at": datetime.now(timezone.utc).isoformat(),
              "services_restarted": False, "files": {}}
    for index, (path, data) in enumerate(changes.items()):
        before = path.read_bytes()
        backup = folder / (str(index) + ".before")
        backup.write_bytes(before)
        metadata = path.stat()
        record["files"][str(path)] = {
            "before": sha(before), "after": sha(data), "backup": str(backup),
            "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid,
            "gid": metadata.st_gid,
        }
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps(record, indent=2) + "\n")
    # Recheck every affected file after backup, before switching any file.
    for name, entry in record["files"].items():
        if sha(Path(name).read_bytes()) != entry["before"]:
            raise RuntimeError("Concurrent edit; deployment not applied: " + name)
    for path, data in changes.items():
        atomic(path, data, record["files"][str(path)])
    for name, entry in record["files"].items():
        if sha(Path(name).read_bytes()) != entry["after"]:
            raise RuntimeError("Installed hash mismatch: " + name)
    print(json.dumps({"status": "installed", "commit": commit, "files": len(changes),
                      "manifest": str(manifest), "services_restarted": False}))


def rollback(folder):
    record = json.loads((folder / "manifest.json").read_text())
    for name, entry in record["files"].items():
        if sha(Path(name).read_bytes()) not in (entry["before"], entry["after"]):
            raise RuntimeError("Preserve newer edit; reconcile rollback for: " + name)
        if sha(Path(entry["backup"]).read_bytes()) != entry["before"]:
            raise RuntimeError("Backup checksum mismatch: " + name)
    for name, entry in record["files"].items():
        atomic(Path(name), Path(entry["backup"]).read_bytes(), entry)
        if sha(Path(name).read_bytes()) != entry["before"]:
            raise RuntimeError("Rollback hash mismatch: " + name)
    print(json.dumps({"status": "rolled_back", "files": len(record["files"]),
                      "services_restarted": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "rollback"))
    parser.add_argument("--folder", type=Path)
    args = parser.parse_args()
    if args.action == "plan":
        print(json.dumps({str(path): sha(data) for path, data in plan().items()}, indent=2))
    elif args.folder is None:
        parser.error("--folder is required for apply/rollback")
    elif args.action == "apply":
        apply(args.folder)
    else:
        rollback(args.folder)


if __name__ == "__main__":
    main()
