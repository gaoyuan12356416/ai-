#!/usr/bin/env python3
"""Settle exact audited blockers without replaying any historical X queue."""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_x_run446 import RecoveryRejected, require, encoded, digest, plan_hash, now_utc, stamp, rows, one, backup_database

SCOPE = {2572: (417,46,5393,1418), 2702: (441,120,5857,1812), 2387: (414,45,5523,1562), 2781: (446,63,None,None)}
AUDIT = "x_auto_run446_blocker_settlement"
MEDIA = re.compile(r"x:POST:/2/media/upload(?:/initialize|/[A-Za-z0-9_.:-]+/append)\Z")
KIND = "x-run446-blocker-settlement-v1"


def connect(paths, writable=False):
    mode = "rw" if writable else "ro"
    c = sqlite3.connect(Path(paths["auto"]).resolve().as_uri()+"?mode="+mode,uri=True,timeout=10)
    c.row_factory=sqlite3.Row
    c.execute("ATTACH DATABASE ? AS native",(Path(paths["native"]).resolve().as_uri()+"?mode="+mode,))
    c.execute("ATTACH DATABASE ? AS dispatch",(Path(paths["central"]).resolve().as_uri()+"?mode="+mode,))
    if not writable:c.execute("PRAGMA query_only=ON")
    return c


