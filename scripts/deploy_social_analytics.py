#!/usr/bin/env python3
"""GitHub-first, staged social reporting deployment and row-scoped rollback."""
import argparse
import contextlib
try:
    import fcntl
except ImportError:  # Permit isolated helper tests on Windows; deployment is Linux-only.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
BASE = Path("/mnt/data-disk/social-analytics")
MAIN = Path("/root/drama_material_service")
WEB = Path("/usr/share/nginx/html")
X_LINK = Path("/opt/x-post-automation/current")
DAILY_LINK = Path("/opt/youtube-publisher-daily-report/current")
POST_DB = Path("/var/lib/x-post-automation/accounts.sqlite3")
AUTO_DB = Path("/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3")
USER_DB = MAIN / "data/drama_material_jobs.sqlite3"
DAILY_STATE = Path("/mnt/data-disk/youtube-publisher-daily-report")
PUBLIC = Path("/mnt/data-disk/x-post-automation/s2l")
TRIGGERS = [
    "x-post-schedule.timer", "x-post-schedule-claim.timer", "x-post-manual.timer",
    "x-post-daily.timer", "x-auto-post-runner.timer", "x-auto-post-scheduler.timer",
    "x-auto-post-metric.timer", "x-auto-post-runner.path", "youtube-publisher-daily-report.timer",
]
WORKERS = [p.replace(".timer", ".service") for p in TRIGGERS if p.endswith(".timer")]
WORKERS += ["x-post-catchup.service"]
SERVICES = ["drama-material-api.service", "x-post-automation.service", "x-auto-post-service.service"]
X_FILES = [
    "features/x_posts/service.py", "features/x_posts/attribution.py",
    "features/x_auto_posts/core.py", "features/x_auto_posts/service.py",
]
MAIN_FILES = [
    "app.py", "features/x_posts/service.py", "features/x_posts/attribution.py",
    "features/x_posts/attribution_runtime.py", "features/x_posts/attribution_migration.py",
]
STATIC = [
    "quick-nav.js", "social-analytics.html", "social-analytics.js", "social-analytics.css",
    "youtube-analytics.html", "youtube-analytics-redirect.js",
]
REPORT_FILES = ["scripts/social_publisher_daily_report.py", "scripts/youtube_publisher_daily_report.py"]
NGINX = Path("/etc/nginx/default.d/social-analytics.conf")
X_FLAG = Path("/etc/systemd/system/x-post-automation.service.d/70-social-attribution.conf")
REPORT_ACCESS = Path("/etc/systemd/system/youtube-publisher-daily-report.service.d/70-social-analytics.conf")


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def active(unit):
    return command("systemctl", "show", unit, "-p", "ActiveState", "--value")


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def copy(src, dst, *, public=False):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if public:
        dst.chmod(0o644)


