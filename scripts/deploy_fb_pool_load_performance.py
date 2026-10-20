"""Main-API-only pool-dialog patch; FB executors and timers remain untouched.

Load an isolated module instance so its baseline and allowlist cannot mutate the
previous rollout's deploy or rollback command. Reuse only its GitHub/file guards.
Only the main API imports gateway.py; old auto publishing uses the sidecar's
direct source/Graph/GPU adapters. Manual work and enabled pools must be absent
because they use the main API's query/identity broker. No database, schema, gate,
app.py, GPU, worker, scheduler, or timer change is permitted.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_source = Path(__file__).with_name("deploy_fb_page_list_performance.py")
_spec = importlib.util.spec_from_file_location("_fb_pool_load_performance_delivery", _source)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Cannot load the verified performance deployment flow")
_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_release)

_release.BASELINE = "0770d15"
_release.REMOTE_REF = "refs/heads/codex/fb-pool-load-performance-20261020"
_release.BACKEND_FILES = {"features/fb_video_publish/gateway.py"}
_release.ALLOWED = _release.BACKEND_FILES | _release.STATIC_FILES
base = _release.base
pool_release = _release.pool_release
LONG_RUNNING = [base.FB_UNIT, pool_release.WORKER, pool_release.POOL_UNIT]
TIMERS = pool_release.TIMERS
ONESHOTS = ["fb-auto-post-" + name + ".service" for name in base.OLD_NAMES] + [
    "fb-video-publish-prepare.service"]
SCOPE = "main_api_gateway_static_only"


def manual_idle():
    """Verify observed broker-dependent work is idle; this is not an admission lock."""
    with base.database() as conn:
        tasks = conn.execute("""SELECT COUNT(*) FROM fb_video_task
            WHERE status NOT IN ('published','failed','cancelled')
               OR prepare_lease<>'' OR link_lease<>'' OR execution_lease<>''""").fetchone()[0]
        pools = conn.execute("SELECT COUNT(*) FROM fb_video_pool WHERE status='enabled'").fetchone()[0]
    base.require(tasks == 0 and pools == 0, "Manual publishing or an enabled pool requires a separate safe window")
    return {"unfinished_or_leased_manual_tasks": tasks, "enabled_pools": pools}


def unit_snapshot(unit):
    return {**base.state(unit), "main_pid": base.run("systemctl", "show", unit, "-p", "MainPID", "--value")}


def protected_snapshot():
    return {unit: unit_snapshot(unit) for unit in LONG_RUNNING + TIMERS + ONESHOTS}


def verify_protected(before):
    after = protected_snapshot()
    for unit in LONG_RUNNING:
        base.require(after[unit] == before[unit], "Publisher process changed during main-only deployment: " + unit)
    for unit in TIMERS:
        base.require(after[unit] == before[unit], "Timer changed during main-only deployment: " + unit)
    for unit in ONESHOTS:
        base.require(after[unit]["enabled"] == before[unit]["enabled"], "Oneshot enabled state changed: " + unit)
    # Timer-triggered services may start/finish normally. Their observed PID/state
    # changes are recorded rather than being mistaken for a deployment restart.
    return {"after": after, "natural_oneshot_transitions":
            {unit: {"before": before[unit], "after": after[unit]}
             for unit in ONESHOTS if before[unit] != after[unit]}}


def restart_main(saved, app_hash):
    base.require(saved["active"] == "active", "Main API must be active before the patch")
    base.require(base.sha(base.MAIN / "app.py") == app_hash, "Main app.py changed during deployment")
    subprocess.run(["systemctl", "restart", base.API_UNIT], check=True)
    base.health("http://127.0.0.1:8787/api/ui/topbar")
    current = unit_snapshot(base.API_UNIT)
    base.require(current["active"] == saved["active"] and current["enabled"] == saved["enabled"],
                 "Main API service state mismatch")


def apply_release(targets, commit):
    manual_idle()
    before = protected_snapshot()
    api_before = unit_snapshot(base.API_UNIT)
    base.require(api_before["active"] == "active", "Main API is not active")
    app_hash = base.sha(base.MAIN / "app.py")
    base.require(bool(app_hash), "Main app.py is unavailable")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = base.BASE / "backups" / ("pool-load-" + stamp + "-" + commit[:8])
    backup.mkdir(parents=True)
    manifest_path = backup / "manifest.json"
    manifest = {"scope": SCOPE, "baseline": _release.BASELINE, "commit": commit,
                "protected_before": before, "api_before": api_before, "main_app_sha256": app_hash,
                "files": [], "phase": "prepared"}
    for index, (source, target, mode) in enumerate(targets):
        saved = backup / ("file-" + str(index))
        shutil.copy2(target, saved)
        manifest["files"].append({"target": str(target), "backup": str(saved),
                                  "before": base.sha(target), "after": base.sha(source), "mode_before": mode})
    pool_release.json_save(manifest_path, manifest)
    base.snapshot_db(backup / "operational.sqlite3")
    modified, reload_started = False, False
    try:
        manual_idle()
        _release.validate_restore(manifest)
        for (source, _, _), item in zip(targets, manifest["files"]):
            base.require(base.sha(source) == item["after"], "Release source changed after preflight")
            base.require(base.sha(Path(item["target"])) == item["before"], "Live source changed after preflight")
        modified = True
        for source, target, mode in targets:
            base.atomic_bytes(source.read_bytes(), target, mode)
        for item in manifest["files"]:
            base.require(base.sha(Path(item["target"])) == item["after"], "Installed file hash mismatch")
        manual_idle()
        reload_started = True
        restart_main(api_before, app_hash)
        manifest["protected_verification"] = verify_protected(before)
        manifest["api_after"] = unit_snapshot(base.API_UNIT)
        manifest["phase"] = "deployed"
        pool_release.json_save(manifest_path, manifest)
        print(json.dumps({"deployed": True, "commit": commit, "manifest": str(manifest_path),
                          "files": len(targets), "main_api_restarted": True,
                          "fb_executors_and_timers_untouched": True,
                          "main_app_unchanged": True, "database_migrated": False}), flush=True)
    except Exception:
        if modified:
            _release.restore_files(manifest)
            # Restore main API code in memory only if it was already reloaded.
            # No FB process or timer is stopped, even on failure.
            if reload_started:
                restart_main(api_before, app_hash)
        manifest["phase"] = "failed_restored"
        manifest["protected_verification"] = verify_protected(before)
        pool_release.json_save(manifest_path, manifest)
        raise


def rollback(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    base.require(manifest.get("scope") == SCOPE, "Use the original deployer for this manifest")
    _release.validate_restore(manifest)
    manual_idle()
    before = protected_snapshot()
    api_before = unit_snapshot(base.API_UNIT)
    base.require(base.sha(base.MAIN / "app.py") == manifest["main_app_sha256"], "Main app.py drift before rollback")
    _release.restore_files(manifest)
    restart_main(api_before, manifest["main_app_sha256"])
    manifest["rollback_protected_verification"] = verify_protected(before)
    manifest["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
    pool_release.json_save(manifest_path, manifest)
    print(json.dumps({"rolled_back": True, "fb_executors_and_timers_untouched": True,
                      "database_and_links_untouched": True}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    parser.add_argument("--remote-ref", default=_release.REMOTE_REF)
    args = parser.parse_args()
    base.check_mount()
    if args.rollback:
        rollback(args.rollback)
        return
    commit = _release.verified_commit(args.remote_ref)
    targets = _release.stage_targets()
    idle = manual_idle()
    print(json.dumps({"preflight_ok": True, "commit": commit, "files": len(targets), **idle,
                      "restart_scope": [base.API_UNIT]}), flush=True)
    if args.apply:
        apply_release(targets, commit)


if __name__ == "__main__":
    main()
