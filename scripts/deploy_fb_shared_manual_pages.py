"""Deploy the shared manual Page catalog without replacing unrelated live code."""
import argparse
import ast
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from types import MethodType, SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release
from scripts import deploy_fb_skipped_recheck as fb
from scripts.deploy_fb_profile_priority import owner_active
from scripts.deploy_mobo_upcoming import runtime_image
from scripts.deploy_pipeline_runtime import preserve_tree_owners

BASE = "21e10416c48c371054bfcaa43abbc95eded8daca"
MODULE = "features/fb_video_publish/sources.py"
TEST = "tests/test_fb_shared_manual_pages.py"
MAIN = Path("/root/drama_material_service")


def method(text):
    cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == "SourceRepository")
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_page_rows")
    return "".join(text.splitlines(keepends=True)[node.lineno - 1:node.end_lineno])


def overlay(path):
    baseline = subprocess.check_output(["git", "-C", str(SOURCE), "show", BASE + ":" + MODULE]).decode()
    desired = (SOURCE / MODULE).read_text()
    current = Path(path).read_text()
    before, after = method(baseline), method(desired)
    assert method(current) == before and current.count(before) == 1, "page_query_changed_since_review"
    result = current.replace(before, after, 1)
    compile(result, MODULE, "exec")
    return result.encode()


def prepare(folder):
    assert release.run("findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk") == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    assert shutil.disk_usage("/mnt/data-disk").free > 3 * 1024 ** 3
    commit = release.run("git", "-C", SOURCE, "rev-parse", "HEAD")
    assert not release.run("git", "-C", SOURCE, "status", "--porcelain")
    assert release.run("git", "-C", SOURCE, "branch", "-r", "--contains", commit)
    current, _, db = release.settings("fb")
    previous, candidate = current.resolve(), folder / "candidate"
    before_image = runtime_image(previous)
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    preserve_tree_owners(previous, candidate)
    info = dict(platform="fb", commit=commit, source=str(SOURCE), previous=str(previous),
                candidate=str(candidate), previous_image=before_image, files={}, static={}, status="prepared")
    release.atomic(candidate / MODULE, overlay(previous / MODULE))
    info["files"][MODULE] = dict(before=release.sha(previous / MODULE), after=release.sha(candidate / MODULE))
    # The API delegates Page reads to the sidecar; keep its source copy aligned
    # for future releases without restarting the unrelated main API owner.
    original, replacement = folder / "main-source.old", folder / "main-source.new"
    shutil.copy2(MAIN / MODULE, original)
    replacement.write_bytes(overlay(MAIN / MODULE))
    info["static"][str(MAIN / MODULE)] = dict(before=release.sha(original), after=release.sha(replacement),
                                            backup=str(original), next=str(replacement))
    release.atomic(candidate / TEST, (SOURCE / TEST).read_bytes())
    proof = subprocess.run([sys.executable, "-B", "-m", "unittest", "tests.test_fb_shared_manual_pages", "-v"],
                           cwd=candidate, capture_output=True, text=True, timeout=120)
    (folder / "tests.txt").write_text(proof.stdout + proof.stderr)
    assert proof.returncode == 0, "candidate_tests_failed:" + proof.stderr[-2000:]
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as src, \
            closing(sqlite3.connect(folder / "ledger-before.sqlite3")) as dst:
        src.backup(dst)
    (folder / "ledger-before.sqlite3").chmod(0o600)
    assert runtime_image(previous) == before_image, "concurrent_release_changed_restage"
    info["candidate_image"] = runtime_image(candidate)
    info["main_api_pid"] = fb.show("drama-material-api.service", "MainPID")
    release.save(folder / "manifest.json", info)
    print(json.dumps(dict(status="prepared", commit=commit, candidate=str(candidate))), flush=True)


