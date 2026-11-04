"""Exact GitHub overlay for FB task isolation, retaining all other live code.

Prepare is read-only to live services. Apply pauses only FB admission, drains
accepted requests, swaps two release symlinks, and restores prior FB timers.
Business ledgers are backed up but never restored by this helper.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from scripts.deploy_pipeline_runtime import preserve_tree_owners, atomic_copy, verify_source

BASE = "dce503c"
DATA = Path("/mnt/data-disk")
FB_DB = DATA / "fb-auto-post-publisher/fb-auto-post.sqlite3"
DISPATCH_DB = DATA / "pipeline-dispatch/dispatch.sqlite3"
LANES = {
    "fb": (Path("/opt/fb-auto-post/current"), "fb-auto-post-service.service", 18835),
    "coordinator": (Path("/opt/pipeline-dispatch/current"), "pipeline-coordinator.service", 18846),
}
NATIVE = {
    "features/fb_auto_posts/core.py", "features/fb_auto_posts/publisher.py",
    "features/fb_auto_posts/hit_material.py", "features/fb_auto_posts/manual_batch.py",
    "features/fb_auto_posts/pool_extension.py", "features/fb_video_publish/core.py",
    "features/fb_video_publish/guards.py", "features/fb_video_publish/pool_scheduler.py",
    "features/fb_video_publish/worker.py", "features/pipeline_dispatch/fb_native.py",
    "features/pipeline_dispatch/fb_isolation.py",
}
CENTRAL = {"features/pipeline_dispatch/core.py", "features/pipeline_dispatch/adapters.py",
           "features/pipeline_dispatch/service.py", "features/pipeline_dispatch/fb_isolation.py"}
TIMERS = ["fb-auto-post-" + name + ".timer" for name in
          ("effects", "metric-hourly", "metric-repair", "plan", "prepare", "reconcile", "runner", "scheduler")]
TIMERS.append("fb-video-publish-prepare.timer")


def run(argv, *, cwd=None, timeout=60):
    result = subprocess.run([str(x) for x in argv], cwd=cwd, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("command_failed:" + str(argv[0]) + ":" + str(result.returncode))
    return result.stdout.decode().strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def show(unit, field="ActiveState"):
    return run(["systemctl", "show", unit, "-p", field, "--value"])


def backup(source, target):
    with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            assert dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    target.chmod(0o600)


def verify_live(manifest):
    for lane, item in manifest["lanes"].items():
        root = LANES[lane][0]
        assert str(root.resolve()) == item["previous_root"], "runtime_release_drift:" + lane
        for relative, record in item["files"].items():
            path = root / relative
            assert (digest(path) if path.exists() else "") == record["before"], "runtime_file_drift:" + relative


def prepare(source, commit, folder):
    source, folder = Path(source).resolve(), Path(folder).resolve()
    commit = verify_source(source, commit)
    assert DATA.is_mount() and folder.is_relative_to(DATA), "data_disk_stage_required"
    assert run(["findmnt", "-n", "-o", "UUID", str(DATA)]) == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    changed = set(run(["git", "diff", "--name-only", BASE, commit, "--", "features"], cwd=source).splitlines())
    assert changed and changed <= NATIVE | CENTRAL, "unreviewed_feature_overlay"
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {"commit": commit, "source": str(source), "baseline": run(["git", "rev-parse", BASE], cwd=source),
                "status": "preparing", "lanes": {}}
    for lane, (root, unit, port) in LANES.items():
        assert root.is_symlink(), "runtime_must_be_release_symlink"
        candidate = folder / "candidates" / lane
        candidate.parent.mkdir(exist_ok=True)
        shutil.copytree(root.resolve(), candidate, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        preserve_tree_owners(root.resolve(), candidate)
        files = changed & (NATIVE | CENTRAL if lane == "coordinator" else NATIVE)
        item = {"previous_root": str(root.resolve()), "candidate": str(candidate), "files": {}}
        manifest["lanes"][lane] = item
        for relative in sorted(files):
            live, target = root / relative, candidate / relative
            assert target.resolve().is_relative_to(candidate.resolve()), "overlay_symlink_escape"
            original = subprocess.run(["git", "show", BASE + ":" + relative], cwd=source, capture_output=True)
            if original.returncode == 0:
                assert live.is_file() and live.read_bytes().replace(b"\r\n", b"\n") == original.stdout, "unreviewed_runtime_base:" + lane + "/" + relative
            else:
                assert not live.exists(), "new_file_collision:" + relative
            before = digest(live) if live.exists() else ""
            atomic_copy(source / relative, target)
            compile(target.read_bytes(), relative, "exec")
            item["files"][relative] = {"before": before, "after": digest(target)}
    verify_live(manifest)
    backup(FB_DB, folder / "fb-rehearsal.sqlite3")
    backup(DISPATCH_DB, folder / "dispatch-before.sqlite3")
    # Constructors are rehearsed on an online copy; no task execution is run.
    rehearsal = "from features.fb_auto_posts.core import FBAutoPostStore; from features.fb_video_publish.core import Store; import sys,sqlite3; p=sys.argv[1]; FBAutoPostStore(p); Store(p); FBAutoPostStore(p); Store(p); c=sqlite3.connect(p); assert c.execute('pragma quick_check').fetchone()[0]=='ok'; print('migration_rehearsal_ok')"
    proof = run([sys.executable, "-c", rehearsal, folder / "fb-rehearsal.sqlite3"], cwd=folder / "candidates/fb")
    (folder / "migration-rehearsal.log").write_text(proof + "\n")
    manifest["status"] = "prepared"
    save(folder / "manifest.json", manifest)
    print(json.dumps({"status": "prepared", "folder": str(folder), "files": {k: len(v["files"]) for k,v in manifest["lanes"].items()}, "migration": proof}), flush=True)


def configuration():
    with contextlib.closing(sqlite3.connect(DISPATCH_DB.as_uri() + "?mode=ro", uri=True)) as conn:
        version, value = conn.execute("SELECT version,config_json FROM pipeline_config WHERE id=1").fetchone()
        return dict(json.loads(value), version=version)


def fb_enabled(enabled, preparation_only=False):
    from features.pipeline_dispatch.core import DispatchStore, utcnow
    store = DispatchStore.__new__(DispatchStore)
    store.path, store.clock, store.capability_exclusions = str(DISPATCH_DB), utcnow, {}
    store.channel_enabled("fb", enabled, preparation_only=preparation_only)


def native_active():
    with contextlib.closing(sqlite3.connect(FB_DB.as_uri() + "?mode=ro", uri=True)) as conn:
        auto = conn.execute("SELECT count(*) FROM fb_auto_task WHERE status IN ('running','preparing')").fetchone()[0]
        video = conn.execute("SELECT count(*) FROM fb_video_task WHERE status='running' OR prepare_state='preparing' OR link_state='writing'").fetchone()[0]
        return auto + video


def drained(pid):
    sockets = [r for r in run(["ss", "-Hntp"]).splitlines() if ("pid=" + pid + ",") in r]
    listeners = [r for r in run(["ss", "-Hlntp"]).splitlines() if ("pid=" + pid + ",") in r]
    threads = len(list(Path("/proc", pid, "task").iterdir()))
    return {"threads": threads, "sockets": len(sockets), "accept_queue": sum(int(r.split()[1]) for r in listeners)}


def switch(lane, folder, manifest, audit):
    root, unit, port = LANES[lane]
    item = manifest["lanes"][lane]
    marker = "fb-isolation-" + lane + "-" + str(os.getpid())
    rule = ["-p", "tcp", "--dport", str(port), "-m", "conntrack", "--ctstate", "NEW", "-m", "comment", "--comment", marker]
    rule += ["-j", "REJECT", "--reject-with", "tcp-reset"] if lane == "fb" else ["-j", "DROP"]
    watchdog = marker + "-gate"
    run(["systemd-run", "--quiet", "--unit=" + watchdog, "--on-active=" + ("600s" if lane == "fb" else "75s"), "--", "/usr/sbin/iptables", "-D", "INPUT", *rule])
    began = time.monotonic()
    gated = False
    stopped = False
    temporary = root.with_name(root.name + ".fb-isolation-next")
    try:
        run(["iptables", "-I", "INPUT", "1", *rule])
        gated = True
        pid = show(unit, "MainPID")
        deadline = time.monotonic() + (300 if lane == "fb" else 30)
        while True:
            proof = drained(pid)
            # The old coordinator has main+collector; old FB only main+HTTP.
            if not proof["sockets"] and not proof["accept_queue"] and proof["threads"] <= (2 if lane == "coordinator" else 1):
                time.sleep(.3)
                if drained(pid) == proof:
                    break
            assert time.monotonic() < deadline, "accepted_requests_not_drained:" + lane
            time.sleep(.5)
        assert native_active() == 0, "native_work_started_during_drain"
        assert str(root.resolve()) == item["previous_root"], "runtime_release_drift"
        for relative, record in item["files"].items():
            assert (digest(root / relative) if (root / relative).exists() else "") == record["before"]
            assert digest(Path(item["candidate"]) / relative) == record["after"]
        folder.chmod(0o755)
        assert not temporary.exists() and not temporary.is_symlink()
        temporary.symlink_to(item["candidate"], target_is_directory=True)
        stopped = True
        run(["systemctl", "stop", unit], timeout=35)
        assert show(unit, "MainPID") == "0"
        temporary.replace(root)
        item["applied"] = True
        save(folder / "manifest.json", manifest)
        run(["systemctl", "start", unit], timeout=20)
        assert show(unit) == "active", "service_start_failed:" + lane
        audit[lane] = {"old_pid": pid, "new_pid": show(unit, "MainPID"), "drain": proof,
                       "gate_seconds": round(time.monotonic()-began, 3)}
    finally:
        if temporary.is_symlink():
            temporary.unlink()
        if stopped and not item.get("applied"):
            run(["systemctl", "start", unit], timeout=20)
        if gated:
            run(["iptables", "-D", "INPUT", *rule])
        subprocess.run(["systemctl", "stop", watchdog + ".timer"], capture_output=True, timeout=10)
    import requests
    with requests.Session() as http:
        http.trust_env = False
        deadline = time.monotonic() + 15
        while True:
            try:
                if http.get("http://127.0.0.1:%s/health" % port, timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            assert time.monotonic() < deadline, "health_failed:" + lane
            time.sleep(.5)


def apply(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["status"] == "prepared"
    verify_live(manifest)
    states = {unit: show(unit) for unit in TIMERS}
    before = configuration()
    main_pid = show("drama-material-api.service", "MainPID")
    audit = {"before": before, "timer_states": states, "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    save(folder / "apply.json", audit)
    completed = False
    try:
        fb_enabled(False)
        for unit, state in states.items():
            if state == "active":
                run(["systemctl", "stop", unit])
        deadline = time.monotonic() + 45
        while native_active():
            assert time.monotonic() < deadline, "active_fb_work_not_drained"
            time.sleep(.5)
        backup(FB_DB, folder / "fb-before-apply.sqlite3")
        for lane in ("fb", "coordinator"):
            switch(lane, folder, manifest, audit)
            save(folder / "apply.json", audit)
        completed = True
    finally:
        changed = any(item.get("applied") for item in manifest["lanes"].values())
        # After schema migration, an old constructor cannot safely be restored.
        # A partial apply leaves FB paused for a reviewed forward repair.
        if completed or not changed:
            fb_enabled("fb" in before["enabled_channels"], "fb" in before.get("preparation_only_channels", []))
            for unit, state in states.items():
                if state == "active":
                    run(["systemctl", "start", unit])
        audit.update(completed=completed, after=configuration(),
                     main_api_pid_unchanged=show("drama-material-api.service", "MainPID") == main_pid)
        save(folder / "apply.json", audit)
    manifest["status"] = "applied"
    save(folder / "manifest.json", manifest)
    print(json.dumps({"status": "applied", "commit": manifest["commit"], "folder": str(folder),
                      "main_api_pid_unchanged": audit["main_api_pid_unchanged"],
                      "gates": {k: audit[k]["gate_seconds"] for k in LANES}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply"])
    parser.add_argument("--folder", required=True)
    parser.add_argument("--source")
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.action == "prepare":
        assert args.source and args.commit
        prepare(args.source, args.commit, args.folder)
    else:
        apply(args.folder)


if __name__ == "__main__":
    main()
