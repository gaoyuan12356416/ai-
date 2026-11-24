"""Scoped update for the two FB log views. Leaves all publisher units and ledgers live."""

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess

from release import CURRENT, MAIN, PUBLIC, ROOT, run, write

BASE = "12533ea0af03608d900c0a1860fea75471cd5d35"
LIVE = {
    "fb-auto-publish-templates.html": "adab6323d9a106266b363ad6e2b955b1ff6ff2bce4cef7805cd9b6dac286afbe",
    "fb-auto-publish-templates.js": "88b031afe4ea8dc2275071ca2d2cd7a36ed8b02f77abaa8a9f9407edb53bd33b",
    "fb-auto-publish-runs.html": "a917d687d24e9c5ae475d9fd074e7f46a08f3689c5ceca7ec3fe569871d81450",
    "fb-post-logs.html": "e0da5544983b5b8aec91164e6a6281b6a3c46b357f157d388f4540525938c942",
    "fb-post-logs.js": "513b268729cba01801e73a821d4c0ae127a98b97565417796252a7ffdd413203",
    "fb-auto-publish-runs.js": None,
    "fb-post-address.js": None,
}
STATIC_NAMES = sorted(LIVE, key=lambda name: name.endswith(".html"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def activate(target):
    link = CURRENT.with_name("next-template-history")
    link.symlink_to(target)
    link.replace(CURRENT)
    run("systemctl", "restart", "fb-post-logs.service")
    run("systemctl", "is-active", "--quiet", "fb-post-logs.service")


def rollback(folder):
    previous = Path((folder / "previous").read_text())
    if CURRENT.resolve() not in (ROOT, previous):
        raise RuntimeError("Another version is active")
    for dest in (MAIN, PUBLIC):
        for name in STATIC_NAMES:
            if sha(dest / name) not in (LIVE[name], sha(ROOT / "static" / name)):
                raise RuntimeError("Another release owns " + str(dest / name))
    for dest in (MAIN, PUBLIC):
        for name in reversed(STATIC_NAMES):
            p = dest / name
            if LIVE[name] is None:
                if p.exists():
                    p.unlink()
            else:
                write(p, (folder / name).read_bytes())
    if CURRENT.resolve() == ROOT:
        activate(previous)


def install(folder):
    previous = CURRENT.resolve(strict=True)
    sha_before = subprocess.check_output(["git", "-C", str(previous), "rev-parse", "HEAD"], text=True).strip()
    if sha_before != BASE:
        raise RuntimeError("Check concurrent log service changes before switching")
    for dest in (MAIN, PUBLIC):
        for name in STATIC_NAMES:
            if sha(dest / name) != LIVE[name]:
                raise RuntimeError("Check current file before installation: " + str(dest / name))
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    (folder / "previous").write_text(str(previous))
    for name in STATIC_NAMES:
        if LIVE[name]:
            shutil.copy2(PUBLIC / name, folder / name)
    try:
        activate(ROOT)
        for name in STATIC_NAMES:
            for dest in (MAIN, PUBLIC):
                if sha(dest / name) != LIVE[name]:
                    raise RuntimeError("Concurrent static file change: " + str(dest / name))
                write(dest / name, (ROOT / "static" / name).read_bytes())
    except BaseException:
        rollback(folder)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "rollback"])
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    (install if args.action == "install" else rollback)(args.folder.resolve())
