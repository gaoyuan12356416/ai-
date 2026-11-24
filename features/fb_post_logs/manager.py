"""Versioned per-task controls for the FB native ledger and local dispatcher."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .query import FAMILIES, STOP, display, obj, one, page_keys

NATIVE = "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3"
CENTRAL = "/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3"
KEY = re.compile(r"fb:(auto:[1-9][0-9]{0,17}|video:[a-f0-9]{32})")
FAILURES = {"definite_failure", "credential_failure"}
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS fb_log_request(operation_id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,
       actor_id TEXT NOT NULL,created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fb_log_action(operation_id TEXT NOT NULL,source_key TEXT NOT NULL,
       action TEXT NOT NULL,actor_json TEXT NOT NULL,before_json TEXT NOT NULL,result_json TEXT NOT NULL,
       created_at TEXT NOT NULL,PRIMARY KEY(operation_id,source_key))""",
    "CREATE INDEX IF NOT EXISTS fb_log_history ON fb_log_action(source_key,created_at)",
    "CREATE INDEX IF NOT EXISTS fb_log_video_attempt ON fb_video_attempt(task_id,id)",
    "CREATE INDEX IF NOT EXISTS fb_log_native_op ON pipeline_fb_operation(source_key,sequence)",
    "CREATE INDEX IF NOT EXISTS fb_log_auto_day ON fb_auto_task(coalesce(nullif(planned_publish_at_utc,''),created_at_utc))",
    "CREATE INDEX IF NOT EXISTS fb_log_video_day ON fb_video_task(coalesce(nullif(planned_at_utc,''),created_at_utc))",
    "CREATE INDEX IF NOT EXISTS fb_log_auto_recent ON fb_auto_task(created_at_utc DESC,id DESC)",
    "CREATE INDEX IF NOT EXISTS fb_log_video_recent ON fb_video_task(created_at_utc DESC,task_id DESC)",
)


