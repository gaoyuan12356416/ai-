"""Hash-guarded, GitHub-first additive FB release and ledger-preserving rollback."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

try:
    import pwd
except ImportError:  # Allows isolated Windows tests of the rollback manifest.
    pwd = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from patch_fb_video_publish_integration import patch_app, patch_quick_nav
from features.fb_video_publish.navigation import ensure_navigation

BASE_COMMIT = "41b5ca4f1e8f64945f56b2482e8e3dd96e2c9e89"
BASE = Path("/mnt/data-disk/fb-video-publish")
MAIN = Path("/root/drama_material_service")
FB = Path("/opt/fb-auto-post/current")
STATIC = Path("/usr/share/nginx/html")
DB = Path("/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3")
LINK_ROOT = Path("/mnt/data-disk/fb-auto-post-public/s2l/facebook")
API_UNIT = "drama-material-api.service"
FB_UNIT = "fb-auto-post-service.service"
NEW_UNITS = ["fb-video-publish-worker.service", "fb-video-publish-prepare.timer"]
OLD_NAMES = ("scheduler", "plan", "prepare", "runner", "reconcile", "effects", "metric-hourly", "metric-repair")
OLD_TIMERS = ["fb-auto-post-" + name + ".timer" for name in OLD_NAMES]
MAIN_HASH = "cc68f0f4d5f7bf22ba7c6a1fbe50c56f4db360f4ea565b9fc62859055fe4405e"
NAV_HASH = "718e6995428a78e5ce0e035afef061eb6ecd9f80136585a8a3ad7f1db2254d67"
SHORT_SERVER = Path("/etc/nginx/conf.d/gy.g2flow.com.conf")
SHORT_SNIPPET = Path("/etc/nginx/snippets/fb-video-publish-s2l.conf")
ADMISSION_GATE = Path("/etc/systemd/system/fb-auto-post-service.service.d/zz-fb-video-closed.conf")
SHORT_SERVER_HASH = "b0bdebc15a8e885ee468ff9a0017e0f3b59ce57255ed304f18b70bd8281c5150"
OLD_FILES = [
    "features/fb_auto_posts/" + name + ".py"
    for name in ("core", "gpu", "hit_material", "manual_batch", "pool_extension", "service")
]


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def require(test, message):
    if not test:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def atomic_bytes(data, target, mode=0o644):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".fb-video-next")
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def database():
    return closing(sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True, timeout=15))


def old_facts(path=DB):
    with closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fb_auto_%' ORDER BY name")]
        facts = {}
        for name in names:
            require(name.replace("_", "").isalnum(), "Unexpected table name")
            # Primary operational facts; metric values can be huge and unrelated.
            if "metric" in name:
                continue
            digest = hashlib.sha256()
            count = 0
            for row in conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid'):
                digest.update(json.dumps(row, ensure_ascii=False, default=str).encode())
                digest.update(b"\n")
                count += 1
            facts[name] = {"count": count, "sha256": digest.hexdigest()}
        return facts


def active_work():
    busy = []
    for name in OLD_NAMES:
        unit = "fb-auto-post-" + name + ".service"
        if run("systemctl", "show", unit, "-p", "ActiveState", "--value") in ("active", "activating", "deactivating"):
            busy.append(unit)
    with database() as conn:
        tasks = conn.execute("SELECT COUNT(*) FROM fb_auto_task WHERE status IN ('preparing','running')").fetchone()[0]
    return busy, tasks


def check_mount():
    require(run("findmnt", "-no", "UUID", "/mnt/data-disk") == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8",
            "Data disk UUID mismatch")
    require(os.access("/mnt/data-disk", os.W_OK) and shutil.disk_usage("/mnt/data-disk").free > 5 * 1024**3,
            "Data disk unavailable or insufficient space")


def state(unit):
    return {"active": run("systemctl", "show", unit, "-p", "ActiveState", "--value"),
            "enabled": run("systemctl", "show", unit, "-p", "UnitFileState", "--value")}


def health(url):
    for _ in range(20):
        try:
            with urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    raise RuntimeError("Health check failed: " + url)


def staged_targets(staging):
    require(sha(MAIN / "app.py") == MAIN_HASH, "Current main API drifted; review before deployment")
    require(sha(MAIN / "static/quick-nav.js") == NAV_HASH, "Current quick navigation drifted")
    require(sha(STATIC / "quick-nav.js") == NAV_HASH, "Public quick navigation differs from main")
    require(sha(SHORT_SERVER) == SHORT_SERVER_HASH, "Short-domain nginx config drifted")
    items = []

    def add(source, target, mode=0o644):
        items.append((Path(source), Path(target), mode))

    for relative in OLD_FILES:
        expected = subprocess.check_output(["git", "-C", str(ROOT), "show", BASE_COMMIT + ":" + relative])
        require(sha(FB / relative) == hashlib.sha256(expected).hexdigest(), "FB baseline drift: " + relative)
        add(ROOT / relative, FB / relative)
    for source in sorted((ROOT / "features/fb_video_publish").glob("*.py")):
        relative = source.relative_to(ROOT)
        add(source, FB / relative)
        add(source, MAIN / relative)
    add(ROOT / "scripts/fb_video_publish_worker.py", FB / "scripts/fb_video_publish_worker.py")
    for source in sorted((ROOT / "static").glob("fb-video-publish*")) + [ROOT / "static/fb-pages.html"]:
        require(source.suffix in (".html", ".js", ".css"), "Unexpected static file")
        for target_root in (STATIC, MAIN / "static", FB / "static"):
            add(source, target_root / source.name)
    patched_app = staging / "app.py"
    patched_app.write_text(patch_app((MAIN / "app.py").read_text()), encoding="utf-8")
    add(patched_app, MAIN / "app.py")
    navjs = staging / "quick-nav.js"
    navjs.write_text(patch_quick_nav((MAIN / "static/quick-nav.js").read_text()), encoding="utf-8")
    add(navjs, MAIN / "static/quick-nav.js")
    add(navjs, STATIC / "quick-nav.js")
    for number, path in enumerate((MAIN / "static/navigation.json", STATIC / "navigation.json")):
        nav = ensure_navigation(json.loads(path.read_text(encoding="utf-8-sig")))
        source = staging / ("navigation-" + str(number) + ".json")
        source.write_text(json.dumps(nav, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        add(source, path)
    short = staging / "gy.g2flow.com.conf"
    anchor = "    include /etc/nginx/snippets/drama-youtube-s2l.conf;"
    current = SHORT_SERVER.read_text()
    require(current.count(anchor) == 1, "Short-domain include anchor changed")
    short.write_text(current.replace(anchor, anchor + "\n    include /etc/nginx/snippets/fb-video-publish-s2l.conf;"), encoding="utf-8")
    add(short, SHORT_SERVER)
    add(ROOT / "deploy/nginx-fb-video-publish.conf", Path("/etc/nginx/default.d/fb-video-publish.conf"))
    add(ROOT / "deploy/nginx-fb-video-short-links.conf", SHORT_SNIPPET)
    for unit in ("fb-video-publish-worker.service", "fb-video-publish-prepare.service", "fb-video-publish-prepare.timer"):
        add(ROOT / "deploy" / unit, Path("/etc/systemd/system") / unit)
    dropin = staging / "fb-video-publish.conf"
    dropin.write_text(
        "[Service]\nEnvironment=FB_VIDEO_PUBLISH_ENABLED=1\nEnvironment=FB_VIDEO_DAILY_LIMIT=1000\n"
        "Environment=FB_VIDEO_SHARED_GPU_PREPARES=2\n"
        "Environment=FB_VIDEO_SHORT_LINK_ROOT=" + str(LINK_ROOT) + "\n"
        "Environment=FB_VIDEO_SHORT_LINK_OWNER=fb-auto-post\n", encoding="utf-8")
    add(dropin, Path("/etc/systemd/system/fb-auto-post-service.service.d/fb-video-publish.conf"))
    for source, target, _ in items:
        require("features/fb_gpu" not in str(target) and "features/random_gpu" not in str(target), "GPU deploy prohibited")
        require(source.is_file(), "Missing source: " + str(source))
    return items


def snapshot_db(destination):
    with database() as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)


def unit_present(unit):
    """An apply failure may precede installing the new unit files."""
    result = subprocess.run(["systemctl", "show", unit, "-p", "LoadState", "--value"],
                            capture_output=True, text=True, check=False)
    loaded = result.stdout.strip()
    if loaded == "not-found" and result.returncode in (0, 1, 4):
        return False
    require(result.returncode == 0 and bool(loaded), "Cannot inspect unit: " + unit)
    return True


def manual_facts():
    """The old database may not have reached the additive migration yet."""
    with database() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('fb_video_task','fb_video_link')")}
        active = (conn.execute(
            "SELECT COUNT(*) FROM fb_video_task WHERE status NOT IN ('published','failed','cancelled')")
                  .fetchone()[0]) if "fb_video_task" in tables else 0
        links_exist = (conn.execute("SELECT 1 FROM fb_video_link LIMIT 1").fetchone()
                       is not None) if "fb_video_link" in tables else False
    return active, links_exist


def rollback_targets(manifest):
    """Only restore copied targets; unchanged originals are valid after partial apply."""
    restore = []
    for item in manifest["files"]:
        target = Path(item["target"])
        saved = Path(item["backup"]) if item["backup"] else None
        before = item.get("before", sha(saved) if saved else None)
        current = sha(target)
        require(current in {before, item["after"]}, "Rollback target drift: " + str(target))
        if current == before:
            continue
        if saved:
            require(sha(saved) == before, "Rollback backup drift: " + str(saved))
        restore.append(item)
    return restore


def guarded_rollback(manifest_path, manifest, active, *, sidecar_stopped=False):
    """Retain guards/history until new tasks drain, with a reversible admission gate."""
    closed = b"[Service]\nEnvironment=FB_VIDEO_PUBLISH_ENABLED=0\n"
    if not any(item["target"] == str(ADMISSION_GATE) for item in manifest["files"]):
        saved = manifest_path.parent / "rollback-admission-gate"
        before = sha(ADMISSION_GATE)
        if ADMISSION_GATE.exists():
            require(not saved.exists() or sha(saved) == before, "Admission gate backup drift")
            if not saved.exists():
                shutil.copy2(ADMISSION_GATE, saved)
        manifest["files"].append({
            "target": str(ADMISSION_GATE), "backup": str(saved) if before is not None else None,
            "before": before, "after": hashlib.sha256(closed).hexdigest(),
            "mode_before": ADMISSION_GATE.stat().st_mode & 0o777 if before is not None else 0o644,
        })
        # Persist the recovery record before adding an otherwise untracked gate.
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    atomic_bytes(closed, ADMISSION_GATE)
    for target in (MAIN / "static/navigation.json", STATIC / "navigation.json"):
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        for group in config:
            for item in group.get("items", []):
                if item.get("module") == "fb_video_publish":
                    item["enabled"] = False
        atomic_bytes((json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode(), target)
        for item in manifest["files"]:
            if item["target"] == str(target):
                item["after"] = sha(target)
    manifest["admission_closed_with_active_tasks"] = True
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    old_running = [] if sidecar_stopped else [u for u in OLD_TIMERS if state(u)["active"] == "active"]
    if old_running:
        subprocess.run(["systemctl", "stop", *old_running], check=True)
    restarted = False
    try:
        if sidecar_stopped or active_work() == ([], 0):
            subprocess.run(["systemctl", "restart", FB_UNIT], check=True)
            health("http://127.0.0.1:18835/health")
            restarted = True
    finally:
        if old_running:
            subprocess.run(["systemctl", "start", *old_running], check=True)
    print(json.dumps({"guarded_rollback": True, "entry_and_worker_closed": True,
                      "guarded_source_retained": True, "active_tasks": active,
                      "sidecar_gate_restarted": restarted,
                      "restart_pending_old_work_drain": not restarted}))


def rollback(manifest_path):
    manifest = json.loads(manifest_path.read_text())
    require(manifest_path.resolve().is_relative_to(BASE / "backups"), "Invalid rollback path")
    # Stop new workers first. Facts and immutable links are never restored.
    for unit in NEW_UNITS:
        if unit_present(unit):
            subprocess.run(["systemctl", "disable", "--now", unit], check=True)
    if unit_present("fb-video-publish-prepare.service"):
        subprocess.run(["systemctl", "stop", "fb-video-publish-prepare.service"], check=True)
    active, links_exist = manual_facts()
    rollback_targets(manifest)
    if active:
        guarded_rollback(manifest_path, manifest, active)
        return
    running = [u for u in OLD_TIMERS if state(u)["active"] == "active"]
    if running:
        subprocess.run(["systemctl", "stop", *running], check=True)
    try:
        require(active_work() == ([], 0), "Old publish work still active; retry rollback after drain")
        subprocess.run(["systemctl", "stop", FB_UNIT], check=True)
        # A batch may have committed after the first read but before admission stopped.
        active, final_links = manual_facts()
        if active:
            guarded_rollback(manifest_path, manifest, active, sidecar_stopped=True)
            return
        links_exist = links_exist or final_links
        restore = rollback_targets(manifest)
        keep_routes = {str(SHORT_SERVER), str(SHORT_SNIPPET)} if links_exist else set()
        for item in restore:
            target = Path(item["target"])
            if str(target) in keep_routes:
                continue
            if item["backup"]:
                atomic_bytes(Path(item["backup"]).read_bytes(), target, item["mode_before"])
            else:
                target.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        subprocess.run(["systemctl", "restart", FB_UNIT, API_UNIT], check=True)
        health("http://127.0.0.1:18835/health")
        health("http://127.0.0.1:8787/api/ui/topbar")
    finally:
        if running:
            subprocess.run(["systemctl", "start", *running], check=True)
    print(json.dumps({"rolled_back": True, "database_and_short_links_retained": True,
                      "public_short_link_route_retained": links_exist}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    check_mount()
    if args.rollback:
        rollback(args.rollback)
        return
    commit = run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    require(not run("git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"), "Dirty release checkout")
    require(run("git", "-C", str(ROOT), "branch", "-r", "--contains", commit), "Commit not verified from remote")
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    staging = BASE / "staging" / (now + "-" + commit[:8])
    staging.mkdir(parents=True)
    targets = staged_targets(staging)
    import py_compile
    for source, _, _ in targets:
        if source.suffix == ".py":
            py_compile.compile(str(source), doraise=True)
    snapshot_db(staging / "rehearsal.sqlite3")
    before = old_facts(staging / "rehearsal.sqlite3")
    from features.fb_video_publish.core import Store
    Store(staging / "rehearsal.sqlite3")
    require(old_facts(staging / "rehearsal.sqlite3") == before, "Migration rehearsal modified old facts")
    with closing(sqlite3.connect(staging / "rehearsal.sqlite3")) as conn:
        require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Rehearsal integrity failed")
    print(json.dumps({"preflight_ok": True, "commit": commit, "files": len(targets), "migration_rehearsed": True, "active": active_work()}), flush=True)
    if not args.apply:
        return
    saved_states = {u: state(u) for u in OLD_TIMERS + [FB_UNIT, API_UNIT] + NEW_UNITS}
    running = [u for u in OLD_TIMERS if saved_states[u]["active"] == "active"]
    if running:
        subprocess.run(["systemctl", "stop", *running], check=True)
    manifest_path, changed = None, False
    try:
        require(active_work() == ([], 0), "Existing publisher work active; old timers restored, retry after drain")
        backup = BASE / "backups" / (now + "-" + commit[:8])
        backup.mkdir(parents=True)
        snapshot_db(backup / "operational.sqlite3")
        manifest = {"commit": commit, "states": saved_states, "files": [], "old_facts": old_facts()}
        for index, (source, target, mode) in enumerate(targets):
            saved = backup / ("file-" + str(index)) if target.exists() else None
            if saved:
                shutil.copy2(target, saved)
            manifest["files"].append({"source": str(source), "target": str(target),
                                     "backup": str(saved) if saved else None,
                                     "before": sha(target), "after": sha(source), "mode": mode,
                                     "mode_before": target.stat().st_mode & 0o777 if target.exists() else mode})
        manifest_path = backup / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        subprocess.run(["systemctl", "stop", FB_UNIT], check=True)
        changed = True
        for source, target, mode in targets:
            atomic_bytes(source.read_bytes(), target, mode)
        owner = pwd.getpwnam("fb-auto-post")
        LINK_ROOT.mkdir(parents=True, exist_ok=True)
        os.chown(LINK_ROOT, owner.pw_uid, owner.pw_gid)
        LINK_ROOT.chmod(0o750)
        # Only the independent new namespace gains Nginx read access; inherited
        # named ACL remains constrained by each 0640 immutable file's mask.
        subprocess.run(["setfacl", "-m", "u:nginx:rx,d:u:nginx:rx,d:m:rx", str(LINK_ROOT)], check=True)
        Store(DB)
        require(old_facts() == manifest["old_facts"], "Incremental migration changed old publisher facts")
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        subprocess.run(["systemctl", "restart", FB_UNIT], check=True)
        health("http://127.0.0.1:18835/health")
        subprocess.run(["systemctl", "restart", API_UNIT], check=True)
        health("http://127.0.0.1:8787/api/ui/topbar")
        subprocess.run(["systemctl", "enable", "--now", *NEW_UNITS], check=True)
        for item in manifest["files"]:
            require(sha(Path(item["target"])) == item["after"], "Deployed hash mismatch")
        manifest["deployed"] = True
        manifest["migration_preserved_old_facts"] = True
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps({"deployed": True, "commit": commit, "manifest": str(manifest_path)}), flush=True)
    except Exception:
        if changed:
            print("Partial deployment retained with manifest " + str(manifest_path) + "; inspect and run --rollback", flush=True)
        raise
    finally:
        if running:
            subprocess.run(["systemctl", "start", *running], check=True)
        require(all(state(u)["active"] == saved_states[u]["active"] for u in OLD_TIMERS), "Old timer states changed")


if __name__ == "__main__":
    main()
