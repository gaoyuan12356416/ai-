#!/usr/bin/env python3
"""Install this verified release's additive audits and independent report unit.

Run on CPU after fetching a pinned GitHub commit. Defaults to validation only.
Never executes a publishing runner or sends a message.
"""
import argparse
import ast
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.post_daily_report import ensure_storage, DEFAULT_PATHS

TARGETS = [
    ("features/tt_posts/core.py", "/opt/tt-post/current/features/tt_posts/core.py", "cadd8aeb556b5968db2394a5313c7b6787ce7afff4bfc7b014b3965e18e0bcf0"),
    ("features/x_posts/service.py", "/opt/x-post-automation/current/features/x_posts/service.py", "3763ee958cbd4505321bfd44e03045f3589e5e679a25af8158948959bc4b813a"),
    ("features/x_posts/service.py", "/root/drama_material_service/features/x_posts/service.py", "3763ee958cbd4505321bfd44e03045f3589e5e679a25af8158948959bc4b813a"),
    ("features/fb_auto_posts/core.py", "/opt/fb-auto-post/current/features/fb_auto_posts/core.py", "8f0cc7cac5a15ca69153b55145e0e70ae814b5a770fe1e4a0ef564ed265b8a54"),
]
FB_STEMS = ["fb-auto-post-" + x for x in ("scheduler", "plan", "prepare", "runner", "reconcile")]


