#!/usr/bin/env python3
"""Install only this service from an already staged exact GitHub commit."""
import datetime
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys


def run(*args):
    return subprocess.check_output(args, universal_newlines=True).strip()


def main():
    os.umask(0o077)
    module = Path(__file__).resolve().parent
    release = module.parent.parent
    commit = sys.argv[1]
    if release.name != commit or len(commit) != 40:
        raise SystemExit("release directory must be the exact GitHub commit")
    uuid = run("findmnt", "-no", "UUID", "/mnt/data-disk")
    if uuid != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise SystemExit("required data disk is not mounted")
    if shutil.disk_usage("/mnt/data-disk").free < 5 * 1024 ** 3:
        raise SystemExit("insufficient data disk space")
    state = Path("/mnt/data-disk/codex-image-api")
    state.mkdir(mode=0o711, exist_ok=True)
    backup = state / "backups" / (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + commit[:8])
    backup.mkdir(parents=True)
    current = Path("/opt/codex-image-api/current")
    previous = str(current.resolve()) if current.exists() else None
    targets = {
        "/etc/systemd/system/codex-image-api.service": module / "deploy/codex-image-api.service",
        "/etc/systemd/system/codex-image-worker.service": module / "deploy/codex-image-worker.service",
        "/etc/nginx/default.d/codex-image-api.conf": module / "deploy/codex-image-api.conf",
    }
    saved = {}
    for target in list(targets) + ["/etc/codex-image-api.env"]:
        path = Path(target)
        destination = backup / path.name
        saved[target] = str(destination) if path.exists() else None
        if path.exists():
            shutil.copy2(path, destination)
    manifest = {"commit": commit, "previous_release": previous, "files": saved}
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    try:
        pwd.getpwnam("codex-image-runner")
    except KeyError:
        run("useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/sbin/nologin", "codex-image-runner")
    config = Path("/etc/codex-image-api.env")
    if not config.exists():
        shutil.copyfile(module / "deploy/service.env.example", config)
    os.chmod(config, 0o600)
    for target, source in targets.items():
        shutil.copyfile(source, target)
        os.chmod(target, 0o644)
    try:
        run("nginx", "-t")
    except Exception:
        for target, original in saved.items():
            if original:
                shutil.copy2(original, target)
            elif Path(target).exists():
                Path(target).unlink()
        raise
    temporary = current.with_name("current.next")
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(release)
    os.replace(temporary, current)
    (state / "release.json").write_text(json.dumps({"commit": commit, "backup": str(backup)}, indent=2))
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "codex-image-api.service", "codex-image-worker.service")
    run("systemctl", "restart", "codex-image-api.service", "codex-image-worker.service")
    run("systemctl", "reload", "nginx")
    print(json.dumps({"commit": commit, "backup": str(backup), "previous_release": previous}))


if __name__ == "__main__":
    main()
