"""Activate or roll back the standalone OAuth service and its one Nginx route."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = Path("/mnt/data-disk/x-new-app-sandbox-20261019")
CURRENT = BASE / "current"
PROXY = Path("/etc/nginx/default.d/x-oauth.conf")
MAP = Path("/etc/nginx/conf.d/x-ads-oauth-callback-map.conf")
OLD = b"proxy_pass http://$x_new_app_callback_upstream/callback$is_args$args;"
NEW = OLD.replace(b"x_new_app_callback_upstream", b"x_ads_callback_upstream")
SERVICE = "x-new-app-test.service"


def run(*args):
    subprocess.run(args, check=True)


def write(path, data, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temp.write(data)
        os.fchmod(temp.fileno(), mode)
    os.replace(temp.name, path)


def change_proxy(old, new):
    content = PROXY.read_bytes()
    if content.count(old) != 1 or new in content:
        raise RuntimeError("callback directive changed")
    write(PROXY, content.replace(old, new), PROXY.stat().st_mode & 0o777)


def switch(directory):
    directory = directory.resolve(strict=True)
    if directory.parent != BASE / "releases":
        raise RuntimeError("unexpected release path")
    link = CURRENT.with_name(".current-ads-%s" % os.getpid())
    link.symlink_to(directory)
    os.replace(link, CURRENT)
    run("systemctl", "restart", SERVICE)
    for attempt in range(12):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18881/health", timeout=2) as response:
                if json.load(response).get("ok") is True:
                    return
        except OSError:
            pass
        time.sleep(.5)
    raise RuntimeError("standalone authorization service did not start")


def rollback(receipt):
    info = json.loads(receipt.read_text())
    previous, deployed = Path(info["previous"]), Path(info["release"])
    if CURRENT.resolve() not in (previous, deployed):
        raise RuntimeError("another release is active")
    if MAP.exists() and hashlib.sha256(MAP.read_bytes()).hexdigest() != info["map_sha256"]:
        raise RuntimeError("Ads callback map has changed")
    if NEW in PROXY.read_bytes():
        change_proxy(NEW, OLD)
    elif PROXY.read_bytes().count(OLD) != 1:
        raise RuntimeError("callback directive has changed")
    if MAP.exists():
        MAP.unlink()
    run("nginx", "-t")
    run("systemctl", "reload", "nginx")
    if CURRENT.resolve() == deployed:
        switch(previous)


def activate(expected):
    release = Path(__file__).resolve().parents[1]
    previous = CURRENT.resolve(strict=True)
    if previous.name != expected or release.parent != BASE / "releases" or release == previous:
        raise RuntimeError("standalone release has changed")
    if MAP.exists() or PROXY.read_bytes().count(OLD) != 1 or NEW in PROXY.read_bytes():
        raise RuntimeError("unexpected callback configuration")
    source = (release / "deploy" / MAP.name).read_bytes()
    backup = Path(tempfile.mkdtemp(prefix="ads-oauth-", dir=BASE / "backups"))
    receipt = backup / "release.json"
    shutil.copy2(PROXY, backup / PROXY.name)
    info = {"previous": str(previous), "release": str(release), "map_sha256": hashlib.sha256(source).hexdigest()}
    write(receipt, json.dumps(info).encode(), 0o600)
    command = "exec /usr/bin/python3 %s rollback %s" % (shlex.quote(__file__), shlex.quote(str(receipt)))
    write(backup / "rollback.sh", ("#!/bin/sh\n" + command + "\n").encode(), 0o700)
    print("rollback", backup / "rollback.sh", flush=True)
    try:
        write(MAP, source, 0o644)
        change_proxy(OLD, NEW)
        run("nginx", "-t")
        switch(release)
        run("systemctl", "reload", "nginx")
    except Exception:
        rollback(receipt)
        raise


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "activate":
        activate(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "rollback":
        rollback(Path(sys.argv[2]))
    else:
        raise SystemExit("use activate <previous-commit> or rollback <release.json>")