def run(*args):
    return subprocess.check_output(list(args), text=True).strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ro(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.execute("PRAGMA query_only=ON")
    return db


def fb_idle():
    for stem in FB_STEMS:
        if run("systemctl", "show", stem + ".service", "-p", "ActiveState", "--value") not in ("inactive", "failed"):
            return False
    with ro(DEFAULT_PATHS["fb"]) as db:
        task_busy = db.execute("SELECT count(*) FROM fb_auto_task WHERE status IN ('running','publishing','claimed','preparing','reconciling') OR lease_owner<>''").fetchone()[0]
        due_busy = db.execute("SELECT count(*) FROM fb_auto_due_slot WHERE lease_owner<>''").fetchone()[0]
    # Only listen socket itself is expected. Stop if any HTTP request is active.
    established = run("ss", "-Htn", "state", "established", "(", "sport", "=", ":18835", ")")
    return not task_busy and not due_busy and not established


def counts(db, tables):
    return {name: db.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in tables}


def apply_audit(channel):
    tables = ["tt_post_queue", "tt_post_schedule_run", "tt_post_event"] if channel == "tt" else ["x_post_queue", "x_post_schedule_run", "x_post_publish_log"]
    db = sqlite3.connect(DEFAULT_PATHS[channel], timeout=2)
    db.execute("PRAGMA busy_timeout=2000")
    db.execute("BEGIN IMMEDIATE")
    before = counts(db, tables)
    try:
        if channel == "tt":
            source = (ROOT / "features/tt_posts/core.py").read_text()
            start = source.index("            # Reporting-only history:")
            end = source.index("            pending_language_rows =", start)
            code = textwrap.dedent(source[start:end])
            exec(compile(code, "<verified-TT-report-audit>", "exec"), {"conn": db})
        else:
            tree = ast.parse((ROOT / "features/x_posts/service.py").read_text())
            statements = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SCHEDULE_CONFIG_AUDIT_DDL" for t in node.targets))
            for statement in statements: db.execute(statement)
        if counts(db, tables) != before: raise RuntimeError("audit_changed_business_counts")
        db.commit()
        return before
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def replace_file(source, target):
    target = Path(target).resolve()
    old = target.stat()
    temp = target.with_name(target.name + ".report-audit-tmp")
    temp.write_bytes(Path(source).read_bytes())
    os.chmod(temp, old.st_mode)
    os.chown(temp, old.st_uid, old.st_gid)
    os.replace(temp, target)


def fb_health():
    import urllib.request
    for _ in range(10):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18835/health", timeout=3) as response:
                if json.load(response).get("ok") is True: return True
        except Exception: pass
        time.sleep(1)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    state = Path("/mnt/data-disk/post-daily-report")
    ensure_storage(state)
    os.chmod(state, 0o700)
    verified = []
    for relative, target, expected in TARGETS:
        current, new = digest(target), digest(ROOT / relative)
        if current not in (expected, new): raise RuntimeError("live_source_drift:" + target)
        compile((ROOT / relative).read_text(), relative, "exec")
        verified.append({"relative": relative, "target": target, "old_sha256": current, "new_sha256": new})
    print(json.dumps({"status": "validated", "files": verified}, ensure_ascii=False), flush=True)
    if not args.apply: return
    required = sum(Path(DEFAULT_PATHS[k]).stat().st_size for k in ("tt", "x", "fb")) * 2 + 64 * 1024 * 1024
    if shutil.disk_usage(state).free < required: raise RuntimeError("insufficient_backup_disk_space")
    backup = state / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {"release": str(ROOT), "files": verified, "timers_active": [], "backup": str(backup)}
    for i, item in enumerate(verified):
        dest = backup / (str(i) + ".source")
        shutil.copy2(item["target"], dest)
        item["backup"] = str(dest)
    for channel in ("tt", "x", "fb"):
        src = ro(DEFAULT_PATHS[channel]); dest = sqlite3.connect(str(backup / (channel + ".sqlite3")))
        try: src.backup(dest, pages=256, sleep=0.02)
        finally: src.close(); dest.close()
    for stem in FB_STEMS:
        if run("systemctl", "show", stem + ".timer", "-p", "ActiveState", "--value") == "active":
            manifest["timers_active"].append(stem + ".timer")
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"status": "backed_up", "backup": str(backup)}), flush=True)
    # Record-only triggers are safe online; counts checked under the same lock.
    manifest["tt_business_counts"] = apply_audit("tt")
    manifest["x_business_counts"] = apply_audit("x")
    paused = manifest["timers_active"]
    switched_fb = False
    resume_timers = True
    try:
        if paused: subprocess.check_call(["systemctl", "stop"] + paused)
        deadline = time.monotonic() + 45
        while not fb_idle():
            if time.monotonic() >= deadline: raise RuntimeError("fb_not_drained_no_restart")
            time.sleep(2)
        # Recheck after pausing triggers; never replace a drifted live file.
        for item in verified:
            if digest(item["target"]) != item["old_sha256"]: raise RuntimeError("live_changed_after_backup")
        for item in verified:
            replace_file(ROOT / item["relative"], item["target"])
            if item["relative"].startswith("features/fb_auto_posts/"):
                switched_fb = True
                resume_timers = False
        subprocess.check_call(["systemctl", "restart", "fb-auto-post-service.service"])
        if not fb_health(): raise RuntimeError("fb_health_failed")
        with ro(DEFAULT_PATHS["fb"]) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='fb_auto_due_target_snapshot'").fetchone():
                raise RuntimeError("fb_audit_schema_missing")
        for item in verified:
            if digest(item["target"]) != item["new_sha256"]: raise RuntimeError("installed_hash_mismatch")
        resume_timers = True
    except Exception:
        if switched_fb:
            resume_timers = False
            manifest["status"] = "rollback_requires_health_verification"
            item = verified[-1]
            replace_file(item["backup"], item["target"])
            subprocess.check_call(["systemctl", "restart", "fb-auto-post-service.service"])
            if not fb_health(): raise RuntimeError("fb_rollback_unhealthy_timers_kept_paused")
            resume_timers = True
            manifest["status"] = "rolled_back_fb_healthy"
        raise
    finally:
        if paused and resume_timers: subprocess.check_call(["systemctl", "start"] + paused)
        manifest["timers_restored"] = resume_timers
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    opt = Path("/opt/post-daily-report");opt.mkdir(exist_ok=True)
    temp_link = opt / "current.new"
    if temp_link.is_symlink(): temp_link.unlink()
    temp_link.symlink_to(ROOT)
    os.replace(temp_link, opt / "current")
    for name in ("post-daily-report.service", "post-daily-report.timer"):
        destination = Path("/etc/systemd/system") / name
        if destination.exists(): shutil.copy2(destination, backup / name)
        shutil.copy2(ROOT / "deploy" / name, destination)
    subprocess.check_call(["systemctl", "daemon-reload"])
    manifest["status"] = "installed_timer_not_enabled"
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"status": manifest["status"], "backup": str(backup)}, ensure_ascii=False))


if __name__ == "__main__": main()
