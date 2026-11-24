"""Project original X Tag receipts. This module never calls a publisher or X."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from features.x_auto_posts.receipt_reconciliation import _inspect, ReceiptReconciliationError
from features.x_posts.service import XPostStore
from .failed_delivery import DDL as DELIVERY_DDL

TERMINAL = {"published","failed","skipped","no_candidate","canceled"}
MEDIA = re.compile(r"x:POST:/2/media/upload/(?:initialize|[0-9]+/(?:append|finalize))|x:GET:/2/media/upload\?media_id=[0-9]+")
FAIL_MESSAGE = "原执行节点已返回失败，操作停在媒体上传阶段，未提交创建帖子请求。"


class Review(ValueError):
    """Missing or conflicting evidence leaves only this queue in review."""


def require(ok, message):
    if not ok:
        raise Review(message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_ledgers(posts, auto, dispatch):
    def uri(path):
        return Path(path).resolve(strict=True).as_uri() + "?mode=rw"
    db = sqlite3.connect(uri(posts), uri=True, timeout=3, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ? AS a", (uri(auto),))
    db.execute("ATTACH DATABASE ? AS d", (uri(dispatch),))
    db.execute("PRAGMA foreign_keys=ON")
    if hasattr(os,"geteuid") and os.geteuid()==0:
        for path in (posts,auto,dispatch):
            owner = Path(path).stat()
            for suffix in ("-shm","-wal"):
                sidecar = str(path)+suffix
                try:
                    os.chown(sidecar,owner.st_uid,owner.st_gid)
                    os.chmod(sidecar,owner.st_mode & 0o777)
                except FileNotFoundError:
                    pass
    return db


def initialize(db):
    db.execute(DELIVERY_DDL)
    db.execute("CREATE INDEX IF NOT EXISTS x_tag_operations_by_source ON pipeline_x_operation(source_key,generation)")
    db.execute("CREATE TABLE IF NOT EXISTS x_tag_settled_task(task_id INTEGER PRIMARY KEY,queue_id INTEGER NOT NULL,kind TEXT NOT NULL,proof TEXT NOT NULL,settled_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS a.x_tag_task_wait(task_id INTEGER PRIMARY KEY,queue_id INTEGER UNIQUE NOT NULL,retry_at TEXT NOT NULL)")


def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql,args)]


def one(db, sql, args):
    result = rows(db,sql,args)
    require(len(result)==1, "expected one original record")
    return result[0]


def refresh_run(db, rid, stamp):
    run = one(db,"SELECT status,finished_at_utc FROM a.x_auto_run WHERE id=?",(rid,))
    if run["status"]=="canceled":
        return
    tasks=rows(db,"SELECT status,unknown_outcome FROM a.x_auto_task WHERE run_id=?",(rid,))
    if not all(t["status"] in TERMINAL and not t["unknown_outcome"] for t in tasks):
        target="running"
    else:
        failures=sum(t["status"] in {"failed","canceled"} for t in tasks)
        target="completed" if not failures else "failed" if failures==len(tasks) else "partial_failed"
    if run["status"]==target and (target=="running" or run["finished_at_utc"]):
        return
    db.execute("""UPDATE a.x_auto_run SET status=?,updated_at=?,finished_at_utc=?,
                  error_code=CASE WHEN ?='completed' THEN '' ELSE error_code END,
                  error_message=CASE WHEN ?='completed' THEN '' ELSE error_message END WHERE id=?""",
               (target,stamp,"" if target=="running" else run["finished_at_utc"] or stamp,target,target,rid))
    db.execute("""INSERT INTO a.x_auto_event(run_id,event_type,from_status,to_status,message,details_json,created_at)
                  VALUES(?,'run_receipts_settled',?,?,'按原账号任务回执汇总','{}',?)""",(rid,run["status"],target,stamp))


def match_task(db, tid, qid):
    task=one(db,"SELECT * FROM a.x_auto_task WHERE id=?",(tid,))
    q=one(db,"SELECT * FROM x_post_queue WHERE id=?",(qid,))
    log=one(db,"SELECT * FROM x_post_publish_log WHERE queue_id=?",(qid,))
    m=one(db,"SELECT * FROM x_post_manual_run WHERE id=?",(q["manual_run_id"],))
    reservation=one(db,"SELECT * FROM a.x_auto_material_ledger WHERE task_id=?",(tid,))
    require(task["execution_queue_id"]==qid and task["execution_log_id"] in (None,log["id"])
            and m["id"]==task["execution_run_id"] and m["trigger_source"]=="auto_template"
            and m["external_task_key"]=="x-auto-task-"+str(tid)
            and m["template_ref"]=="x-auto-template-"+str(task["template_id"]) and m["template_version"]==task["template_version"]
            and list(map(str,json.loads(m["account_ids_json"])))==[str(task["account_id"])]
            and list(map(str,json.loads(m["material_ids_json"])))==[str(task["material_id"])]
            and str(q["account_id"])==str(log["account_id"])==str(task["account_id"])
            and str(q["material_id"])==str(reservation["material_id"])==str(task["material_id"])
            and str(q["content_id"])==str(reservation["content_id"])==str(task["content_id"])
            and reservation["canonical_queue_id"]==qid and reservation["run_id"]==task["run_id"]
            and reservation["template_id"]==task["template_id"],"queue/account/material identity changed")
    require(not any(task[k] for k in ("claim_token","claim_worker","lease_expires_at_utc")), "task has an active claim")
    return task,q,log


def failure_proof(db, task, q, log, expected_job, receipts):
    key="x:queue:"+str(q["id"])
    job=one(db,"SELECT * FROM d.pipeline_job WHERE source_key=? AND stage='publish'",(key,))
    require(job["id"]==expected_job and job["status"] in {"failed","unknown"}
            and job["channel"]=="x" and job["source_id"]==str(q["id"]), "publisher has not stopped on the original job")
    require(q["delivery_mode"]=="direct" and q["status"] in {"failed","queued"} and log["status"] in {"failed","reserved"}
            and log["attempt_count"]>0 and not any(log[k] for k in ("x_post_id","x_post_url","x_media_id","published_at"))
            and not task["publish_id"] and not task["publish_url"]
            and not db.execute("SELECT 1 FROM d.pipeline_publication WHERE source_key=?",(key,)).fetchone()
            and not db.execute("SELECT 1 FROM x_post_repost_ledger WHERE queue_id=?",(q["id"],)).fetchone(),
            "later publication evidence or an incompatible native stage exists")
    require(task["status"] in {"unknown","retry_wait","reconciling","failed"},"Auto task has changed phase")
    hist={(r["generation"],r["node_id"]):r["claim_hash"] for r in rows(db,
          "SELECT generation,node_id,claim_hash FROM d.pipeline_claim_history WHERE job_id=?",(job["id"],))}
    require(hist and hist.get((job["generation"],job["node_id"]))==job["claim_hash"],"latest claim identity differs")
    seen=set(); accepted=[]
    for f in receipts:
        require(f["job"]==job["id"] and f["acked"] and not f["post"],"pending or later node receipt")
        if f["route"]=="jobs/checkpoint":
            require(f["phase"]=="x_media_initialized","node reached a later checkpoint")
            continue
        require(f["route"]=="jobs/fail","node did not return a final failure")
        delivery=one(db,"SELECT * FROM d.pipeline_delivery WHERE delivery_id=?",(f["id"],))
        pair=(f["generation"],f["node"])
        answer=json.loads(delivery["response_json"])
        require(delivery["job_id"]==job["id"] and delivery["generation"]==pair[0]
                and hist.get(pair)==delivery["claim_hash"] and answer.get("accepted") is True
                and answer.get("status") in {"unknown","failed"}
                and (not f.get("sha") or f["sha"]==delivery["request_sha256"]), "worker failure acknowledgement mismatch")
        seen.add(pair)
        accepted.append(dict(id=f["id"],node=pair[1],generation=pair[0],sha256=delivery["request_sha256"],error=f["error"]))
    require(seen==set(hist),"every claimed generation needs its own acknowledged failure")
    claims=rows(db,"SELECT generation,queue_id,log_id FROM pipeline_x_claim WHERE source_key=? AND stage='publish'",(key,))
    require(claims and all(r["queue_id"]==q["id"] and r["log_id"]==log["id"] for r in claims),"native claim binding differs")
    fields="operation_id,generation,operation,request_sha256"
    native=rows(db,"SELECT "+fields+" FROM pipeline_x_operation WHERE source_key=? ORDER BY operation_id",(key,))
    central=rows(db,"SELECT "+fields+" FROM d.pipeline_operation WHERE job_id=? ORDER BY operation_id",(job["id"],))
    by_id={r["operation_id"]:r for r in central}
    require(native and central and all(MEDIA.fullmatch(r["operation"]) for r in central+native)
            and all(by_id.get(r["operation_id"])==r for r in native)
            and any(r["operation"].endswith("/initialize") for r in native),"operations do not prove media-only failure")
    checkpoints={r[0] for r in db.execute("SELECT phase FROM pipeline_x_checkpoint WHERE source_key=?",(key,))}
    remote_phases={r[0] for r in db.execute("SELECT phase FROM d.pipeline_checkpoint WHERE job_id=?",(job["id"],))}
    require("x_media_initialized" in checkpoints and checkpoints<={"x_media_initialized","x_preflight"}
            and remote_phases=={"x_media_initialized"},"upload reached a later stage")
    proof=dict(source_key=key,job_id=job["id"],generation=job["generation"],node_failures=accepted,
               native_operations=len(native),central_operations=len(central),create_post_operations=0,
               native_sha256=hashlib.sha256(encode(native).encode()).hexdigest(),
               central_sha256=hashlib.sha256(encode(central).encode()).hexdigest(),
               conclusion="upload_interrupted_before_post")
    return job,proof


def project(db, item, receipts=()):
    tid,qid=int(item["task"]),int(item["queue"])
    previous=db.execute("SELECT * FROM x_tag_settled_task WHERE task_id=?",(tid,)).fetchone()
    task,q,log=match_task(db,tid,qid)
    kind=item["kind"]; stamp=now()
    require(kind in {"upload","late"},"unsupported outcome")
    if previous:
        require(previous["queue_id"]==qid and previous["kind"]==kind,"conflicting previous settlement")
    if kind=="late":
        receipt=_inspect(db,db,tid)["receipt"]
        pub=one(db,"SELECT job_id,publish_id FROM d.pipeline_publication WHERE source_key=?",("x:queue:"+str(qid),))
        require(pub["publish_id"]==receipt["post_id"] and pub["job_id"]==item["job"]
                and (not item.get("post") or receipt["post_id"]==item["post"]), "confirmed Post ID differs")
        proof=dict(receipt=receipt,job_id=pub["job_id"])
        if previous and task["status"]=="published" and task["publish_id"]==receipt["post_id"]:
            refresh_run(db,task["run_id"],stamp)
            return "already"
        target="published"
        db.execute("""UPDATE a.x_auto_task SET status='published',execution_log_id=?,publish_id=?,publish_url=?,published_at_utc=?,
                   finished_at_utc=?,unknown_outcome=0,error_code='',error_message='',claim_phase='',next_attempt_at_utc='',updated_at=? WHERE id=?""",
                   (log["id"],receipt["post_id"],receipt["post_url"],receipt["published_at_utc"],receipt["published_at_utc"],stamp,tid))
    else:
        job, proof=failure_proof(db,task,q,log,item["job"],receipts)
        if previous and task["status"]=="failed" and not task["unknown_outcome"] and not log["unknown_outcome"] and job["status"]=="failed" and q["status"]=="failed":
            refresh_run(db,task["run_id"],stamp)
            return "already"
        target="failed"; error=job["error_code"] or log["error_code"] or "x_media_upload_interrupted"
        db.execute("UPDATE x_post_queue SET status='failed',updated_at=? WHERE id=? AND status<>'failed'",(stamp,qid))
        db.execute("UPDATE x_post_publish_log SET status='failed',unknown_outcome=0,error_code=?,error_message=?,updated_at=? WHERE id=?",(error,FAIL_MESSAGE,stamp,log["id"]))
        XPostStore._sync_run(db,qid,stamp)
        db.execute("""UPDATE a.x_auto_task SET status='failed',unknown_outcome=0,execution_log_id=?,error_code=?,error_message=?,
                      claim_phase='',next_attempt_at_utc='',updated_at=?,finished_at_utc=COALESCE(NULLIF(finished_at_utc,''),?) WHERE id=?""",
                   (log["id"],error,FAIL_MESSAGE,stamp,stamp,tid))
        stored=encode(proof)
        db.execute("""UPDATE d.pipeline_job SET status='failed',source_status='failed',error_message=?,canonical_receipt=?,
                      updated_at=?,finished_at=COALESCE(NULLIF(finished_at,''),?) WHERE id=? AND generation=?""",
                   (FAIL_MESSAGE,stored,stamp,stamp,job["id"],job["generation"]))
        db.execute("DELETE FROM d.pipeline_resource WHERE job_id=? AND generation=? AND quarantined=1 AND legacy=0",
                   (job["id"],job["generation"]))
        if not previous:
            db.execute("INSERT INTO d.pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,'x_tag_media_failure_settled',?,?)",
                       (job["id"],stored,stamp))
    db.execute("UPDATE a.x_auto_material_ledger SET last_task_status=?,updated_at=? WHERE task_id=?",(target,stamp,tid))
    if not previous or task["status"]!=target:
        db.execute("""INSERT INTO a.x_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at)
                      VALUES(?,?,'x_tag_original_receipt_settled',?,?,?,?,?)""",
                   (task["run_id"],tid,task["status"],target,FAIL_MESSAGE if kind=="upload" else "已核对原帖回执",encode(proof),stamp))
    db.execute("INSERT OR IGNORE INTO x_tag_settled_task VALUES(?,?,?,?,?)",(tid,qid,kind,encode(proof),stamp))
    refresh_run(db,task["run_id"],stamp)
    return "settled"


def refresh_waits(db, accounts):
    gates=rows(db,"""SELECT t.id AS task,t.execution_queue_id AS queue,t.account_id,g.source_key,g.retry_at,g.error,
                      w.retry_at AS previous FROM a.x_auto_task t JOIN a.x_auto_run r ON r.id=t.run_id
                      JOIN d.pipeline_media_gate g ON g.source_key='x:queue:'||t.execution_queue_id
                      JOIN d.pipeline_job j ON j.source_key=g.source_key AND j.stage='prepare'
                      LEFT JOIN a.x_tag_task_wait w ON w.task_id=t.id
                      WHERE r.status IN ('queued','running') AND t.status IN ('ready','retry_wait','reconciling')
                      AND g.phase='pending' AND g.error='x_account_verification_rejected'
                      AND j.status='queued' AND j.hold_reason='media_decision_pending' AND j.generation=0""")
    keep=set(); changes=0
    for g in gates:
        acc=accounts.execute("SELECT status,last_error,last_error_at FROM x_authorized_account WHERE id=?",(g["account_id"],)).fetchone()
        if not acc or acc[0]!="error" or not (acc[1] or "").startswith("x_account_verification_rejected"):
            continue
        try:
            failed_at=datetime.fromisoformat(acc[2].replace("Z","+00:00"))
        except (AttributeError,ValueError):
            continue
        keep.add(g["task"])
        # Anchor the retry to the actual refusal. Polls cannot push it forward indefinitely.
        due=(failed_at+timedelta(minutes=10)).astimezone(timezone.utc).isoformat(timespec="milliseconds")
        if due>g["retry_at"] and due>datetime.now(timezone.utc).isoformat(timespec="milliseconds"):
            db.execute("UPDATE d.pipeline_media_gate SET retry_at=? WHERE source_key=? AND phase='pending' AND error=? AND retry_at=?",
                       (due,g["source_key"],g["error"],g["retry_at"]))
            changes+=1
        if g["previous"]!=due:
            db.execute("""INSERT INTO a.x_tag_task_wait VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                          queue_id=excluded.queue_id,retry_at=excluded.retry_at""",(g["task"],g["queue"],due))
    for old in rows(db,"SELECT * FROM a.x_tag_task_wait"):
        if old["task_id"] in keep:
            continue
        db.execute("""UPDATE d.pipeline_media_gate SET retry_at='' WHERE source_key=? AND phase='pending'
                      AND error='x_account_verification_rejected' AND retry_at=?""",
                   ("x:queue:"+str(old["queue_id"]),old["retry_at"]))
        db.execute("DELETE FROM a.x_tag_task_wait WHERE task_id=?",(old["task_id"],))
    return dict(blocked=len(keep),backoff_changed=changes)


def candidates(db, since):
    successes=rows(db,"""SELECT t.id AS task,t.execution_queue_id AS queue,pub.job_id AS job,l.x_post_id AS post,'late' AS kind
                   FROM a.x_auto_task t JOIN x_post_publish_log l ON l.queue_id=t.execution_queue_id
                   JOIN d.pipeline_publication pub ON pub.source_key='x:queue:'||t.execution_queue_id AND pub.publish_id=l.x_post_id
                   WHERE t.status IN ('failed','unknown') AND l.status='published' AND l.unknown_outcome=0 AND l.x_post_id<>''
                   AND (julianday(l.published_at)>=julianday(?) OR julianday(t.finished_at_utc)>=julianday(?))""",(since,since))
    uploads=rows(db,"""SELECT DISTINCT t.id AS task,t.execution_queue_id AS queue,f.job_id AS job,'upload' AS kind
                 FROM x_tag_failed_delivery f JOIN a.x_auto_task t ON t.execution_queue_id=CAST(substr(f.source_key,9) AS INTEGER)
                 JOIN a.x_auto_run r ON r.id=t.run_id
                 JOIN x_post_publish_log l ON l.queue_id=t.execution_queue_id
                 WHERE t.unknown_outcome=1 AND r.status<>'canceled' AND l.status IN ('failed','reserved')
                 AND NOT EXISTS(SELECT 1 FROM x_tag_settled_task s WHERE s.task_id=t.id)""")
    return successes+uploads


def stored_receipts(db, job):
    return [dict(job=r["job_id"],id=r["receipt_id"],node=r["node_id"],generation=r["generation"],
                 route="jobs/fail",acked=True,post=False,error=r["error_code"])
            for r in db.execute("SELECT * FROM x_tag_failed_delivery WHERE job_id=?",(job,))]


def apply_items(db, items, evidence=None):
    result={"settled":[],"already":[],"review":[]}
    for item in items:
        try:
            db.execute("BEGIN IMMEDIATE")
            proof=(evidence or {}).get(str(item["job"]),[]) if evidence is not None else stored_receipts(db,item["job"])
            state=project(db,item,proof)
            db.commit()
            result[state].append(item["task"])
        except (Review,ReceiptReconciliationError,sqlite3.OperationalError) as exc:
            db.rollback()
            result["review"].append({"task":item["task"],"reason":str(exc)[:180]})
        except BaseException:
            db.rollback()
            raise
    return result
