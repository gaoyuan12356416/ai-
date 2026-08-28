"""Audited operator command invoking the GitHub-deployed recovery APIs only."""
import contextlib
import base64
import datetime
import gzip
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

if not __debug__:
    raise RuntimeError("Recovery guard assertions must not be disabled")

COMMIT = "e300542887fb89314bef145b752c3ad8aa6c5c9c"
BASE = pathlib.Path("/mnt/data-disk/x-post-automation/backups/20260828-pool-blockers-155312")
RELEASE = pathlib.Path("/mnt/data-disk/x-post-automation/releases") / COMMIT
LIVE = pathlib.Path("/var/lib/x-post-automation/accounts.sqlite3")
PHASE = sys.argv[1]
INDEX_SHA = sys.argv[2]
assert PHASE in {"copy", "live"}
assert len(INDEX_SHA) == 64 and all(c in "0123456789abcdef" for c in INDEX_SHA)
OPERATOR_COMMIT = pathlib.Path(__file__).resolve().parents[2].name
assert len(OPERATOR_COMMIT) == 40 and all(c in "0123456789abcdef" for c in OPERATOR_COMMIT)
OPERATOR_SHA = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
assert pathlib.Path("/opt/x-post-automation/current").resolve() == RELEASE
os.umask(0o077)
sys.path.insert(0, str(RELEASE))
sys.path.insert(0, str(RELEASE / "deploy/recovery"))
from x_post_incident_preflight_20260828 import IncidentPreflight, _encoded, _write_private
from scripts.x_post_bound_drama_media_recovery import execute_recovery
from scripts.x_post_daily_runner import process_lock
from scripts.x_post_drama_media_repair_backfill import load_drama_environment_files
from scripts.x_post_media_repair_backfill import configured_environment
from scripts.x_post_schedule_runner import ScheduleConfig
from features.x_posts.service import XPostStore, BOUND_DRAMA_FAILED_MEDIA_RECOVERY_REASON

ACTOR = "codex_20260828_verified_original_drama"

TABLES = {
    "queues": "x_post_queue", "logs": "x_post_publish_log",
    "relays": "x_post_repost_ledger", "pools": "x_post_drama_pool",
    "runs": "x_post_schedule_run",
    "audit": "x_post_schedule_bound_drama_failed_media_recovery_audit",
}
ALLOWED = {
    "queues": {"material_url", "original_material_url", "media_repair_trigger_code",
               "media_repair_job_key", "media_repair_profile", "media_repair_source_sha256",
               "media_validation_mode", "preflight_sha256", "preflight_size",
               "preflight_duration", "status", "updated_at"},
    "logs": {"status", "error_code", "error_message", "unknown_outcome", "updated_at"},
    "relays": {"status", "error_code", "error_message", "unknown_outcome", "updated_at"},
    "pools": {"status", "last_checked_at", "last_error_code", "last_error_message", "updated_at"},
    "runs": {"status", "error_code", "error_message", "finished_at", "lease_heartbeat_at",
             "updated_at", "failed_count", "unknown_count"},
}
TIMERS = ["x-post-schedule.timer", "x-post-schedule-claim.timer", "x-post-manual.timer"]
SERVICES = ["x-post-schedule.service", "x-post-schedule-claim.service", "x-post-manual.service"]
target = BASE / "rehearsal.sqlite3" if PHASE == "copy" else LIVE
result_path = BASE / (PHASE + "-recovery-result.json")
assert not result_path.exists(), "Phase already has a result; inspect, never repeat automatically"
result = {"phase": PHASE, "commit": COMMIT, "checkpoint_index_sha256": INDEX_SHA,
          "operator_commit": OPERATOR_COMMIT, "operator_sha256": OPERATOR_SHA,
          "db_path": str(target), "validated_runs": [], "applied_runs": [],
          "x_write_attempted": False, "status": "started"}


def active(unit):
    return subprocess.check_output(["systemctl", "show", unit, "-p", "ActiveState", "--value"],
                                   universal_newlines=True).strip()


def snapshot(path):
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        return {key: [dict(row) for row in conn.execute("SELECT * FROM %s ORDER BY id" % table)]
                for key, table in TABLES.items()}


def write_snapshot(path, document):
    raw = _encoded(document)
    payload = gzip.compress(raw, mtime=0)
    _write_private(path, {"encoding": "gzip+base64", "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
                         "uncompressed_bytes": len(raw), "row_counts": {key: len(rows) for key, rows in document.items()},
                         "payload": base64.b64encode(payload).decode("ascii")})


def backup(path, destination):
    assert not destination.exists()
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as src:
        with contextlib.closing(sqlite3.connect(str(destination))) as dst:
            src.backup(dst)
            assert dst.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert not dst.execute("PRAGMA foreign_key_check").fetchall()
    os.chmod(str(destination), 0o600)


def forbidden_io(*args, **kwargs):
    raise RuntimeError("Cached-only apply attempted media or repair IO")


class NoRepair:
    def __getattr__(self, name):
        return forbidden_io


