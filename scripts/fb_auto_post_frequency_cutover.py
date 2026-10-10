#!/usr/bin/env python3
"""One-shot, date-bounded template 1 cutover. Never invokes a publish route.

--preflight reads production state and writes evidence only. --apply is permitted
only on 2026-10-10 22:30 <= Beijing time < 23:10. An owned durable receipt
permits recovery until 23:50 that same day, without blindly replaying a POST.
"""
from __future__ import annotations

import argparse
import collections
import copy
import json
import inspect
import os
import pathlib
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.core import FBAutoPostStore
from features.fb_auto_posts.strategy import daily_limit, in_slot
from features.fb_auto_posts.validation import config_hash, normalize_template_payload

BEIJING = timezone(timedelta(hours=8))
UTC = timezone.utc
DAY = "2026-10-10"
EFFECTIVE_DAY = "2026-10-11"
EXPECTED_VERSION = 6
EXPECTED_HASH = "6203dea56c0f776340cf8617d1584120c6a6afa8eac004382c5dcf9f821492ca"
MOUNT = pathlib.Path("/mnt/data-disk")
MOUNT_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
DB = MOUNT / "fb-auto-post-publisher/fb-auto-post.sqlite3"
STATE_ROOT = MOUNT / "fb-auto-post-deploy/frequency-random-20261010"
BASE = "http://127.0.0.1:18835/api/admin/fb-auto-publish/templates/1"
ACTOR = dict(user_id="codex-frequency-random-20261010", name="Codex random frequency cutover", is_admin=True, owner_user_id="248")
OLD_SCHEDULE = {"mode": "fixed", "times": ["09:15", "12:15", "15:15", "18:15", "21:15"]}
NEW_SCHEDULE = {"mode": "random", "daily_count": 2, "start": "09:15", "end": "21:55"}
ACTIVE = ("planned", "preparing", "ready", "running", "queued")
PRESERVE_BEFORE = "2026-10-10T16:00:00+00:00"


class CutoverError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CutoverError(message)


def now_utc():
    return datetime.now(UTC)


def check_time(at, recovery=False):
    require(at.tzinfo is not None, "Timezone-aware time required")
    local = at.astimezone(BEIJING)
    start = datetime(2026, 10, 10, 22, 30, tzinfo=BEIJING)
    end = datetime(2026, 10, 10, 23, 50 if recovery else 10, tzinfo=BEIJING)
    require(start <= local < end, "2026-10-10 Beijing cutover window closed (new start before 23:10; owned recovery before 23:50)")


def check_guard_capability():
    require("preserve_unsubmitted_before_utc" in inspect.signature(FBAutoPostStore.set_template_status).parameters, "Runtime lacks transactional disable guard")


def raw_payload(config):
    result = copy.deepcopy(config)
    result.pop("material_type", None)
    return result


def make_candidate(before):
    require(before.get("id") == 1 and before.get("version") == EXPECTED_VERSION, "Template ID/version drift")
    config = before["config"]
    require(before.get("config_sha256") == EXPECTED_HASH == config_hash(config), "Template config hash drift")
    require(config.get("group_ids") == ["62"] and config.get("app_id") == "1479", "Template source drift")
    require(config.get("schedule") == OLD_SCHEDULE and config.get("stagger_minutes") == 40, "Old schedule drift")
    require(config.get("default_daily_count") == 0, "Unlisted Page default drift")
    require(collections.Counter(p["daily_count"] for p in config.get("page_daily_limits", [])) == {2: 144, 0: 1}, "Page frequency allocation drift")
    candidate = copy.deepcopy(config)
    candidate["schedule"] = copy.deepcopy(NEW_SCHEDULE)
    candidate["stagger_minutes"] = 0
    require(normalize_template_payload(raw_payload(candidate)) == candidate, "Candidate normalization changed other fields")
    require({k for k in config if config[k] != candidate[k]} == {"schedule", "stagger_minutes"}, "Unexpected candidate changes")
    return candidate


