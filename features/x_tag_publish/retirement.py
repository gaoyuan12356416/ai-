"""Offline retirement of the ordinary lane; never calls a publishing API.

Dry runs use SQLite read-only connections. Apply is deliberately staged: first
fence ordinary work, then recheck both lanes under database locks and copy pool
rows. A crash between databases is safe to resume with the same manifest. No
queue binding, confirmed receipt, attribution, or material reservation is erased.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
CODE = "x_ordinary_lane_retired"
MESSAGE = "普通 X 自动发布已退役；未发布任务停止，历史与未知回执保留"
POST_TABLES = ("x_post_material_pool", "x_post_queue", "x_post_publish_log",
               "x_post_repost_ledger", "x_post_manual_run", "x_post_schedule_run",
               "x_post_schedule_config", "x_post_manual_material_reservation")
AUTO_TABLES = ("x_auto_template", "x_auto_run", "x_auto_task", "x_auto_material_ledger")
ACTIVE_TASKS = frozenset(("pending", "selecting", "reserved", "preparing", "retry_wait",
                          "ready", "publishing", "reconciling", "unknown"))
FINAL_TASKS = frozenset(("published", "failed", "canceled", "skipped", "no_candidate"))
AUDIT = "x_profile_retirement_audit"
MIGRATIONS = "x_profile_pool_migration"


class RetirementError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(path, *, writable=False):
    p = Path(path).resolve(strict=True)
    db = sqlite3.connect(p.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                         uri=True, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    if not writable:
        db.execute("PRAGMA query_only=ON")
    return db


def normalize_paths(paths):
    keys = ("ordinary_post", "ordinary_auto", "profile_post", "profile_auto")
    if set(paths) != set(keys):
        raise RetirementError("four_lane_database_paths_required")
    result = {k: str(Path(paths[k]).resolve(strict=True)) for k in keys}
    if len(set(os.path.normcase(v) for v in result.values())) != 4:
        raise RetirementError("lane_database_paths_must_be_distinct")
    return result


def table_rows(db, table):
    # Table names are only internal constants, never user supplied SQL.
    return [dict(r) for r in db.execute('SELECT * FROM "%s"' % table)]


def read_state(dbs):
    state = {}
    for lane, db in dbs.items():
        tables = POST_TABLES if lane.endswith("post") else AUTO_TABLES
        existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = set(tables) - existing
        if missing:
            raise RetirementError("missing_tables:%s:%s" % (lane, ",".join(sorted(missing))))
        state[lane] = {table: table_rows(db, table) for table in tables}
        for table in ("pipeline_x_operation", "pipeline_x_claim", "pipeline_x_checkpoint"):
            if table in existing:
                state[lane][table] = table_rows(db, table)
        if MIGRATIONS in existing:
            state[lane][MIGRATIONS] = table_rows(db, MIGRATIONS)
    return state


@contextlib.contextmanager
def databases(paths, *, writable=False, transaction=False):
    with contextlib.ExitStack() as stack:
        dbs = {k: stack.enter_context(contextlib.closing(connect(v, writable=writable)))
               for k, v in sorted(paths.items(), key=lambda p: p[1])}
        try:
            if transaction:
                for db in dbs.values():
                    db.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
            yield dbs
        finally:
            for db in dbs.values():
                if db.in_transaction:
                    db.rollback()


def material(row):
    return str(row.get("material_id") or "").strip()


def scope_queue(q, manual):
    mr = manual.get(q.get("manual_run_id"))
    if mr:
        return mr.get("trigger_source") == "auto_template"
    return q.get("schedule_run_id") is not None and q.get("source_type") == "material"


def queue_reason(q, log, relay, reviewed, *, media_failure=False):
    """Empty means no durable evidence that a Post creation may have happened."""
    log, relay = log or {}, relay or {}
    if (q.get("status") == "published" or log.get("status") == "published"
            or log.get("x_post_id") or log.get("published_at")
            or relay.get("source_post_id") or relay.get("repost_id")
            or relay.get("source_published_at") or relay.get("reposted_at")):
        return "published_or_source_post"
    if (log.get("unknown_outcome") or relay.get("unknown_outcome")
            or log.get("status") in ("post_creating", "repost_creating", "source_published")
            or relay.get("status") in ("source_publishing", "reposting", "needs_review", "source_published")
            or q.get("status") in ("unknown", "needs_review")):
        return "unknown_outcome"
    if (log.get("status") == "media_uploading" or q.get("status") == "publishing"):
        return "inflight_upload"
    if q.get("status") not in ("queued", "reserved", "failed", "canceled", "cancelled"):
        return "unsupported_queue_state"
    if log.get("status", "") not in ("", "reserved", "failed"):
        return "unsupported_log_state"
    attempts = int(log.get("attempt_count") or 0) + int(relay.get("source_attempt_count") or 0) + int(relay.get("repost_attempt_count") or 0)
    if attempts and int(q["id"]) not in reviewed and not media_failure:
        return "prior_attempt_requires_review"
    # An explicit reviewed queue never overrides a receipt or unknown/inflight state.
    if relay and relay.get("status") not in ("reserved", "failed"):
        return "unsupported_relay_state"
    return ""


def indexes(state, lane):
    cache = state.setdefault("_indexes", {})
    if lane in cache:
        return cache[lane]
    p = state[lane]
    cache[lane] = ({r["id"]: r for r in p["x_post_manual_run"]},
                   {r["queue_id"]: r for r in p["x_post_publish_log"]},
                   {r["queue_id"]: r for r in p["x_post_repost_ledger"]})
    return cache[lane]


def related(state, lane, table, mid, key=None):
    cache = state.setdefault("_materials", {})
    identity = (lane, table)
    if identity not in cache:
        by_id, by_key = {}, {}
        for row in state[lane][table]:
            by_id.setdefault(material(row), []).append(row)
            by_key.setdefault(row.get("material_key"), []).append(row)
        cache[identity] = (by_id, by_key)
    by_id, by_key = cache[identity]
    values = by_id.get(mid, []) + (by_key.get(key, []) if key is not None else [])
    return list({id(row): row for row in values}.values())


def pipeline_proof(state, queue_id):
    p = state["ordinary_post"]
    cache = state.setdefault("_pipeline", {})
    if queue_id in cache:
        return cache[queue_id]
    groups = state.setdefault("_pipeline_groups", {})
    if not groups:
        for table in ("pipeline_x_operation", "pipeline_x_claim", "pipeline_x_checkpoint"):
            groups[table] = {}
            for row in p.get(table, []):
                groups[table].setdefault(row["source_key"], []).append(row)
    source_key = "x:queue:" + str(queue_id)
    operations = sorted(groups["pipeline_x_operation"].get(source_key, []),
                        key=lambda r: r["operation_id"])
    claims = sorted(groups["pipeline_x_claim"].get(source_key, []),
                    key=lambda r: (r["stage"], r["generation"]))
    checkpoints = sorted(groups["pipeline_x_checkpoint"].get(source_key, []),
                         key=lambda r: (r["generation"], r["phase"]))
    log = indexes(state, "ordinary_post")[1].get(queue_id)
    cache[queue_id] = dict(queue_id=int(queue_id), sha256=digest({"operations": operations, "claims": claims, "checkpoints": checkpoints}),
                operations=sorted(set(r["operation"] for r in operations)), has_claim=bool(claims),
                checkpoints=sorted(set(r["phase"] for r in checkpoints)),
                claim_identity_valid=all(int(r["queue_id"]) == int(queue_id) and log and int(r["log_id"]) == int(log["id"]) for r in claims))
    return cache[queue_id]


def pipeline_reason(state, queue_id, reviewed):
    proof = pipeline_proof(state, queue_id)
    allowed = ["x:POST:/2/media/upload/initialize"]
    if any(phase not in ("x_preflight", "x_media_initialized") for phase in proof["checkpoints"]):
        return "pipeline_post_checkpoint_requires_review"
    if confirmed_media_failure(state, queue_id):
        return ""
    if any(op not in allowed for op in proof["operations"]):
        return "pipeline_external_operation_requires_review"
    if not proof["claim_identity_valid"]:
        return "pipeline_claim_identity_mismatch"
    if int(queue_id) in reviewed and (not proof["has_claim"] or proof["operations"] != allowed):
        return "reviewed_media_only_evidence_missing"
    return ""


def media_operation(name):
    return bool(re.fullmatch(r"x:POST:/2/media/upload(?:/initialize|/[A-Za-z0-9_.:-]+/(?:append|finalize))", str(name))) or name == "x:GET:/2/media/upload"


def confirmed_media_failure(state, queue_id):
    """Explicit media-stage failures may migrate; this never retries a queue."""
    cache = state.setdefault("_media_failures", {})
    if queue_id in cache:
        return cache[queue_id]
    queues = ordinary_queues(state)
    q = queues.get(queue_id, {})
    _, logs, relays = indexes(state, "ordinary_post")
    log, relay = logs.get(queue_id, {}), relays.get(queue_id, {})
    code, message = log.get("error_code", ""), log.get("error_message", "")
    proof = pipeline_proof(state, queue_id)
    stage_failure = code in ("x_media_processing_timeout", "x_media_processing_failed") or (
        code == "x_upstream_error" and message.startswith(("X媒体初始化失败", "X媒体完成上传失败", "X媒体状态查询返回非JSON响应")))
    if code == "upload_interrupted_no_post":
        stage_failure = bool(proof["operations"] and message == "视频上传因服务切换中断；操作账本确认未发帖，成片与原尝试保留")
    valid = bool(stage_failure and q.get("status") in ("failed", "canceled", "cancelled") and
                 log.get("status") == "failed" and not log.get("unknown_outcome") and
                 not log.get("x_post_id") and not log.get("published_at") and
                 not relay.get("unknown_outcome") and not relay.get("source_post_id") and
                 not relay.get("repost_id") and not relay.get("source_published_at") and
                 not relay.get("reposted_at") and not int(relay.get("repost_attempt_count") or 0) and
                 relay.get("status", "failed") in ("failed", "reserved") and
                 proof["claim_identity_valid"] and all(media_operation(op) for op in proof["operations"]) and
                 all(p in ("x_preflight", "x_media_initialized") for p in proof["checkpoints"]))
    cache[queue_id] = valid
    return valid


def ordinary_queues(state):
    if "_ordinary_queues" not in state:
        state["_ordinary_queues"] = {q["id"]: q for q in state["ordinary_post"]["x_post_queue"]}
    return state["_ordinary_queues"]


def task_reason(task, queues, logs, relays, reviewed, *, state=None):
    if task.get("publish_id") or task.get("published_at_utc") or task.get("status") == "published":
        return "published_auto_task"
    if task.get("unknown_outcome") or task.get("status") in ("unknown", "reconciling"):
        return "unknown_auto_task"
    qid = task.get("execution_queue_id")
    if qid:
        q = queues.get(qid)
        if not q or material(q) != material(task):
            return "auto_queue_identity_mismatch"
        return queue_reason(q, logs.get(qid), relays.get(qid), reviewed,
                            media_failure=bool(state is not None and confirmed_media_failure(state, qid)))
    if task.get("execution_log_id") or task.get("execution_run_id"):
        return "auto_execution_without_queue"
    if int(task.get("publish_attempt_count") or 0):
        return "auto_attempt_without_queue"
    return ""


def _change(changes, lane, table, row, values, now):
    actual = {k: v for k, v in values.items() if k in row and row[k] != v}
    if not actual:
        return
    if "updated_at" in row:
        actual["updated_at"] = now
    if table == "x_post_schedule_config":
        pk = {"source_type": row["source_type"]}
    elif table == "x_auto_material_ledger":
        pk = {"material_id": row["material_id"]}
    else:
        pk = {"id": row["id"]}
    changes.append(dict(lane=lane, table=table, key=pk,
                        before={k: ("<redacted>" if k == "claim_token" and row[k] else row[k]) for k in actual},
                        after=actual, before_hash=digest(row), after_hash=digest(dict(row, **actual))))


def make_plan(paths, *, reviewed_no_post_queue_ids=(), now=None):
    paths = normalize_paths(paths)
    with databases(paths, transaction=True) as dbs:
        state = read_state(dbs)
    return plan_from_state(paths, state, reviewed_no_post_queue_ids, now or utc_now())


def plan_from_state(paths, state, reviewed_ids, now):
    reviewed = set(map(int, reviewed_ids))
    op, oa = state["ordinary_post"], state["ordinary_auto"]
    manual, logs, relays = indexes(state, "ordinary_post")
    queues = {q["id"]: q for q in op["x_post_queue"]}
    if not reviewed <= set(queues):
        raise RetirementError("reviewed_queue_not_found")
    changes, held = [], []
    canceled_queues = set()
    auto_by_queue = {}
    for task in oa["x_auto_task"]:
        if task.get("execution_queue_id"):
            auto_by_queue.setdefault(task["execution_queue_id"], []).append(task)
    for q in queues.values():
        if not scope_queue(q, manual) or q.get("status") not in ("queued", "reserved", "publishing", "unknown", "needs_review", "failed", "canceled", "cancelled"):
            continue
        if q.get("status") in ("failed", "canceled", "cancelled") and logs.get(q["id"], {}).get("status") != "reserved":
            continue
        reason = queue_reason(q, logs.get(q["id"]), relays.get(q["id"]), reviewed) or pipeline_reason(state, q["id"], reviewed)
        for task in auto_by_queue.get(q["id"], []):
            if task.get("unknown_outcome") or task.get("status") in ("unknown", "reconciling"):
                reason = reason or "unknown_auto_task"
            elif task.get("publish_id") or task.get("published_at_utc") or task.get("status") == "published":
                reason = reason or "published_auto_task"
            elif material(task) != material(q):
                reason = reason or "auto_queue_identity_mismatch"
        if reason:
            held.append(dict(kind="queue", id=q["id"], material_id=material(q), reason=reason))
            continue
        if q["id"] not in logs:
            # A pre-plan queue without a log is safely fenced by the queue and parent;
            # no synthetic external receipt is created.
            pass
        else:
            _change(changes, "ordinary_post", "x_post_publish_log", logs[q["id"]],
                    dict(status="failed", error_code=CODE, error_message=MESSAGE), now)
        if q.get("status") not in ("canceled", "cancelled"):
            _change(changes, "ordinary_post", "x_post_queue", q, dict(status="failed"), now)
        if q["id"] in relays:
            _change(changes, "ordinary_post", "x_post_repost_ledger", relays[q["id"]],
                    dict(status="failed", error_code=CODE, error_message=MESSAGE), now)
        canceled_queues.add(q["id"])
    for table, column in (("x_post_manual_run", "manual_run_id"), ("x_post_schedule_run", "schedule_run_id")):
        for run in op[table]:
            scoped = (run.get("trigger_source") == "auto_template" if table == "x_post_manual_run" else run.get("source_type") == "material")
            if not scoped:
                continue
            children = [q for q in queues.values() if q.get(column) == run["id"]]
            touched = any(q["id"] in canceled_queues for q in children)
            if not touched and not (not children and run.get("status") in ("queued", "claimed", "running")):
                continue
            published = failed = unknown = 0
            still_pending = False
            for q in children:
                log, relay = logs.get(q["id"], {}), relays.get(q["id"], {})
                is_unknown = (bool(log.get("unknown_outcome") or relay.get("unknown_outcome")) or
                              log.get("status") in ("post_creating", "repost_creating") or
                              relay.get("status") in ("source_publishing", "reposting", "needs_review"))
                unknown += int(is_unknown)
                published += int(log.get("status") == "published")
                failed += int(not is_unknown and (q["id"] in canceled_queues or log.get("status") == "failed"))
                if not is_unknown and q["id"] not in canceled_queues and log.get("status") not in ("published", "failed"):
                    still_pending = True
            values = dict(queued_count=len(children), published_count=published,
                          failed_count=failed, unknown_count=unknown)
            if unknown:
                values.update(status="needs_review")
            elif not still_pending:
                values.update(status="stopped", error_code=CODE, error_message=MESSAGE, finished_at=now)
            _change(changes, "ordinary_post", table, run,
                    values, now)
    for config in op["x_post_schedule_config"]:
        if config.get("source_type") == "material" and config.get("enabled"):
            _change(changes, "ordinary_post", "x_post_schedule_config", config,
                    dict(enabled=0, version=int(config["version"]) + 1), now)
    for template in oa["x_auto_template"]:
        _change(changes, "ordinary_auto", "x_auto_template", template, dict(enabled=0), now)
    canceled_tasks = set()
    for task in oa["x_auto_task"]:
        if task["status"] not in ACTIVE_TASKS:
            continue
        reason = task_reason(task, queues, logs, relays, reviewed, state=state)
        if task.get("execution_queue_id"):
            reason = reason or pipeline_reason(state, task["execution_queue_id"], reviewed)
            bound = queues.get(task["execution_queue_id"], {})
            if manual.get(bound.get("manual_run_id"), {}).get("trigger_source") != "auto_template":
                reason = reason or "auto_queue_outside_retirement_scope"
        if task.get("execution_queue_id") and task["execution_queue_id"] not in canceled_queues:
            q = queues.get(task["execution_queue_id"], {})
            if q.get("status") not in ("failed", "canceled", "cancelled"):
                reason = reason or "auto_queue_not_retired"
        if reason:
            held.append(dict(kind="auto_task", id=task["id"], material_id=material(task), reason=reason))
            continue
        _change(changes, "ordinary_auto", "x_auto_task", task,
                dict(status="canceled", error_code=CODE, error_message=MESSAGE, finished_at_utc=now,
                     claim_phase="", claim_worker="", claim_token="", lease_expires_at_utc="", next_attempt_at_utc=""), now)
        canceled_tasks.add(task["id"])
    for ledger in oa["x_auto_material_ledger"]:
        if ledger["task_id"] in canceled_tasks:
            _change(changes, "ordinary_auto", "x_auto_material_ledger", ledger,
                    dict(last_task_status="canceled"), now)
    for run in oa["x_auto_run"]:
        children = [t for t in oa["x_auto_task"] if t["run_id"] == run["id"]]
        if run["status"] in ("queued", "running") and all(t["id"] in canceled_tasks or t["status"] in FINAL_TASKS for t in children):
            _change(changes, "ordinary_auto", "x_auto_run", run,
                    dict(status="canceled", error_code=CODE, error_message=MESSAGE, finished_at_utc=now), now)
    candidates, exclusions, candidate_ids, candidate_keys = [], [], set(), set()
    for pool in sorted(op["x_post_material_pool"], key=lambda p: p["id"]):
        if pool["status"] != "unpublished":
            continue
        reason = migration_reason(pool, state, reviewed)
        if not reason and (material(pool) in candidate_ids or pool["material_key"] in candidate_keys):
            reason = "duplicate_source_candidate"
        if reason:
            exclusions.append(dict(source_pool_id=pool["id"], material_id=material(pool), reason=reason))
        else:
            candidates.append(dict(source_pool_id=pool["id"], material_id=material(pool),
                                   source_hash=digest(pool), row={k: v for k, v in pool.items() if k != "id"}))
            candidate_ids.add(material(pool))
            candidate_keys.add(pool["material_key"])
    plan = dict(version=VERSION, created_at=now, paths=paths,
                reviewed_no_post_queue_ids=sorted(reviewed), changes=changes,
                migrate=candidates, excluded=exclusions, held=held)
    selected_ids = {c["material_id"] for c in candidates}
    relevant = canceled_queues | {q["id"] for q in queues.values() if material(q) in selected_ids}
    plan["pipeline_evidence"] = [pipeline_proof(state, qid) for qid in sorted(relevant)]
    plan["plan_sha256"] = digest(plan)
    return plan


def migration_reason(pool, state, reviewed, *, applying=False):
    mid, key = material(pool), pool.get("material_key")
    if not mid or not key:
        return "missing_material_identity"
    for lane in ("ordinary_post", "profile_post"):
        p = state[lane]
        manual, logs, relays = indexes(state, lane)
        pool_rows = related(state, lane, "x_post_material_pool", mid, key)
        if any(r.get("status") == "published" or r.get("published_at") for r in pool_rows):
            return lane + ":published_pool_history"
        if lane == "profile_post" and pool_rows:
            return "target_pool_exists"
        if any(r.get("state") == "active" for r in related(state, lane, "x_post_manual_material_reservation", mid, key)):
            return lane + ":active_manual_reservation"
        for q in related(state, lane, "x_post_queue", mid, key):
            reason = queue_reason(q, logs.get(q["id"]), relays.get(q["id"]), reviewed if lane == "ordinary_post" else set(),
                                  media_failure=lane == "ordinary_post" and confirmed_media_failure(state, q["id"]))
            if lane == "ordinary_post":
                reason = reason or pipeline_reason(state, q["id"], reviewed)
            if reason:
                return lane + ":" + reason
            if lane == "profile_post":
                return "target_queue_history"
            if not scope_queue(q, manual):
                return "ordinary_nonretired_queue"
            if applying and q.get("status") not in ("failed", "canceled", "cancelled"):
                return "ordinary_queue_not_fenced"
    queues = ordinary_queues(state)
    _, logs, relays = indexes(state, "ordinary_post")
    for lane in ("ordinary_auto", "profile_auto"):
        a = state[lane]
        if lane == "profile_auto" and related(state, lane, "x_auto_material_ledger", mid):
            return "target_auto_material_history"
        for task in related(state, lane, "x_auto_task", mid):
            if lane == "profile_auto":
                return "target_auto_task_history"
            reason = task_reason(task, queues, logs, relays, reviewed, state=state)
            if reason:
                return "ordinary_auto:" + reason
            if applying and task["status"] in ACTIVE_TASKS:
                return "ordinary_auto_task_not_fenced"
    return ""


def _verify_plan(plan):
    body = dict(plan)
    expected = body.pop("plan_sha256", "")
    if plan.get("version") != VERSION or digest(body) != expected:
        raise RetirementError("manifest_hash_mismatch")
    paths = normalize_paths(plan["paths"])
    if paths != plan["paths"]:
        raise RetirementError("manifest_path_changed")
    return paths


def _row_for_change(db, change):
    key, value = next(iter(change["key"].items()))
    row = db.execute('SELECT * FROM "%s" WHERE "%s"=?' % (change["table"], key), (value,)).fetchone()
    return dict(row) if row else None


def _verify_changes(dbs, plan):
    for change in plan["changes"]:
        row = _row_for_change(dbs[change["lane"]], change)
        if row is None or digest(row) not in (change["before_hash"], change["after_hash"]):
            raise RetirementError("changed_row:%s:%s:%s" % (change["lane"], change["table"], canonical(change["key"])))


def _verify_pipeline(state, plan):
    for expected in plan["pipeline_evidence"]:
        if pipeline_proof(state, expected["queue_id"]) != expected:
            raise RetirementError("pipeline_evidence_changed:%s" % expected["queue_id"])


def _audit_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS %s(plan_sha256 TEXT NOT NULL,phase TEXT NOT NULL,applied_at TEXT NOT NULL,summary_json TEXT NOT NULL,PRIMARY KEY(plan_sha256,phase))' % AUDIT)


def _migration_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS %s(source_db TEXT NOT NULL,source_pool_id INTEGER NOT NULL,material_id TEXT NOT NULL,target_pool_id INTEGER NOT NULL,source_hash TEXT NOT NULL,plan_sha256 TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(source_db,source_pool_id),UNIQUE(target_pool_id),FOREIGN KEY(target_pool_id) REFERENCES x_post_material_pool(id))' % MIGRATIONS)


def backup_databases(paths, directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {}
    for lane, path in paths.items():
        dest = directory / (lane + ".sqlite3")
        if dest.exists():
            # Never overwrite the pre-apply copy on a resumed invocation.
            receipt[lane] = str(dest)
            continue
        with contextlib.closing(connect(path)) as source, contextlib.closing(sqlite3.connect(dest)) as target:
            os.chmod(dest, 0o600)
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RetirementError("backup_integrity_failed:" + lane)
        receipt[lane] = str(dest)
    return receipt


def apply_plan(plan, *, backup_directory, after_phase=None):
    """Ordinary admission is retired; unrelated publishers may keep running.

    Canonical queue/log updates and the publisher's mark_publishing transition
    serialize on SQLite BEGIN IMMEDIATE. In-flight or uncertain affected rows
    are excluded, and every planned row plus pipeline proof is rechecked. The
    target admission transaction locks/rechecks target reservations; no global
    publisher drain or deletion of existing pipeline claims is required.

    after_phase is an offline fault-injection hook; it is not exposed by the CLI.
    """
    paths = _verify_plan(plan)
    reviewed = set(plan["reviewed_no_post_queue_ids"])
    backups = backup_databases(paths, backup_directory)
    # Validate all intended changes before the first mutation. Each source stage
    # has its own transaction; the same manifest accepts its exact after image.
    with databases(paths, writable=True, transaction=True) as dbs:
        _verify_changes(dbs, plan)
        state = read_state(dbs)
        _verify_pipeline(state, plan)
        for item in plan["migrate"]:
            source = next((r for r in state["ordinary_post"]["x_post_material_pool"] if r["id"] == item["source_pool_id"]), None)
            if source is None or digest(source) != item["source_hash"]:
                raise RetirementError("source_pool_changed:%s" % item["source_pool_id"])
        for lane in ("ordinary_post", "ordinary_auto"):
            db = dbs[lane]
            _audit_schema(db)
            count = 0
            for change in (c for c in plan["changes"] if c["lane"] == lane):
                if digest(_row_for_change(db, change)) == change["after_hash"]:
                    continue
                cols = list(change["after"])
                key, value = next(iter(change["key"].items()))
                db.execute('UPDATE "%s" SET %s WHERE "%s"=?' % (change["table"], ",".join('"%s"=?' % c for c in cols), key),
                           [change["after"][c] for c in cols] + [value])
                count += 1
            db.execute('INSERT OR IGNORE INTO %s VALUES(?,?,?,?)' % AUDIT,
                       (plan["plan_sha256"], "retire", utc_now(), canonical({"changes": count})))
            db.commit()
            if after_phase:
                after_phase(lane)
    # Reacquire all four write locks. This closes the source-commit race against
    # manual submission and target reservation before admitting any material.
    with databases(paths, writable=True, transaction=True) as dbs:
        _verify_changes(dbs, plan)
        state = read_state(dbs)
        _verify_pipeline(state, plan)
        target = dbs["profile_post"]
        _migration_schema(target)
        prior = {(r["source_db"], r["source_pool_id"]): r for r in table_rows(target, MIGRATIONS)}
        added, repeated = [], []
        prepared = []
        prepared_ids, prepared_keys = set(), set()
        target_columns = {r[1] for r in target.execute("PRAGMA table_info(x_post_material_pool)")}
        for item in plan["migrate"]:
            identity = (paths["ordinary_post"], item["source_pool_id"])
            audit = prior.get(identity)
            if audit:
                row = target.execute("SELECT material_id,material_key,created_at,created_by_user_id,created_by_name FROM x_post_material_pool WHERE id=?", (audit["target_pool_id"],)).fetchone()
                if not row or audit["source_hash"] != item["source_hash"] or str(row["material_id"]) != item["material_id"]:
                    raise RetirementError("migration_receipt_conflict")
                for key in ("material_key", "created_at", "created_by_user_id", "created_by_name"):
                    if row[key] != item["row"][key]:
                        raise RetirementError("migrated_identity_changed:" + key)
                repeated.append(audit["target_pool_id"])
                continue
            source = next((r for r in state["ordinary_post"]["x_post_material_pool"] if r["id"] == item["source_pool_id"]), None)
            if source is None or digest(source) != item["source_hash"]:
                raise RetirementError("source_pool_changed:%s" % item["source_pool_id"])
            reason = migration_reason(source, state, reviewed, applying=True)
            if reason:
                raise RetirementError("migration_conflict:%s:%s" % (item["source_pool_id"], reason))
            if not set(item["row"]) <= target_columns:
                raise RetirementError("pool_schema_mismatch")
            if item["material_id"] in prepared_ids or item["row"]["material_key"] in prepared_keys:
                raise RetirementError("duplicate_manifest_candidate")
            prepared_ids.add(item["material_id"])
            prepared_keys.add(item["row"]["material_key"])
            prepared.append(item)
        for item in prepared:
            cols = list(item["row"])
            cur = target.execute('INSERT INTO x_post_material_pool(%s) VALUES(%s)' %
                                 (",".join('"%s"' % c for c in cols), ",".join("?" for _ in cols)),
                                 [item["row"][c] for c in cols])
            target_id = cur.lastrowid
            target.execute('INSERT INTO %s VALUES(?,?,?,?,?,?,?)' % MIGRATIONS,
                           (paths["ordinary_post"], item["source_pool_id"], item["material_id"], target_id,
                            item["source_hash"], plan["plan_sha256"], utc_now()))
            added.append(target_id)
        _audit_schema(target)
        target.execute('INSERT OR IGNORE INTO %s VALUES(?,?,?,?)' % AUDIT,
                       (plan["plan_sha256"], "migrate", utc_now(), canonical({"target_ids": added})))
        target.commit()
    return dict(plan_sha256=plan["plan_sha256"], migrated=len(added), already_migrated=len(repeated),
                target_ids=added, backups=backups, held=len(plan["held"]), excluded=len(plan["excluded"]))


@contextlib.contextmanager
def process_locks(paths):
    """Optionally acquire operator-specified existing locks, without waiting."""
    if not paths:
        yield
        return
    try:
        import fcntl
    except ImportError:
        raise RetirementError("production_apply_requires_posix_flock") from None
    with contextlib.ExitStack() as stack:
        for path in sorted(set(str(Path(p).resolve(strict=True)) for p in paths)):
            handle = stack.enter_context(open(path, "r+b"))
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RetirementError("publisher_lock_busy:" + path) from None
        yield
