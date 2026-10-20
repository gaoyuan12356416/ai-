#!/usr/bin/env python3
"""GitHub-first Facebook report-only release. Never writes publisher tables."""
import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
BASE = Path("/mnt/data-disk/social-analytics")
MAIN = Path("/root/drama_material_service")
WEB = Path("/usr/share/nginx/html")
DAILY_LINK = Path("/opt/youtube-publisher-daily-report/current")
DAILY_STATE = Path("/mnt/data-disk/youtube-publisher-daily-report")
FB_DB = Path("/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3")
USER_DB = MAIN / "data/drama_material_jobs.sqlite3"
X_DB = Path("/var/lib/x-post-automation/accounts.sqlite3")
ATTR_DB = BASE / "attribution.sqlite3"
TIMER = "social-facebook-attribution.timer"
COLLECTOR = "social-facebook-attribution.service"
DAILY_TIMER = "youtube-publisher-daily-report.timer"
BASE_COMMIT = "9d5bce5"
STATIC = ["social-analytics.html", "social-analytics.js", "social-analytics.css"]
CONFIGS = {
    "deploy/social-facebook-attribution.service": "/etc/systemd/system/" + COLLECTOR,
    "deploy/social-facebook-attribution.timer": "/etc/systemd/system/" + TIMER,
    "deploy/social-facebook-daily-access.conf":
        "/etc/systemd/system/youtube-publisher-daily-report.service.d/80-social-facebook.conf",
}


def command(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError("command_failed:" + args[0] + ":" + str(result.returncode))
    return result.stdout.strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def controlled_tree(path):
    """Freeze the complete release tree; runtime Python caches are not code."""
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("release_tree_invalid:" + str(root))
    entries = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if name not in (".git", "__pycache__") and not name.endswith(".pyc"))
        for name in directories + sorted(files):
            if name in (".git", "__pycache__") or name.endswith(".pyc"):
                continue
            item = Path(directory) / name
            relative = item.relative_to(root).as_posix()
            mode = item.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise RuntimeError("release_tree_symlink:" + str(item))
            if stat.S_ISDIR(mode):
                entries[relative] = {"type": "directory"}
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                with item.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                entries[relative] = {"type": "file", "sha256": digest.hexdigest()}
            else:
                raise RuntimeError("release_tree_special_file:" + str(item))
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "entries": entries}


def verify_daily_trees(manifest):
    frozen = manifest.get("daily_trees")
    if not isinstance(frozen, dict) or set(frozen) != {"previous_daily", "release"}:
        raise RuntimeError("release_tree_manifest_required")
    for name in ("previous_daily", "release"):
        if controlled_tree(manifest[name]) != frozen[name]:
            raise RuntimeError("release_tree_changed:" + name)


def active(unit):
    return command("systemctl", "show", unit, "-p", "ActiveState", "--value")


def atomic_copy(src, dst, mode=0o644):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp = dst.with_name(dst.name + ".facebook-next")
    shutil.copyfile(src, temp)
    temp.chmod(mode)
    temp.replace(dst)


def switch(link, target):
    temp = Path(link).with_name(Path(link).name + ".facebook-next")
    if temp.is_symlink():
        temp.unlink()
    temp.symlink_to(target)
    temp.replace(link)


def save(manifest):
    path = Path(manifest["backup"]) / "deployment.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)
    print(json.dumps({"status": manifest["status"], "backup": manifest["backup"]}), flush=True)


def snapshot(source, target):
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as c:
        with closing(sqlite3.connect(target)) as out:
            c.backup(out)
    Path(target).chmod(0o600)


def backup_file(backup, target):
    target = Path(target)
    if not target.anchor or ".." in target.parts:
        raise RuntimeError("absolute_backup_target_required")
    return Path(backup) / "files" / target.relative_to(target.anchor)


@contextmanager
def lock(path):
    import fcntl
    with Path(path).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def changed_files():
    analytics = [str(p.relative_to(SOURCE)) for p in sorted((SOURCE / "features/social_analytics").glob("*.py"))]
    daily = [str(p.relative_to(SOURCE)) for p in sorted((SOURCE / "features/social_daily_report").glob("*.py"))]
    return analytics, daily + analytics + [
        "scripts/social_publisher_daily_report.py", "scripts/social_facebook_attribution.py"]


def baseline_bytes(relative):
    result = subprocess.run(["git", "-C", str(SOURCE), "show", BASE_COMMIT + ":" + relative],
                            capture_output=True, timeout=20)
    return None if result.returncode else result.stdout.replace(b"\r\n", b"\n")


