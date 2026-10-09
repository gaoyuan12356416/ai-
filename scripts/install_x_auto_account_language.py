#!/usr/bin/env python3
"""Deploy account-derived X Auto languages onto a verified runtime composite."""

import argparse
import ast
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request


DATA = Path("/mnt/data-disk/x-post-automation")
CURRENT = Path("/opt/x-post-automation/current")
MAIN_STATIC = Path("/root/drama_material_service/static")
NGINX_STATIC = Path("/usr/share/nginx/html")
AUTO_DB = Path("/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3")
POST_DB = Path("/var/lib/x-post-automation/accounts.sqlite3")
AUTO_UNITS = ["x-auto-post-runner.timer", "x-auto-post-scheduler.timer", "x-auto-post-metric.timer", "x-auto-post-runner.path"]
OTHER_TIMERS = ["x-post-schedule.timer", "x-post-schedule-claim.timer", "x-post-manual.timer", "x-post-daily.timer"]
AUTO_SERVICES = ["x-auto-post-runner.service", "x-auto-post-scheduler.service", "x-auto-post-metric.service"]
SERVICE = "x-auto-post-service.service"
EXPECTED_CODE = {
    "features/x_auto_posts/core.py": "90b9ef7fd273727b988ed518545841f34103763fa2caef1561744353cc175ba0",
    "features/x_auto_posts/validation.py": "6b588c7d38357eb3894abd8fe61becfe8444b04c1a427b073a683be9ae557186",
    "features/x_auto_posts/service.py": "25772c1c7d623846715b1c16e2604430931277bebfdd4b5a0c50f859c5461285",
}
EXPECTED_STATIC = {
    "x-auto-publish-template.html": "19ed0c73f1580d297cdaec91b731bb4a29fb76979373e1ec68461328f796a4f0",
    "x-auto-publish-template.js": "60be72fae50d47db238a0b47eb9260db0379f1cb258ea27b83472fd272f63a56",
    "x-auto-publish-templates.html": "7b83fec5470d1af8e3403e8c52468f4822330c767ac371eb807cc80addf6c0b4",
    "x-auto-publish-templates.js": "b687fb44d0323d4aeac8ab43af580390810fcc696210a8f07037e00fcd000ddb",
}
EXTRA_FILES = [
    "scripts/test_x_auto_post_service.py", "scripts/test_x_auto_post_store.py",
    "scripts/test_x_auto_post_validation.py", "scripts/test_x_auto_publish_ui.py",
    "scripts/test_x_auto_post_static_deploy.py", "scripts/test_x_account_language_routing.py",
    "scripts/install_x_auto_account_language.py", "doc/x-auto-account-language-20261009/README.md",
]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state(unit):
    return subprocess.check_output(["systemctl", "show", unit, "-p", "ActiveState", "--value"], text=True).strip()


def mounted_disk():
    uuid = subprocess.check_output(["findmnt", "-no", "UUID", "/mnt/data-disk"], text=True).strip()
    if uuid != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8" or not os.access(DATA, os.W_OK):
        raise RuntimeError("verified data disk is unavailable")
    if shutil.disk_usage(DATA).free < 512 * 1024 * 1024:
        raise RuntimeError("insufficient data disk space")


def switch(path):
    temporary = CURRENT.with_name("current.auto-account-language-new")
    temporary.symlink_to(path)
    temporary.replace(CURRENT)


def ready():
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:18833/health", timeout=3) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("X Auto service readiness timed out")


