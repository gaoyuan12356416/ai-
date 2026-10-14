#!/usr/bin/env python3
"""Deploy the snapshot fix onto the verified live composite; never publish."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "deployment_helpers", SOURCE / "ops/x-pool-drama-language-20261012/install.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
helpers.RESCUE = "x-material-snapshot-restore-20261014"
BASE = helpers.BASE
CURRENT = helpers.CURRENT
MAIN = helpers.MAIN
# X Auto has Requires=x-post-automation.service: stopping the X Sidecar also
# stops Auto, so restore it explicitly before accepting health or resuming timers.
SERVICES = ["x-post-automation.service", "x-auto-post-service.service", "drama-material-api.service"]
EXPECTED = {
    "features/x_posts/service.py": "c41468438003a3d07875cb01949aec2ddd5577bda28fcaee74794960901c3ac9",
    "features/x_accounts/oauth_service.py": "9fcf9ebe889a37073137cd8ee12e9a0a942a16fa494a3b8a467ed48260faaa49",
    "scripts/x_post_daily_runner.py": "5e7627198ad75770d1c214cf75cb267a47ae28741dde2825994f76ec90388e28",
    "scripts/x_post_schedule_runner.py": "d44bff77ae86f4f47c1d8f4d8ba6f8e9b9d4cba0e8f2011810897ddd44b881fe",
}
MAIN_FILES = ["features/x_posts/service.py"]
TESTS = [
    "scripts.test_x_post_multi_schedule_store", "scripts.test_x_post_schedule_runner",
    "scripts.test_x_post_material_pool", "scripts.test_x_post_daily",
    "scripts.test_x_post_manual_sidecar", "scripts.test_x_post_pool_snapshot",
    "scripts.test_x_post_material_random_relay", "scripts.test_x_post_priority_manual_store",
    "scripts.test_x_post_auto_template_bridge",
]
COLUMN = "material_pool_snapshot_max_id"


def fingerprints(db, *, auto=False):
    result = {}
    with helpers.ro(db) as conn:
        table_scope = "name IN ('x_auto_run','x_auto_task')" if auto else "name LIKE 'x_post_%'"
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND " + table_scope + " ORDER BY name"
        ):
            columns = [r[1] for r in conn.execute('PRAGMA table_info("' + table + '")')
                       if r[1] != COLUMN]
            digest = hashlib.sha256()
            count = 0
            sql = 'SELECT ' + ','.join('"' + c + '"' for c in columns) + ' FROM "' + table + '" ORDER BY rowid'
            for row in conn.execute(sql):
                digest.update(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode())
                count += 1
            result[table] = {"count": count, "sha256": digest.hexdigest()}
    return result


def migrate(stage, db, *, service_user=False):
    command = ["/usr/bin/python3", "-B", "-c",
               "import sys; from features.x_posts.service import ensure_storage; ensure_storage(sys.argv[1])",
               str(db)]
    if service_user:
        command = ["runuser", "-u", "x-post-automation", "--"] + command
    helpers.run(command, cwd=stage, timeout=120)


def drain(timers):
    for unit, old in timers.items():
        if old == "active":
            helpers.run(["systemctl", "stop", unit])
    deadline = time.monotonic() + 900
    while any(helpers.state(unit) in {"active", "activating", "deactivating"}
              for unit in helpers.RUNNERS):
        if time.monotonic() > deadline:
            raise RuntimeError("publisher drain timeout; no code switched")
        time.sleep(3)


def restore(backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    for unit, old in manifest["services"].items():
        if old == "active" and helpers.state(unit) != "active":
            helpers.run(["systemctl", "start", unit], timeout=90)
    return helpers.restore(backup)


def rollback_code(backup, manifest):
    helpers.switch(Path(manifest["previous_release"]))
    for name in MAIN_FILES:
        helpers.atomic_copy(backup / "main" / name, MAIN / name)
    for unit, old in manifest["services"].items():
        if old == "active":
            helpers.run(["systemctl", "restart", unit], timeout=90)
    helpers.ready()


def install(commit, backup):
    assert len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
    mounted = helpers.run(["findmnt", "-no", "UUID", "/mnt/data-disk"]).stdout.strip()
    assert mounted == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8", "data disk unavailable"
    assert shutil.disk_usage(BASE).free > 5 * 1024 ** 3, "insufficient backup space"
    backup = backup.resolve()
    assert backup.is_relative_to(BASE / "maintenance") and not backup.exists()
    backup.mkdir(mode=0o700)
    old_release = CURRENT.resolve(strict=True)
    for name, digest in EXPECTED.items():
        assert helpers.sha(old_release / name) == digest, "runtime drift: " + name
    for name in MAIN_FILES:
        assert helpers.sha(MAIN / name) == EXPECTED[name], "main runtime drift: " + name
    stage = BASE / "releases" / (commit + "-material-snapshot")
    assert not stage.exists()
    helpers.progress(backup, "staging_and_linux_tests")
    shutil.copytree(old_release, stage, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    overlays = list(EXPECTED) + [name.replace(".", "/") + ".py" for name in TESTS]
    for name in overlays:
        helpers.atomic_copy(SOURCE / name, stage / name)
    tests = subprocess.run(["/usr/bin/python3", "-B", "-m", "unittest", *TESTS],
                           cwd=stage, capture_output=True, text=True, timeout=240)
    (backup / "staged-tests.txt").write_text(tests.stdout + tests.stderr)
    assert tests.returncode == 0, "staged Linux tests failed; production untouched"
    helpers.ready()
    manifest = {
        "commit": commit, "previous_release": str(old_release), "release": str(stage),
        "timers": {unit: helpers.state(unit) for unit in helpers.TIMERS},
        "services": {unit: helpers.state(unit) for unit in SERVICES},
    }
    helpers.save(backup / "manifest.json", manifest)
    helpers.run(["systemd-run", "--unit=" + helpers.RESCUE, "--on-active=45m",
                 "/usr/bin/python3", str(Path(__file__).resolve()), "--restore", str(backup)])
    switched = False
    try:
        helpers.progress(backup, "draining_publishers")
        drain(manifest["timers"])
        with open("/run/x-post-daily/runner.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert CURRENT.resolve(strict=True) == old_release
            for name, digest in EXPECTED.items():
                assert helpers.sha(old_release / name) == digest, "runtime changed while staging"
            for port in (8810, 18833):
                active = helpers.run(["ss", "-Htn", "state", "established", "sport = :%d" % port]).stdout.strip()
                assert not active, "active sidecar request; no code switched"
            for unit, old in reversed(list(manifest["services"].items())):
                if old == "active":
                    helpers.run(["systemctl", "stop", unit], timeout=90)
            for label, db in helpers.DBS.items():
                with helpers.ro(db) as original, sqlite3.connect(backup / (label + "-before.sqlite3")) as copy:
                    original.backup(copy)
                (backup / (label + "-before.sqlite3")).chmod(0o600)
            shutil.copytree("/var/lib/x-post-automation/tokens", backup / "tokens")
            (backup / "tokens").chmod(0o700)
            for name in MAIN_FILES:
                saved = backup / "main" / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(MAIN / name, saved)
            before = fingerprints(helpers.DBS["post"])
            auto_before = fingerprints(helpers.DBS["auto"], auto=True)
            tokens = helpers.token_inventory()
            helpers.save(backup / "ledger-before.json", before)
            helpers.save(backup / "auto-ledger-before.json", auto_before)
            helpers.save(backup / "tokens-before.json", tokens)
            # Rehearse the exact additive migration and verify all historic values.
            rehearsal = backup / "migration-rehearsal.sqlite3"
            shutil.copy2(backup / "post-before.sqlite3", rehearsal)
            migrate(stage, rehearsal)
            assert fingerprints(rehearsal) == before, "migration altered historical facts"
            helpers.progress(backup, "migration_verified_switching_code")
            migrate(stage, helpers.DBS["post"], service_user=True)
            helpers.switch(stage)
            switched = True
            for name in MAIN_FILES:
                helpers.atomic_copy(SOURCE / name, MAIN / name)
            for unit, old in manifest["services"].items():
                if old == "active":
                    helpers.run(["systemctl", "start", unit], timeout=90)
            health = helpers.ready()
            after = fingerprints(helpers.DBS["post"])
            assert before == after, "publisher facts changed during maintenance"
            assert auto_before == fingerprints(helpers.DBS["auto"], auto=True), "Auto publication facts changed"
            assert tokens == helpers.token_inventory(), "token files changed during maintenance"
            with helpers.ro(helpers.DBS["post"]) as conn:
                assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                assert conn.execute("SELECT count(*) FROM x_post_schedule_run WHERE " + COLUMN + " IS NOT NULL").fetchone()[0] == 0
            hashes = {name: helpers.sha(stage / name) for name in EXPECTED}
            assert all(hashes[name] == helpers.sha(SOURCE / name) for name in EXPECTED)
            assert all(hashes[name] == helpers.sha(MAIN / name) for name in MAIN_FILES)
            helpers.save(backup / "verification.json", {
                "commit": commit, "release": str(stage), "backup": str(backup),
                "health": health, "hashes": hashes, "ledger_unchanged": True,
                "token_inventory_unchanged": True, "migration_rehearsed": True,
                "auto_ledger_unchanged": True,
                "quick_check": "ok", "posts_created_by_deployment": 0,
            })
            helpers.progress(backup, "verified")
    except Exception as exc:
        helpers.save(backup / "failure.json", {"error_type": type(exc).__name__, "message": str(exc)[:500]})
        if switched:
            rollback_code(backup, manifest)
        raise
    finally:
        restored = restore(backup)
        helpers.progress(backup, "timers_restored", restored=restored["restored"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    if args.restore:
        print(json.dumps(restore(args.restore)))
    elif args.rollback:
        manifest = json.loads((args.rollback / "manifest.json").read_text())
        # Preserve the current operator timer choices during a later code rollback.
        states = {unit: helpers.state(unit) for unit in helpers.TIMERS}
        try:
            drain(states)
            with open("/run/x-post-daily/runner.lock", "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                rollback_code(args.rollback, manifest)
        finally:
            for unit, old in states.items():
                if old == "active":
                    helpers.run(["systemctl", "start", unit])
    else:
        assert args.commit and args.backup
        install(args.commit, args.backup)