def baseline_check(path, relative):
    previous = baseline_bytes(relative)
    if previous is None:
        if Path(path).exists():
            raise RuntimeError("unexpected_existing_file:" + str(path))
    elif not Path(path).is_file() or hashlib.sha256(previous).hexdigest() != sha(path):
        raise RuntimeError("live_baseline_drift:" + str(path))


def preflight(sha_value):
    if len(sha_value) != 40 or any(x not in "0123456789abcdef" for x in sha_value):
        raise RuntimeError("full_sha_required")
    if command("findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk") != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise RuntimeError("data_disk_missing")
    BASE.mkdir(mode=0o755, exist_ok=True)
    if shutil.disk_usage(BASE).free < 2 * 1024 ** 3:
        raise RuntimeError("insufficient_disk")
    if command("git", "-C", str(SOURCE), "rev-parse", "HEAD") != sha_value:
        raise RuntimeError("source_sha_mismatch")
    if command("git", "-C", str(SOURCE), "status", "--porcelain"):
        raise RuntimeError("source_checkout_dirty")
    branches = command("git", "-C", str(SOURCE), "branch", "-r", "--contains", sha_value)
    if not branches.strip():
        raise RuntimeError("commit_not_in_fetched_remote")
    analytics, daily = changed_files()
    for relative in analytics:
        baseline_check(MAIN / relative, relative)
    for relative in daily:
        baseline_check(DAILY_LINK / relative, relative)
    for relative in STATIC:
        for root in (MAIN / "static", WEB):
            baseline_check(root / relative, "static/" + relative)
    for target in CONFIGS.values():
        if Path(target).exists():
            raise RuntimeError("collector_config_already_installed")
    return analytics, daily


