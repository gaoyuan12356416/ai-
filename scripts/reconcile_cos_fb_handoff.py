"""Restore pre-publication FB preparations from verified, already-paid handoff receipts."""
import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from recover_fb_prepared_receipts import restore, require, dumps, digest, _artifact
from features.pipeline_dispatch.cloud_jobs import fingerprint


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)


def reconcile(args):
    items=json.loads(Path(args.evidence).read_text())
    require(items and len({i["id"] for i in items})==len(items), "non-unique handoff items")
    operation=fingerprint(items)
    ledger=readonly(args.snapshot);ledger.row_factory=sqlite3.Row
    central=sqlite3.connect(Path(args.central).resolve().as_uri()+("?mode=rw" if args.apply else "?mode=ro"),uri=True,timeout=10)
    central.row_factory=sqlite3.Row
    central.execute("ATTACH DATABASE ? AS native",(Path(args.native).resolve().as_uri()+("?mode=rw" if args.apply else "?mode=ro"),))
    central.execute("BEGIN IMMEDIATE" if args.apply else "BEGIN")
    try:
        plan=[];evidence=[]
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        for item in items:
            jobid=int(item["id"]);source=item["source_key"];gen=item["generation"]
            j=central.execute("select * from pipeline_job where id=?",(jobid,)).fetchone()
            require(j and (j["node_id"],j["channel"],j["family"],j["stage"],j["generation"],j["source_key"])
                    ==("cos-cloud","fb","auto","prepare",gen,source),"changed job")
            t=central.execute("select * from native.fb_auto_task where id=?",(int(j["source_id"]),)).fetchone()
            a=_artifact(item["preparation"])
            audit=json.loads(t["selection_json"] or "{}").get("preparation_receipt_recovery",{})
            if j["status"]=="completed" and audit.get("operation_sha256")==operation and t["prepared_sha256"]==a["sha256"]:
                continue
            require(j["status"]=="failed" and t["status"]=="failed" and
                    (j["error_code"],t["error_code"]) in {("cos_receipt_pending","fb_auto_prepare_worker_interrupted"),
                    ("cos_frozen_action_conflict","cos_frozen_action_conflict")},"not a matching preparation failure")
            require(not any(j[k] for k in ("external_intent","grandfathered","hold_reason")) and
                    not any(t[k] for k in ("graph_post_id","unknown_outcome","lease_owner","lease_expires_at_utc",
                        "prepared_sha256","prepared_media_url")),"already owned or delivered")
            require(t["planned_publish_at_utc"]>now, "scheduled time passed")
            tpl=central.execute("select status,current_version from native.fb_auto_template where id=?",(t["template_id"],)).fetchone()
            require(tuple(tpl)==("enabled",t["template_version"]), "template changed")
            original=ledger.execute("select paid,job from work where identity=?",(str(jobid)+":"+str(gen),)).fetchone()
            require(original and original["paid"],"not a paid handoff")
            job=json.loads(original["job"])
            require(job["payload"]["request"]==item["request"] and job["source_key"]==source and job["id"]==jobid,"source changed")
            require(a["profile"]==item["request"]["expected_profile"] and a["job_id"]==t["gpu_job_id"] and
                    a["content_id"]==t["content_id"] and item["request"]["source_url"]==t["source_media_url"],"artifact mismatch")
            ck=central.execute("select * from native.pipeline_fb_claim where source_key=? and stage='prepare' and generation=?",(source,gen)).fetchone()
            hist=central.execute("select * from pipeline_claim_history where job_id=? and generation=?",(jobid,gen)).fetchone()
            require(ck and hist and not ck["receipt_json"] and hist["claim_hash"]==ck["claim_hash"]==digest(job["claim_token"]),"claim mismatch")
            frozen=json.loads(ck["task_json"])
            require(all(frozen[k]==t[k] for k in ("id","run_id","page_id","template_id","template_version","planned_publish_at_utc","material_id","content_id","gpu_job_id","source_media_url")),"selection changed")
            old=ledger.execute("select digest,provider_id from action where identity=?",(item["action"],)).fetchone()
            require(tuple(old)==(item["binding"],item["provider_id"]) and
                    central.execute("select 1 from pipeline_cloud_intent where job_id=? and generation=? and action_key=?",(jobid,gen,item["action"])).fetchone(),"paid identity mismatch")
            checks=[("pipeline_job","source_key=? and stage='publish' ",source),("pipeline_publication","source_key=?",source),
                    ("native.pipeline_fb_operation","source_key=?",source),("native.pipeline_canonical_hold","source_key=?",source),
                    ("native.fb_auto_publish_attempt","task_id=?",t["id"]),("native.fb_auto_publish_ledger","task_id=?",t["id"])]
            for tab in ("pipeline_resource","pipeline_legacy_resource","pipeline_operation","pipeline_checkpoint","pipeline_receipt"):
                checks.append((tab,"job_id=?",jobid))
            for tab,where,val in checks:
                require(not central.execute("select 1 from "+tab+" where "+where+" limit 1",(val,)).fetchone(),"publication or hold present "+tab)
            require(not central.execute("""select 1 from native.fb_auto_publish_ledger where page_id=? and material_id=?
                and (status in ('published','submitted','running','unknown') or graph_post_id<>'' or unknown_outcome=1) limit 1""",
                (t["page_id"],t["material_id"])).fetchone(),"material already published")
            require(not central.execute("""select 1 from native.fb_auto_task where id<>? and page_id=? and material_id=?
                and status in ('ready','running','preparing','publishing','submitted','unknown') limit 1""",
                (t["id"],t["page_id"],t["material_id"])).fetchone(),"material in use")
            payload={"job_id":jobid,"generation":gen,"node_id":"cos-cloud","worker_id":job["worker_id"],
                "claim_token":job["claim_token"],"receipt_id":fingerprint([operation,jobid]),
                "result":{"status":"ready","artifact":a,"preparation":item["preparation"]}}
            evidence.append({"job_id":jobid,"payload":payload,"receipt_created_at":now})
            plan.append({"job_id":jobid,"task_id":t["id"],"source_key":source,"generation":gen})
        if args.apply and plan:
            restore(central,{"captured_at":now,"items":evidence},{"operation_sha256":operation,"items":plan})
            central.commit()
        else:
            central.rollback()
        print("applied" if args.apply else "verified",len(plan),"already reconciled",len(items)-len(plan),operation)
    finally:
        central.close();ledger.close()


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for n in ("central","native","snapshot","evidence"):p.add_argument("--"+n,required=True)
    p.add_argument("--apply",action="store_true")
    reconcile(p.parse_args())