def release_directory(path):
    """Only code release parents are public; backup/manifest parents stay private."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o755)


def copy_code(src, dst, root):
    root = Path(root).resolve()
    destination = Path(dst).resolve()
    if root not in destination.parents:
        raise RuntimeError("code destination outside release")
    parent = destination.parent
    while parent != root:
        release_directory(parent)
        parent = parent.parent
    copy(src, dst, public=True)


def check_readable(unit, paths):
    """Check Unix permissions as the actual service account before switching."""
    user = command("systemctl", "show", unit, "-p", "User", "--value") or "root"
    group = command("systemctl", "show", unit, "-p", "Group", "--value")
    script = "import pathlib,sys;[(pathlib.Path(p).open('rb').close()) for p in sys.argv[1:]]"
    args = ["runuser", "--user", user]
    if group:
        args += ["--group", group]
    command(*args, "--", "/usr/bin/python3", "-c", script, *[str(path) for path in paths])
    return dict(user=user, group=group)


def switch(link, target):
    temp = link.with_name(link.name + ".social-next")
    if temp.is_symlink():
        temp.unlink()
    temp.symlink_to(target)
    temp.replace(link)


def save(manifest):
    path = Path(manifest["backup"]) / "deployment.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)
    print(json.dumps({"status": manifest["status"], "backup": manifest["backup"]}), flush=True)


def verify_baseline():
    baseline = json.loads((SOURCE / "deploy/social-analytics-baseline.json").read_text())
    roots = dict(main=MAIN, x=X_LINK, static=WEB)
    for group, names in baseline.items():
        for name, sha in names.items():
            path = roots[group] / name if group in roots else Path(name)
            if digest(path) != sha:
                raise RuntimeError("baseline drift: " + str(path))


def folder_files(relative):
    return [str(p.relative_to(SOURCE)) for p in sorted((SOURCE / relative).glob("*.py"))]


def backup_sqlite(src, dst):
    with contextlib.closing(sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)) as c:
        with contextlib.closing(sqlite3.connect(dst)) as out:
            c.backup(out)
    dst.chmod(0o600)


def verify_publication_facts(backup_path, live_path, prefix):
    """Compare every pre-existing publisher column except the authorized URL.

    The backup schema defines the boundary, so new attribution tables and new
    tenant columns do not cause differences. Queue, dedupe, recovery, attempts,
    receipts and scheduling facts are all included without exposing row values.
    """
    def quoted(value):
        return '"' + value.replace('"', '""') + '"'

    def content_hash(connection, table, columns, without_rowid, primary):
        ordering = ",".join(quoted(name) for name in primary) if without_rowid else "rowid"
        selected = ",".join(quoted(name) for name in columns)
        if not without_rowid:
            selected = "rowid," + selected
        digest_value, count = hashlib.sha256(), 0
        cursor = connection.execute("SELECT " + selected + " FROM " + quoted(table) + " ORDER BY " + ordering)
        for row in cursor:
            values = [({"bytes": value.hex()} if isinstance(value, bytes) else value) for value in row]
            digest_value.update(json.dumps(values, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode())
            digest_value.update(b"\n")
            count += 1
        return {"rows": count, "sha256": digest_value.hexdigest(), "columns": len(columns)}

    with contextlib.closing(sqlite3.connect(Path(backup_path).resolve().as_uri() + "?mode=ro", uri=True)) as old, \
         contextlib.closing(sqlite3.connect(Path(live_path).resolve().as_uri() + "?mode=ro", uri=True)) as live:
        for connection in (old, live):
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
        tables = [(name, sql) for name, sql in old.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")
            if name.startswith(prefix)]
        if not tables:
            raise RuntimeError("publisher baseline contains no expected tables")
        result = {}
        for table, definition in tables:
            fields = list(old.execute("PRAGMA table_info(" + quoted(table) + ")"))
            columns = [row[1] for row in fields if not (
                table == "x_post_publish_log" and row[1] == "long_url")]
            primary = [row[1] for row in sorted(fields, key=lambda row: row[5]) if row[5]]
            without_rowid = "WITHOUT ROWID" in (definition or "").upper()
            before = content_hash(old, table, columns, without_rowid, primary)
            after = content_hash(live, table, columns, without_rowid, primary)
            if before != after:
                raise RuntimeError("publisher facts changed outside migration: " + table)
            result[table] = after
        return result


def protect_files(manifest, paths):
    backup = Path(manifest["backup"])
    for path in paths:
        key = str(path)
        if key not in manifest["external_files"]:
            manifest["external_files"][key] = path.exists()
            if path.exists():
                copy(path, backup / "files" / key.lstrip("/"))
    save(manifest)


def stage(sha):
    if fcntl is None:
        raise RuntimeError("deployment_requires_linux")
    if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError("full_commit_sha_required")
    assert command("findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk") == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    assert command("git", "-C", str(SOURCE), "rev-parse", "HEAD") == sha
    assert not command("git", "-C", str(SOURCE), "status", "--porcelain"), "checkout must be clean"
    verify_baseline()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + sha[:12]
    release_directory(BASE)
    private = BASE / "backups"
    private.mkdir(parents=True, exist_ok=True)
    private.chmod(0o700)
    backup = private / stamp
    backup.mkdir(parents=True, mode=0o700)
    release_directory(BASE / "releases")
    releases = BASE / "releases" / sha
    if releases.exists():
        raise RuntimeError("release_already_exists")
    release_directory(releases)
    main_files = MAIN_FILES + folder_files("features/social_analytics")
    report_files = REPORT_FILES + folder_files("features/social_analytics") + folder_files("features/social_daily_report")
    for kind, old, names in (("x", X_LINK.resolve(), X_FILES), ("daily", DAILY_LINK.resolve(), report_files)):
        release = releases / kind
        shutil.copytree(old, release, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        release.chmod(0o755)
        for name in names:
            copy_code(SOURCE / name, release / name, release)
        for name in names:
            subprocess.check_call(["python3", "-m", "py_compile", str(release / name)])
    runtime_access = {
        "x": check_readable("x-post-automation.service", [releases / "x" / name for name in X_FILES]),
        "auto": check_readable("x-auto-post-service.service", [releases / "x" / name for name in X_FILES]),
        "daily": check_readable("youtube-publisher-daily-report.service",
                                [releases / "daily" / name for name in report_files]),
    }
    verify_baseline()
    protected_source = main_files + X_FILES + report_files + ["static/" + p for p in STATIC]
    protected_source += ["scripts/deploy_social_analytics.py", "scripts/social_analytics_permissions.py",
                         "deploy/social-analytics-baseline.json"]
    manifest = dict(
        status="prepared", commit=sha, backup=str(backup), source=str(SOURCE),
        releases=str(releases), previous_x=str(X_LINK.resolve()), previous_daily=str(DAILY_LINK.resolve()),
        main_files=main_files, report_files=report_files, external_files={},
        runtime_access=runtime_access,
        files={name: digest(SOURCE / name) for name in set(protected_source)},
    )
    save(manifest)
    return manifest


def assert_staged(manifest):
    for name, expected in manifest["files"].items():
        assert digest(SOURCE / name) == expected, "source changed: " + name
    assert command("git", "-C", str(SOURCE), "rev-parse", "HEAD") == manifest["commit"]
    assert not command("git", "-C", str(SOURCE), "status", "--porcelain"), "source checkout became dirty"
    for kind, names in (("x", X_FILES), ("daily", manifest["report_files"])):
        for name in names:
            if digest(Path(manifest["releases"]) / kind / name) != manifest["files"][name]:
                raise RuntimeError("staged release changed: " + kind + "/" + name)


@contextlib.contextmanager
def quiesced(manifest, *, restoring=False):
    if not restoring:
        manifest["triggers"] = {u: active(u) for u in TRIGGERS}
        manifest["services"] = {u: active(u) for u in SERVICES}
        assert all(v == "active" for v in manifest["services"].values())
    locks = []
    save(manifest)
    restart = False
    try:
        enabled = [u for u in TRIGGERS if active(u) == "active"]
        if enabled:
            command("systemctl", "stop", *enabled)
        deadline = time.monotonic() + 120
        while any(active(u) in {"active", "activating", "deactivating"} for u in WORKERS):
            if time.monotonic() > deadline:
                raise RuntimeError("publisher busy; no in-flight operation was terminated")
            time.sleep(2)
        for path in ("/run/x-post-daily/runner.lock", "/run/x-auto-post/scheduler.lock",
                     str(DAILY_STATE / "run.lock")):
            handle = open(path, "r+b")
            locks.append(handle)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with contextlib.closing(sqlite3.connect(POST_DB.resolve().as_uri() + "?mode=ro", uri=True)) as c:
            assert not c.execute("SELECT 1 FROM x_post_queue WHERE status='publishing' LIMIT 1").fetchone(), "X write in flight"
        # With runners drained and locked, close HTTP ingress before taking snapshots.
        command("systemctl", "stop", "x-auto-post-service.service", "x-post-automation.service")
        manifest["paused_services"] = ["x-auto-post-service.service", "x-post-automation.service"]
        manifest["status"] = "quiesced"
        save(manifest)
        yield
        restart = True
    finally:
        for handle in reversed(locks):
            handle.close()
        if restart:
            try:
                restore_triggers(manifest)
            except Exception:
                # A partial trigger restoration is not permission to continue writes.
                with contextlib.suppress(Exception):
                    enabled = [unit for unit in TRIGGERS if active(unit) == "active"]
                    if enabled:
                        command("systemctl", "stop", *enabled)
                manifest["status"] = "held_for_recovery"
                save(manifest)
                raise
        else:
            manifest["status"] = "held_for_recovery"
            save(manifest)


def restore_triggers(manifest):
    if manifest.get("status") not in {
        "deployed", "deployed_triggers_restored", "rolled_back_compatible_x_code_retained",
    }:
        raise RuntimeError("recovery_verification_required_before_resuming")
    for unit in SERVICES:
        if active(unit) != "active":
            command("systemctl", "start", unit)
    expected = [u for u, value in manifest["triggers"].items() if value == "active"]
    if expected:
        command("systemctl", "start", *expected)
    assert {u: active(u) for u in TRIGGERS} == manifest["triggers"]


def settings():
    result = {}
    for line in (MAIN / ".env").read_text().splitlines():
        if line.startswith("ADMIN_MAPPING_MYSQL_") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value.strip().strip('"').strip("'")
    result.update(JOB_DB_PATH=str(USER_DB), SOCIAL_ANALYTICS_X_DB_PATH=str(POST_DB), X_ATTRIBUTION_AUTO_DB_PATH=str(AUTO_DB))
    return result


def health(*, allow_legacy=False):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            for port in (8810, 18833):
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3) as response:
                    assert response.status == 200
            for prefix in ("social", "youtube"):
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8787/api/%s-analytics/report" % prefix, timeout=5):
                        raise AssertionError("anonymous report was permitted")
                except urllib.error.HTTPError as exc:
                    try:
                        assert exc.code in ({401, 403, 404} if allow_legacy and prefix == "social" else {401, 403})
                    finally:
                        exc.close()
            assert all(active(u) == "active" for u in SERVICES)
            return
        except (OSError, AssertionError):
            time.sleep(1)
    raise RuntimeError("service health verification failed")


def install_navigation():
    path = WEB / "navigation.json"
    groups = json.loads(path.read_text())
    old = [(group, item) for group in groups for item in group.get("items", [])
           if item.get("key") == "youtubeAnalytics"]
    if len(old) != 1:
        raise RuntimeError("old_report_navigation_not_unique")
    old_group, old_item = old[0]
    for group in groups:
        group["items"] = [item for item in group.get("items", []) if item.get("key") != "youtubeAnalytics"]
    assert not any(group.get("key") == "social_reports" for group in groups)
    groups.append(dict(
        key="social_reports", label="社媒数据报表", order=50, module="social_analytics",
        enabled=old_group.get("enabled") is not False,
        adminOnly=bool(old_group.get("adminOnly") or old_item.get("adminOnly")),
        items=[dict(key="socialAnalytics", label="发布效果分析", description="按人员、渠道、账号和剧目查看发布与效果",
                    kind="page", href="/social-analytics.html", enabled=old_item.get("enabled") is not False,
                    order=10, module="social_analytics")],
    ))
    temp = path.with_suffix(".social-next")
    temp.write_text(json.dumps(groups, ensure_ascii=False, indent=2) + "\n")
    temp.chmod(0o644)
    temp.replace(path)


def verify_report_runtime(manifest):
    """Run a no-send preview in the report's restrictive read-only sandbox."""
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    # Keep the production run.lock held. The verifier has its own fresh output
    # and lock directory, so it cannot return already_running or read an old card.
    preview_state = Path(manifest["backup"]) / "report-preview"
    preview_state.mkdir(mode=0o700, exist_ok=False)
    args = [
        "systemd-run", "--wait", "--pipe", "--collect",
        "--unit=social-analytics-verify-" + manifest["commit"][:12],
        "--property=User=root", "--property=ProtectSystem=strict",
        "--property=ProtectHome=read-only", "--property=PrivateTmp=true",
        "--property=WorkingDirectory=" + str(DAILY_LINK),
        "--property=ReadWritePaths=%s -%s %s" % (preview_state, USER_DB.parent, POST_DB.parent),
        "--property=ReadOnlyPaths=%s %s" % (USER_DB, POST_DB),
        "/usr/bin/python3", str(DAILY_LINK / "scripts/youtube_publisher_daily_report.py"),
        "--preview", "--date", day, "--state-dir", str(preview_state),
    ]
    output = command(*args)
    summaries = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            summaries.append(value)
    folder = preview_state / "previews" / day
    if not any(row.get("status") == "ready" and row.get("date") == day
               and Path(row.get("path", "")).resolve() == folder.resolve() for row in summaries):
        raise RuntimeError("daily_runtime_preview_not_generated")
    report_path = folder / "report.json"
    report = json.loads(report_path.read_text())
    if not report.get("quality", {}).get("complete"):
        raise RuntimeError("daily_runtime_report_incomplete")
    manifest["report_preview"] = {"date": day, "path": str(report_path),
                                  "sha256": digest(report_path), "totals": report["totals"]}