def check_rollback_scope(info):
    """Protect only unfinished tasks which really need the new shared scope."""
    from features.fb_video_publish import sources
    from features.fb_video_publish.runtime_source import BrokerReadOnlyMySQL
    switched = info.get("shared_policy_activated_at", "")
    assert switched, "activation_time_missing"
    _, _, db = release.settings("fb")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT page_id,snapshot_json FROM fb_video_task WHERE created_at_utc>=? "
                            "AND status IN ('queued','running') AND source_kind IN ('manual','pool')", (switched,)).fetchall()
    if not rows:
        return
    pid = fb.show("fb-auto-post-service.service", "MainPID")
    env = dict(v.split("=", 1) for v in Path("/proc", pid, "environ").read_text().split("\0") if "=" in v)
    repo = sources.SourceRepository(None, SimpleNamespace(mysql=BrokerReadOnlyMySQL(env)))
    namespace = dict(vars(sources))
    prior = method((Path(info["previous"]) / MODULE).read_text())
    exec(compile(textwrap.dedent(prior), "previous_page_scope", "exec"), namespace)
    repo._page_rows = MethodType(namespace["_page_rows"], repo)
    checked = set()
    for page_id, snapshot in rows:
        actor = json.loads(snapshot)["actor"]
        key = (actor["tenant_key"], actor["user_id"], page_id)
        if key not in checked:
            assert repo.list_pages(actor, page_ids=[page_id]), "unfinished_task_requires_shared_page_scope"
            checked.add(key)


def activate(folder, rollback=False):
    import fcntl
    with (folder / "switch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        info = json.loads((folder / "manifest.json").read_text())
        for label in ("previous", "candidate"):
            assert runtime_image(Path(info[label])) == info[label + "_image"], "runtime_changed_restage:" + label
        release.verify_current(info, rollback)
        for path, record in info["static"].items():
            assert release.sha(path) == record["after" if rollback else "before"], "main_source_changed"
        if rollback:
            check_rollback_scope(info)
        baseline = fb.configuration()
        opened = "fb" in baseline["enabled_channels"]
        prep_only = "fb" in baseline.get("preparation_only_channels", [])
        original_active = fb.native_active
        original_timers = fb.TIMERS
        fb.native_active = owner_active
        fb.TIMERS = [unit for unit in original_timers if unit not in {
            "fb-auto-post-effects.timer", "fb-auto-post-metric-hourly.timer", "fb-auto-post-metric-repair.timer"}]
        active = owner_active()
        hold = bool(active and opened and not prep_only)
        audit = dict(enabled=opened, preparation_only=prep_only, owner_at_start=active,
                     started_at=datetime.now(timezone.utc).isoformat(), status="draining")
        receipt = folder / ("rollback-admission.json" if rollback else "apply-admission.json")
        release.save(receipt, audit)

        def interrupted(_signum, _frame):
            raise SystemExit("switch interrupted; restoring admission")

        signal.signal(signal.SIGTERM, interrupted)
        try:
            if hold:
                fb.fb_enabled(True, True)
            last = None
            while active:
                if last != active:
                    print("Waiting for", active, "FB owner operations", flush=True)
                    audit["owner_active"] = active
                    release.save(receipt, audit)
                    last = active
                time.sleep(2)
                active = owner_active()
            if not rollback:
                info["shared_policy_activated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                release.save(folder / "manifest.json", info)
            release.activate("fb", folder, rollback, defer_plan_read=False)
        finally:
            if hold:
                fb.fb_enabled(True, False)
            fb.native_active = original_active
            fb.TIMERS = original_timers
            after = fb.configuration()
            audit.update(status="finished", ended_at=datetime.now(timezone.utc).isoformat(),
                         final_enabled="fb" in after["enabled_channels"],
                         final_preparation_only="fb" in after.get("preparation_only_channels", []))
            release.save(receipt, audit)
        release.health(18835)
        destination = Path(info["previous" if rollback else "candidate"])
        pid = fb.show("fb-auto-post-service.service", "MainPID")
        assert Path("/opt/fb-auto-post/current").resolve() == destination
        assert Path("/proc", pid, "cwd").resolve() == destination
        print(json.dumps(dict(status="verified", main_api_pid_unchanged=
                              fb.show("drama-material-api.service", "MainPID") == info["main_api_pid"])), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply", "rollback"))
    parser.add_argument("--folder", required=True, type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to("/mnt/data-disk")
    if args.action == "prepare":
        prepare(folder)
    else:
        activate(folder, args.action == "rollback")
