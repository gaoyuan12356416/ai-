"""Release the new producer plus the episode renderer. FB and tag consume draft.cta verbatim."""

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
from features.story_copy import store

WORKER = "story-copy-worker.service"
PACKAGE = "features/story_copy/"


def prepare(folder):
    commit = d.read("git", "-C", SRC, "rev-parse", "HEAD")
    previous = d.read("git", "-C", SRC, "rev-parse", "HEAD^")
    folder.mkdir(parents=True, exist_ok=False)
    rec = dict(
        commit=commit, central_before=str(d.CENTRAL.resolve(strict=True)), roots={}
    )
    for central in (True, False):
        origin = (
            Path(rec["central_before"])
            if central
            else d.ROOTS["x"].resolve(strict=True)
        )
        candidate = folder / ("central" if central else "candidate-x")
        shutil.copytree(
            origin,
            candidate,
            symlinks=True,
            copy_function=os.link,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )
        names = (
            ("model.py", "store.py", "generator.py", "worker.py")
            if central
            else ("model.py",)
        )
        files = {}
        for short in names:
            name = PACKAGE + short
            target = candidate / name
            before = target.read_bytes()
            expected = d.run(
                "git", "-C", SRC, "show", previous + ":" + name, stdout=-1
            ).stdout
            if before != expected:
                raise RuntimeError(
                    "preserve concurrent changes in " + str(origin / name)
                )
            after = (SRC / name).read_bytes()
            compile(after, name, "exec")
            d.atomic(target, after)
            files[name] = dict(before=d.sha(before), after=d.sha(after))
        if not central:
            rec["roots"]["x"] = dict(
                before_root=str(origin), candidate=str(candidate), files=files
            )
    d.save(folder, rec)


def central_switch(old, new):
    if d.CENTRAL.resolve() == new:
        return
    while True:
        with store.connection(timeout=10) as c:
            c.execute("BEGIN IMMEDIATE")
            if d.CENTRAL.resolve() != old:
                raise RuntimeError("another caption release is active")
            if not c.execute(
                "SELECT 1 FROM batches WHERE finished IS NULL LIMIT 1"
            ).fetchone():
                # The write lock prevents another claim between the last check and stop.
                d.run("systemctl", "stop", WORKER)
                try:
                    d.symlink(d.CENTRAL, new)
                    d.run("systemctl", "start", WORKER)
                except BaseException:
                    d.symlink(d.CENTRAL, old)
                    d.run("systemctl", "restart", WORKER)
                    raise
                return
        time.sleep(2)


def revert_ready():
    with store.connection() as c:
        for row in c.execute("""SELECT key,payload,draft FROM jobs WHERE state='ready' AND final IS NULL
              AND json_extract(draft,'$.click_hook') IS NOT NULL""").fetchall():
            t = json.loads(row["payload"])
            native = (
                store.TAG_POSTS
                if t["family"] == "ta"
                else store.SOURCES[t["family"]][1]
            )
            with store.connection(native, readonly=True) as src:
                if src.execute(
                    "SELECT 1 FROM story_copy_receipt WHERE task_key=?", (row["key"],)
                ).fetchone():
                    continue
            draft = json.loads(row["draft"])
            draft["cta"] = draft.pop("previous_cta", draft["cta"].removeprefix("👆 "))
            for k in ("click_hook", "hook_key"):
                draft.pop(k, None)
            c.execute(
                "UPDATE jobs SET draft=? WHERE key=? AND state='ready' AND final IS NULL AND draft=?",
                (store.dump(draft), row["key"], row["draft"]),
            )
            c.commit()


def switch(folder, reverse):
    rec = json.loads((folder / "record.json").read_text())
    old, new = Path(rec["central_before"]), folder / "central"
    if d.CENTRAL.resolve() not in (old, new):
        raise RuntimeError("another caption release is active")
    if d.sched(folder, "snapshot", "x", reverse):
        raise RuntimeError("unable to snapshot ordinary X schedulers")
    try:
        d.switch_lane(folder, rec, "x", reverse)
    finally:
        if d.sched(folder, "restore", "x", reverse):
            raise RuntimeError("verify ordinary X scheduler restoration")
    central_switch(new, old) if reverse else central_switch(old, new)
    if reverse:
        revert_ready()
    rec["state"] = "rolled_back" if reverse else "active"
    rec["switched_at"] = time.time()
    d.save(folder, rec)
    print(rec["state"], rec["commit"], flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("prepare", "apply", "rollback"))
    p.add_argument("folder", type=Path)
    a = p.parse_args()
    folder = a.folder.resolve()
    if folder.parent != Path("/mnt/data-disk/story-copy-deploy") or not os.path.ismount(
        "/mnt/data-disk"
    ):
        raise SystemExit("unexpected deployment path")
    if a.action == "prepare":
        prepare(folder)
    else:
        switch(folder, a.action == "rollback")