def verify_changes(before, after, factories):
    proof = {key: value for factory in factories.values() for key, value in factory.items.items()}
    frozen = factories[348].frozen
    ids = {"queues": set(proof), "logs": {r["id"] for r in frozen["logs"]},
           "relays": {r["id"] for r in frozen["relays"]},
           "pools": {r["id"] for r in frozen["pools"]}, "runs": {348, 350}}
    assert set(proof) == set(range(635, 648)) | {667, 668, 669}
    for key in ALLOWED:
        previous = {row["id"]: row for row in before[key]}
        current = {row["id"]: row for row in after[key]}
        assert set(previous) == set(current), "Row count or identity changed: " + key
        for row_id, old in previous.items():
            new = current[row_id]
            if row_id not in ids[key]:
                assert old == new, "Unrelated row changed: %s/%s" % (key, row_id)
                continue
            assert set(old) == set(new)
            assert all(old[field] == new[field] for field in old if field not in ALLOWED[key]), \
                "Frozen field changed: %s/%s" % (key, row_id)
            if key == "queues":
                item = proof[row_id]
                assert new["status"] == "queued" and new["media_validation_mode"] == "preflight"
                assert new["original_material_url"] == old["material_url"]
                for field in ALLOWED[key] - {"status", "updated_at", "media_validation_mode"}:
                    assert new[field] == item[field], "Media proof mismatch: " + field
            elif key in {"logs", "relays"}:
                assert new["status"] == "reserved" and new["unknown_outcome"] == 0
                assert not new["error_code"] and not new["error_message"]
                if key == "logs":
                    assert new["attempt_count"] == 0
                    assert not any(new[k] for k in ("x_media_id", "x_post_id", "x_post_url", "started_at", "published_at"))
                else:
                    assert new["source_attempt_count"] == new["repost_attempt_count"] == 0
                    assert not any(new[k] for k in ("source_post_id", "source_post_url", "repost_id", "source_published_at", "reposted_at"))
            elif key == "pools":
                assert new["status"] == "active" and not new["last_error_code"] and not new["last_error_message"]
            elif key == "runs":
                assert new["status"] == "running" and new["failed_count"] == new["unknown_count"] == 0
                assert not new["error_code"] and not new["error_message"] and not new["finished_at"]
    old_audit = {row["id"]: row for row in before["audit"]}
    new_audit = {row["id"]: row for row in after["audit"]}
    assert all(new_audit[key] == row for key, row in old_audit.items())
    delta = [row for key, row in new_audit.items() if key not in old_audit]
    assert len(delta) == 16 and {row["queue_id"] for row in delta} == set(proof)
    old_queues = {row["id"]: row for row in before["queues"]}
    new_queues = {row["id"]: row for row in after["queues"]}
    old_logs = {row["queue_id"]: row for row in before["logs"]}
    old_pools = {row["id"]: row for row in before["pools"]}
    old_runs = {row["id"]: row for row in before["runs"]}
    relay_queues = {row["queue_id"] for row in before["relays"]}
    for row in delta:
        queued, updated = old_queues[row["queue_id"]], new_queues[row["queue_id"]]
        logged = old_logs[queued["id"]]
        pool, run = old_pools[queued["drama_pool_item_id"]], old_runs[queued["schedule_run_id"]]
        expected = {
            "schedule_run_id": queued["schedule_run_id"], "queue_id": queued["id"],
            "drama_pool_item_id": queued["drama_pool_item_id"], "content_id": queued["content_id"],
            "episode_number": queued["episode_number"], "replay_generation": queued["drama_replay_generation"],
            "account_id": queued["account_id"], "assigned_source_queue_id": pool["assigned_source_queue_id"],
            "recovery_reason": BOUND_DRAMA_FAILED_MEDIA_RECOVERY_REASON, "actor": ACTOR,
            "deployed_commit": COMMIT, "previous_run_status": run["status"],
            "previous_queue_status": queued["status"], "previous_log_status": logged["status"],
            "previous_pool_status": pool["status"], "previous_error_code": logged["error_code"],
            "previous_material_url": queued["material_url"], "final_material_url": updated["material_url"],
            "validated_relay_count": int(queued["id"] in relay_queues), "created_at": updated["updated_at"],
        }
        for field in ("preflight_sha256", "preflight_size", "preflight_duration", "media_repair_trigger_code",
                      "media_repair_job_key", "media_repair_profile", "media_repair_source_sha256"):
            expected[field] = updated[field]
        assert set(row) == set(expected) | {"id"}, "Unexpected recovery audit schema"
        assert all(row[field] == value for field, value in expected.items()), \
            "Recovery audit identity or proof mismatch: %s" % queued["id"]
    return {"queue_count": 16, "log_count": 16, "relay_count": 11, "audit_added": 16,
            "all_unrelated_rows_unchanged": True, "frozen_identity_unchanged": True,
            "zero_x_attempts": True, "sqlite_quick_check": "ok", "foreign_key_violations": 0}


