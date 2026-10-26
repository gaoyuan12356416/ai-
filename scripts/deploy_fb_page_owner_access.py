"""Deploy the additive per-Page owner grant and its private SQL table allowlist."""
from __future__ import annotations

import importlib.util
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

_source = Path(__file__).with_name("deploy_fb_page_list_performance.py")
_spec = importlib.util.spec_from_file_location("_fb_page_owner_delivery", _source)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Cannot load verified deployment flow")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)
release.BASELINE = "cd9e06891c24bfab3a3a1260cd07f6765556d2ec"
release.REMOTE_REF = "refs/heads/codex/fb-page-owner-access-20261026"
release.BACKEND_FILES = {
    "features/fb_video_publish/sources.py",
    "features/fb_video_publish/query_broker.py",
}
release.STATIC_FILES = set()
release.ALLOWED = release.BACKEND_FILES
base, pool = release.base, release.pool_release
_restore_files, _rollback = release.restore_files, release.rollback
_active_work = base.active_work


def verify_independent_effects():
    # This collector queries Meta/MySQL and writes its own effect tables. These
    # reviewed files do not import the changed source/broker or call either API.
    for relative in ("scripts/fb_auto_post_effect_runner.py", "scripts/fb_auto_post_metric_runner.py",
                     "features/fb_auto_posts/effect_collector.py", "features/fb_auto_posts/effects.py"):
        expected = base.sha(release.ROOT / relative)
        base.require(expected and base.sha(base.FB / relative) == expected,
                     "Independent effects source drift; cannot skip its drain: " + relative)


def active_work():
    busy, count = _active_work()
    if "fb-auto-post-effects.service" in busy:
        verify_independent_effects()
        busy = [unit for unit in busy if unit != "fb-auto-post-effects.service"]
    return busy, count


def assert_rollback_clear():
    # The old source cannot authorize newly accepted owner-only work. Never
    # restore a database, clear a queue, or strand pending work to undo this grant.
    with base.database() as conn:
        pending = conn.execute("""SELECT COUNT(*) FROM fb_video_task
            WHERE status NOT IN ('published','failed','cancelled')
               OR unknown_outcome<>0""").fetchone()[0]
        enabled = conn.execute("SELECT COUNT(*) FROM fb_video_pool WHERE status='enabled'").fetchone()[0]
    base.require(not pending and not enabled,
                 "Reconcile pending/unknown tasks and enabled pools before narrowing Page access; keep current code and database")


def guarded_restore(manifest):
    # Repeat after draining: a task could have arrived after rollback preflight.
    assert_rollback_clear()
    return _restore_files(manifest)


def rollback(manifest_path):
    assert_rollback_clear()
    return _rollback(manifest_path)


def apply_release(targets, commit):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = base.BASE / "backups" / ("page-owner-" + stamp + "-" + commit[:8])
    backup.mkdir(parents=True)
    saved = pool.saved_states()
    base.require(saved[base.FB_UNIT]["active"] == "active", "Sidecar must be healthy before patch")
    base.require(saved[base.API_UNIT]["active"] == "active", "Main API must be healthy before patch")
    manifest_path = backup / "manifest.json"
    manifest = {"commit": commit, "baseline": release.BASELINE, "states": saved,
                "files": [], "phase": "prepared", "main_api_restarted": True,
                "reopen_started": False, "database_migrated": False}
    for index, (source, target, mode) in enumerate(targets):
        destination = backup / ("file-" + str(index))
        shutil.copy2(target, destination)
        manifest["files"].append({"target": str(target), "backup": str(destination),
                                  "before": base.sha(target), "after": base.sha(source),
                                  "mode_before": mode})
    pool.json_save(manifest_path, manifest)
    modified = False
    try:
        release.assert_pool_window()
        pool.quiesce(saved)
        release.assert_pool_window()
        base.snapshot_db(backup / "operational.sqlite3")
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["before"], "Target changed during drain")
            base.require(base.sha(Path(item["backup"])) == item["before"], "Backup changed during drain")
        for (source, _, _), item in zip(targets, manifest["files"]):
            base.require(base.sha(source) == item["after"], "Release source changed during drain")
        modified = True
        for source, target, mode in targets:
            base.atomic_bytes(source.read_bytes(), target, mode)
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["after"], "Installed hash mismatch")
        # Both processes cache Python modules. The main API owns the SQL broker;
        # replacing only the sidecar would leave the new owner table rejected.
        # Persist before any restart can accept a new owner-only operation.
        manifest.update(phase="files_installed", reopen_started=True)
        pool.json_save(manifest_path, manifest)
        release.resume(saved, restart_main=True)
        manifest["phase"] = "deployed"
        pool.json_save(manifest_path, manifest)
        print(json.dumps({"deployed": True, "commit": commit, "manifest": str(manifest_path),
                          "files": len(targets), "database_migrated": False,
                          "main_api_restarted": True, "service_states_preserved": True}), flush=True)
    except Exception:
        if manifest["reopen_started"]:
            # Acceptance may have happened even when a health/resume check failed.
            # Keep compatible files and the manifest for recovery, never silently
            # revoke this new grant or roll the SQLite ledger back.
            manifest["phase"] = "reopen_failed_new_code_retained"
            pool.json_save(manifest_path, manifest)
            print("Inspect service recovery; new code and all ledgers retained: " + str(manifest_path), flush=True)
        else:
            if modified:
                _restore_files(manifest)
            release.resume(saved, restart_main=modified)
            manifest["phase"] = "failed_restored"
            pool.json_save(manifest_path, manifest)
        raise


release.apply_release = apply_release
release.restore_files = guarded_restore
release.rollback = rollback
base.active_work = active_work

if __name__ == "__main__":
    release.main()
