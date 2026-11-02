"""Scoped GitHub-source deployment; drain accepted requests before cutover."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_x_auto_duration as base

BASE_COMMIT = "b4d81b30fda15d33e21b16f426b6cbeeab9b8e10"
FILES = ["features/x_posts/account_limits.py", "features/x_posts/service.py",
         "features/x_accounts/oauth_service.py", "scripts/x_post_daily_runner.py",
         "static/x-post-material-pool.html"]
TESTS = ["scripts/test_x_pool_schedule_save.py", "scripts/test_x_pool_schedule_save_ui.js",
         "scripts/test_x_accounts.py", "scripts/test_x_post_daily.py",
         "scripts/test_x_post_multi_schedule_store.py", "scripts/test_x_post_schedule_runner.py"]
PAUSABLE = ["x-post-schedule.service", "x-post-manual.service", "x-post-daily.service",
            "x-post-catchup.service"]


def write_manifest(m):
    (Path(m["backup"]) / "manifest.json").write_text(json.dumps(m, indent=2))


def check_baseline(previous):
    for name in FILES:
        if name.endswith("account_limits.py"):
            if (previous / name).exists():
                raise RuntimeError("account limit already deployed")
            continue
        expected = subprocess.check_output(["git", "-C", str(SOURCE), "show", BASE_COMMIT + ":" + name])
        assert base.digest(previous / name) == hashlib.sha256(expected).hexdigest(), "runtime drift: " + name


def stage():
    assert base.run("findmnt", "-no", "UUID", "/mnt/data-disk") == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    assert shutil.disk_usage(base.DATA).free > 1024 ** 3
    assert not base.run("git", "-C", str(SOURCE), "status", "--porcelain")
    commit = base.run("git", "-C", str(SOURCE), "rev-parse", "HEAD")
    previous = base.CURRENT.resolve()
    check_baseline(previous)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-pool-save-" + commit[:8]
    release, backup = base.DATA / "releases" / stamp, base.DATA / "backups" / stamp
    backup.mkdir(mode=0o700)
    shutil.copytree(previous, release, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in FILES + TESTS + ["scripts/deploy_x_pool_schedule_save.py"]:
        base.copy(SOURCE / name, release / name)
    with (backup / "regression.txt").open("w") as output:
        subprocess.check_call(["python3", "-m", "unittest", "scripts.test_x_pool_schedule_save",
                               "scripts.test_x_post_multi_schedule_store", "scripts.test_x_post_schedule_runner",
                               "scripts.test_x_post_daily", "-q"], cwd=release, stdout=output, stderr=output)
    targets = [base.MAIN / name for name in ("features/x_posts/service.py", "features/x_posts/account_limits.py",
                                            "static/x-post-material-pool.html")]
    targets.append(base.NGINX / "x-post-material-pool.html")
    external = {}
    for target in targets:
        external[str(target)] = base.digest(target) if target.exists() else None
        if target.exists():
            base.copy(target, backup / "files" / str(target).lstrip("/"))
    m = dict(commit=commit, previous_release=str(previous), release=str(release), backup=str(backup),
             external=external, status="staged")
    write_manifest(m)
    print(json.dumps(m), flush=True)


def assert_quiet():
    pid = base.run("systemctl", "show", "x-post-automation.service", "-p", "MainPID", "--value")
    if len(list(Path("/proc", pid, "task").iterdir())) != 1:
        return False
    with contextlib.closing(sqlite3.connect("file:" + str(base.POST_DB) + "?mode=ro", uri=True)) as conn:
        return conn.execute("SELECT COUNT(*) FROM x_post_queue WHERE status='publishing'").fetchone()[0] == 0


def restore_code(m):
    for target, old_hash in m["external"].items():
        if old_hash is not None:
            base.copy(Path(m["backup"]) / "files" / target.lstrip("/"), Path(target))
        elif Path(target).exists():
            Path(target).unlink()
    base.switch(Path(m["previous_release"]))


def apply(m, rollback=False, resume_drain=False):
    expected = Path(m["release"] if rollback else m["previous_release"])
    assert base.CURRENT.resolve() == expected, "release changed; rebase before deploying"
    if not rollback:
        check_baseline(expected)
        for target, digest in m["external"].items():
            assert (base.digest(Path(target)) if Path(target).exists() else None) == digest, "external drift"
    active_triggers = m["triggers"] if resume_drain else [u for u in base.TRIGGERS if base.state(u) == "active"]
    auto_active = resume_drain or base.state("x-auto-post-service.service") == "active"
    paused_pids = list(m["paused_pids"]) if resume_drain else []
    m["triggers"] = active_triggers
    rescue = "x-pool-save-rescue-" + m["commit"][:8] + ("-continued" if resume_drain else "")
    # Recovery is also available if the SSH session or deployment process dies.
    for unit in PAUSABLE:
        pid = int(base.run("systemctl", "show", unit, "-p", "MainPID", "--value") or 0)
        if pid and pid not in paused_pids:
            paused_pids.append(pid)
    m["paused_pids"] = paused_pids
    write_manifest(m)
    rescue_script = Path(m["backup"]) / "resume.sh"
    rescue_script.write_text("#!/bin/bash\n" + "\n".join("kill -CONT %d 2>/dev/null || true" % p for p in paused_pids)
                            + "\nsystemctl start " + " ".join((['x-auto-post-service.service'] if auto_active else []) + active_triggers) + "\n")
    base.run("systemd-run", "--quiet", "--unit=" + rescue, "--on-active=140m", "/bin/bash", str(rescue_script))
    if resume_drain:
        base.run("systemctl", "stop", "x-pool-save-rescue-" + m["commit"][:8] + ".timer")
    switched = False
    try:
        if active_triggers:
            base.run("systemctl", "stop", *active_triggers)
        for pid in paused_pids:
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        # Auto's non-daemon HTTP workers finish their accepted task on SIGTERM.
        # The OAuth Sidecar stays up throughout this drain.
        if auto_active:
            base.run("systemctl", "stop", "--no-block", "x-auto-post-service.service")
        deadline, last_note = time.monotonic() + 8000, 0
        while base.state("x-auto-post-service.service") in {"active", "deactivating", "activating"} or not assert_quiet():
            if time.monotonic() >= deadline:
                raise RuntimeError("accepted requests still running; restored publishers without cutover")
            if time.monotonic() - last_note > 30:
                print("Waiting for accepted requests to finish; OAuth service remains running", flush=True)
                last_note = time.monotonic()
            time.sleep(2)
        assert base.CURRENT.resolve() == expected
        # These runners Require the Sidecar and systemd will stop them at its
        # restart. Their accepted requests are now finished and durable; future
        # timer ticks resume the same frozen queues through the existing leases.
        base.run("systemctl", "stop", *PAUSABLE)
        base.run("systemctl", "stop", "x-post-automation.service", "drama-material-api.service")
        before = {"post": base.db_facts(base.POST_DB), "auto": base.db_facts(base.AUTO_DB)}
        tokens = base.token_facts()
        base.snapshot(base.POST_DB, Path(m["backup"]) / ("rollback-posts.sqlite3" if rollback else "posts.sqlite3"))
        base.snapshot(base.AUTO_DB, Path(m["backup"]) / ("rollback-auto.sqlite3" if rollback else "auto.sqlite3"))
        switched = True
        if rollback:
            restore_code(m)
        else:
            for target in m["external"]:
                name = "static/x-post-material-pool.html" if target.startswith(str(base.NGINX)) else str(Path(target).relative_to(base.MAIN))
                base.copy(Path(m["release"]) / name, Path(target))
            base.switch(Path(m["release"]))
        base.run("systemctl", "start", "x-post-automation.service", "drama-material-api.service")
        if auto_active:
            base.run("systemctl", "start", "x-auto-post-service.service")
        base.health()
        assert {"post": base.db_facts(base.POST_DB), "auto": base.db_facts(base.AUTO_DB)} == before, "ledger changed at cutover"
        assert base.token_facts() == tokens, "credentials changed at cutover"
        m["status"] = "rolled_back" if rollback else "deployed"
        m["ledger_unchanged"] = m["tokens_unchanged"] = True
    except BaseException:
        if switched and not rollback:
            base.run("systemctl", "stop", "x-auto-post-service.service", "x-post-automation.service", "drama-material-api.service")
            restore_code(m)
        base.run("systemctl", "start", "x-post-automation.service", "drama-material-api.service")
        raise
    finally:
        for pid in paused_pids:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        if auto_active:
            base.run("systemctl", "start", "x-auto-post-service.service")
        if active_triggers:
            base.run("systemctl", "start", *active_triggers)
        assert all(base.state(u) == "active" for u in active_triggers)
        base.run("systemctl", "stop", rescue + ".timer")
        write_manifest(m)
    print(json.dumps(m), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--resume-drain", action="store_true")
    args = parser.parse_args()
    if args.manifest:
        apply(json.loads(Path(args.manifest).read_text()), args.rollback, args.resume_drain)
    else:
        stage()