timers_before = None
paused = False
completed = False
apply_entered = False
timer_errors = {}
try:
    assert hashlib.sha256((BASE / "cpu-verified/index.json").read_bytes()).hexdigest() == INDEX_SHA
    for run_id, count in ((348, 13), (350, 3)):
        prepared = json.loads((BASE / ("cpu-verified/prepare-%s-report.json" % run_id)).read_text())
        assert prepared["status"] == "validated" and prepared["prepared_count"] == count
        assert not prepared["x_write_attempted"]
    if PHASE == "live":
        copied = json.loads((BASE / "copy-recovery-result.json").read_text())
        assert copied["status"] == "verified" and copied["applied_runs"] == [348, 350]
        assert copied["checkpoint_index_sha256"] == INDEX_SHA and copied["commit"] == COMMIT
        timers_before = {timer: active(timer) for timer in TIMERS}
        assert all(state == "active" for state in timers_before.values()), "Timer state drift"
        paused = True
        subprocess.run(["systemctl", "stop"] + TIMERS, check=True, timeout=20)
        for attempt in range(20):
            if all(active(service) not in {"active", "activating", "deactivating"} for service in SERVICES):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Consumers have not drained; no forced stop")
    with configured_environment(load_drama_environment_files()):
        config = ScheduleConfig.from_env()
        assert str(config.lock_path) == "/run/x-post-daily/runner.lock"
        with process_lock(config.lock_path) as held:
            assert held is not None, "Original process lock busy"

            @contextlib.contextmanager
            def borrowed_lock(path):
                assert str(path) == str(config.lock_path) and not held.closed
                yield held

            factories = {run_id: IncidentPreflight(BASE, run_id, deployed_commit=COMMIT,
                         reuse_only=True, expected_index_sha256=INDEX_SHA, db_path=target)
                         for run_id in (348, 350)}
            for factory in factories.values():
                factory.assert_database()
            before = snapshot(target)
            assert not any(row["status"] in {"media_uploading", "post_creating", "repost_creating"}
                           for row in before["logs"]), "X writes are in flight"
            backup(target, BASE / (PHASE + ".before-apply.sqlite3"))
            write_snapshot(BASE / (PHASE + "-ledger-before.json"), before)
            store = XPostStore(target)

            def execute(run_id, apply):
                factory = factories[run_id]
                factory.assert_database()
                return execute_recovery(config, target, factory.manifest, deployed_commit=COMMIT,
                    actor=ACTOR, apply=apply, store=store,
                    preflight_candidate=factory, lock_factory=borrowed_lock,
                    downloader=forbidden_io, prober=forbidden_io, repair_client=NoRepair())

            for run_id in (348, 350):
                validated = execute(run_id, False)
                assert validated["status"] == "validated" and not validated["x_write_attempted"]
                result["validated_runs"].append(run_id)
            assert snapshot(target) == before, "Validation changed the ledger"
            for run_id in (348, 350):
                apply_entered = True
                result["apply_entered_run"] = run_id
                _write_private(result_path, result)
                applied = execute(run_id, True)
                assert applied["status"] == "applied" and not applied["x_write_attempted"]
                result["applied_runs"].append(run_id)
                _write_private(BASE / ("%s-%s-apply.json" % (PHASE, run_id)), factories[run_id].report(applied))
                _write_private(result_path, result)
                print(json.dumps({"phase": PHASE, "run_id": run_id, "status": "applied",
                                  "updated_count": applied["updated_count"], "x_write_attempted": False}), flush=True)
            after = snapshot(target)
            write_snapshot(BASE / (PHASE + "-ledger-after.json"), after)
            result["verification"] = verify_changes(before, after, factories)
            result["status"] = "verified"
            completed = True
except Exception as exc:
    result["status"] = "failed_requires_inspection"
    result["error_type"] = type(exc).__name__
    code = str(getattr(exc, "code", "operator_recovery_guard_failed"))
    result["error_code"] = code if all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in code) else "operator_recovery_guard_failed"
    print(json.dumps({"status": result["status"], "error_type": result["error_type"],
                      "error_code": result["error_code"], "applied_runs": result["applied_runs"]}), flush=True)
finally:
    if paused and (completed or not apply_entered):
        try:
            subprocess.run(["systemctl", "start"] + [timer for timer, state in timers_before.items() if state == "active"],
                           check=True, timeout=20)
        except Exception as exc:
            timer_errors["restore"] = type(exc).__name__
    if timers_before is not None:
        result["timers_before"] = timers_before
        result["timers_after"] = {}
        for timer in TIMERS:
            try:
                result["timers_after"][timer] = active(timer)
                if paused and (completed or not apply_entered):
                    if result["timers_after"][timer] != timers_before[timer]:
                        timer_errors[timer] = "state_not_restored"
            except Exception as exc:
                timer_errors[timer] = type(exc).__name__
                result["timers_after"][timer] = "query_failed"
    result["database_verified"] = completed
    if timer_errors:
        result["timer_errors"] = timer_errors
        if completed:
            result["status"] = "verified_timer_restore_failed"
    result["timestamp"] = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        _write_private(result_path, result)
    finally:
        print(json.dumps(result, sort_keys=True), flush=True)
sys.exit(0 if completed and not timer_errors else 2)