def inspect(c, task_id, evidence, clock):
    require(task_id in SCOPE,"task_outside_scope")
    run_id,account,qid,parent_id=SCOPE[task_id]
    task=one(c,"SELECT * FROM x_auto_task WHERE id=?",(task_id,))
    require((task["run_id"],str(task["account_id"]),task["execution_queue_id"],task["execution_run_id"])==(run_id,str(account),qid,parent_id),"task_identity_changed")
    require(not any(task.get(k) for k in ("claim_token","claim_worker","lease_expires_at_utc","publish_id","publish_url")),"task_active_or_published")
    proof={"task":task}
    if task_id in (2572,2702):
        require(task["status"] in {"failed","retry_wait","unknown"} and task["unknown_outcome"]==1,"task_not_closed_unknown")
        audit=one(c,"SELECT * FROM native.x_run446_media_close_audit WHERE queue_id=?",(qid,))
        stored=json.loads(audit["evidence_json"])
        require(stored.get("finding")=="media_upload_stopped_before_post_intent"
                and stored.get("plan",{}).get("plan_sha256")==audit["plan_sha256"],"close_audit_invalid")
        queue=one(c,"SELECT id,account_id,manual_run_id,status FROM native.x_post_queue WHERE id=?",(qid,))
        log=one(c,"SELECT * FROM native.x_post_publish_log WHERE queue_id=?",(qid,))
        require((queue["account_id"],queue["manual_run_id"],queue["status"],log["status"],log["unknown_outcome"])
                ==(account,parent_id,"failed","failed",0) and log["account_id"]==account
                and not log["x_post_id"] and not log["published_at"],"canonical_not_closed")
        proof.update(audit=audit,queue=queue,log=log)
    elif task_id==2781:
        require(task["status"]=="pending" and not task["unknown_outcome"] and not task["material_id"],"pending_task_changed")
        blocker=one(c,"SELECT id,run_id,account_id,status,execution_queue_id,execution_run_id FROM x_auto_task WHERE id=2642")
        hold=one(c,"SELECT id,source_key,stage,status,hold_reason,generation,error_code FROM dispatch.pipeline_job WHERE id=32064")
        require(str(blocker["account_id"])=="63" and blocker["execution_queue_id"]==5452
                and blocker["execution_run_id"]==1476 and blocker["status"] in {"retry_wait","unknown","reconciling"},"blocker_changed")
        require((hold["source_key"],hold["stage"],hold["status"],hold["hold_reason"])
                ==("x:queue:5452","publish","unknown","operator_unknown_receipt_reconciliation"),"unknown_hold_not_explicit")
        proof.update(blocker=blocker,hold=hold)
    else:
        require(task["status"] in {"retry_wait","unknown","failed"} and task["unknown_outcome"]==1,"task_2387_changed")
        key="x:queue:5523"
        audit=one(c,"SELECT * FROM native.pipeline_x_media_only_no_post_audit WHERE job_id=31995")
        central_audit=one(c,"SELECT * FROM dispatch.pipeline_x_media_only_no_post_audit WHERE job_id=31995")
        applied=one(c,"SELECT job_id,plan_sha256,state FROM dispatch.pipeline_x_media_only_reconciliation_apply WHERE job_id=31995")
        permit=one(c,"SELECT * FROM native.pipeline_x_prepost_recovery WHERE source_key=?",(key,))
        require(audit==central_audit and applied["state"]=="committed" and applied["plan_sha256"]==audit["plan_sha256"]==permit["plan_sha256"],"prior_no_post_audit_disagrees")
        original=json.loads(audit["evidence_json"])
        require(original.get("finding")=="exact_generation_stopped_in_media_upload_before_Post"
                and original.get("plan",{}).get("plan_sha256")==audit["plan_sha256"],"prior_no_post_finding_invalid")
        require((permit["stage"],permit["from_generation"],permit["to_generation"],permit["prior_attempts"],permit["state"],permit["started_at"])==("publish",1,2,1,"armed",""),"prior_recovery_permit_changed")
        job=one(c,"SELECT * FROM dispatch.pipeline_job WHERE id=31995")
        require((job["source_key"],job["stage"],job["status"],job["generation"],job["error_code"])
                ==(key,"publish","failed",3,"x_recovery_generation_changed")
                and not any(job[k] for k in ("external_intent","canonical_receipt","hold_reason")),"generation_failure_changed")
        require(not job["lease_expires_at"] or stamp(job["lease_expires_at"])<clock,"worker_lease_active")
        native_ops=rows(c,"SELECT * FROM native.pipeline_x_operation WHERE source_key=? ORDER BY operation_id",(key,))
        central_ops=rows(c,"SELECT * FROM dispatch.pipeline_operation WHERE job_id=31995 ORDER BY operation_id")
        require(native_ops and central_ops and all(row["generation"]==1 and MEDIA.fullmatch(row["operation"]) for row in native_ops+central_ops),"post_or_later_operation")
        op_fields=("operation_id","generation","operation","request_sha256")
        require({tuple(row[k] for k in op_fields) for row in native_ops} <= {tuple(row[k] for k in op_fields) for row in central_ops},"operation_ledgers_disagree")
        original_proof=original["plan"].get("proof",{})
        require(all({tuple(row[k] for k in op_fields) for row in current}==
                    {tuple(row[k] for k in op_fields) for row in original_proof.get(name,[])}
                    for name,current in (("native_ops",native_ops),("central_ops",central_ops))),"operations_changed_since_no_post_audit")
        checkpoints=rows(c,"SELECT generation,phase FROM native.pipeline_x_checkpoint WHERE source_key=?",(key,))+rows(c,"SELECT generation,phase FROM dispatch.pipeline_checkpoint WHERE job_id=31995")
        require(all(row["phase"] in {"x_preflight","x_media_initialized"} and row["generation"]==1 for row in checkpoints),"later_checkpoint")
        for table in ("pipeline_receipt","pipeline_resource","pipeline_legacy_resource"):
            require(not rows(c,"SELECT * FROM dispatch."+table+" WHERE job_id=31995"),"later_or_active_evidence:"+table)
        require(not rows(c,"SELECT * FROM dispatch.pipeline_publication WHERE source_key=?",(key,)),"publication_exists")
        queue=one(c,"SELECT * FROM native.x_post_queue WHERE id=5523")
        log=one(c,"SELECT * FROM native.x_post_publish_log WHERE queue_id=5523")
        parent=one(c,"SELECT * FROM native.x_post_manual_run WHERE id=1562")
        require((queue["account_id"],queue["manual_run_id"],queue["status"],log["status"],log["attempt_count"],log["unknown_outcome"])
                ==(45,1562,"queued","reserved",1,0) and log["account_id"]==45
                and not any(log.get(k) for k in ("x_post_id","x_post_url","published_at")),"native_5523_changed")
        require(rows(c,"SELECT id FROM native.x_post_queue WHERE manual_run_id=1562")==[{"id":5523}]
                and parent["expected_count"]==parent["queued_count"]==1 and not parent["published_count"],"parent_scope_changed")
        ev=evidence or {}; receipt=ev.get("receipt",{})
        require(ev.get("job_id")==31995 and ev.get("generation")==3 and ev.get("worker_execution_stopped") is True
                and ev.get("no_later_success") is True and 0<=(clock-stamp(ev.get("checked_at"))).total_seconds()<=900,"node_evidence_required")
        require(receipt.get("route")=="jobs/fail" and receipt.get("error_code")=="x_recovery_generation_changed"
                and receipt.get("unknown_outcome") is False and receipt.get("acked") is False
                and re.fullmatch(r"[a-f0-9]{64}",str(receipt.get("payload_sha256",""))),"definitive_unacked_failure_required")
        raw_receipts=ev.get("receipts",[])
        require(len(raw_receipts)==1,"exact_failure_receipt_required")
        raw=raw_receipts[0]; payload=raw.get("payload",{})
        require(raw.get("route")=="jobs/fail" and not raw.get("acked_at")
                and raw.get("payload_sha256")==receipt["payload_sha256"]
                and payload.get("job_id")==31995 and payload.get("generation")==3
                and payload.get("error_code")=="x_recovery_generation_changed" and payload.get("unknown_outcome") is False
                and payload.get("claim_token_sha256")==job["claim_hash"]
                and payload.get("worker_id")==job["worker_id"] and payload.get("node_id")==job["node_id"]
                and "claim_token" not in payload,"failure_receipt_claim_mismatch")
        proof.update(audit=audit,applied=applied,permit=permit,job={k:v for k,v in job.items() if k not in {"updated_at","source_status","source_meta_json"}},
                     native_ops=native_ops,central_ops=central_ops,checkpoints=checkpoints,queue=queue,log=log,parent=parent,evidence=ev)
    return proof


