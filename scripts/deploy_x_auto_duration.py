#!/usr/bin/env python3
"""Deploy only the duration-policy overlay on the verified live X composite."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
DATA = Path("/mnt/data-disk/x-post-automation")
CURRENT = Path("/opt/x-post-automation/current")
MAIN = Path("/root/drama_material_service")
NGINX = Path("/usr/share/nginx/html")
POST_DB = Path("/var/lib/x-post-automation/accounts.sqlite3")
AUTO_DB = Path("/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3")
BASELINE = json.loads((SOURCE / "deploy/x-auto-duration-baseline.json").read_text())
MAIN_FILES = ["features/x_posts/service.py", "features/x_posts/selector.py",
              "features/x_posts/random_template.py"]
STATIC = [p for p in BASELINE if p.startswith("static/")]
TRIGGERS = ["x-post-schedule.timer", "x-post-schedule-claim.timer", "x-post-manual.timer",
            "x-post-daily.timer", "x-auto-post-runner.timer", "x-auto-post-scheduler.timer",
            "x-auto-post-metric.timer", "x-auto-post-runner.path"]
WORKERS = [u.replace(".timer", ".service") for u in TRIGGERS if u.endswith(".timer")]
SERVICES = ["x-post-automation.service", "x-auto-post-service.service", "drama-material-api.service"]
TESTS = ["test_x_auto_post_validation.py", "test_x_auto_post_publisher.py",
         "test_x_auto_post_selector.py", "test_x_auto_post_metrics.py",
         "test_x_auto_post_service.py", "test_x_auto_publish_ui.py",
         "test_x_auto_post_static_deploy.py", "test_x_post_auto_template_bridge.py",
         "test_x_random_template.py", "test_x_auto_duration_ui.js"]


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def state(unit):
    return run("systemctl", "show", unit, "-p", "ActiveState", "--value")


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def switch(target):
    temp = CURRENT.with_name("current.duration-next")
    temp.symlink_to(target)
    temp.replace(CURRENT)


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def health():
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            for port in (8810, 18833):
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3) as response:
                    body = response.read()
                    assert response.status == 200
                    if port == 8810:
                        assert body.strip() == b"ok"
                    else:
                        payload = json.loads(body)
                        assert payload.get("ok") is True and payload["gates"]["is_open"] is True
            assert all(state(u) == "active" for u in SERVICES)
            return
        except (OSError, AssertionError):
            time.sleep(1)
    raise RuntimeError("service health verification failed")


def db_facts(path):
    with contextlib.closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                  if r[0].startswith(("x_post_", "x_auto_"))]
        facts = {}
        for table in tables:
            h, count = hashlib.sha256(), 0
            for row in conn.execute('SELECT * FROM "' + table + '" ORDER BY rowid'):
                h.update(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode())
                count += 1
            facts[table] = {"count": count, "sha256": h.hexdigest()}
        return facts


def token_facts():
    return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode & 0o777,
                     p.stat().st_uid, p.stat().st_gid)
            for p in Path("/var/lib/x-post-automation/tokens").glob("*.json")}


def snapshot(source, target):
    with contextlib.closing(sqlite3.connect("file:" + str(source) + "?mode=ro", uri=True)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    target.chmod(0o600)


def baseline():
    for name, expected in BASELINE.items():
        if name.startswith("static/"):
            for root in (MAIN, NGINX):
                target = root / (Path(name).name if root == NGINX else name)
                assert digest(target) == expected, "static drift: " + str(target)
        else:
            assert digest(CURRENT / name) == expected, "runtime drift: " + name
    for name in MAIN_FILES:
        assert digest(MAIN / name) == BASELINE[name], "main drift: " + name


@contextlib.contextmanager
def paused():
    before = {u: state(u) for u in TRIGGERS}
    active = [u for u in TRIGGERS if before[u] == "active"]
    locks = []
    try:
        if active:
            run("systemctl", "stop", *active)
        deadline = time.monotonic() + 60
        while any(state(u) in {"active", "activating", "deactivating"} for u in WORKERS):
            if time.monotonic() >= deadline:
                raise RuntimeError("publisher still working; aborted without interruption")
            time.sleep(2)
        for path in ("/run/x-post-daily/runner.lock", "/run/x-auto-post/scheduler.lock"):
            handle = open(path, "a+b")
            locks.append(handle)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # HTTP publishing may run outside a systemd oneshot. Never interrupt it.
        with contextlib.closing(sqlite3.connect("file:" + str(POST_DB) + "?mode=ro", uri=True)) as conn:
            assert not conn.execute("SELECT 1 FROM x_post_queue WHERE status='publishing' LIMIT 1").fetchone(), "X write in flight"
        yield before
    finally:
        for handle in reversed(locks):
            handle.close()
        if active:
            run("systemctl", "start", *active)
        assert {u: state(u) for u in TRIGGERS} == before, "trigger state restore failed"


def restore(manifest, backup):
    run("systemctl", "stop", *reversed(SERVICES))
    for target in manifest["external_files"]:
        copy(backup / "files" / target.lstrip("/"), Path(target))
    switch(Path(manifest["previous_release"]))
    for unit in SERVICES:
        run("systemctl", "start", unit)
    health()


def deploy():
    baseline()
    health()
    commit = run("git", "-C", str(SOURCE), "rev-parse", "HEAD")
    assert not run("git", "-C", str(SOURCE), "status", "--porcelain"), "source checkout is dirty"
    old = CURRENT.resolve()
    release = DATA / "releases" / (commit + "-auto-duration")
    shutil.copytree(old, release, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = list(BASELINE) + ["scripts/" + name for name in TESTS]
    files += ["scripts/deploy_x_auto_duration.py", "deploy/x-auto-duration-baseline.json",
              "doc/x-auto-duration-20261020/README.md"]
    for name in files:
        copy(SOURCE / name, release / name)
    subprocess.check_call(["python3", "-m", "unittest", "discover", "-s", "scripts", "-p", "test_x_auto*.py", "-q"], cwd=release)
    subprocess.check_call(["python3", "-m", "unittest", "scripts.test_x_post_auto_template_bridge",
                           "scripts.test_x_random_template", "-q"], cwd=release)
    if shutil.which("node"):
        subprocess.check_call(["node", "scripts/test_x_auto_duration_ui.js"], cwd=release)
    backup = DATA / "backups" / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-auto-duration-" + commit[:8])
    backup.mkdir(mode=0o700)
    targets = [str(MAIN / name) for name in MAIN_FILES]
    targets += [str(root / (Path(name).name if root == NGINX else name)) for name in STATIC for root in (MAIN, NGINX)]
    manifest = {"commit": commit, "release": str(release), "previous_release": str(old),
                "backup": str(backup), "external_files": targets, "status": "prepared"}
    def receipt():
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for target in targets:
        copy(Path(target), backup / "files" / target.lstrip("/"))
    receipt()
    with paused() as triggers:
        manifest["triggers"] = triggers
        assert CURRENT.resolve() == old
        baseline()
        stopped = False
        try:
            run("systemctl", "stop", *reversed(SERVICES))
            stopped = True
            facts_before = {"post": db_facts(POST_DB), "auto": db_facts(AUTO_DB)}
            tokens_before = token_facts()
            manifest["database_before"] = facts_before
            snapshot(POST_DB, backup / "posts.sqlite3")
            snapshot(AUTO_DB, backup / "auto.sqlite3")
            with tarfile.open(backup / "tokens.tar", "w") as archive:
                archive.add("/var/lib/x-post-automation/tokens", arcname="tokens")
            (backup / "tokens.tar").chmod(0o600)
            receipt()
            for name in MAIN_FILES:
                copy(release / name, MAIN / name)
            for name in STATIC:
                copy(release / name, MAIN / name)
                copy(release / name, NGINX / Path(name).name)
            switch(release)
            for unit in SERVICES:
                run("systemctl", "start", unit)
            health()
            assert {"post": db_facts(POST_DB), "auto": db_facts(AUTO_DB)} == facts_before, "publisher data changed"
            assert token_facts() == tokens_before, "token fingerprints changed"
            for name in MAIN_FILES + STATIC:
                assert digest(MAIN / name) == digest(release / name)
            for name in STATIC:
                assert digest(NGINX / Path(name).name) == digest(release / name)
            manifest["status"] = "deployed"
            manifest["ledger_unchanged"] = manifest["tokens_unchanged"] = True
            receipt()
        except BaseException:
            if stopped:
                restore(manifest, backup)
            manifest["status"] = "rolled_back"
            receipt()
            raise
    print(json.dumps({k: v for k, v in manifest.items() if k != "database_before"}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    assert run("findmnt", "-no", "UUID", "/mnt/data-disk") == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    assert shutil.disk_usage(DATA).free > 1024**3
    if args.rollback:
        manifest = json.loads((args.rollback / "manifest.json").read_text())
        assert CURRENT.resolve() == Path(manifest["release"]), "current release differs"
        with paused():
            restore(manifest, args.rollback)
        print("Code restored; live database, tokens and publication history preserved.")
    else:
        deploy()


if __name__ == "__main__":
    main()
