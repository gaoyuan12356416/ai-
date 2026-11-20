"""Switch the independent caption producer without restarting native publishers."""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SRC))
from ops.story_copy import deploy as d
from ops.story_copy.deploy_click_hooks import central_switch

FILES = tuple("features/story_copy/" + name for name in
              ("worker.py", "generator.py", "store.py", "model.py"))
SERVICES = ("story-copy-worker.service", "story-copy-settings.service",
            "x-post-automation.service", "x-tag-post.service",
            "fb-auto-post.service", "pipeline-coordinator.service")


def services():
    return {name: d.read("systemctl", "show", name, "-p", "MainPID", "-p", "ActiveState")
            for name in SERVICES}


def prepare(folder, base="HEAD^"):
    old = d.CENTRAL.resolve(strict=True)
    files = {}
    for name in FILES:
        expected = d.run("git", "-C", SRC, "show", base + ":" + name, stdout=-1).stdout
        if (old / name).read_bytes() != expected:
            raise RuntimeError("active source differs from reviewed base: " + name)
        after = (SRC / name).read_bytes()
        compile(after, name, "exec")
        files[name] = dict(before=d.sha(expected), after=d.sha(after))
    folder.mkdir(parents=True, exist_ok=False)
    candidate = folder / "central"
    shutil.copytree(old, candidate, symlinks=True, copy_function=os.link,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    for name in FILES:
        d.atomic(candidate / name, (SRC / name).read_bytes())
    d.save(folder, dict(commit=d.read("git", "-C", SRC, "rev-parse", "HEAD"),
                       source=str(SRC), base=base, before=str(old), candidate=str(candidate),
                       files=files, services_before=services(), state="prepared"))
    print("prepared", candidate, flush=True)


def switch(folder, reverse):
    rec = json.loads((folder / "record.json").read_text())
    old, new = Path(rec["before"]), Path(rec["candidate"])
    for name, hashes in rec["files"].items():
        for path, field in ((old, "before"), (new, "after")):
            if d.sha((path / name).read_bytes()) != hashes[field]:
                raise RuntimeError("release source changed: " + str(path / name))
    print("waiting for current caption batch before scoped switch", flush=True)
    central_switch(new, old) if reverse else central_switch(old, new)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if d.read("systemctl", "show", "story-copy-worker.service", "-p", "ActiveState", "--value") == "active":
            break
        time.sleep(1)
    else:
        raise RuntimeError("caption worker did not activate")
    rec.update(state="rolled_back" if reverse else "active", switched_at=time.time(),
               services_after=services())
    d.save(folder, rec)
    print(json.dumps(rec), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply", "rollback"))
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if (folder.parent != Path("/mnt/data-disk/story-copy-deploy")
            or not os.path.ismount("/mnt/data-disk")
            or d.read("findmnt", "-n", "-o", "UUID", "--mountpoint", "/mnt/data-disk")
            != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"):
        raise SystemExit("unexpected deployment path or data disk")
    prepare(folder) if args.action == "prepare" else switch(folder, args.action == "rollback")
