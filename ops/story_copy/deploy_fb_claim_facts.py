"""Stage the caption worker and the FB owner overlay, and switch each independently."""

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE))
from features.story_copy import store
from ops.story_copy import deploy as release
from ops.story_copy.deploy_click_hooks import central_switch

BASE = "2fcc96bb6623468999d256332a954ca5c9958a84"
FILES = ("bridge.py", "sources.py", "store.py", "worker.py", "fb_facts.py")
PACKAGE = Path("features/story_copy")


def prepare(folder):
    old = release.CENTRAL.resolve(strict=True)
    fb = release.ROOTS["fb"].resolve(strict=True)
    commit = release.read("git", "-C", SOURCE, "rev-parse", "HEAD")
    prior = json.loads((fb.parent / "manifest.json").read_text())
    folder.mkdir(mode=0o755)
    backup = folder / "backup"
    backup.mkdir(mode=0o700)
    central = folder / "central"
    native = folder / "fb" / "candidate"
    native.parent.mkdir(mode=0o755)
    for original, target in ((old, central), (fb, native)):
        release.run("cp", "-a", original, target)

    changes = {}
    for filename in FILES:
        name = str(PACKAGE / filename)
        desired = (SOURCE / name).read_bytes()
        destination = central / name
        if filename == "fb_facts.py":
            if destination.exists() or (native / name).exists():
                raise RuntimeError("existing facts helper in active release")
        else:
            base = subprocess.check_output(["git", "-C", SOURCE, "show", BASE + ":" + name])
            if destination.read_bytes() != base:
                raise RuntimeError("caption worker source drift: " + name)
            (backup / filename).write_bytes(base)
        compile(desired, name, "exec")
        release.atomic(destination, desired)
        if filename not in ("bridge.py", "fb_facts.py"):
            continue
        previous = fb / name
        if previous.exists():
            shutil.copy2(previous, backup / "fb-bridge.py")
            # Preserve the native supplier-link branch and any independent FB edits.
            desired = subprocess.check_output(
                ["git", "merge-file", "-p", previous, backup / filename, SOURCE / name])
        compile(desired, name, "exec")
        release.atomic(native / name, desired)
        changes[name] = dict(before=release.sha(previous.read_bytes()) if previous.exists() else "",
                             after=release.sha(desired))

    for label, dbpath in (("captions",store.DB), ("fb", Path(store.SOURCES["fa"][1]))):
        path = backup / (label + ".sqlite3")
        with store.connection(dbpath, readonly=True) as src, closing(sqlite3.connect(path)) as dst:
            src.execute("BEGIN")
            src.backup(dst, pages=1024)
        path.chmod(0o600)
    manifest = dict(platform="fb", previous=str(fb), candidate=str(native), status="prepared",
                    commit=prior["commit"], source=prior["source"], caption_commit=commit, static={}, files=changes)
    release.atomic(native.parent / "manifest.json", json.dumps(manifest, indent=2).encode())
    release.save(folder, dict(commit=commit, central_before=str(old), central=str(central),
                              files={n: release.sha((central / PACKAGE / n).read_bytes()) for n in FILES}))


def switch_fb(folder, reverse):
    manifest = json.loads((folder / "manifest.json").read_text())
    target = Path(manifest["previous" if reverse else "candidate"])
    if release.ROOTS["fb"].resolve() == target:
        return
    sys.path.insert(0, manifest["source"])
    from scripts import deploy_fb_skipped_recheck as fb, deploy_hit_drama
    from scripts.deploy_fb_profile_priority import owner_active

    def finish_active():
        deadline = time.monotonic() + 900
        while owner_active():
            if time.monotonic() >= deadline:
                raise RuntimeError("active FB work preserved; retry after it finishes")
            time.sleep(1)
        return 0

    fb.native_active = finish_active
    deploy_hit_drama.activate("fb", folder, reverse, defer_plan_read=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare","central","fb","rollback"))
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if (Path("/mnt/data-disk") not in folder.parents or
            release.read("findmnt","-n","-o","UUID","--mountpoint","/mnt/data-disk") != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"):
        raise SystemExit("expected the mounted data disk")
    if args.action == "prepare":
        prepare(folder)
    else:
        state = json.loads((folder / "record.json").read_text())
        before, after = Path(state["central_before"]), Path(state["central"])
        if args.action == "central":
            for name, digest in state["files"].items():
                if release.sha((after / PACKAGE / name).read_bytes()) != digest:
                    raise RuntimeError("caption candidate changed: " + name)
            central_switch(before, after)
        elif args.action == "fb":
            switch_fb(folder / "fb", False)
        else:
            switch_fb(folder / "fb", True)
            central_switch(after, before)