def schedule_proof(candidate):
    """Exercise the real scheduler only against a disposable, isolated store."""
    with tempfile.TemporaryDirectory(prefix="fb-frequency-proof-") as directory:
        store = FBAutoPostStore(pathlib.Path(directory) / "proof.sqlite3", rng=random.Random(20261010))
        examples = []
        for offset in range(14):
            day = (datetime(2026, 10, 11) + timedelta(days=offset)).date().isoformat()
            times = store.schedule_times(1, EXPECTED_VERSION + 1, candidate, day)
            require(times == store.schedule_times(1, EXPECTED_VERSION + 1, candidate, day), "Random plan was not stable")
            minute_values = [int(t[:2]) * 60 + int(t[3:]) for t in times]
            require(len(times) == 2 and minute_values[1] - minute_values[0] >= 60, "Invalid random schedule count or gap")
            require(all("09:15" <= t <= "21:55" and not t.endswith(":00") for t in times), "Random schedule outside window")
            counts = collections.Counter()
            for row in candidate["page_daily_limits"]:
                chosen = [t for t in times if in_slot(candidate, row["page_id"], "auto:v7:" + day + ":" + t, times=times)]
                require(len(chosen) == row["daily_count"] == daily_limit(candidate, row["page_id"]), "Page daily count mismatch")
                counts[str(len(chosen))] += 1
            require(dict(counts) == {"2": 144, "0": 1}, "Unexpected Page capacity")
            examples.append({"date": day, "times": times})
        require(daily_limit(candidate, "999999999999999999") == 0, "Unlisted Page unexpectedly enabled")
    return {"isolated_simulation": True, "production_schedule_plan_written": False, "page_daily_counts": dict(counts), "nominal_daily_posts": 288, "examples": examples}


@contextmanager
def read_db(path=DB):
    conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def queue_blockers(conn):
    tomorrow = datetime(2026, 10, 11, tzinfo=BEIJING).astimezone(UTC).isoformat(timespec="seconds")
    tasks = [dict(r) for r in conn.execute("""
        SELECT x.id,x.template_id,x.status,x.planned_publish_at_utc,r.trigger_type
        FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id
        WHERE x.status IN ('preparing','running') OR
          (x.template_id=1 AND x.status IN ('planned','preparing','ready','running','queued')
           AND (r.trigger_type='manual' OR x.planned_publish_at_utc='' OR x.planned_publish_at_utc<?))
        ORDER BY x.id
    """, (tomorrow,))]
    due = [dict(r) for r in conn.execute("""
        SELECT id,status,trigger_type,planned_publish_at_utc FROM fb_auto_due_slot
        WHERE template_id=1 AND (status='preparing' OR
          (trigger_type='manual' AND status='pending')) ORDER BY id
    """)]
    return {"tasks": tasks, "due_slots": due}


def assert_queue_safe(conn):
    blockers = queue_blockers(conn)
    require(not blockers["tasks"] and not blockers["due_slots"], "Unfinished today/manual or preparing/running work blocks cutover: " + json.dumps(blockers, sort_keys=True))


def snapshot(conn):
    """Frozen identities; reconciliation may advance status and fill remote IDs."""
    today_start = datetime(2026, 10, 10, tzinfo=BEIJING).astimezone(UTC).isoformat(timespec="seconds")
    today_end = datetime(2026, 10, 11, tzinfo=BEIJING).astimezone(UTC).isoformat(timespec="seconds")
    def rows(sql, params=()):
        return [dict(row) for row in conn.execute(sql, params)]
    return {
        "tasks": rows("SELECT id,run_id,template_id,template_version,page_id,group_id,material_id,content_id,planned_publish_at_utc,created_at_utc,graph_post_id FROM fb_auto_task ORDER BY id"),
        "runs": rows("SELECT id,template_id,template_version,slot_key,trigger_type,config_json,created_at_utc FROM fb_auto_run ORDER BY id"),
        "ledger": rows("SELECT task_id,page_id,material_id,created_at_utc,graph_post_id FROM fb_auto_publish_ledger ORDER BY task_id"),
        "attempts": rows("SELECT * FROM fb_auto_publish_attempt ORDER BY id"),
        "today_exact": rows("SELECT * FROM fb_auto_task WHERE template_id=1 AND planned_publish_at_utc>=? AND planned_publish_at_utc<? AND status NOT IN ('submitted','unknown') ORDER BY id", (today_start, today_end)),
    }


def verify_snapshot(before, conn):
    current = snapshot(conn)
    for name in ("tasks", "runs", "ledger", "attempts"):
        key = "task_id" if name == "ledger" else "id"
        indexed = {row[key]: row for row in current[name]}
        for row in before[name]:
            require(row[key] in indexed, name + " record deleted")
            after = indexed[row[key]]
            for field, value in row.items():
                if field == "graph_post_id" and not value:
                    continue
                require(after[field] == value, name + " protected field changed: " + field)
    for row in before["today_exact"]:
        found = conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (row["id"],)).fetchone()
        require(found is not None and dict(found) == row, "Existing non-reconciling current-day task changed")
    return {"identities_preserved": True, "attempts_preserved": True, "today_non_reconciling_tasks_unchanged": len(before["today_exact"]), "reconciliation_status_updates_allowed": True}


def durable_json(path, value):
    path = pathlib.Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temp), str(path))
    if os.name == "posix":
        fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def verify_mount():
    actual = subprocess.check_output(["findmnt", "-n", "-o", "UUID", "--mountpoint", str(MOUNT)], text=True).strip()
    require(actual == MOUNT_UUID, "Data disk mount UUID mismatch")
    for path in (DB, STATE_ROOT):
        require(MOUNT.resolve() in path.resolve().parents, "Path escaped the mounted data disk")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CutoverError("Internal API redirect refused")


