#!/usr/bin/env python3
"""GitHub-first, staged GPU/CPU cutover with frozen-ledger verification."""
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
BASELINE = json.loads((SOURCE / "deploy/x-video-20m-baseline.json").read_text())
CPU_ROOT = Path("/mnt/data-disk/x-post-automation")
GPU_ROOT = Path("/data/x-post-media-repair")
CPU_LINK = Path("/opt/x-post-automation/current")
GPU_LINK = GPU_ROOT / "current"
MAIN = Path("/root/drama_material_service")
NGINX = Path("/usr/share/nginx/html")
POST_DB = Path("/var/lib/x-post-automation/accounts.sqlite3")
AUTO_DB = Path("/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3")
GPU_PYTHON = GPU_ROOT / "runtime/python/bin/python"
SERVICES = ["x-post-automation.service", "x-auto-post-service.service", "drama-material-api.service"]
TRIGGERS = ["x-post-schedule.timer", "x-post-schedule-claim.timer", "x-post-manual.timer",
            "x-post-daily.timer", "x-auto-post-runner.timer", "x-auto-post-scheduler.timer",
            "x-auto-post-metric.timer", "x-auto-post-runner.path"]
WORKERS = [u.replace(".timer", ".service") for u in TRIGGERS if u.endswith(".timer")]
WORKERS += ["x-post-catchup.service"]
VERSION_TABLES = ("x_post_queue", "x_post_daily_run", "x_post_catchup_run",
                  "x_post_manual_run", "x_post_schedule_run")


def run(*args):
    return subprocess.check_output(args, universal_newlines=True).strip()


def state(unit):
    return run("systemctl", "show", unit, "-p", "ActiveState", "--value")


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def switch(link, target):
    temp = link.with_name(link.name + ".20m-next")
    assert not temp.exists() and not temp.is_symlink()
    temp.symlink_to(target)
    temp.replace(link)


def read_json(path):
    return json.loads(path.read_text())


def receipt(manifest):
    path = Path(manifest["backup"]) / "manifest.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
    print(json.dumps({"status": manifest["status"], "backup": manifest["backup"]}), flush=True)


def baseline(host):
    groups = {"gpu": GPU_LINK} if host == "gpu" else {"cpu": CPU_LINK, "main": MAIN, "nginx": NGINX}
    for group, root in groups.items():
        for name, expected in BASELINE[group].items():
            target = root / (Path(name).name if group == "nginx" else name)
            actual = digest(target) if target.exists() else None
            assert actual == expected, "baseline drift: " + str(target)


def gpu_health(port=8820):
    with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3) as response:
        body = json.load(response)
    assert body["status"] == "ok"
    assert "standard_20m" in body.get("duration_policies", [])
    assert body["random_template_enabled"] is True
    return body


def cpu_health():
    deadline = time.monotonic() + 45
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
                        assert payload["ok"] and payload["gates"]["is_open"]
            assert all(state(u) == "active" for u in SERVICES)
            return
        except (OSError, AssertionError):
            time.sleep(1)
    raise RuntimeError("CPU health check failed")


def snapshot(source, target):
    with contextlib.closing(sqlite3.connect("file:" + str(source) + "?mode=ro", uri=True)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    target.chmod(0o600)


def db_facts(path, columns=None):
    with contextlib.closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        if columns is None:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                      if r[0].startswith(("x_post_", "x_auto_"))]
            columns = {t: [r[1] for r in conn.execute('PRAGMA table_info("' + t + '")')] for t in tables}
        facts = {}
        for table, names in columns.items():
            h, count = hashlib.sha256(), 0
            for row in conn.execute('SELECT ' + ",".join('"' + n + '"' for n in names)
                                    + ' FROM "' + table + '" ORDER BY rowid'):
                h.update(json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode())
                h.update(b"\n")
                count += 1
            facts[table] = {"count": count, "sha256": h.hexdigest()}
    return {"columns": columns, "facts": facts}


def token_facts():
    return {p.name: [hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode & 0o777,
                     p.stat().st_uid, p.stat().st_gid]
            for p in Path("/var/lib/x-post-automation/tokens").glob("*.json")}


