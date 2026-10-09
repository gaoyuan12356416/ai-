#!/usr/bin/env python3
"""Install the approved drama-save repair while preserving runtime composites."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

EXPECTED = {
    "app.py": "f536e71ae018c89edee5d65a6139c986798748b3d0be0e4c86c3b0b73e66417d",
    "features/x_accounts/client.py": "000d5a32e2e13026cf87ee84fb753ce7069e9f1a73498bd1fea0b4e02e63f929",
    "features/x_posts/service.py": "cef06f9999e692226f110d0790b83c05df71ac96169c204caf3cc896192fdb17",
}
AUTO_TIMERS = ["x-auto-post-runner.timer", "x-auto-post-scheduler.timer"]
UNCHANGED_TIMERS = ["x-post-schedule.timer", "x-post-schedule-claim.timer", "x-post-manual.timer"]
MAIN = Path("/root/drama_material_service")
CURRENT = Path("/opt/x-post-automation/current")
DATA = Path("/mnt/data-disk/x-post-automation")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state(unit):
    return subprocess.check_output(["systemctl", "show", unit, "-p", "ActiveState", "--value"], text=True).strip()


def token_hashes():
    return {p.name: {"sha256": sha(p), "mode": oct(p.stat().st_mode & 0o777)}
            for p in Path("/var/lib/x-post-automation/tokens").glob("*.json")}


def switch(path):
    temporary = CURRENT.with_name("current.drama-save-new")
    temporary.symlink_to(path)
    temporary.replace(CURRENT)


def ready():
    for port, path in [(8810, "/health"), (8787, "/api/ui/topbar")]:
        deadline = time.monotonic() + 35
        while True:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%s%s" % (port, path), timeout=3) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("service readiness timed out on port %s" % port)
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    if len(args.commit) != 40 or any(c not in "0123456789abcdef" for c in args.commit):
        raise ValueError("a full Git commit is required")
    uuid = subprocess.check_output(["findmnt", "-no", "UUID", "/mnt/data-disk"], text=True).strip()
    if uuid != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8" or not os.access("/mnt/data-disk", os.W_OK):
        raise RuntimeError("verified data disk unavailable")
    if shutil.disk_usage(DATA).free < 512 * 1024 * 1024:
        raise RuntimeError("insufficient data disk space")
    previous = CURRENT.resolve()
    for name, expected in EXPECTED.items():
        if sha(MAIN / name) != expected:
            raise RuntimeError("main runtime changed before deployment: " + name)
    if sha(previous / "features/x_posts/service.py") != EXPECTED["features/x_posts/service.py"]:
        raise RuntimeError("sidecar service changed before deployment")
    repo = DATA / "drama-save-deploy.git"
    if not repo.exists():
        run("git", "init", "--bare", str(repo), stdout=subprocess.DEVNULL)
    git_env = dict(os.environ, GIT_SSH_COMMAND="ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes")
    run("git", "--git-dir=" + str(repo), "fetch", "--depth=1",
        "git@github.com:gaoyuan12356416/ai-.git", "refs/heads/codex/x-drama-schedule-save-20261009", env=git_env)
    fetched = subprocess.check_output(["git", "--git-dir=" + str(repo), "rev-parse", "FETCH_HEAD"], text=True).strip()
    if fetched != args.commit:
        raise RuntimeError("GitHub branch does not match approved commit")
    release = DATA / "releases" / (args.commit + "-drama-save")
    shutil.copytree(previous, release, symlinks=True)
    files = list(EXPECTED) + ["scripts/test_x_post_multi_schedule_store.py", "scripts/test_x_accounts_app_contract.py",
                            "scripts/audit_x_drama_schedule_save.py", "scripts/install_x_drama_schedule_save.py",
                            "scripts/rollback_x_drama_schedule_save.py"]
    for name in files:
        payload = subprocess.check_output(["git", "--git-dir=" + str(repo), "show", args.commit + ":" + name])
        target = release / name
        target.write_bytes(payload)
        ast.parse(payload.decode("utf-8"), filename=name)
    run("python3", "-m", "unittest", "scripts.test_x_post_multi_schedule_store",
        "scripts.test_x_accounts", "scripts.test_x_post_schedule_runner",
        "scripts.test_x_accounts_app_contract.XAccountsAppContractTest.test_drama_owner_and_slot_conflicts_survive_client_and_api_mapping",
        "scripts.test_x_accounts_app_contract.XAccountsAppContractTest.test_rate_limit_error_survives_main_backend_mapping",
        "-q", cwd=release)
    backup = DATA / "maintenance" / ("20261009-drama-save-" + args.commit[:8])
    backup.mkdir(mode=0o700)
    for name in EXPECTED:
        target = backup / "main" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MAIN / name, target)
    before_states = {u: state(u) for u in AUTO_TIMERS + UNCHANGED_TIMERS}
    active = [u for u in AUTO_TIMERS if before_states[u] == "active"]
    manifest = {"commit": args.commit, "previous_release": str(previous), "release": str(release),
                "backup": str(backup), "timer_states_before": before_states,
                "old_hashes": EXPECTED, "new_hashes": {n: sha(release / n) for n in EXPECTED}}
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    rescue = "x-drama-save-auto-restore-" + args.commit[:8]
    if active:
        run("systemd-run", "--quiet", "--unit=" + rescue, "--on-active=5m",
            "--property=RequiresMountsFor=/mnt/data-disk", "/usr/bin/systemctl", "start", *active)
    switched = False
    try:
        if active:
            run("systemctl", "stop", *active)
        deadline = time.monotonic() + 100
        while any(state(u) in {"active", "activating", "deactivating"}
                  for u in ["x-auto-post-runner.service", "x-auto-post-scheduler.service"]):
            if time.monotonic() >= deadline:
                raise RuntimeError("active Auto request did not drain; aborting without interruption")
            time.sleep(1)
        source = sqlite3.connect("file:/var/lib/x-post-automation/accounts.sqlite3?mode=ro", uri=True)
        source.execute("PRAGMA query_only=ON")
        destination = sqlite3.connect(str(backup / "accounts-before.sqlite3"))
        source.backup(destination)
        destination.close()
        source.close()
        os.chmod(backup / "accounts-before.sqlite3", 0o600)
        manifest["token_hashes_before"] = token_hashes()
        for name, expected in EXPECTED.items():
            if sha(MAIN / name) != expected:
                raise RuntimeError("runtime changed during preflight: " + name)
        switched = True
        for name in EXPECTED:
            shutil.copy2(release / name, MAIN / name)
        switch(release)
        run("systemctl", "restart", "x-post-automation.service", "drama-material-api.service")
        ready()
        for name in EXPECTED:
            if sha(MAIN / name) != manifest["new_hashes"][name]:
                raise RuntimeError("main file readback mismatch: " + name)
        if sha(CURRENT / "features/x_posts/service.py") != manifest["new_hashes"]["features/x_posts/service.py"]:
            raise RuntimeError("sidecar file readback mismatch")
        if any(state(u) != before_states[u] for u in UNCHANGED_TIMERS):
            raise RuntimeError("unrelated publisher timer state changed")
        manifest["status"] = "deployed"
        manifest["token_hashes_after"] = token_hashes()
        if manifest["token_hashes_after"] != manifest["token_hashes_before"]:
            raise RuntimeError("token hash/mode changed during deployment")
        print(json.dumps({k: v for k, v in manifest.items() if not k.startswith("token_hashes")}), flush=True)
    except BaseException:
        if switched:
            for name in EXPECTED:
                shutil.copy2(backup / "main" / name, MAIN / name)
            switch(previous)
            run("systemctl", "restart", "x-post-automation.service", "drama-material-api.service")
            ready()
        raise
    finally:
        if active:
            run("systemctl", "start", *active)
            if any(state(u) != "active" for u in active):
                raise RuntimeError("Auto timers did not restore; rescue timer retained")
            run("systemctl", "stop", rescue + ".timer")
        manifest["timer_states_after"] = {u: state(u) for u in AUTO_TIMERS + UNCHANGED_TIMERS}
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