def stage(sha_value):
    analytics, daily = preflight(sha_value)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-fb-" + sha_value[:12]
    backup = BASE / "backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    release = BASE / "releases" / sha_value / "daily"
    if release.exists():
        raise RuntimeError("release_exists")
    release.parent.mkdir(parents=True, mode=0o755)
    previous_daily = DAILY_LINK.resolve()
    previous_tree = controlled_tree(previous_daily)
    shutil.copytree(previous_daily, release, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    release.chmod(0o755)
    for relative in daily:
        atomic_copy(SOURCE / relative, release / relative)
    command("/usr/bin/python3", "-c",
            "import features.social_analytics.routes;import features.social_analytics.facebook;"
            "import scripts.social_facebook_attribution;import scripts.social_publisher_daily_report", cwd=release)
    daily_trees = {"previous_daily": previous_tree, "release": controlled_tree(release)}
    if DAILY_LINK.resolve() != previous_daily or controlled_tree(previous_daily) != previous_tree:
        raise RuntimeError("previous_release_changed_during_stage")
    targets = {str(MAIN / p): p for p in analytics}
    targets.update({str(root / p): "static/" + p for root in (MAIN / "static", WEB) for p in STATIC})
    targets.update({v: k for k, v in CONFIGS.items()})
    files = {}
    for path, relative in targets.items():
        target = Path(path)
        old = sha(target) if target.exists() else None
        if old is not None:
            dest = backup_file(backup, path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
        files[path] = {"relative": relative, "before": old, "after": sha(SOURCE / relative)}
    snapshot(FB_DB, backup / "fb-before.sqlite3")
    if ATTR_DB.exists():
        snapshot(ATTR_DB, backup / "attribution-before.sqlite3")
    snapshot(DAILY_STATE / "delivery.sqlite3", backup / "delivery-before.sqlite3")
    manifest = dict(
        status="staged", commit=sha_value, source=str(SOURCE), backup=str(backup),
        release=str(release), previous_daily=str(previous_daily), files=files,
        daily_trees=daily_trees,
        daily_files={p: sha(release / p) for p in daily},
        daily_timer_before=active(DAILY_TIMER),
        untouched_pids={u: command("systemctl", "show", u, "-p", "MainPID", "--value") for u in (
            "fb-auto-post-service.service", "fb-video-publish-worker.service",
            "x-post-automation.service", "x-auto-post-service.service")},
    )
    save(manifest)
    # Full freeze is rehearsed only against private backup databases here.
    snapshot(USER_DB, backup / "users-before.sqlite3")
    rehearsal = command("/usr/bin/python3", str(release / "scripts/social_facebook_attribution.py"),
                        "--fb-db", str(backup / "fb-before.sqlite3"), "--users-db", str(backup / "users-before.sqlite3"),
                        "--ledger", str(backup / "rehearsal.sqlite3"), "--mode", "full", cwd=release)
    manifest["rehearsal"] = json.loads(rehearsal.splitlines()[-1])
    manifest["status"] = "prepared"
    save(manifest)


def load(backup):
    root = Path(backup).resolve()
    if root.parent != (BASE / "backups").resolve():
        raise RuntimeError("invalid_backup")
    manifest = json.loads((root / "deployment.json").read_text())
    if manifest["backup"] != str(root):
        raise RuntimeError("backup_identity_mismatch")
    return manifest


def verify_files(manifest, rollback=False):
    for path, values in manifest["files"].items():
        current = sha(path) if Path(path).exists() else None
        allowed = {values["before"], values["after"]} if rollback else {values["before"]}
        if current not in allowed:
            raise RuntimeError("destination_changed:" + path)
        if rollback and values["before"] is not None:
            saved = backup_file(manifest["backup"], path)
            if not saved.is_file() or sha(saved) != values["before"]:
                raise RuntimeError("backup_changed:" + path)
        if not rollback and sha(Path(manifest["source"]) / values["relative"]) != values["after"]:
            raise RuntimeError("source_changed")
    # Both apply and rollback call this again while holding the delivery lock.
    # Include unchanged files, additions and deletions in both release directories.
    verify_daily_trees(manifest)


def health():
    for _ in range(20):
        try:
            if active("drama-material-api.service") != "active":
                raise RuntimeError("api_not_active")
            for prefix in ("social", "youtube"):
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8787/api/" + prefix + "-analytics/options", timeout=3):
                        raise RuntimeError("unauthenticated_report_allowed")
                except urllib.error.HTTPError as error:
                    try:
                        if error.code not in (401, 403):
                            raise
                    finally:
                        error.close()
            return
        except (OSError, RuntimeError):
            time.sleep(1)
    raise RuntimeError("health_verification_failed")


def preview(manifest):
    destination = Path(manifest["backup"]) / "preview-state"
    destination.mkdir(mode=0o700, exist_ok=False)
    output = command(
        "systemd-run", "--wait", "--pipe", "--collect", "--unit=social-facebook-verify-" + manifest["commit"][:12],
        "--property=User=root", "--property=ProtectSystem=strict", "--property=ProtectHome=read-only",
        "--property=PrivateTmp=true", "--property=WorkingDirectory=" + str(DAILY_LINK),
        "--property=ReadWritePaths=" + " ".join(map(str, [destination, USER_DB.parent, X_DB.parent, FB_DB.parent, BASE])),
        "--property=ReadOnlyPaths=" + " ".join(map(str, [USER_DB, X_DB, FB_DB, ATTR_DB])),
        "/usr/bin/python3", str(DAILY_LINK / "scripts/social_publisher_daily_report.py"),
        "--preview", "--date", "2026-10-19", "--state-dir", str(destination))
    if '"status": "ready"' not in output:
        raise RuntimeError("preview_not_ready")
    report = json.loads((destination / "previews/2026-10-19/report.json").read_text())
    verify_preview_report(report)
    return {"status": "ready", "report_sha256": sha(destination / "previews/2026-10-19/report.json"),
            "totals": report["totals"]}


def verify_preview_report(report):
    if report.get("report_date") != "2026-10-19":
        raise RuntimeError("preview_wrong_date")
    shared = report["shared_report"]
    quality = shared["quality"]["platforms"]
    if set(quality) != {"youtube", "x", "facebook"}:
        raise RuntimeError("preview_missing_platform")
    sources = quality["facebook"]["sources"]
    if set(sources) != {"2049", "2310"}:
        raise RuntimeError("preview_missing_source")
    for source in sources.values():
        collector = source.get("collector", {})
        if collector.get("freshness") != "fresh" or collector.get("state") != "complete":
            raise RuntimeError("preview_collector_incomplete")
        if source.get("error"):
            raise RuntimeError("preview_source_failed")
    expected = {"youtube": 91037, "x": 78042, "facebook": 1181542}
    if {p: q["source_totals"]["revenue_cents"] for p, q in quality.items()} != expected:
        raise RuntimeError("preview_snapshot_source_changed")
    if shared["platform_totals"]["facebook"]["revenue_cents"] != 593983:
        raise RuntimeError("preview_facebook_attribution_changed")
    if report["totals"]["revenue_cents"] != 763062:
        raise RuntimeError("preview_totals_changed")


def apply(backup):
    manifest = load(backup)
    if manifest["status"] != "prepared":
        raise RuntimeError("prepared_manifest_required")
    verify_files(manifest)
    if active("youtube-publisher-daily-report.service") not in ("inactive", "failed"):
        raise RuntimeError("daily_in_flight")
    with lock(DAILY_STATE / "run.lock"):
        verify_files(manifest)
        if DAILY_LINK.resolve() != Path(manifest["previous_daily"]):
            raise RuntimeError("daily_release_drift")
        command("systemctl", "stop", DAILY_TIMER)
        manifest["status"] = "applying"
        save(manifest)
        # Freeze independently; neither publisher nor original SQLite is edited.
        manifest["initial_collection"] = json.loads(command(
            "/usr/bin/python3", str(Path(manifest["release"]) / "scripts/social_facebook_attribution.py"),
            "--mode", "full", cwd=manifest["release"]).splitlines()[-1])
        for path, values in manifest["files"].items():
            atomic_copy(Path(manifest["source"]) / values["relative"], path)
        switch(DAILY_LINK, manifest["release"])
        command("systemctl", "daemon-reload")
        command("systemctl", "start", COLLECTOR)
        command("systemctl", "restart", "drama-material-api.service")
        health()
        manifest["preview"] = preview(manifest)
        for unit, pid in manifest["untouched_pids"].items():
            if command("systemctl", "show", unit, "-p", "MainPID", "--value") != pid:
                raise RuntimeError("publisher_pid_changed:" + unit)
        for path, values in manifest["files"].items():
            if sha(path) != values["after"]:
                raise RuntimeError("deployed_hash_mismatch")
        manifest["status"] = "verified"
        save(manifest)
        command("systemctl", "enable", "--now", TIMER)
        if manifest["daily_timer_before"] == "active":
            command("systemctl", "start", DAILY_TIMER)
        manifest["status"] = "deployed"
        save(manifest)


def rollback(backup):
    manifest = load(backup)
    verify_files(manifest, rollback=True)
    if DAILY_LINK.resolve() not in {Path(manifest["previous_daily"]), Path(manifest["release"])}:
        raise RuntimeError("daily_release_drift")
    if active("youtube-publisher-daily-report.service") not in ("inactive", "failed"):
        raise RuntimeError("daily_in_flight")
    with lock(DAILY_STATE / "run.lock"):
        verify_files(manifest, rollback=True)
        if DAILY_LINK.resolve() not in {Path(manifest["previous_daily"]), Path(manifest["release"])}:
            raise RuntimeError("daily_release_drift")
        command("systemctl", "stop", DAILY_TIMER)
        if Path(CONFIGS["deploy/social-facebook-attribution.timer"]).exists():
            command("systemctl", "disable", "--now", TIMER)
        if Path(CONFIGS["deploy/social-facebook-attribution.service"]).exists():
            command("systemctl", "stop", COLLECTOR)
        for path, values in manifest["files"].items():
            target = Path(path)
            if values["before"] is None:
                if target.exists():
                    target.unlink()
            else:
                atomic_copy(backup_file(manifest["backup"], path), target)
        switch(DAILY_LINK, manifest["previous_daily"])
        command("systemctl", "daemon-reload")
        command("systemctl", "restart", "drama-material-api.service")
        health()
        if manifest["daily_timer_before"] == "active":
            command("systemctl", "start", DAILY_TIMER)
        # Attribution, source databases, links and delivery receipts are retained.
        manifest["status"] = "rolled_back"
        save(manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "apply", "rollback"))
    parser.add_argument("--sha")
    parser.add_argument("--backup")
    args = parser.parse_args()
    if args.action == "stage" and args.sha:
        stage(args.sha)
    elif args.action in ("apply", "rollback") and args.backup:
        try:
            globals()[args.action](args.backup)
        except Exception as error:
            manifest = load(args.backup)
            manifest["last_error"] = type(error).__name__
            manifest["failed_action"] = args.action
            # Never resume scheduled delivery until validation or rollback succeeds.
            if manifest["status"] in ("applying", "verified"):
                manifest["status"] = "held_for_recovery"
            save(manifest)
            raise
    else:
        parser.error("stage needs --sha; apply/rollback needs --backup")


if __name__ == "__main__":
    main()