def make_plan(paths, task_id, evidence=None, clock=None):
    clock=clock or now_utc(); paths={k:str(Path(v).resolve()) for k,v in paths.items()}
    with contextlib.closing(connect(paths)) as c:
        c.execute("BEGIN")
        proof=inspect(c,int(task_id),evidence,clock)
    plan={"kind":KIND,"task_id":int(task_id),"paths":paths,"proof":proof,"evidence":evidence,"captured_at":clock.isoformat()}
    plan["plan_sha256"]=plan_hash(plan)
    return plan


def apply_plan(plan, backup_dir, actor, clock=None):
    clock=clock or now_utc()
    require(plan.get("kind")==KIND and plan.get("plan_sha256")==plan_hash(plan),"plan_hash_changed")
    require(str(actor).strip(),"actor_required")
    fresh=make_plan(plan["paths"],plan["task_id"],plan.get("evidence"),clock)
    require(fresh["proof"]==plan["proof"],"plan_state_drift")
    backups={label:backup_database(path,backup_dir,"blocker-"+label) for label,path in plan["paths"].items()}
    task_id=plan["task_id"]; timestamp=clock.isoformat(timespec="seconds")
    with contextlib.closing(connect(plan["paths"],True)) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            proof=inspect(c,task_id,plan.get("evidence"),clock)
            require(proof==plan["proof"],"plan_state_drift")
            c.execute("CREATE TABLE IF NOT EXISTS "+AUDIT+"(task_id INTEGER PRIMARY KEY CHECK(task_id IN (2572,2702,2387,2781)),plan_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)")
            require(not c.execute("SELECT 1 FROM "+AUDIT+" WHERE task_id=?",(task_id,)).fetchone(),"already_settled")
            c.execute("INSERT INTO "+AUDIT+" VALUES(?,?,?,?)",(task_id,plan["plan_sha256"],encoded({"actor":actor,"plan":plan,"backups":backups}),timestamp))
            code="x_post_account_needs_review" if task_id==2781 else "x_recovery_generation_changed" if task_id==2387 else proof["log"]["error_code"]
            message=("同账号旧任务 2642（队列 5452）的发布结果仍待人工核对，本次未提交发布" if task_id==2781 else
                     "旧任务已核对为未创建帖子，保留原失败记录；未重新发布")
            if task_id==2387:
                require(c.execute("UPDATE native.x_post_queue SET status='failed',updated_at=? WHERE id=5523 AND status='queued'",(timestamp,)).rowcount==1,"queue_cas")
                require(c.execute("UPDATE native.x_post_publish_log SET status='failed',error_code=?,error_message=?,updated_at=? WHERE queue_id=5523 AND status='reserved' AND attempt_count=1 AND unknown_outcome=0",(code,message,timestamp)).rowcount==1,"log_cas")
                c.execute("UPDATE native.x_post_manual_run SET status='completed_with_errors',failed_count=1,unknown_count=0,finished_at=?,updated_at=? WHERE id=1562",(timestamp,timestamp))
                c.execute("INSERT INTO dispatch.pipeline_event(job_id,event_type,details_json,created_at) VALUES(31995,?,?,?)",("operator_run446_generation_failure_settled",encoded({"plan_sha256":plan["plan_sha256"],"actor":actor,"outbox_unchanged":True,"queue_retried":False}),timestamp))
            task=proof["task"]
            c.execute("UPDATE x_auto_task SET status='failed',unknown_outcome=0,error_code=?,error_message=?,claim_phase='',next_attempt_at_utc='',finished_at_utc=CASE WHEN finished_at_utc='' THEN ? ELSE finished_at_utc END,updated_at=? WHERE id=?",(code,message,timestamp,timestamp,task_id))
            c.execute("UPDATE x_auto_material_ledger SET last_task_status='failed',updated_at=? WHERE task_id=?",(timestamp,task_id))
            c.execute("INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(?,?,'operator_run446_blocker_settled',?,'failed',?,?,?)",(task["run_id"],task_id,task["status"],message,encoded({"plan_sha256":plan["plan_sha256"],"actor":actor,"queue_retried":False,"prior_unknown":task["unknown_outcome"]}),timestamp))
            statuses=[r[0] for r in c.execute("SELECT status FROM x_auto_task WHERE run_id=?",(task["run_id"],))]
            if all(s in {"published","failed","canceled","skipped","no_candidate"} for s in statuses):
                failed=sum(s in {"failed","canceled"} for s in statuses)
                target="failed" if failed==len(statuses) else "partial_failed" if failed else "completed"
                c.execute("UPDATE x_auto_run SET status=?,updated_at=?,finished_at_utc=CASE WHEN finished_at_utc='' THEN ? ELSE finished_at_utc END WHERE id=?",(target,timestamp,timestamp,task["run_id"]))
            c.commit()
        except Exception:c.rollback();raise
    return {"applied":True,"task_id":task_id,"plan_sha256":plan["plan_sha256"],"backups":backups,"platform_called":False,"outbox_modified":False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("action",choices=("plan","apply"))
    p.add_argument("--task-id",type=int);p.add_argument("--auto",default="/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3")
    p.add_argument("--native",default="/var/lib/x-post-automation/accounts.sqlite3");p.add_argument("--central",default="/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3")
    p.add_argument("--evidence");p.add_argument("--plan");p.add_argument("--backup-dir");p.add_argument("--actor");p.add_argument("--output",required=True)
    args=p.parse_args()
    with os.fdopen(os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),"w",encoding="utf-8") as out:
        if args.action=="plan":
            result=make_plan({k:getattr(args,k) for k in ("auto","native","central")},args.task_id,json.loads(Path(args.evidence).read_text()) if args.evidence else None)
        else:
            require(args.plan and args.backup_dir and args.actor,"apply_arguments_required")
            result=apply_plan(json.loads(Path(args.plan).read_text()),args.backup_dir,args.actor)
        out.write(json.dumps(result,ensure_ascii=False,indent=2)+"\n");out.flush();os.fsync(out.fileno())
    print(encoded({"ok":True,"task_id":result["task_id"],"plan_sha256":result["plan_sha256"]}))


if __name__=="__main__":
    try:main()
    except RecoveryRejected as exc:print(encoded({"ok":False,"code":str(exc)}));raise SystemExit(2)