def deploy(manifest):
    verify_baseline()
    assert_staged(manifest)
    sys.path.insert(0, str(SOURCE))
    from features.x_posts.attribution_runtime import resolver
    from features.x_posts.attribution_migration import create_manifest, run as migrate
    backup, releases = Path(manifest["backup"]), Path(manifest["releases"])
    paths = [MAIN / p for p in manifest["main_files"]]
    paths += [WEB / p for p in STATIC] + [MAIN / "static" / p for p in STATIC]
    paths += [WEB / "navigation.json", NGINX, X_FLAG, REPORT_ACCESS]
    protect_files(manifest, paths)
    with quiesced(manifest):
        for name, path in (("posts", POST_DB), ("auto", AUTO_DB), ("users", USER_DB),
                           ("delivery", DAILY_STATE / "delivery.sqlite3")):
            backup_sqlite(path, backup / (name + ".sqlite3"))
        # Additive schema only; no queue state, publication or dedupe changes.
        for path, table, column in ((POST_DB, "x_post_manual_run", "actor_tenant_key"),
                                    (AUTO_DB, "x_auto_template", "created_by_tenant_key")):
            with sqlite3.connect(path) as c:
                if column not in {r[1] for r in c.execute("PRAGMA table_info(" + table + ")")}:
                    c.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " TEXT NOT NULL DEFAULT ''")
        manifest["migration"] = create_manifest(POST_DB, PUBLIC, resolver(settings()), backup / "links")
        manifest["status"] = "migration_planned"
        save(manifest)
        for name in manifest["main_files"]:
            copy_code(SOURCE / name, MAIN / name, MAIN)
        command("systemctl", "restart", "drama-material-api.service")
        manifest["migration_result"] = migrate(backup / "links", manifest["migration"]["manifest_sha256"])
        manifest["publication_facts"] = {
            "posts": verify_publication_facts(backup / "posts.sqlite3", POST_DB, "x_post_"),
            "auto": verify_publication_facts(backup / "auto.sqlite3", AUTO_DB, "x_auto_"),
        }
        manifest["status"] = "links_verified"
        save(manifest)
        # Permissions are separately audited; preserve unrelated current values.
        command("python3", str(SOURCE / "scripts/social_analytics_permissions.py"),
                "--db", str(USER_DB), "--navigation", str(backup / "files/usr/share/nginx/html/navigation.json"),
                "--manifest", str(backup / "permissions.json"), "--mode", "dry-run")
        command("python3", str(SOURCE / "scripts/social_analytics_permissions.py"),
                "--db", str(USER_DB), "--manifest", str(backup / "permissions.json"), "--mode", "apply")
        for name in STATIC:
            copy(SOURCE / "static" / name, WEB / name, public=True)
            copy(SOURCE / "static" / name, MAIN / "static" / name, public=True)
        install_navigation()
        NGINX.write_text((Path("/etc/nginx/default.d/youtube-analytics.conf").read_text()).replace(
            "/api/youtube-analytics/", "/api/social-analytics/"))
        X_FLAG.parent.mkdir(parents=True, exist_ok=True)
        X_FLAG.write_text("[Service]\nEnvironment=X_POST_ATTRIBUTION_V2_ENABLED=1\n")
        REPORT_ACCESS.parent.mkdir(parents=True, exist_ok=True)
        REPORT_ACCESS.write_text(
            "[Service]\n# WAL companions require directory access; the main DB remains read-only.\n"
            "ReadWritePaths=/var/lib/x-post-automation\nReadOnlyPaths=/var/lib/x-post-automation/accounts.sqlite3\n")
        switch(X_LINK, releases / "x")
        switch(DAILY_LINK, releases / "daily")
        command("systemctl", "daemon-reload")
        command("nginx", "-t")
        command("systemctl", "reload", "nginx")
        command("systemctl", "start", "x-post-automation.service", "x-auto-post-service.service")
        health()
        verify_report_runtime(manifest)
        manifest["status"] = "deployed"
        save(manifest)
    manifest["status"] = "deployed_triggers_restored"
    save(manifest)