class API:
    def __init__(self):
        pid = subprocess.check_output(["systemctl", "show", "fb-auto-post-service.service", "--property=MainPID", "--value"], text=True).strip()
        require(pid.isdigit() and int(pid) > 1, "Service PID unavailable")
        pairs = pathlib.Path("/proc", pid, "environ").read_bytes().split(b"\0")
        values = dict(item.split(b"=", 1) for item in pairs if b"=" in item)
        self._token = values[b"FB_AUTO_POST_INTERNAL_TOKEN"].decode()
        self._opener = urllib.request.build_opener(NoRedirect())

    def __call__(self, suffix="", payload=None):
        require(suffix in ("", "/enable", "/disable"), "Unsupported management route")
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}
        data = None
        if payload is None:
            headers["X-FB-Auto-Actor"] = json.dumps(ACTOR, ensure_ascii=True)
        else:
            data = json.dumps(dict(payload, _actor=ACTOR), ensure_ascii=False).encode("utf-8")
        try:
            with self._opener.open(urllib.request.Request(BASE + suffix, data=data, headers=headers), timeout=120) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise CutoverError("Management API returned HTTP " + str(exc.code)) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise CutoverError("Management API response unavailable; readback required") from None
        require(result.get("ok") is True, "Management API did not confirm success")
        return result


def template_matches(template, version, config, status):
    return (template.get("id") == 1 and template.get("version") == version
            and template.get("config") == config and template.get("config_sha256") == config_hash(config)
            and template.get("status") == status)


def frozen_schedule_readback(db_path, wait_seconds=0):
    deadline = time.monotonic() + min(50, max(0, wait_seconds))
    while True:
        with read_db(db_path) as conn:
            plan = conn.execute("SELECT times_json FROM fb_auto_schedule_plan WHERE template_id=1 AND template_version=? AND local_date=?", (EXPECTED_VERSION + 1, EFFECTIVE_DAY)).fetchone()
            due = [dict(row) for row in conn.execute("SELECT slot_key,planned_publish_at_utc,status FROM fb_auto_due_slot WHERE template_id=1 AND template_version=? AND trigger_type='auto' AND slot_key LIKE ? ORDER BY planned_publish_at_utc", (EXPECTED_VERSION + 1, "auto:v7:" + EFFECTIVE_DAY + ":%"))]
        require(len(due) <= 2, "Too many frozen next-day due slots")
        if plan and len(due) == 2:
            times = json.loads(plan[0])
            require(len(times) == 2 and len(set(times)) == 2, "Frozen random schedule count differs")
            minutes = sorted(int(t[:2]) * 60 + int(t[3:]) for t in times)
            require(all("09:15" <= t <= "21:55" and not t.endswith(":00") for t in times) and minutes[1] - minutes[0] >= 60, "Frozen random times violate window/gap")
            require(sorted(row["slot_key"][-5:] for row in due) == sorted(times), "Frozen schedule/due slots differ")
            require(all(datetime.fromisoformat(row["planned_publish_at_utc"]).astimezone(BEIJING).strftime("%Y-%m-%d:%H:%M") == EFFECTIVE_DAY + ":" + row["slot_key"][-5:] for row in due), "Frozen due timestamp differs")
            return {"status": "frozen", "date": EFFECTIVE_DAY, "times": times, "due_slots": due}
        if time.monotonic() >= deadline:
            return {"status": "awaiting_natural_scheduler", "date": EFFECTIVE_DAY, "plan_present": bool(plan), "due_slot_count": len(due)}
        time.sleep(max(0, min(5, deadline - time.monotonic())))