def dump(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(v):
    return hashlib.sha256(dump(v).encode()).hexdigest()


def rows(c, sql, args):
    return [dict(r) for r in c.execute(sql, args)]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Manager:
    def __init__(self, native=NATIVE, central=CENTRAL):
        self.native, self.central = Path(native), Path(central)

    def connect(self, write=False):
        mode = "?mode=rw" if write else "?mode=ro"
        c = sqlite3.connect(self.native.resolve().as_uri() + mode, uri=True, timeout=4)
        c.row_factory = sqlite3.Row
        try:
            c.execute("ATTACH DATABASE ? AS dispatch", (self.central.resolve().as_uri() + mode,))
            if not write:
                c.execute("PRAGMA query_only=ON")
            return c
        except BaseException:
            c.close()
            raise

    def install(self):
        with closing(sqlite3.connect(self.native.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as c:
            for sql in SCHEMA:
                c.execute(sql)
                c.commit()

    def query(self, params):
        items = []
        with closing(self.connect()) as c:
            c.execute("BEGIN")
            keys, pagination = page_keys(c, params)
            for family, identity, _ in keys:
                s = snapshot(c, family, identity)
                item = display(c, family, s["task"], s)
                item["action_version"] = digest(s)
                for action in ("reset", "stop"):
                    reason = unavailable(s, action)
                    item["can_" + action] = not reason
                    item[action + "_reason"] = reason
                items.append(item)
        return {"items": items, "pagination": pagination}

    def apply(self, payload, actor):
        if not isinstance(payload, dict) or set(payload) != {"operation_id", "items", "action"}:
            raise ValueError("操作参数无效")
        op, items, action = payload["operation_id"], payload["items"], payload["action"]
        if (
            not isinstance(op, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", op)
            or action not in ("reset", "stop")
        ):
            raise ValueError("操作编号或操作类型无效")
        if (
            not isinstance(items, list)
            or not 1 <= len(items) <= 100
            or (action == "stop" and len(items) != 1)
        ):
            raise ValueError("操作条数无效")
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != {"queue_id", "version"}
                or not isinstance(item["queue_id"], str)
                or not KEY.fullmatch(item["queue_id"])
                or not isinstance(item["version"], str)
                or not re.fullmatch("[a-f0-9]{64}", item["version"])
            ):
                raise ValueError("任务或版本参数无效，请刷新")
        if len({i["queue_id"] for i in items}) != len(items):
            raise ValueError("任务重复")
        binding = digest([actor["tenant_key"], actor["user_id"], action, items])
        results = []
        for item in items:
            key = item["queue_id"]
            with closing(self.connect(True)) as c:
                c.execute("BEGIN IMMEDIATE")
                stamp = now()
                c.execute(
                    "INSERT OR IGNORE INTO fb_log_request VALUES(?,?,?,?)",
                    (op, binding, actor["user_id"], stamp),
                )
                if (
                    c.execute(
                        "SELECT request_hash FROM fb_log_request WHERE operation_id=?", (op,)
                    ).fetchone()[0]
                    != binding
                ):
                    raise ValueError("操作编号已用于其他请求")
                prior = one(
                    c,
                    "SELECT result_json FROM fb_log_action WHERE operation_id=? AND source_key=?",
                    (op, key),
                )
                if prior:
                    results.append(json.loads(prior["result_json"]))
                    continue
                _, family, identity = key.split(":")
                s = snapshot(c, family, identity)
                reason = unavailable(s, action)
                if not reason and item["version"] != digest(s):
                    reason = "任务状态已变化，请刷新后重新选择"
                if not reason:
                    c.execute("SAVEPOINT change_task")
                    try:
                        change(c, s, action, stamp, op)
                    except sqlite3.IntegrityError:
                        c.execute("ROLLBACK TO change_task")
                        reason = "任务与当前发布安排冲突，请刷新核对"
                    finally:
                        c.execute("RELEASE change_task")
                result = {
                    "queue_id": key,
                    "ok": not reason,
                    "message": reason or ("已停止发布" if action == "stop" else "已重新入队"),
                }
                history = {k: s.get(k) for k in ("jobs", "hold", "attempt_count", "ledger")}
                history["task"] = {
                    k: s["task"].get(k)
                    for k in (
                        "status",
                        "error_code",
                        "error_message",
                        "prepare_state",
                        "link_state",
                        "completed_at_utc",
                    )
                }
                c.execute(
                    "INSERT INTO fb_log_action VALUES(?,?,?,?,?,?,?)",
                    (op, key, action, dump(actor), dump(history), dump(result), stamp),
                )
                c.commit()
                results.append(result)
        success = sum(r["ok"] for r in results)
        return {"items": results, "succeeded": success, "skipped": len(results) - success}


def snapshot(c, family, identity):
    table, pk, _ = FAMILIES[family]
    task = one(c, "SELECT * FROM " + table + " WHERE " + pk + "=?", (identity,))
    key = "fb:" + family + ":" + str(identity)
    attempts = "fb_auto_publish_attempt" if family == "auto" else "fb_video_attempt"
    count = c.execute("SELECT count(*) FROM " + attempts + " WHERE task_id=?", (identity,)).fetchone()[0]
    s = dict(
        key=key,
        family=family,
        task=task,
        attempt_count=count,
        publication=one(c, "SELECT * FROM dispatch.pipeline_publication WHERE source_key=?", (key,)),
        hold=one(c, "SELECT reason FROM pipeline_canonical_hold WHERE source_key=?", (key,)),
    )
    if (
        not task
        or task["status"] not in ("planned", "ready", "queued", "failed")
        or task["unknown_outcome"]
        or task["graph_post_id"]
        or s["publication"]
    ):
        return s
    s["last_action"] = one(
        c,
        "SELECT rowid AS revision FROM fb_log_action WHERE source_key=? AND json_extract(result_json,'$.ok')=1 ORDER BY rowid DESC LIMIT 1",
        (key,),
    ).get("revision", 0)
    s["ledger"] = (
        one(c, "SELECT * FROM fb_auto_publish_ledger WHERE task_id=?", (identity,))
        if family == "auto"
        else {}
    )
    s["attempts"] = rows(
        c,
        "SELECT id,result_kind,error_code"
        + (",graph_post_id" if family == "video" else "")
        + " FROM "
        + attempts
        + " WHERE task_id=? ORDER BY id",
        (identity,),
    )
    s["native_ops"] = rows(
        c,
        "SELECT operation_id,stage,generation,result_json,checkpoint_json FROM pipeline_fb_operation WHERE source_key=? ORDER BY sequence",
        (key,),
    )
    s["jobs"] = rows(
        c,
        """SELECT id,stage,status,generation,external_intent,hold_reason,grandfathered,
        error_code,error_message,lease_expires_at,canonical_receipt FROM dispatch.pipeline_job WHERE source_key=? ORDER BY stage""",
        (key,),
    )
    s["central_ops"] = []
    s["occupied"] = False
    s["remote_inflight"] = False
    for job in s["jobs"]:
        jid = job["id"]
        s["central_ops"].extend(
            rows(
                c,
                "SELECT operation_id,job_id,generation,status FROM dispatch.pipeline_operation WHERE job_id=?",
                (jid,),
            )
        )
        if (
            job["status"] not in ("completed", "failed")
            and c.execute(
                "SELECT 1 FROM dispatch.sqlite_master WHERE name='pipeline_cloud_intent'"
            ).fetchone()
        ):
            s["remote_inflight"] |= bool(
                c.execute(
                    "SELECT 1 FROM dispatch.pipeline_cloud_intent WHERE job_id=? AND generation=? LIMIT 1",
                    (jid, job["generation"]),
                ).fetchone()
            )
        for table in ("pipeline_resource", "pipeline_legacy_resource"):
            if c.execute("SELECT 1 FROM dispatch." + table + " WHERE job_id=? LIMIT 1", (jid,)).fetchone():
                s["occupied"] = True
    s["competing"] = False
    if task["status"] == "failed" and task["material_id"]:
        for other, (table, pk, _) in FAMILIES.items():
            args = [task["page_id"], task["material_id"], task["created_at_utc"]]
            sql = "SELECT 1 FROM " + table + " WHERE page_id=? AND material_id=? AND created_at_utc>=?"
            if family == other:
                sql += " AND " + pk + "<>?"
                args.append(identity)
            if c.execute(
                sql
                + " AND (status IN ('running','submitted','published','unknown') OR graph_post_id<>'' OR unknown_outcome<>0) LIMIT 1",
                args,
            ).fetchone():
                s["competing"] = True
    return s


def stage(s):
    t = s["task"]
    if s["family"] == "video":
        return "publish" if t["prepare_state"] == "ready" else "prepare"
    if t["status"] == "ready" or (
        t["status"] == "failed" and t["prepared_media_url"] and t["prepared_sha256"]
    ):
        return "publish"
    return "prepare"


def unavailable(s, action):
    t = s["task"]
    if not t:
        return "任务不存在"
    state = t["status"]
    if state == "published" or s["publication"]:
        return "已发布"
    if state == "submitted" or t["graph_post_id"]:
        return "已提交 Meta，请核对平台回执"
    if state == "unknown" or t["unknown_outcome"]:
        return "发布结果待确认"
    if state not in ("planned", "queued", "ready", "failed"):
        return {
            "failed_without_retry": "原任务禁止重试",
            "skipped": "原任务已跳过",
            "cancelled": "原任务已取消",
        }.get(state, "任务正在执行")
    if (
        s["ledger"].get("status") in ("submitted", "unknown", "published", "failed_without_retry")
        or s["ledger"].get("graph_post_id")
        or s["ledger"].get("unknown_outcome")
    ):
        return "发布账本已有回执或禁止重试"
    if (
        any(t.get(k) for k in ("lease_owner", "execution_lease", "prepare_lease", "link_lease"))
        or s["occupied"]
        or s["remote_inflight"]
    ):
        return "任务正在执行"
    if t.get("prepare_state") == "preparing" or t.get("link_state") == "writing":
        return "任务正在准备"
    if s["hold"] and s["hold"]["reason"] != STOP:
        return "任务在其他流程中暂停"
    if s["competing"]:
        return "该 Page 的相同素材已有后续发布记录"
    jobs = s["jobs"]
    if not jobs:
        return "任务尚未同步至调度器"
    for j in jobs:
        if j["status"] == "completed" and j["stage"] == "prepare":
            continue
        if (
            j["status"] not in ("queued", "ready", "failed", "backlog_paused")
            or j["grandfathered"]
            or j["hold_reason"] not in ("", STOP)
        ):
            return "调度任务正在执行、已结束或由其他流程保留"
        if j["status"] == "backlog_paused" and j["hold_reason"] != STOP:
            return "调度任务已暂停"
    if any(a["result_kind"] not in FAILURES or a.get("graph_post_id") for a in s["attempts"]):
        return "存在未确认失败的发布尝试"
    native_ids = set()
    for op in s["native_ops"]:
        result = obj(op["result_json"])
        if (
            op["stage"] != "publish"
            or result.get("kind") not in FAILURES
            or result.get("post_id")
            or result.get("graph_post_id")
            or (op["checkpoint_json"] and obj(op["checkpoint_json"]) != result)
        ):
            return "平台操作回执待核对"
        native_ids.add(op["operation_id"])
    if any(
        o["operation_id"] not in native_ids and o["status"] != "confirmed_unissued" for o in s["central_ops"]
    ):
        return "中央调度仍有未核对的发布许可"
    for j in jobs:
        if j["external_intent"] and not any(
            o["job_id"] == j["id"]
            and o["generation"] == j["generation"]
            and (o["operation_id"] in native_ids or o["status"] == "confirmed_unissued")
            for o in s["central_ops"]
        ):
            return "存在未核对的外部执行记录"
    if any(j["stage"] == "publish" and j["status"] != "completed" for j in jobs) and stage(s) == "prepare":
        return "发布调度与任务的准备阶段不一致，请先核对"
    if action == "stop":
        if s["hold"]:
            return "任务已停止"
        if state == "failed":
            return "任务已失败，无需停止"
    elif not any(j["stage"] == stage(s) and j["status"] != "completed" for j in jobs):
        return "未找到与原任务阶段一致的调度记录"
    return ""


def change(c, s, action, stamp, op):
    t, key = s["task"], s["key"]
    family = s["family"]
    identity = t[FAMILIES[family][1]]
    if action == "stop":
        c.execute(
            "INSERT INTO pipeline_canonical_hold(source_key,cutoff,reason) VALUES(?,?,?)", (key, stamp, STOP)
        )
        for job in s["jobs"]:
            if job["status"] != "completed":
                c.execute(
                    "UPDATE dispatch.pipeline_job SET status='backlog_paused',hold_reason=?,updated_at=? WHERE id=?",
                    (STOP, stamp, job["id"]),
                )
    else:
        phase = stage(s)
        if family == "video":
            new_state = "queued"
            c.execute(
                """UPDATE fb_video_task SET status='queued',error_code='',error_message='',prepare_retry_at='',
                link_retry_at='',prepare_state=CASE WHEN prepare_state='ready' THEN 'ready' ELSE 'planned' END,
                updated_at_utc=? WHERE task_id=?""",
                (stamp, identity),
            )
        else:
            new_state = "ready" if phase == "publish" else "planned"
            until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
            c.execute(
                """UPDATE fb_auto_task SET status=?,error_code='',error_message='',completed_at_utc='',
                         next_prepare_at_utc='',recovery_until_utc=max(recovery_until_utc,?) WHERE id=?""",
                (new_state, until, identity),
            )
            from features.fb_auto_posts.core import FBAutoPostStore

            FBAutoPostStore._refresh_run(c, t["run_id"], stamp)
        c.execute("DELETE FROM pipeline_canonical_hold WHERE source_key=? AND reason=?", (key, STOP))
        for job in s["jobs"]:
            if job["stage"] != phase:
                # Only rearm the executable phase. A completed preparation and its artifact stay intact.
                if phase == "publish" and job["stage"] == "prepare" and job["status"] != "completed":
                    c.execute(
                        "UPDATE dispatch.pipeline_job SET status='completed',hold_reason='',finished_at=?,updated_at=? WHERE id=?",
                        (stamp, stamp, job["id"]),
                    )
                continue
            c.execute(
                """UPDATE dispatch.pipeline_job SET status='queued',hold_reason='',source_status=?,external_intent=0,
                node_id='',worker_id='',claim_hash='',lease_expires_at='',error_code='',error_message='',started_at='',
                finished_at='',canonical_receipt='',updated_at=? WHERE id=?""",
                (new_state, stamp, job["id"]),
            )
            if phase == "publish" and (s["attempt_count"] or s["native_ops"]):
                proof = dict(
                    source_key=key,
                    actor="operator_authorized",
                    generation=job["generation"],
                    attempts=max(s["attempt_count"], len(s["native_ops"])),
                    operation_id=op,
                )
                c.execute(
                    "INSERT INTO dispatch.pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)",
                    (job["id"], "authorized_fb_unpublished_recovery", dump(proof), stamp),
                )
    for job in s["jobs"]:
        c.execute(
            "INSERT INTO dispatch.pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)",
            (
                job["id"],
                "operator_fb_log_" + action,
                dump({"operation_id": op, "previous_status": job["status"]}),
                stamp,
            ),
        )
