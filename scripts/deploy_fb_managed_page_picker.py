"""Static-only managed Page picker release; no services, database or queue writes."""
import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import deploy_fb_page_list_performance as release

release.BASELINE = "3af3f69"
release.REMOTE_REF = "refs/heads/codex/fb-publish-managed-pages-20261023"
release.BACKEND_FILES = set()
release.ALLOWED = release.STATIC_FILES
base = release.base


def processes():
    return {unit: base.run("systemctl", "show", unit, "-p", "MainPID", "--value")
            for unit in (base.API_UNIT, base.FB_UNIT)}


def install(data, item):
    target = Path(item["target"])
    base.atomic_bytes(data, target, item["mode_before"])
    os.chown(target, item["uid"], item["gid"])


def restore(manifest):
    release.validate_restore(manifest)
    for item in manifest["files"]:
        install(Path(item["backup"]).read_bytes(), item)
        base.require(base.sha(Path(item["target"])) == item["before"], "Restore hash mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    base.check_mount()
    if args.rollback:
        manifest = json.loads(args.rollback.read_text())
        before = processes()
        restore(manifest)
        manifest["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        release.pool_release.json_save(args.rollback, manifest)
        print(json.dumps({"rolled_back": True, "processes_unchanged": before == processes()}))
        return
    commit = release.verified_commit(release.REMOTE_REF)
    targets = release.stage_targets()
    base.require(len(targets) == 12, "Expected four static assets in three runtime locations")
    print(json.dumps({"preflight_ok": True, "commit": commit, "files": len(targets)}), flush=True)
    if not args.apply:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = base.BASE / "backups" / ("managed-page-picker-" + stamp + "-" + commit[:8])
    backup.mkdir(parents=True)
    manifest_path = backup / "manifest.json"
    manifest = {"commit": commit, "phase": "prepared", "files": [], "pids_before": processes()}
    for index, (source, target, mode) in enumerate(targets):
        saved = backup / ("file-" + str(index))
        shutil.copy2(target, saved)
        info = target.stat()
        manifest["files"].append(dict(target=str(target), backup=str(saved), before=base.sha(target),
                                      after=base.sha(source), mode_before=mode, uid=info.st_uid, gid=info.st_gid))
    release.pool_release.json_save(manifest_path, manifest)
    for (source, target, _), item in zip(targets, manifest["files"]):
        base.require(base.sha(target) == item["before"] == base.sha(Path(item["backup"])), "Baseline drift")
        base.require(base.sha(source) == item["after"], "Source drift")
    try:
        # Shared JS first, then HTML cache versions. Either order uses a compatible read-only API.
        ordered = sorted(zip(targets, manifest["files"]), key=lambda pair: pair[0][0].suffix != ".js")
        for (source, target, _), item in ordered:
            base.require(base.sha(target) == item["before"], "Target changed during release")
            install(source.read_bytes(), item)
            base.require(base.sha(target) == item["after"], "Installed hash mismatch")
        manifest["phase"] = "completed"
    except Exception:
        restore(manifest)
        manifest["phase"] = "restored_after_failure"
        release.pool_release.json_save(manifest_path, manifest)
        raise
    manifest["pids_after"] = processes()
    release.pool_release.json_save(manifest_path, manifest)
    print(json.dumps({"deployed": True, "manifest": str(manifest_path), "files": len(targets),
                      "processes_unchanged": manifest["pids_before"] == manifest["pids_after"],
                      "database_and_queues_untouched": True}), flush=True)


if __name__ == "__main__":
    main()
