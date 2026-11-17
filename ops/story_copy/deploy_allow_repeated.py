"""Switch only the caption producer; keep publisher processes and receipts intact."""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SRC))
from ops.story_copy import deploy as d
from ops.story_copy.deploy_click_hooks import central_switch
from features.story_copy import store

NAME = "features/story_copy/store.py"


def prepare(folder):
    old = d.CENTRAL.resolve(strict=True)
    expected = d.run("git", "-C", SRC, "show", "HEAD^:" + NAME, stdout=-1).stdout
    if (old / NAME).read_bytes() != expected:
        raise RuntimeError("active caption store differs from reviewed base")
    folder.mkdir(parents=True, exist_ok=False)
    candidate = folder / "central"
    shutil.copytree(old, candidate, symlinks=True, copy_function=os.link,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    after = (SRC / NAME).read_bytes()
    compile(after, NAME, "exec")
    d.atomic(candidate / NAME, after)
    with store.connection(readonly=True) as source:
        with sqlite3.connect(folder / "captions-before.sqlite3") as backup:
            source.backup(backup)
    (folder / "captions-before.sqlite3").chmod(0o600)
    d.save(folder, dict(commit=d.read("git", "-C", SRC, "rev-parse", "HEAD"),
                       before=str(old), candidate=str(candidate),
                       before_hash=d.sha(expected), after_hash=d.sha(after), state="prepared"))


def switch(folder, reverse):
    rec = json.loads((folder / "record.json").read_text())
    old, new = Path(rec["before"]), Path(rec["candidate"])
    if d.sha((old / NAME).read_bytes()) != rec["before_hash"]:
        raise RuntimeError("rollback source changed")
    if d.sha((new / NAME).read_bytes()) != rec["after_hash"]:
        raise RuntimeError("candidate source changed")
    central_switch(new, old) if reverse else central_switch(old, new)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if d.state("story-copy-worker.service") == "active":
            with store.connection(readonly=True) as c:
                index = next(r for r in c.execute("PRAGMA index_list(jobs)") if r["name"] == "jobs_exact")
                if not index["unique"]:
                    break
        time.sleep(1)
    else:
        raise RuntimeError("verify caption service and index migration")
    rec.update(state="rolled_back" if reverse else "active", switched_at=time.time(),
               worker_pid=d.read("systemctl", "show", "story-copy-worker.service", "-p", "MainPID", "--value"))
    d.save(folder, rec)
    print(json.dumps(rec))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply", "rollback"))
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if folder.parent != Path("/mnt/data-disk/story-copy-deploy") or not os.path.ismount("/mnt/data-disk"):
        raise SystemExit("unexpected deployment path")
    prepare(folder) if args.action == "prepare" else switch(folder, args.action == "rollback")
