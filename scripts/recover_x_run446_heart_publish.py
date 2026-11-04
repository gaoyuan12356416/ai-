#!/usr/bin/env python3
"""Rearm only HeartFlare's prepared, zero-attempt attribution failure.

Keeps task 2712 / manual run 1826 / queue 5908 and its final artifact intact.
The normal dispatcher performs the next publication; this tool calls no platform.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.x_auto_posts.operator_retry import EXTERNAL_KEY, frozen_identity, fingerprint
from features.x_posts.random_template import validate_receipt

KIND = "x-run446-heart-prepared-publish-v1"
KEY = "x:queue:5908"
AUDIT = "x_auto_run446_heart_publish_recovery"
ERROR = "x_attribution_resolution_unavailable"


class RecoveryRejected(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise RecoveryRejected(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def plan_hash(value):
    return digest({k:v for k,v in value.items() if k != "plan_sha256"})


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def stamp(value):
    try:
        result=dt.datetime.fromisoformat(str(value).replace("Z","+00:00"))
    except ValueError as exc:
        raise RecoveryRejected("timestamp_invalid") from exc
    require(result.tzinfo is not None,"timezone_required")
    return result


def rows(c, sql, parameters=()):
    return [dict(row) for row in c.execute(sql,parameters)]


def one(c, sql, parameters=()):
    found=rows(c,sql,parameters);require(len(found)==1,"exact_row_required")
    return found[0]


def backup_database(path, directory, label):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=directory/(label+"-"+uuid.uuid4().hex+".sqlite3")
    os.close(os.open(str(target),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600))
    with contextlib.closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)) as source:
        with contextlib.closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            require(destination.execute("PRAGMA quick_check").fetchall()==[("ok",)],"backup_invalid")
    with target.open("r+b") as stream:
        os.fsync(stream.fileno());hasher=hashlib.sha256()
        for chunk in iter(lambda:stream.read(1024*1024),b""):hasher.update(chunk)
    return {"path":str(target.resolve()),"sha256":hasher.hexdigest()}


def artifact_from_receipt(request, result):
    try:
        validate_receipt(request, result)
    except Exception as exc:
        raise RecoveryRejected("native_final_receipt_invalid") from exc
    return {"url":result["output_url"],"sha256":result["output_sha256"],"size":result["output_size"],
            "mime":"video/mp4","duration_seconds":result["probe"]["duration"],"profile":result["profile"],"recipe":result["recipe"]}


def connect(paths, writable=False):
    # SQLite BEGIN IMMEDIATE needs rw attached DBs even if no native write is
    # intended. Deny native mutations explicitly, rather than readonly ATTACH.
    mode = "rw" if writable else "ro"
    c = sqlite3.connect(Path(paths["auto"]).resolve().as_uri()+"?mode="+mode, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    for alias, key in (("native", "native"), ("dispatch", "central")):
        c.execute("ATTACH DATABASE ? AS "+alias, (Path(paths[key]).resolve().as_uri()+"?mode="+mode,))
    if not writable:
        c.execute("PRAGMA query_only=ON")
    else:
        denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
                  sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_CREATE_TRIGGER,
                  sqlite3.SQLITE_DROP_TRIGGER}
        c.set_authorizer(lambda action, arg1, arg2, database, trigger:
                         sqlite3.SQLITE_DENY if database == "native" and action in denied else sqlite3.SQLITE_OK)
    return c


def validate_evidence(evidence, clock):
    require(isinstance(evidence, dict), "fix_evidence_required")
    require((evidence.get("job_id"), evidence.get("generation"), evidence.get("node_id")) ==
            (33431, 1, "cpu-main"), "evidence_scope_changed")
    require(evidence.get("worker_execution_stopped") is True
            and evidence.get("outbox_pending_for_scope") == 0
            and evidence.get("attribution_verified") is True, "recovery_not_verified")
    require(re.fullmatch(r"[a-f0-9]{40}", str(evidence.get("fix_revision", ""))), "fix_revision_required")
    require(0 <= (clock-stamp(evidence.get("checked_at"))).total_seconds() <= 900, "evidence_stale")


def inspect(c, clock):
    task = one(c, "SELECT * FROM x_auto_task WHERE id=2712")
    require((task["run_id"], task["template_id"], task["template_version"], str(task["account_id"]),
             task["execution_run_id"], task["execution_queue_id"], task["status"], task["error_code"]) ==
            (446, 4, 6, "46", 1826, 5908, "failed", ERROR), "auto_identity_or_failure_changed")
    require(not any(task.get(k) for k in ("unknown_outcome", "publish_id", "publish_url", "claim_token",
                "claim_worker", "lease_expires_at_utc")), "auto_active_unknown_or_published")
    retry = one(c, "SELECT * FROM x_auto_operator_retry WHERE task_id=2712")
    identity = SimpleNamespace(**dict(task, account_snapshot=json.loads(task["account_snapshot_json"])))
    config = one(c, "SELECT config_sha256 FROM x_auto_run WHERE id=446")
    require((retry["generation"], retry["prior_run_id"], retry["external_task_key"]) == (1,1657,EXTERNAL_KEY)
            and retry["identity_sha256"] == fingerprint(frozen_identity(identity))
            and retry["config_sha256"] == config["config_sha256"], "operator_retry_audit_changed")
    queue = one(c, "SELECT * FROM native.x_post_queue WHERE id=5908")
    log = one(c, "SELECT * FROM native.x_post_publish_log WHERE queue_id=5908")
    parent = one(c, "SELECT * FROM native.x_post_manual_run WHERE id=1826")
    require((queue["account_id"],queue["manual_run_id"],queue["status"],log["id"],log["account_id"],log["status"]) ==
            (46,1826,"queued",5853,46,"reserved"), "native_identity_changed")
    require(not any(log.get(k) for k in ("attempt_count","unknown_outcome","x_media_id","x_post_id","x_post_url","started_at","published_at")), "native_publish_evidence")
    require((parent["trigger_source"],parent["external_task_key"],parent["template_ref"],parent["template_version"],
             parent["expected_count"],parent["queued_count"],parent["published_count"],parent["unknown_count"]) ==
            ("auto_template",EXTERNAL_KEY,"x-auto-template-4",6,1,1,0,0), "native_parent_changed")
    require(rows(c,"SELECT id FROM native.x_post_queue WHERE manual_run_id=1826") == [{"id":5908}], "parent_scope_changed")
    require(str(task["material_id"]) == str(queue["material_id"]) and task["body_template"] == queue["body_template"], "frozen_selection_changed")
    ledger = one(c,"SELECT * FROM x_auto_material_ledger WHERE task_id=2712")
    require((ledger["material_id"],ledger["canonical_queue_id"],ledger["last_task_status"]) ==
            (str(queue["material_id"]),5908,"failed"), "material_ledger_changed")
    account = one(c,"SELECT id,status,publish_approved FROM native.x_authorized_account WHERE id=46")
    require(account["status"] == "active" and account["publish_approved"] == 1, "account_not_publishable")
    for table in ("pipeline_x_operation", "pipeline_canonical_hold"):
        require(not rows(c,"SELECT * FROM native."+table+" WHERE source_key=?",(KEY,)), "native_write_or_hold")
    require(not rows(c,"SELECT * FROM native.x_post_repost_ledger WHERE queue_id=5908"), "native_repost_evidence")
    prep = one(c,"SELECT * FROM native.x_post_random_template_preparation WHERE queue_id=5908")
    require(prep["status"] == "ready", "final_artifact_not_ready")
    request,result = json.loads(prep["request_json"]),json.loads(prep["result_json"])
    artifact = artifact_from_receipt(request,result)
    source = one(c,"SELECT * FROM native.pipeline_x_artifact WHERE queue_id=5908")
    source_artifact = json.loads(source["artifact_json"])
    require(request["queue_id"] == 5908 and request.get("content_id") == str(queue["content_id"])
            and request["source_sha256"] == queue["preflight_sha256"] == source_artifact.get("sha256")
            and request["source_size"] == queue["preflight_size"] == source_artifact.get("size")
            and request["source_url"] == source_artifact.get("url") and source["source_url"] == queue["material_url"], "source_identity_changed")
    jobs = rows(c,"SELECT * FROM dispatch.pipeline_job WHERE source_key=? ORDER BY id",(KEY,))
    require(len(jobs)==2 and [(j["id"],j["stage"],j["status"],j["generation"]) for j in jobs] ==
            [(33356,"prepare","completed",1),(33431,"publish","failed",1)], "dispatch_stages_changed")
    histories,acks = [],[]
    for job in jobs:
        require((job["source_id"],job["channel"],job["family"]) == ("5908","x","queue")
                and not job["external_intent"] and not job["hold_reason"], "dispatch_write_or_hold")
        meta = json.loads(job["source_meta_json"])
        require(meta.get("canonical_fingerprint") == canonical_fingerprint(queue,"id")
                and not any(meta.get(k) for k in ("unknown_outcome","reconcile_only","source_attempt_count","publish_id")), "dispatch_frozen_identity_changed")
        require(all(json.loads(job["artifact_json"]).get(k) == v for k,v in artifact.items()), "final_artifact_changed")
        require(not job["lease_expires_at"] or stamp(job["lease_expires_at"]) < clock, "worker_lease_active")
        for table in ("pipeline_operation","pipeline_resource","pipeline_legacy_resource"):
            require(not rows(c,"SELECT * FROM dispatch."+table+" WHERE job_id=?",(job["id"],)), "dispatch_write_or_fence")
        history = one(c,"SELECT * FROM dispatch.pipeline_claim_history WHERE job_id=?",(job["id"],))
        require((history["generation"],history["claim_hash"],history["node_id"],history["worker_id"]) ==
                (1,job["claim_hash"],job["node_id"],job["worker_id"]), "claim_identity_changed")
        histories.append(history)
    pub = jobs[1]
    require(pub["error_code"] == ERROR and not pub["canonical_receipt"]
            and stamp(pub["finished_at"]) <= clock-dt.timedelta(seconds=120), "failure_not_settled")
    for table in ("pipeline_checkpoint","pipeline_receipt"):
        require(not rows(c,"SELECT * FROM dispatch."+table+" WHERE job_id=33431"), "publish_receipt_exists")
    require(not rows(c,"SELECT * FROM dispatch.pipeline_publication WHERE source_key=?",(KEY,)), "publication_exists")
    acks = rows(c,"SELECT * FROM dispatch.pipeline_delivery WHERE job_id=33431 ORDER BY delivery_id")
    require(acks and all(r["generation"]==1 and r["claim_hash"]==pub["claim_hash"]
            and json.loads(r["response_json"]).get("accepted") is True
            and json.loads(r["response_json"]).get("status")=="failed" for r in acks), "failed_ack_missing")
    failed = rows(c,"SELECT * FROM dispatch.pipeline_event WHERE job_id=33431 AND event_type='failed' ORDER BY id")
    require(failed and all(json.loads(r["details_json"]).get("code")==ERROR
            and json.loads(r["details_json"]).get("unknown") is False for r in failed), "failure_not_definitive")
    bindings = rows(c,"SELECT * FROM native.pipeline_x_claim WHERE source_key=?",(KEY,))
    require(len(bindings)==1 and (bindings[0]["stage"],bindings[0]["generation"],bindings[0]["queue_id"],bindings[0]["log_id"],bindings[0]["claim_hash"]) ==
            ("prepare",1,5908,5853,jobs[0]["claim_hash"]), "native_publish_binding_present")
    checkpoints = rows(c,"SELECT * FROM native.pipeline_x_checkpoint WHERE source_key=?",(KEY,))
    require(checkpoints and all(r["phase"]=="x_preflight" and r["generation"]==1 for r in checkpoints), "native_publish_checkpoint")
    stable_jobs = [{k:v for k,v in j.items() if k not in {"updated_at","source_status"}} for j in jobs]
    return {"task_id":2712,"queue_id":5908,"job_id":33431,"generation":1,
            "final_sha256":artifact["sha256"],"final_size":artifact["size"],
            "hashes":{k:digest(v) for k,v in {"task":task,"retry":retry,"queue":queue,"log":log,
                "parent":parent,"ledger":ledger,"preparation":prep,"source":source,"jobs":stable_jobs,
                "histories":histories,"acks":acks,"failed":failed,"bindings":bindings,"checkpoints":checkpoints}.items()}}


def make_plan(paths, evidence, clock=None):
    clock=clock or now_utc();validate_evidence(evidence,clock)
    paths={k:str(Path(v).resolve()) for k,v in paths.items()}
    with contextlib.closing(connect(paths)) as c:
        c.execute("BEGIN");proof=inspect(c,clock)
    plan={"kind":KIND,"paths":paths,"proof":proof,"evidence":evidence,"captured_at":clock.isoformat()}
    plan["plan_sha256"]=plan_hash(plan)
    return plan


def apply_plan(plan, backup_dir, actor, clock=None):
    clock=clock or now_utc()
    require(plan.get("kind")==KIND and plan.get("plan_sha256")==plan_hash(plan),"plan_hash_changed")
    require(str(actor).strip(),"actor_required");validate_evidence(plan["evidence"],clock)
    require(make_plan(plan["paths"],plan["evidence"],clock)["proof"]==plan["proof"],"plan_state_drift")
    backups={k:backup_database(v,backup_dir,"heart-publish-"+k) for k,v in plan["paths"].items()}
    instant=clock.isoformat(timespec="seconds")
    with contextlib.closing(connect(plan["paths"],True)) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            require(inspect(c,clock)==plan["proof"],"plan_state_drift")
            c.execute("CREATE TABLE IF NOT EXISTS "+AUDIT+"(task_id INTEGER PRIMARY KEY CHECK(task_id=2712),plan_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)")
            require(not rows(c,"SELECT * FROM "+AUDIT),"already_recovered")
            audit={"actor":actor,"plan":plan,"backups":backups,"native_modified":False,"platform_called":False}
            c.execute("INSERT INTO "+AUDIT+" VALUES(2712,?,?,?)",(plan["plan_sha256"],encoded(audit),instant))
            require(c.execute("UPDATE dispatch.pipeline_job SET status='ready',started_at='',finished_at='',lease_expires_at='',claim_hash='',node_id='',worker_id='',error_code='',error_message='',updated_at=? WHERE id=33431 AND stage='publish' AND status='failed' AND generation=1 AND external_intent=0 AND error_code=?",(instant,ERROR)).rowcount==1,"dispatch_cas")
            require(c.execute("UPDATE x_auto_task SET status='retry_wait',error_code='',error_message='',next_attempt_at_utc=?,finished_at_utc='',updated_at=? WHERE id=2712 AND status='failed' AND unknown_outcome=0 AND execution_run_id=1826 AND execution_queue_id=5908 AND error_code=?",(instant,instant,ERROR)).rowcount==1,"auto_cas")
            require(c.execute("UPDATE x_auto_material_ledger SET last_task_status='retry_wait',updated_at=? WHERE task_id=2712 AND canonical_queue_id=5908 AND last_task_status='failed'",(instant,)).rowcount==1,"ledger_cas")
            c.execute("UPDATE x_auto_run SET status='running',finished_at_utc='',updated_at=? WHERE id=446 AND status IN ('completed','partial_failed','failed','needs_review')",(instant,))
            detail=encoded({"actor":actor,"plan_sha256":plan["plan_sha256"],"fix_revision":plan["evidence"]["fix_revision"],"queue_id":5908,"artifact_reused":True,"reselected":False})
            c.execute("INSERT INTO dispatch.pipeline_event(job_id,event_type,details_json,created_at) VALUES(33431,'audited_run446_heart_publish_recovery',?,?)",(detail,instant))
            c.execute("INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(446,2712,'operator_prepared_publish_rearmed','failed','retry_wait',?,?,?)",("归因修复后继续原队列发布，保留已完成视频及原素材",detail,instant))
            c.commit()
        except Exception:
            c.rollback();raise
    return {"applied":True,"task_id":2712,"queue_id":5908,"job_id":33431,"plan_sha256":plan["plan_sha256"],"backups":backups,"native_modified":False,"platform_called":False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("action",choices=("plan","apply"))
    p.add_argument("--auto",default="/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3")
    p.add_argument("--native",default="/var/lib/x-post-automation/accounts.sqlite3")
    p.add_argument("--central",default="/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3")
    p.add_argument("--evidence");p.add_argument("--plan");p.add_argument("--backup-dir");p.add_argument("--actor");p.add_argument("--output",required=True)
    args=p.parse_args()
    with os.fdopen(os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),"w",encoding="utf-8") as out:
        if args.action=="plan":
            require(args.evidence,"evidence_required")
            result=make_plan({k:getattr(args,k) for k in ("auto","native","central")},json.loads(Path(args.evidence).read_text()))
        else:
            require(args.plan and args.backup_dir and args.actor,"apply_arguments_required")
            result=apply_plan(json.loads(Path(args.plan).read_text()),args.backup_dir,args.actor)
        out.write(json.dumps(result,ensure_ascii=False,indent=2)+"\n");out.flush();os.fsync(out.fileno())
    print(encoded({"ok":True,"action":args.action,"plan_sha256":result["plan_sha256"]}))


if __name__=="__main__":
    try:main()
    except RecoveryRejected as exc:print(encoded({"ok":False,"code":str(exc)}));raise SystemExit(2)
