#!/usr/bin/env python3
"""Overlay only the FB metric adapter; preserve the deployed publisher and its ledgers."""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_fb_skipped_recheck as fb
from scripts import deploy_hit_drama as release
from scripts.deploy_pipeline_runtime import preserve_tree_owners, verify_source

BASE = "1e2689e6cf0eaf5cc962db288b26284519d9d514"
FILES = ["features/fb_auto_posts/service.py", "features/fb_auto_posts/metric_mysql.py",
         "scripts/fb_auto_post_metric_runner.py", "scripts/fb_auto_metric_broker.py"]
ENV = Path("/etc/fb-auto-post.env")
BROKER = Path("/etc/systemd/system/fb-auto-metric-broker.service")
TIMERS = ["fb-auto-post-metric-" + name + ".timer" for name in ("hourly", "repair")]
METRIC_ENV_KEYS = ("FB_AUTO_METRIC_STORAGE=", "FB_AUTO_METRIC_BROKER_PORT=")
# Reviewed deployed Moboreels overlay (its two unrelated service.py hunks are
# retained). The metric runner and aggregation source still match BASE exactly.
LIVE_SERVICE_BASELINES = {"a2c7a3fafc49e3d6911006d256e084d0719871ec6c02f69d231e7fd9f139c028",
    "b91cff5c5cfc7aa8cb231b17895ad44ef66ed6c19871aed9f1070977f05b7bd5"}


def metric_settings(text):
    return [line for line in text.splitlines() if line.startswith(METRIC_ENV_KEYS)]


def write_metric_settings(settings):
    # Preserve unrelated environment edits made since the release was staged.
    lines = [line for line in ENV.read_text().splitlines() if not line.startswith(METRIC_ENV_KEYS)]
    release.atomic(ENV, ("\n".join(lines + list(settings)) + "\n").encode())
    ENV.chmod(0o600)


def validate_rollback_snapshot(path, reader=None):
    # The old archive predates new MySQL generations. It is never sufficient by
    # itself once a refresh has run, even if the same calendar dates exist.
    from features.fb_auto_posts.metric_mysql import GatedMetricReader, config_from_file
    if reader is None:
        reader = GatedMetricReader(config_from_file("/etc/codex-private-storage-archive.json"))
    rows = reader.query([
        "SELECT HEX(JSON_OBJECT('kind','generation','id',id,'generation_key',generation_key,'row_count',row_count)) FROM ads_ai.fb_auto_metric_generation WHERE status='ready'",
        "SELECT HEX(JSON_OBJECT('kind','pointer','platform',platform,'metric_date',metric_date,'product',product,'generation_id',generation_id)) FROM ads_ai.fb_auto_metric_active_pointer"])
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON"); db.execute("BEGIN")
        existing = {row[0]: (row[1], row[2]) for row in db.execute("SELECT id,generation_key,row_count FROM fb_auto_metric_generation WHERE status='ready'")}
        for row in rows:
            if row['kind'] == 'generation':
                assert existing.get(row['id']) == (row['generation_key'], row['row_count']), "restore_ads_ai_metric_delta_before_rollback"
        expected = {(row['platform'], row['metric_date'], row['product']): row['generation_id'] for row in rows if row['kind'] == 'pointer'}
        actual = {(row[0], row[1], row[2]): row[3] for row in db.execute("SELECT platform,metric_date,product,generation_id FROM fb_auto_metric_active_pointer")}
        assert actual == expected, "restore_current_metric_pointers_before_rollback"
    finally:
        db.close()


def stage(folder):
    assert release.run("findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk") == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    commit = verify_source(SOURCE, "HEAD")
    current = Path("/opt/fb-auto-post/current")
    previous = current.resolve(); candidate = folder / "candidate"
    folder.mkdir(parents=True, exist_ok=False); folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    preserve_tree_owners(previous, candidate)
    manifest = {"commit": commit, "source": str(SOURCE), "status": "staged", "lanes": {
        "fb": {"previous_root": str(previous), "candidate": str(candidate), "files": {}}}}
    for name in FILES:
        live, target = previous / name, candidate / name
        old = subprocess.run(["git", "-C", str(SOURCE), "show", BASE + ":" + name], capture_output=True)
        content = (SOURCE / name).read_bytes()
        if old.returncode == 0:
            actual = live.read_bytes().replace(b"\r\n", b"\n")
            if name == "features/fb_auto_posts/service.py":
                assert actual == old.stdout or fb.digest(live) in LIVE_SERVICE_BASELINES, "service_baseline_changed"
                anchors = [b"    metric_store = FBAutoPostStore(metric_path)\n",
                    b"    metric_store = FBAutoPostStore(metric_path, initialize_storage=not Path(metric_path).is_file())\n"]
                assert sum(actual.count(anchor) for anchor in anchors) == 1, "metric_factory_anchor_changed"
                anchor = next(anchor for anchor in anchors if anchor in actual)
                content = actual.replace(anchor, b"    from .metric_mysql import metric_store_from_env\n    metric_store = metric_store_from_env(env)\n")
            else:
                assert actual == old.stdout, "baseline_changed:" + name
        else:
            assert not live.exists(), "new_file_collision:" + name
        target.parent.mkdir(parents=True, exist_ok=True)
        release.atomic(target, content)
        target.chmod(0o644)
        manifest["lanes"]["fb"]["files"][name] = {"before": fb.digest(live) if live.exists() else "", "after": fb.digest(target)}
    shutil.copy2(ENV, folder / "fb-auto-post.env.before"); (folder / "fb-auto-post.env.before").chmod(0o600)
    fb.save(folder / "manifest.json", manifest)
    print(json.dumps({"staged": True, "commit": commit, "candidate": str(candidate)}))


