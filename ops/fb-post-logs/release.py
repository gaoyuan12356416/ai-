"""Install or roll back this independent admin page; never roll back business data."""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from features.fb_post_logs.manager import Manager, NATIVE
from features.fb_video_publish.navigation import ITEMS

PUBLIC = Path("/usr/share/nginx/html")
MAIN = Path("/root/drama_material_service/static")
CURRENT = Path("/opt/fb-post-logs/current")
UNIT = Path("/etc/systemd/system/fb-post-logs.service")
NGINX = Path("/etc/nginx/default.d/fb-post-logs.conf")
ENTRY = next(i for i in ITEMS if i["key"] == "fbPostLogs")
NAV = [p / "navigation.json" for p in (PUBLIC, MAIN)]
FILES = {
    p / name: ROOT / "static" / name
    for p in (PUBLIC, MAIN)
    for name in ("fb-post-logs.html", "fb-post-logs.js")
}
FILES.update({UNIT: Path(__file__).with_name(UNIT.name), NGINX: Path(__file__).with_name(NGINX.name)})


def run(*args):
    subprocess.run(args, check=True)


def write(path, data):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(data)
        tmp = Path(f.name)
    tmp.chmod(mode)
    tmp.replace(path)


def nav(path, install):
    original = path.read_bytes()
    config = json.loads(original)
    group = next(g for g in config if g["key"] == "facebook_platform")
    items = group["items"]
    prior = next((i for i in items if i["key"] == ENTRY["key"]), None)
    if prior and prior != ENTRY:
        raise RuntimeError("Concurrent navigation customization: " + str(path))
    if bool(prior) == install:
        return
    if install:
        items.append(ENTRY)
    else:
        items.remove(prior)
    if original != path.read_bytes():
        raise RuntimeError("Navigation changed during update: " + str(path))
    write(path, (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode())


def rollback(folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    for p, expected in FILES.items():
        if p.exists() and p.read_bytes() != expected.read_bytes():
            raise RuntimeError("A newer release owns " + str(p))
    if CURRENT.is_symlink() and CURRENT.resolve() != ROOT:
        raise RuntimeError("A newer service is active")
    for p in NAV:
        nav(p, False)
    if NGINX.exists():
        NGINX.unlink()
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
    if UNIT.exists():
        run("systemctl", "disable", "--now", UNIT.name)
    for path in FILES:
        if path.exists():
            path.unlink()
    run("systemctl", "daemon-reload")
    if CURRENT.is_symlink():
        CURRENT.unlink()
    print("rolled back", manifest["commit"])


def install(folder):
    if any(p.exists() for p in [*FILES, CURRENT]) or CURRENT.is_symlink():
        raise RuntimeError("Existing FB log release; inspect before replacing")
    for p in NAV:
        groups = json.loads(p.read_bytes())
        fb = next(g for g in groups if g["key"] == "facebook_platform")
        if any(i["key"] == ENTRY["key"] for i in fb["items"]):
            raise RuntimeError("Navigation key already in use")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    for ix, path in enumerate(NAV):
        shutil.copy2(path, folder / ("navigation-" + str(ix) + ".json"))
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "commit": commit,
                "files": {str(p): hashlib.sha256(s.read_bytes()).hexdigest() for p, s in FILES.items()},
            },
            indent=2,
        )
    )
    # Rehearse the additive indexes and an actual query on a consistent native snapshot.
    backup = folder / "fb-before.sqlite3"
    with closing(sqlite3.connect(Path(NATIVE).as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("BEGIN")
        c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        with closing(sqlite3.connect(backup)) as target:
            c.backup(target, pages=512, sleep=0.05)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Native log backup integrity check failed")
    backup.chmod(0o600)
    rehearsal = folder / "fb-rehearsal.sqlite3"
    shutil.copyfile(backup, rehearsal)
    sample = Manager(rehearsal)
    sample.install()
    assert sample.query({"page_size": "2"})["pagination"]["total"] >= 0
    rehearsal.unlink()
    Manager().install()
    try:
        CURRENT.parent.mkdir(parents=True, exist_ok=True)
        CURRENT.symlink_to(ROOT, target_is_directory=True)
        for dest, source in FILES.items():
            write(dest, source.read_bytes())
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", UNIT.name)
        run("systemctl", "is-active", "--quiet", UNIT.name)
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
        for p in NAV:
            nav(p, True)
    except BaseException:
        rollback(folder)
        raise
    print("installed", commit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "rollback"])
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    (install if args.action == "install" else rollback)(args.folder.resolve())
