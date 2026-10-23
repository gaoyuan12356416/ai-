"""Scoped manual/Excel policy release; additive title migration, immutable historical facts."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import deploy_fb_page_material_pool as pool_release

base = pool_release.base
ROOT = Path(__file__).resolve().parents[1]
BASELINE = "6a4e1f9"
REMOTE_REF = "refs/heads/codex/fb-manual-gates-20261023"
BACKEND_FILES = {"features/fb_video_publish/" + n + ".py" for n in
                 ("core", "templates", "sources", "service", "excel", "worker", "client", "gateway")}
SIDECAR = "features/fb_auto_posts/service.py"
NGINX_FILE = "deploy/nginx-fb-video-publish.conf"
NGINX_TARGET = Path("/etc/nginx/default.d/fb-video-publish.conf")
STATIC_FILES = {"static/" + n for n in ("fb-pages.html", "fb-video-publish.html",
                "fb-video-publish-records.html", "fb-video-publish.js", "fb-excel-publish.js")}
ALLOWED = BACKEND_FILES | STATIC_FILES | {SIDECAR, NGINX_FILE}
RESTORE_UNITS = [base.FB_UNIT] + pool_release.TIMERS + [pool_release.WORKER, pool_release.POOL_UNIT]
BJ = timezone(timedelta(hours=8))


def verified_commit(remote_ref):
    commit = base.run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    base.require(not base.run("git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"),
                 "Dirty release checkout")
    remote = base.run("git", "-C", str(ROOT), "remote", "get-url", "origin")
    base.require(remote in {"git@github.com:gaoyuan12356416/ai-.git",
                            "https://github.com/gaoyuan12356416/ai-.git"},
                 "Unexpected GitHub repository")
    base.require(re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]+", remote_ref) is not None, "Invalid remote ref")
    advertised = base.run("git", "-C", str(ROOT), "ls-remote", "--exit-code", "origin", remote_ref).split()
    base.require(advertised == [commit, remote_ref], "Release is not the exact pushed GitHub ref")
    return commit


def stage_targets():
    changed = base.run("git", "-C", str(ROOT), "diff", "--name-only", BASELINE, "HEAD").splitlines()
    targets = []
    for relative in changed:
        # Documentation, tests, and this delivery script stay in the release checkout.
        if relative not in ALLOWED:
            base.require(not relative.startswith(("features/", "static/", "deploy/")) and relative != "app.py",
                         "Runtime file outside manual policy patch: " + relative)
            continue
        source = ROOT / relative
        base.require(source.is_file(), "Deletion is not supported by this patch")
        if source.suffix == ".py":
            compile(source.read_bytes(), str(source), "exec")
        expected = subprocess.check_output(["git", "-C", str(ROOT), "show", BASELINE + ":" + relative])
        digest = hashlib.sha256(expected).hexdigest()
        destinations = ([NGINX_TARGET] if relative == NGINX_FILE else
                        [base.FB / relative] if relative == SIDECAR else
                        [base.MAIN / relative, base.FB / relative])
        if relative in STATIC_FILES:
            destinations.append(base.STATIC / Path(relative).name)
        for target in destinations:
            base.require(base.sha(target) == digest, "Live baseline drift: " + str(target))
            targets.append((source, target, target.stat().st_mode & 0o777))
    base.require(targets, "No permitted runtime changes")
    return targets


def assert_pool_window(now=None):
    """A restart skips earlier pool slots, so refuse deployment near any live slot."""
    current = (now or datetime.now(timezone.utc)).astimezone(BJ)
    with base.database() as conn:
        schedules = [row[0] for row in conn.execute("SELECT times_json FROM fb_video_pool WHERE status='enabled'")]
    for raw in schedules:
        times = json.loads(raw)
        base.require(isinstance(times, list) and times, "Invalid enabled pool schedule")
        for value in times:
            base.require(isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value),
                         "Invalid enabled pool time")
            hour, minute = map(int, value.split(":"))
            for offset in (-1, 0, 1):
                slot = (current + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
                # Includes the current slot's 60-second normal trigger window.
                base.require(not -60 <= (slot - current).total_seconds() <= 300,
                             "Enabled pool has a nearby time point; deploy outside its trigger window")
    return len(schedules)


def allowed_targets():
    result = {str(root / relative) for root in (base.MAIN, base.FB) for relative in ALLOWED}
    result.add(str(NGINX_TARGET))
    result.update(str(base.STATIC / Path(relative).name) for relative in STATIC_FILES)
    return result


def validate_restore(manifest):
    paths = allowed_targets()
    for item in manifest["files"]:
        target, backup = Path(item["target"]), Path(item["backup"])
        base.require(str(target) in paths, "Rollback path outside patch")
        base.require(base.sha(target) in {item["before"], item["after"]}, "Rollback target drift: " + str(target))
        base.require(base.sha(backup) == item["before"], "Rollback backup drift: " + str(backup))


def restore_files(manifest):
    validate_restore(manifest)
    for item in manifest["files"]:
        base.atomic_bytes(Path(item["backup"]).read_bytes(), Path(item["target"]), item["mode_before"])


def resume(saved, restart_main=False):
    subprocess.run(["nginx", "-t"], check=True)
    subprocess.run(["systemctl", "reload", "nginx"], check=True)
    if restart_main and saved[base.API_UNIT]["active"] == "active":
        subprocess.run(["systemctl", "restart", base.API_UNIT], check=True)
        base.health("http://127.0.0.1:8787/api/ui/topbar")
    pool_release.restore_states(saved, [base.FB_UNIT])
    if saved[base.FB_UNIT]["active"] == "active":
        base.health("http://127.0.0.1:18835/health")
    pool_release.restore_states(saved, pool_release.TIMERS + [pool_release.WORKER, pool_release.POOL_UNIT])
    base.require(base.state(base.API_UNIT) == saved[base.API_UNIT], "Main API service state changed")


def apply_release(targets, commit):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = base.BASE / "backups" / ("manual-gates-" + stamp + "-" + commit[:8])
    backup.mkdir(parents=True)
    saved = pool_release.saved_states()
    base.require(saved[base.FB_UNIT]["active"] == "active", "Sidecar must be healthy before patch")
    manifest_path = backup / "manifest.json"
    main_changed = any(source == ROOT / "features/fb_video_publish/gateway.py" for source, _, _ in targets)
    manifest = {"commit": commit, "baseline": BASELINE, "states": saved, "files": [], "phase": "prepared",
                "main_api_restarted": main_changed}
    for index, (source, target, mode) in enumerate(targets):
        destination = backup / ("file-" + str(index))
        shutil.copy2(target, destination)
        manifest["files"].append({"target": str(target), "backup": str(destination),
                                  "before": base.sha(target), "after": base.sha(source), "mode_before": mode})
    pool_release.json_save(manifest_path, manifest)
    modified = resumed = False
    try:
        assert_pool_window()
        pool_release.quiesce(saved)
        # Recheck after drain; a long drain must not silently skip a scheduled slot.
        assert_pool_window()
        base.snapshot_db(backup / "operational.sqlite3")
        rehearsal = backup / "migration-rehearsal.sqlite3"
        shutil.copy2(backup / "operational.sqlite3", rehearsal)
        manifest["migration_rehearsal"] = pool_release.migrate_preserving(rehearsal)
        pool_release.json_save(manifest_path, manifest)
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
        # Sidecar remains stopped; migrate only after backup, rehearsal, and hash checks.
        manifest["migration_live"] = pool_release.migrate_preserving(base.DB)
        manifest["phase"] = "files_installed"
        pool_release.json_save(manifest_path, manifest)
        resumed = True
        resume(saved, restart_main=main_changed)
        manifest["phase"] = "deployed"
        pool_release.json_save(manifest_path, manifest)
        print(json.dumps({"deployed": True, "commit": commit, "manifest": str(manifest_path),
                          "files": len(targets), "database_migrated": "additive fb_video_page.title", "main_api_restarted": main_changed,
                          "service_states_preserved": True}), flush=True)
    except Exception:
        # Old code cannot execute new relaxed admissions. Never restore it over
        # pending manual tasks; retain new schema, ledger and links in all cases.
        if modified:
            pool_release.quiesce(saved)
            if resumed:
                require_no_pending_manual()
            restore_files(manifest)
        resume(saved, restart_main=modified and main_changed)
        manifest["phase"] = "failed_restored"
        pool_release.json_save(manifest_path, manifest)
        raise


def require_no_pending_manual():
    with base.database() as conn:
        count = conn.execute("""SELECT COUNT(*) FROM fb_video_task WHERE source_kind='manual'
            AND (status NOT IN ('published','failed','cancelled') OR unknown_outcome<>0)""").fetchone()[0]
    base.require(count == 0, "Reconcile pending/unknown manual tasks before rollback; never restore database")


def rollback(manifest_path):
    require_no_pending_manual()
    manifest = json.loads(manifest_path.read_text())
    validate_restore(manifest)
    assert_pool_window()
    saved = pool_release.saved_states()
    modified = False
    try:
        pool_release.quiesce(saved)
        assert_pool_window()
        require_no_pending_manual()
        modified = True
        restore_files(manifest)
    except Exception:
        if not modified:
            resume(saved)
        # A partial restore leaves producers stopped until its files are reconciled.
        raise
    resume(saved, restart_main=manifest.get("main_api_restarted", False))
    manifest["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
    pool_release.json_save(manifest_path, manifest)
    print(json.dumps({"rolled_back": True, "database_and_links_untouched": True,
                      "service_states_preserved": True}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    parser.add_argument("--remote-ref", default=REMOTE_REF)
    args = parser.parse_args()
    base.check_mount()
    if args.rollback:
        rollback(args.rollback)
        return
    commit = verified_commit(args.remote_ref)
    targets = stage_targets()
    enabled = assert_pool_window()
    print(json.dumps({"preflight_ok": True, "commit": commit, "files": len(targets),
                      "enabled_pools": enabled, "in_flight": pool_release.in_flight()}), flush=True)
    if args.apply:
        apply_release(targets, commit)


if __name__ == "__main__":
    main()