def apply(folder, rollback=False):
    manifest = json.loads((folder / "manifest.json").read_text())
    environment_before = ENV.read_text()
    if not rollback:
        release.atomic(folder / "fb-auto-post.env.before-apply", environment_before.encode())
        (folder / "fb-auto-post.env.before-apply").chmod(0o600)
        manifest["metric_env_before"] = metric_settings(environment_before)
        fb.save(folder / "manifest.json", manifest)
    if rollback:
        # Do not roll back to a missing/old archive. The current snapshot must
        # first be restored by the archive operator when the source was removed.
        assert Path("/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3").is_file(), "restore_metric_archive_first"
        validate_rollback_snapshot("/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3")
    else:
        migration = json.loads((folder.parent / "migration.json").read_text())
        assert migration.get("verified") is True and migration.get("pointers_verified", 0) > 0
        assert (SOURCE.parent / "venv/bin/python").is_file(), "metric_python_environment_missing"
        assert not release.run("ss", "-Hlnt", "( sport = :18449 )"), "metric_broker_port_in_use"
        broker_root = Path("/opt/fb-auto-metrics"); broker_root.mkdir(exist_ok=True)
        target = broker_root / "current"
        assert not target.exists() and not target.is_symlink(), "metric_release_already_exists"
        target.symlink_to(SOURCE, target_is_directory=True)
        shutil.copy2(SOURCE / "deploy/fb-auto-metric-broker.service", BROKER)
        release.run("systemctl", "daemon-reload")
        release.run("systemctl", "enable", "--now", BROKER.name)
        release.health(18449)
    lane = dict(manifest["lanes"]["fb"])
    if rollback:
        lane = {"previous_root": lane["candidate"], "candidate": lane["previous_root"],
                "files": {name: {"before": row["after"], "after": row["before"]}
                          for name, row in lane["files"].items() if row["before"]}}
    switch_manifest = {"lanes": {"fb": lane}}
    states = {unit: fb.show(unit) for unit in fb.TIMERS}; config = fb.configuration(); audit = {}
    release.run(sys.executable, release.HEALTH, "snapshot", "--lane", "fb", "--output", folder / "schedulers-before.json")
    try:
        fb.fb_enabled(False)
        for unit, state in states.items():
            if state == "active": release.run("systemctl", "stop", unit)
        # Gate and drain accepted owner requests before changing its import.
        if rollback:
            previous = manifest.get("metric_env_before", metric_settings((folder / "fb-auto-post.env.before").read_text()))
            write_metric_settings(previous)
        else:
            write_metric_settings(["FB_AUTO_METRIC_STORAGE=mysql", "FB_AUTO_METRIC_BROKER_PORT=18449"])
        switch_folder = folder / ("rollback-switch" if rollback else "switch"); switch_folder.mkdir(exist_ok=True)
        fb.switch("fb", switch_folder, switch_manifest, audit)
        for mode in ("hourly", "repair"):
            dropin = Path("/etc/systemd/system/fb-auto-post-metric-" + mode + ".service.d/90-ads-ai.conf")
            if rollback:
                dropin.unlink(missing_ok=True)
            else:
                dropin.parent.mkdir(parents=True, exist_ok=True)
                argument = "yesterday" if mode == "hourly" else "repair"
                dropin.write_text("[Service]\nUser=root\nGroup=root\nWorkingDirectory=/opt/fb-auto-metrics/current\n"
                                  "ExecStart=\nExecStart=" + str(SOURCE.parent / "venv/bin/python")
                                  + " /opt/fb-auto-metrics/current/scripts/fb_auto_post_metric_runner.py " + argument + "\n")
        release.run("systemctl", "daemon-reload")
        manifest["status"] = "rolled_back" if rollback else "active"
        fb.save(folder / "manifest.json", manifest)
    except BaseException:
        actual = str(Path("/opt/fb-auto-post/current").resolve())
        # A failed drain leaves the old process/source in place. Restore its
        # matching backend before restoring triggers. If the pointer switched,
        # retain the matching new backend and record the exact recovery state.
        if actual == lane["previous_root"]:
            write_metric_settings(metric_settings(environment_before))
            if not rollback:
                release.run("systemctl", "disable", "--now", BROKER.name)
                target = Path("/opt/fb-auto-metrics/current")
                if target.is_symlink() and target.resolve() == SOURCE:
                    target.unlink()
        fb.save(folder / "failure-state.json", {"owner_source": actual,
            "metric_settings": metric_settings(ENV.read_text()),
            "owner_pointer_changed": actual == lane["candidate"],
            "source_is_known": actual in (lane["candidate"], lane["previous_root"])})
        raise
    finally:
        fb.fb_enabled("fb" in config["enabled_channels"], "fb" in config.get("preparation_only_channels", []))
        for unit, state in states.items():
            if state == "active": release.run("systemctl", "start", unit)
        fb.save(folder / "switch-audit.json", audit)
        release.run(sys.executable, release.HEALTH, "restore", "--baseline", folder / "schedulers-before.json",
                    "--wait-seconds", "25", "--output", folder / "schedulers-after.json")
    if rollback:
        release.run("systemctl", "disable", "--now", BROKER.name)
    print(json.dumps({"status": manifest["status"], "commit": manifest["commit"], "audit": audit}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("mode", choices=("stage", "apply", "rollback"))
    parser.add_argument("--folder", required=True); args = parser.parse_args(); folder = Path(args.folder).resolve()
    assert folder.is_relative_to("/mnt/data-disk")
    if args.mode == "stage": stage(folder)
    else: apply(folder, args.mode == "rollback")