def rollback(manifest):
    """Restore only this migration's links and settings, never replace a database."""
    sys.path.insert(0, str(SOURCE))
    from features.x_posts.attribution_migration import run as migrate
    backup = Path(manifest["backup"])
    with quiesced(manifest, restoring=True):
        if manifest.get("migration"):
            manifest["rollback_links"] = migrate(
                backup / "links", manifest["migration"]["manifest_sha256"], rollback=True)
        permission_state = backup / "permissions.json"
        if permission_state.exists():
            command("python3", str(SOURCE / "scripts/social_analytics_permissions.py"),
                    "--db", str(USER_DB), "--manifest", str(permission_state), "--mode", "rollback")
        # Keep X's compatible reader/validator and additive schema. Restore legacy
        # generation only after old links are restored; new posts/receipts survive.
        for filename, existed in manifest["external_files"].items():
            path = Path(filename)
            if path in {MAIN / name for name in MAIN_FILES if name.startswith("features/x_posts/")} or path == X_FLAG:
                continue
            if existed:
                copy(backup / "files" / filename.lstrip("/"), path)
            elif path.exists():
                path.unlink()
        X_FLAG.write_text("[Service]\nEnvironment=X_POST_ATTRIBUTION_V2_ENABLED=0\n")
        switch(DAILY_LINK, Path(manifest["previous_daily"]))
        command("systemctl", "daemon-reload")
        command("nginx", "-t")
        command("systemctl", "reload", "nginx")
        for unit in SERVICES:
            command("systemctl", "restart", unit)
        health(allow_legacy=True)
        manifest["status"] = "rolled_back_compatible_x_code_retained"
        save(manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["stage", "deploy", "rollback", "restore-triggers"])
    parser.add_argument("--sha")
    parser.add_argument("--backup")
    args = parser.parse_args()
    os.umask(0o077)
    if args.mode == "stage":
        stage(args.sha)
        return
    backup = Path(args.backup).resolve(strict=True)
    assert BASE / "backups" in backup.parents
    manifest = json.loads((backup / "deployment.json").read_text())
    if args.mode == "deploy":
        deploy(manifest)
    elif args.mode == "rollback":
        rollback(manifest)
    else:
        health(allow_legacy=manifest.get("status") == "rolled_back_compatible_x_code_retained")
        restore_triggers(manifest)


if __name__ == "__main__":
    main()