def db_facts(path, tables=None):
    with contextlib.closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("database integrity check failed")
        if tables is None:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        result = {}
        for table in tables:
            rows = conn.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall()
            result[table] = {"count": len(rows), "sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()}
        return result


def token_facts():
    return {p.name: {"sha256": sha(p), "mode": oct(p.stat().st_mode & 0o777), "uid": p.stat().st_uid, "gid": p.stat().st_gid}
            for p in Path("/var/lib/x-post-automation/tokens").glob("*.json")}


def baseline(previous):
    if CURRENT.resolve() != previous:
        raise RuntimeError("runtime release changed during deployment")
    for name, expected in EXPECTED_CODE.items():
        if sha(previous / name) != expected:
            raise RuntimeError("X Auto source baseline changed: " + name)
    for root in (MAIN_STATIC, NGINX_STATIC):
        for name, expected in EXPECTED_STATIC.items():
            if sha(root / name) != expected:
                raise RuntimeError("static source baseline changed: " + str(root / name))


def rollback(backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    for label, root in (("main", MAIN_STATIC), ("nginx", NGINX_STATIC)):
        for name in EXPECTED_STATIC:
            shutil.copy2(backup / label / name, root / name)
    switch(Path(manifest["previous_release"]))
    run("systemctl", "restart", SERVICE)
    ready()


def requested_rollback(backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    if CURRENT.resolve() != Path(manifest["release"]):
        raise RuntimeError("current release differs from this rollback point")
    before = {u: state(u) for u in AUTO_UNITS + OTHER_TIMERS}
    active = [u for u in AUTO_UNITS if before[u] == "active"]
    rescue = "x-auto-language-rollback-restore-" + manifest["commit"][:8]
    if active:
        run("systemd-run", "--quiet", "--unit=" + rescue, "--on-active=5m", "/usr/bin/systemctl", "start", *active)
    try:
        if active:
            run("systemctl", "stop", *active)
        deadline = time.monotonic() + 90
        while any(state(u) in {"active", "activating", "deactivating"} for u in AUTO_SERVICES):
            if time.monotonic() >= deadline:
                raise RuntimeError("X Auto requests did not drain; rollback aborted")
            time.sleep(1)
        import fcntl
        with open("/run/x-post-daily/runner.lock", "a+b") as publish_lock, open("/run/x-auto-post/scheduler.lock", "a+b") as scheduler_lock:
            fcntl.flock(publish_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(scheduler_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rollback(backup)
        if any(state(u) != before[u] for u in OTHER_TIMERS):
            raise RuntimeError("unrelated timer state changed")
    finally:
        if active:
            run("systemctl", "start", *active)
            if any(state(u) != "active" for u in active):
                raise RuntimeError("Auto timers did not restore; rescue timer retained")
            run("systemctl", "stop", rescue + ".timer")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit")
    parser.add_argument("--expected-release")
    parser.add_argument("--rollback")
    args = parser.parse_args()
    mounted_disk()
    if args.rollback:
        requested_rollback(Path(args.rollback))
        print(json.dumps({"ok": True, "status": "rolled_back", "backup": args.rollback}))
        return
    if not args.commit or len(args.commit) != 40 or any(c not in "0123456789abcdef" for c in args.commit) or not args.expected_release:
        raise ValueError("full GitHub commit and expected release are required")
    previous = Path(args.expected_release).resolve()
    baseline(previous)
    repo = DATA / "auto-account-language-deploy.git"
    if not repo.exists():
        run("git", "init", "--bare", str(repo), stdout=subprocess.DEVNULL)
    git_env = dict(os.environ, GIT_SSH_COMMAND="ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes")
    run("git", "--git-dir=" + str(repo), "fetch", "--depth=1", "git@github.com:gaoyuan12356416/ai-.git", args.commit, env=git_env)
    fetched = subprocess.check_output(["git", "--git-dir=" + str(repo), "rev-parse", "FETCH_HEAD"], text=True).strip()
    if fetched != args.commit:
        raise RuntimeError("GitHub commit does not match requested release")
    release = DATA / "releases" / (args.commit + "-auto-account-language")
    shutil.copytree(previous, release, symlinks=True)
    files = list(EXPECTED_CODE) + ["static/" + name for name in EXPECTED_STATIC] + EXTRA_FILES
    for name in files:
        target = release / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "--git-dir=" + str(repo), "show", args.commit + ":" + name]))
        if name.endswith(".py"):
            ast.parse(target.read_text(), filename=name)
    run("python3", "-m", "unittest", "discover", "-s", "scripts", "-p", "test_x_auto*.py", "-q", cwd=release)
    run("python3", "-m", "unittest", "scripts.test_x_account_language_routing", "scripts.test_x_post_auto_template_bridge", "-q", cwd=release)
    backup = DATA / "maintenance" / ("20261009-auto-account-language-" + args.commit[:8])
    backup.mkdir(mode=0o700)
    units_before = {u: state(u) for u in AUTO_UNITS + OTHER_TIMERS}
    active_auto = [u for u in AUTO_UNITS if units_before[u] == "active"]
    manifest = {"commit": args.commit, "previous_release": str(previous), "release": str(release), "backup": str(backup), "units_before": units_before}
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    for label, root in (("main", MAIN_STATIC), ("nginx", NGINX_STATIC)):
        (backup / label).mkdir()
        for name in EXPECTED_STATIC:
            shutil.copy2(root / name, backup / label / name)
    rescue = "x-auto-language-restore-" + args.commit[:8]
    if active_auto:
        run("systemd-run", "--quiet", "--unit=" + rescue, "--on-active=5m", "/usr/bin/systemctl", "start", *active_auto)
    stopped_service = switched = False
    try:
        if active_auto:
            run("systemctl", "stop", *active_auto)
        deadline = time.monotonic() + 90
        while any(state(u) in {"active", "activating", "deactivating"} for u in AUTO_SERVICES):
            if time.monotonic() >= deadline:
                raise RuntimeError("X Auto requests did not drain; aborting without interruption")
            time.sleep(1)
        import fcntl
        with open("/run/x-post-daily/runner.lock", "a+b") as publish_lock, open("/run/x-auto-post/scheduler.lock", "a+b") as scheduler_lock:
            fcntl.flock(publish_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(scheduler_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run("systemctl", "stop", SERVICE)
            stopped_service = True
            baseline(previous)
            manifest["auto_before"] = db_facts(AUTO_DB)
            manifest["post_before"] = db_facts(POST_DB, ["x_post_queue", "x_post_publish_log", "x_post_repost_ledger"])
            manifest["tokens_before"] = token_facts()
            for source_path, name in ((AUTO_DB, "auto-before.sqlite3"), (POST_DB, "posts-before.sqlite3")):
                with contextlib.closing(sqlite3.connect("file:" + str(source_path) + "?mode=ro", uri=True)) as source, contextlib.closing(sqlite3.connect(str(backup / name))) as destination:
                    source.backup(destination)
                os.chmod(backup / name, 0o600)
            (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
            switched = True
            for root in (MAIN_STATIC, NGINX_STATIC):
                for name in EXPECTED_STATIC:
                    shutil.copy2(release / "static" / name, root / name)
            switch(release)
            run("systemctl", "start", SERVICE)
            ready()
            if db_facts(AUTO_DB) != manifest["auto_before"] or db_facts(POST_DB, list(manifest["post_before"])) != manifest["post_before"]:
                raise RuntimeError("publisher ledger changed during deployment")
            if token_facts() != manifest["tokens_before"]:
                raise RuntimeError("token hashes or permissions changed during deployment")
            for root in (MAIN_STATIC, NGINX_STATIC):
                for name in EXPECTED_STATIC:
                    if sha(root / name) != sha(release / "static" / name):
                        raise RuntimeError("static readback mismatch")
            if any(state(u) != units_before[u] for u in OTHER_TIMERS):
                raise RuntimeError("unrelated timer state changed")
            manifest["status"] = "deployed"
    except BaseException:
        manifest["status"] = "failed"
        if switched:
            rollback(backup)
        elif stopped_service:
            run("systemctl", "start", SERVICE)
            ready()
        raise
    finally:
        if active_auto:
            run("systemctl", "start", *active_auto)
            if any(state(u) != "active" for u in active_auto):
                raise RuntimeError("Auto timers did not restore; rescue timer retained")
            run("systemctl", "stop", rescue + ".timer")
        manifest["units_after"] = {u: state(u) for u in AUTO_UNITS + OTHER_TIMERS}
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({k: v for k, v in manifest.items() if k not in {"tokens_before", "auto_before", "post_before"}}), flush=True)


if __name__ == "__main__":
    main()