def migrate(post_db, auto_db, release):
    sys.path.insert(0, str(release))
    from features.x_posts.service import ensure_storage as posts
    from features.x_auto_posts.core import ensure_storage as automatic
    for _ in range(2):
        posts(post_db)
        automatic(auto_db)
    for path, tables in ((post_db, VERSION_TABLES), (auto_db, ("x_auto_run",))):
        with contextlib.closing(sqlite3.connect(path)) as conn:
            for table in tables:
                assert not conn.execute("SELECT 1 FROM " + table + " WHERE duration_policy_version<>1 LIMIT 1").fetchone()


def assert_ledger(path, before):
    assert db_facts(path, before["columns"])["facts"] == before["facts"], "historical rows changed: " + str(path)


def stage(host):
    baseline(host)
    commit = run("git", "-C", str(SOURCE), "rev-parse", "HEAD")
    assert not run("git", "-C", str(SOURCE), "status", "--porcelain"), "checkout is dirty"
    root, link = (GPU_ROOT, GPU_LINK) if host == "gpu" else (CPU_ROOT, CPU_LINK)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-video-20m-" + commit[:12]
    backup, release = root / "backups" / stamp, root / "releases" / (commit + "-video-20m")
    backup.mkdir(mode=0o700)
    assert not release.exists(), "release already exists"
    old = link.resolve(strict=True)
    shutil.copytree(old, release, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    names = set(BASELINE[host])
    if host == "cpu":
        names.update(BASELINE["main"])
    names.update(("scripts/deploy_x_video_20m.py", "deploy/x-video-20m-baseline.json",
                  "doc/x-video-20m-20261020/README.md"))
    for name in names:
        copy(SOURCE / name, release / name)
    manifest = {"host": host, "commit": commit, "backup": str(backup), "release": str(release),
                "previous_release": str(old), "status": "staging",
                "files": {name: digest(release / name) for name in sorted(names)}}
    receipt(manifest)
    if host == "gpu":
        subprocess.check_call([str(GPU_PYTHON), "-m", "unittest", "scripts.test_x_post_media_repair",
                               "scripts.test_x_random_template", "scripts.test_x_duration_policy", "-q"], cwd=SOURCE)
        for name in BASELINE["gpu"]:
            subprocess.check_call([str(GPU_PYTHON), "-m", "py_compile", str(release / name)])
    else:
        # Use the final composite, including independently deployed account UI.
        for test in (SOURCE / "scripts").glob("test_x_*"):
            copy(test, release / "scripts" / test.name)
        report = backup / "regression.txt"
        with report.open("w") as output:
            # The catalog inspects GPU source, intentionally absent from the CPU
            # package. Run it in the exact GitHub checkout; test CPU behavior in
            # the composite. Do not discover obsolete tests from older overlays.
            subprocess.check_call(["python3", "-m", "unittest", "scripts.test_x_post_error_catalog", "-q"],
                                  cwd=SOURCE, stdout=output, stderr=output)
            modules = ["scripts." + p.stem for p in sorted((SOURCE / "scripts").glob("test_x_*.py"))
                       if p.stem != "test_x_post_error_catalog"]
            subprocess.check_call(["python3", "-m", "unittest", *modules, "-q"],
                                  cwd=release, stdout=output, stderr=output)
        if shutil.which("node"):
            subprocess.check_call(["node", "scripts/test_x_auto_duration_ui.js"], cwd=release)
        snapshot(POST_DB, backup / "rehearsal-posts.sqlite3")
        snapshot(AUTO_DB, backup / "rehearsal-auto.sqlite3")
        facts = {key: db_facts(backup / ("rehearsal-" + name + ".sqlite3"))
                 for key, name in (("post", "posts"), ("auto", "auto"))}
        migrate(backup / "rehearsal-posts.sqlite3", backup / "rehearsal-auto.sqlite3", release)
        for key, name in (("post", "posts"), ("auto", "auto")):
            assert_ledger(backup / ("rehearsal-" + name + ".sqlite3"), facts[key])
        manifest["migration_rehearsal"] = {"integrity": "ok", "old_columns_unchanged": True,
                                         "all_historical_versions": 1, "idempotent": True}
    manifest["status"] = "prepared"
    receipt(manifest)


def assert_staged(manifest):
    release = Path(manifest["release"])
    for name, expected in manifest["files"].items():
        assert digest(release / name) == expected, "prepared file drift: " + name


@contextlib.contextmanager
def paused(manifest):
    manifest["triggers"] = {u: state(u) for u in TRIGGERS}
    manifest["services"] = {u: state(u) for u in SERVICES}
    assert all(v == "active" for v in manifest["services"].values())
    active = [u for u, s in manifest["triggers"].items() if s == "active"]
    locks = []
    receipt(manifest)
    try:
        if active:
            run("systemctl", "stop", *active)
        deadline = time.monotonic() + 120
        while any(state(u) in {"active", "activating", "deactivating"} for u in WORKERS):
            if time.monotonic() > deadline:
                raise RuntimeError("publish worker is busy; aborted without interruption")
            time.sleep(2)
        for path in ("/run/x-post-daily/runner.lock", "/run/x-auto-post/scheduler.lock"):
            handle = open(path, "r+b")
            locks.append(handle)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with contextlib.closing(sqlite3.connect("file:" + str(POST_DB) + "?mode=ro", uri=True)) as conn:
            assert not conn.execute("SELECT 1 FROM x_post_queue WHERE status='publishing' LIMIT 1").fetchone(), "X write in flight"
        yield
    finally:
        for handle in reversed(locks):
            handle.close()
        if active:
            run("systemctl", "start", *active)
        assert {u: state(u) for u in TRIGGERS} == manifest["triggers"], "trigger restore failed"


def restore_cpu(manifest):
    run("systemctl", "stop", *reversed(SERVICES))
    backup = Path(manifest["backup"])
    for target, existed in manifest.get("external_files", {}).items():
        path = Path(target)
        if existed:
            copy(backup / "files" / target.lstrip("/"), path)
        elif path.exists():
            path.unlink()
    switch(CPU_LINK, Path(manifest["previous_release"]))
    for unit in SERVICES:
        run("systemctl", "start", unit)
    cpu_health()


def deploy_gpu(manifest):
    baseline("gpu")
    assert_staged(manifest)
    assert GPU_LINK.resolve() == Path(manifest["previous_release"])
    assert state("x-post-media-repair.service") == "active"
    assert state("x-post-media-repair-tunnel.service") == "active"
    # The CPU coordinator must already be quiesced. A live request is a hard stop.
    pid = run("systemctl", "show", "x-post-media-repair.service", "-p", "MainPID", "--value")
    for _ in range(2):
        assert len(list(Path("/proc/" + pid + "/task").iterdir())) == 1, "GPU has an active request"
        time.sleep(0.3)
    try:
        run("systemctl", "stop", "x-post-media-repair.service")
        switch(GPU_LINK, Path(manifest["release"]))
        run("systemctl", "start", "x-post-media-repair.service")
        deadline = time.monotonic() + 30
        while True:
            try:
                manifest["health"] = gpu_health()
                break
            except (OSError, AssertionError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        manifest["status"] = "deployed"
    except BaseException:
        run("systemctl", "stop", "x-post-media-repair.service")
        switch(GPU_LINK, Path(manifest["previous_release"]))
        run("systemctl", "start", "x-post-media-repair.service")
        manifest["status"] = "rolled_back"
        raise
    finally:
        receipt(manifest)


def deploy_cpu(manifest):
    baseline("cpu")
    assert_staged(manifest)
    cpu_health()
    backup, release = Path(manifest["backup"]), Path(manifest["release"])
    assert CPU_LINK.resolve() == Path(manifest["previous_release"])
    with paused(manifest):
        stopped = False
        try:
            baseline("cpu")
            # Stop the API ingress before the publisher, after the workers drain.
            run("systemctl", "stop", *reversed(SERVICES))
            stopped = True
            before = {"post": db_facts(POST_DB), "auto": db_facts(AUTO_DB)}
            tokens = token_facts()
            snapshot(POST_DB, backup / "posts.sqlite3")
            snapshot(AUTO_DB, backup / "auto.sqlite3")
            with tarfile.open(backup / "tokens.tar", "w") as archive:
                archive.add("/var/lib/x-post-automation/tokens", arcname="tokens")
            (backup / "tokens.tar").chmod(0o600)
            manifest.update(database_before=before, tokens_before=tokens, external_files={})
            for group, root in (("main", MAIN), ("nginx", NGINX)):
                for name in BASELINE[group]:
                    target = root / (Path(name).name if group == "nginx" else name)
                    manifest["external_files"][str(target)] = target.exists()
                    if target.exists():
                        copy(target, backup / "files" / str(target).lstrip("/"))
            manifest["status"] = "cpu_quiesced_waiting_for_gpu"
            receipt(manifest)
            deadline = time.monotonic() + 180
            while True:
                try:
                    manifest["gpu_health"] = gpu_health(18820)
                    break
                except (OSError, AssertionError):
                    if time.monotonic() > deadline:
                        raise RuntimeError("GPU compatibility wait expired; restoring CPU")
                    time.sleep(2)
            migrate(POST_DB, AUTO_DB, release)
            assert_ledger(POST_DB, before["post"])
            assert_ledger(AUTO_DB, before["auto"])
            for group, root in (("main", MAIN), ("nginx", NGINX)):
                for name in BASELINE[group]:
                    copy(release / name, root / (Path(name).name if group == "nginx" else name))
            switch(CPU_LINK, release)
            for unit in SERVICES:
                run("systemctl", "start", unit)
            cpu_health()
            assert_ledger(POST_DB, before["post"])
            assert_ledger(AUTO_DB, before["auto"])
            assert token_facts() == tokens, "token fingerprints or permissions changed"
            for group, root in (("main", MAIN), ("nginx", NGINX)):
                for name in BASELINE[group]:
                    assert digest(root / (Path(name).name if group == "nginx" else name)) == digest(release / name)
            manifest.update(status="deployed", historical_ledger_unchanged=True, tokens_unchanged=True)
            receipt(manifest)
        except BaseException:
            if stopped:
                restore_cpu(manifest)
            manifest["status"] = "rolled_back"
            receipt(manifest)
            raise
    manifest["triggers_restored"] = True
    receipt(manifest)


def rollback_cpu(manifest):
    assert CPU_LINK.resolve() == Path(manifest["release"]), "current release differs"
    with paused(manifest):
        # Old code must never consume a version-2 queue/run. Keep the additive GPU
        # service in place; it still supports all historical requests.
        with contextlib.closing(sqlite3.connect(POST_DB)) as conn:
            for table in VERSION_TABLES:
                assert not conn.execute(
                    "SELECT 1 FROM " + table + " WHERE duration_policy_version=2 "
                    "AND status IN ('queued','publishing','running','claimed','preparing') LIMIT 1"
                ).fetchone(), "version-2 work remains: " + table
        with contextlib.closing(sqlite3.connect(AUTO_DB)) as conn:
            assert not conn.execute(
                "SELECT 1 FROM x_auto_run WHERE duration_policy_version=2 "
                "AND status NOT IN ('completed','partial_failed','failed','canceled') LIMIT 1"
            ).fetchone(), "version-2 automatic run remains"
        restore_cpu(manifest)
    print("Code restored. Live databases, tokens, links and ledgers were not restored from backup.", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, choices=("cpu", "gpu"))
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--deploy", type=Path)
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    assert sum((args.stage, bool(args.deploy), bool(args.rollback))) == 1
    if args.host == "cpu":
        assert run("findmnt", "-no", "UUID", "/mnt/data-disk") == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
        assert shutil.disk_usage(CPU_ROOT).free > 1024**3
    if args.stage:
        stage(args.host)
    else:
        manifest = read_json((args.deploy or args.rollback) / "manifest.json")
        assert manifest["host"] == args.host
        if args.rollback:
            assert args.host == "cpu", "keep the additive GPU policy for legacy compatibility"
            rollback_cpu(manifest)
        else:
            assert manifest["status"] == "prepared"
            (deploy_gpu if args.host == "gpu" else deploy_cpu)(manifest)


if __name__ == "__main__":
    main()