def apply_cutover(api, db_path, state_root, now_fn=now_utc, schedule_wait_seconds=0):
    """All production changes go through the supported management API."""
    state_root = pathlib.Path(state_root)
    receipt_path = state_root / "receipt.json"
    check_time(now_fn(), recovery=receipt_path.exists())
    check_guard_capability()
    state_root.mkdir(parents=True, exist_ok=True)
    current = api()["template"]
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        require(receipt.get("operation") == ACTOR["user_id"] and receipt.get("day") == DAY, "Receipt ownership mismatch")
        before = receipt["before"]
        candidate = make_candidate(before)
        require(receipt.get("candidate") == candidate, "Receipt candidate drift")
        require((state_root / "publisher-before.sqlite3").is_file(), "Receipt backup missing")
    else:
        candidate = make_candidate(current)
        require(current["status"] == "enabled", "Cannot adopt an independently disabled template")
        with read_db(db_path) as conn:
            assert_queue_safe(conn)
            backup_path = state_root / "publisher-before.sqlite3"
            require(not backup_path.exists(), "Orphan backup requires inspection")
            with closing(sqlite3.connect(str(backup_path))) as target:
                conn.backup(target, pages=1024)
        with read_db(backup_path) as conn:
            protected = snapshot(conn)
        before = current
        receipt = {"operation": ACTOR["user_id"], "day": DAY, "phase": "prepared", "before": before, "candidate": candidate, "protected": protected, "proof": schedule_proof(candidate)}
        durable_json(receipt_path, receipt)

    def persist(phase):
        receipt["phase"] = phase
        receipt["updated_at_utc"] = now_fn().astimezone(UTC).isoformat(timespec="seconds")
        durable_json(receipt_path, receipt)

    def mutate(suffix, payload, phase, wanted_version, wanted_config, wanted_status):
        # A prepared receipt alone must never extend the first disable window.
        check_time(now_fn(), recovery=phase != "disable")
        require(api()["template"] == current, "Template changed before management write")
        with read_db(db_path) as conn:
            assert_queue_safe(conn)
        persist(phase + "_requested")
        response_failed = False
        try:
            api(suffix, payload)
        except Exception:
            response_failed = True
        # An ambiguous POST is never repeated here. Readback proves its result.
        observed = api()["template"]
        require(template_matches(observed, wanted_version, wanted_config, wanted_status), "Operation " + phase + " not confirmed by readback" + (" after ambiguous response" if response_failed else ""))
        persist({"disable": "disabled", "save": "saved", "enable": "enabled"}[phase])
        return observed

    old, new = EXPECTED_VERSION, EXPECTED_VERSION + 1
    phase = receipt["phase"]
    if template_matches(current, new, candidate, "enabled"):
        require(phase in ("save_requested", "saved", "enable_requested", "enabled", "complete"), "Unowned candidate enable")
    else:
        require(phase != "complete", "Completed cutover subsequently changed; refusing recovery")
        if template_matches(current, old, before["config"], "enabled"):
            require(phase in ("prepared", "disable_requested"), "Old template unexpectedly re-enabled")
            current = mutate("/disable", {"expected_version": old, "preserve_unsubmitted_before_utc": PRESERVE_BEFORE}, "disable", old, before["config"], "disabled")
        if template_matches(current, old, before["config"], "disabled"):
            require(receipt["phase"] in ("disable_requested", "disabled", "save_requested"), "No owned disable receipt; refusing recovery")
            payload = raw_payload(candidate)
            payload["expected_version"] = old
            current = mutate("", payload, "save", new, candidate, "disabled")
        if template_matches(current, new, candidate, "disabled"):
            require(receipt["phase"] in ("save_requested", "saved", "enable_requested"), "No owned save receipt; refusing recovery")
            current = mutate("/enable", {"expected_version": new}, "enable", new, candidate, "enabled")
        require(template_matches(current, new, candidate, "enabled"), "Concurrent template/config/status drift; no further writes")
    with read_db(db_path) as conn:
        audit = verify_snapshot(receipt["protected"], conn)
    persist("complete")
    schedule_readback = frozen_schedule_readback(db_path, schedule_wait_seconds)
    result = {"ok": True, "template_id": 1, "version": new, "status": current["status"], "config_sha256": current["config_sha256"], "schedule": current["config"]["schedule"], "stagger_minutes": 0, "effective_beijing_date": EFFECTIVE_DAY, "audit": audit, "proof": receipt["proof"], "production_schedule_readback": schedule_readback, "backup": str(state_root / "publisher-before.sqlite3")}
    durable_json(state_root / "acceptance.json", result)
    return result


def preflight(api, db_path, state_root):
    check_guard_capability()
    current = api()["template"]
    candidate = make_candidate(current)
    require(current["status"] == "enabled", "Preflight expects the original enabled template")
    with read_db(db_path) as conn:
        blockers = queue_blockers(conn)
    result = {"ok": True, "mode": "preflight", "production_state_written": False, "version": current["version"], "config_sha256": current["config_sha256"], "candidate_config_sha256": config_hash(candidate), "changed_fields": ["schedule", "stagger_minutes"], "candidate_schedule": candidate["schedule"], "blockers_now": blockers, "apply_window_beijing": "2026-10-10 22:30 <= new start < 23:10; owned recovery < 23:50", "proof": schedule_proof(candidate)}
    pathlib.Path(state_root).mkdir(parents=True, exist_ok=True)
    durable_json(pathlib.Path(state_root) / "preflight.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    verify_mount()
    if args.apply:
        check_time(now_utc(), recovery=(STATE_ROOT / "receipt.json").exists())
    api = API()
    # Serialize this operation's receipts, never the publisher's own lock.
    import fcntl
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with (STATE_ROOT / "operation.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = preflight(api, DB, STATE_ROOT) if args.preflight else apply_cutover(api, DB, STATE_ROOT, schedule_wait_seconds=50)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except CutoverError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
