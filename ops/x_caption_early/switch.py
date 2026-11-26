"""Switch only the X selection owner and the caption worker to staged Git revisions."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time


X_LINK = Path("/opt/x-tag-publisher/current")
COPY_LINK = Path("/opt/story-copy/current")
X_UNIT = "x-tag-auto.service"
COPY_UNIT = "story-copy-worker.service"
TRIGGERS = ("x-tag-auto-runner.timer", "x-tag-auto-runner.path", "x-tag-auto-scheduler.timer")
X_FILES = ["features/x_auto_posts/" + m + ".py" for m in ("service", "core", "publisher", "selector")]
COPY_FILES = ["features/story_copy/" + m + ".py" for m in ("store", "sources", "generator", "model")]
CAPTIONS = "/mnt/data-disk/story-copy/captions.sqlite3"
TASKS = "/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3"


def call(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def active(unit):
    return call("systemctl", "show", unit, "-p", "ActiveState", "--value") == "active"


def point(link, target):
    temporary = link.with_name(".caption-slot-" + str(os.getpid()))
    temporary.symlink_to(target)
    os.replace(temporary, link)


def stage(base, repo, name, link, files, revision):
    old = link.resolve(strict=True)
    new = base / ("release-" + name)
    if new.exists():
        raise RuntimeError("Release directory already exists: " + str(new))
    shutil.copytree(old, new, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    for relative in files:
        previous_blob = call("git", "-C", repo, "rev-parse", revision + "^:" + relative)
        if call("git", "hash-object", old / relative) != previous_blob:
            raise RuntimeError("Active production file changed: " + relative)
        content = subprocess.check_output(["git", "-C", str(repo), "show", revision + ":" + relative])
        (new / relative).write_bytes(content)
        if call("git", "hash-object", new / relative) != call("git", "-C", repo, "rev-parse", revision + ":" + relative):
            raise RuntimeError("Staging checksum mismatch: " + relative)
    return {"previous": str(old), "release": str(new), "revision": revision}


def switch_captions(link, target):
    # An immediate transaction excludes a new claim between the empty check and
    # stopping the worker. Published results and pending rows are never modified.
    with contextlib.closing(sqlite3.connect(CAPTIONS, timeout=3)) as db:
        while True:
            db.execute("BEGIN IMMEDIATE")
            busy = db.execute("SELECT EXISTS(SELECT 1 FROM batches WHERE finished IS NULL) OR "
                              "EXISTS(SELECT 1 FROM jobs WHERE state='generating')").fetchone()[0]
            if not busy:
                break
            db.rollback()
            time.sleep(1)
        last = db.execute("SELECT coalesce(max(id),0) FROM batches").fetchone()[0]
        call("systemctl", "stop", COPY_UNIT)
        db.rollback()
    point(link, target)
    call("systemctl", "start", COPY_UNIT)
    return last


def verify(unit, target):
    for _ in range(40):
        pid = int(call("systemctl", "show", unit, "-p", "MainPID", "--value") or 0)
        if pid and active(unit) and Path("/proc", str(pid), "cwd").resolve() == target:
            return pid
        time.sleep(.25)
    raise RuntimeError(unit + " did not start in " + str(target))


def main():
    a = argparse.ArgumentParser()
    a.add_argument("mode", choices=["stage", "activate", "rollback"])
    a.add_argument("root", type=Path)
    a.add_argument("--x"); a.add_argument("--copy")
    args = a.parse_args()
    base = args.root.resolve()
    if call("findmnt", "-nro", "UUID", "/mnt/data-disk") != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise RuntimeError("Data disk unavailable")
    manifest = base / "release.json"
    if args.mode == "stage":
        repo = base / "git"
        state = {"x": stage(base, repo, "x", X_LINK, X_FILES, args.x),
                 "copy": stage(base, repo, "copy", COPY_LINK, COPY_FILES, args.copy)}
        manifest.write_text(json.dumps(state, indent=2))
        return
    state = json.loads(manifest.read_text())
    rollback = args.mode == "rollback"
    before, after = ("release", "previous") if rollback else ("previous", "release")
    oldx, oldc = (Path(state[k][before]) for k in ("x","copy"))
    newx, newc = (Path(state[k][after]) for k in ("x","copy"))
    if X_LINK.resolve() != oldx or COPY_LINK.resolve() != oldc:
        raise RuntimeError("Active releases changed; rebase the scoped switch")
    triggers = [u for u in TRIGGERS if active(u)]
    try:
        if triggers:
            call("systemctl", "stop", *triggers)
        while any(call("systemctl","show",u,"-p","ActiveState","--value") in ("active","activating","deactivating")
                  for u in ("x-tag-auto-runner.service", "x-tag-auto-scheduler.service")):
            time.sleep(.5)
        call("systemctl", "stop", X_UNIT)  # HTTP owner finishes active requests in server_close.
        if rollback:
            with contextlib.closing(sqlite3.connect("file:"+TASKS+"?mode=ro", uri=True)) as db:
                if db.execute("SELECT 1 FROM x_auto_run WHERE run_key GLOB 'x-auto:prep:*' AND status<>'canceled' LIMIT 1").fetchone():
                    raise RuntimeError("Wait for the prepared slot to open before rollback")
        if X_LINK.resolve() != oldx or COPY_LINK.resolve() != oldc:
            raise RuntimeError("Concurrent release before activation")
        last = switch_captions(COPY_LINK, newc)
        verify(COPY_UNIT,newc)
        point(X_LINK,newx)
        call("systemctl","start",X_UNIT)
        pid = verify(X_UNIT,newx)
        stamp = time.time()
        record = {"activated": stamp, "last_previous_batch": last, "x_pid":pid,
                  "caption_pid":verify(COPY_UNIT,newc),"triggers":triggers}
        (base/("rollback-result.json" if rollback else "activation.json")).write_text(json.dumps(record,indent=2))
        print(json.dumps(record))
    except BaseException:
        if X_LINK.resolve() == newx:
            call("systemctl","stop",X_UNIT)
            point(X_LINK,oldx)
        if COPY_LINK.resolve() == newc:
            switch_captions(COPY_LINK,oldc)
        raise
    finally:
        for unit in (COPY_UNIT, X_UNIT):
            if not active(unit):
                call("systemctl","start",unit)
        if triggers:
            call("systemctl","start",*triggers)


if __name__ == "__main__":
    main()

