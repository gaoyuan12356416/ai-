"""Return unsubmitted pre-handoff FB tasks to the existing frozen preparation claim."""
import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from recover_fb_prepared_receipts import require, dumps, FBAutoPostStore


def main(a):
    snap=sqlite3.connect(Path(a.snapshot).resolve().as_uri()+"?mode=ro",uri=True)
    mode="rw" if a.apply else "ro";c=sqlite3.connect(Path(a.central).resolve().as_uri()+"?mode="+mode,uri=True,timeout=10);c.row_factory=sqlite3.Row
    c.execute("attach database ? as native",(Path(a.native).resolve().as_uri()+"?mode="+mode,))
    now=dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        c.execute("begin immediate" if a.apply else "begin")
        for jid in a.ids:
            q=c.execute("select * from pipeline_job where id=?",(jid,)).fetchone()
            require(q and (q["channel"],q["family"],q["stage"])==("fb","auto","prepare"),"scope changed")
            key=q["source_key"];tid=int(q["source_id"])
            if c.execute("select 1 from pipeline_event where job_id=? and event_type='cos_unsubmitted_handoff_restored'",(jid,)).fetchone():continue
            require(q["status"]=="queued" and q["error_code"]=="lease_expired" and not q["external_intent"] and not q["hold_reason"],"dispatch claim active or held")
            f=c.execute("select * from native.pipeline_fb_claim where source_key=? and stage='prepare' order by generation desc limit 1",(key,)).fetchone()
            require(f and not f["receipt_json"],"frozen claim unavailable")
            w=snap.execute("select paid,job from work where identity=?",(str(jid)+":"+str(f["generation"]),)).fetchone()
            require(w and w[0]==0 and json.loads(w[1])["source_key"]==key,"not unpaid at handoff")
            frozen=json.loads(f["task_json"]);t=c.execute("select * from native.fb_auto_task where id=?",(tid,)).fetchone()
            expected=json.loads(f["descriptor_json"])["request"]
            require(all(frozen[k]==t[k] for k in ("id","run_id","page_id","template_id","template_version","material_id","content_id","gpu_job_id","source_media_url","planned_publish_at_utc")) and expected==json.loads(w[1])["payload"]["request"],"frozen selection changed")
            require(t["status"]=="failed" and t["error_code"]=="fb_auto_prepare_worker_interrupted" and t["completed_at_utc"]>=a.cutover and
                    not any(t[k] for k in ("graph_post_id","unknown_outcome","lease_owner","lease_expires_at_utc","prepared_sha256","prepared_media_url")),"not an unsubmitted handoff timeout")
            require(frozen["lease_owner"]=="pipeline:cos-cloud:"+str(f["generation"]) and frozen["lease_expires_at_utc"]>now,"original preparation lease expired")
            tpl=c.execute("select status,current_version from native.fb_auto_template where id=?",(t["template_id"],)).fetchone()
            require(tuple(tpl)==("enabled",t["template_version"]) and (t["schedule_policy"]=="target_midnight_v1" or t["planned_publish_at_utc"]>now),"schedule changed")
            for table in ("pipeline_cloud_intent","pipeline_operation","pipeline_checkpoint","pipeline_receipt","pipeline_publication","pipeline_resource","pipeline_legacy_resource"):
                require(not c.execute("select 1 from "+table+" where job_id=? limit 1",(jid,)).fetchone(),"existing action "+table)
            for table,col,val in [("pipeline_fb_operation","source_key",key),("pipeline_canonical_hold","source_key",key),("fb_auto_publish_attempt","task_id",tid),("fb_auto_publish_ledger","task_id",tid)]:
                require(not c.execute("select 1 from native."+table+" where "+col+"=? limit 1",(val,)).fetchone(),"existing native action")
            require(not c.execute("select 1 from pipeline_job where source_key=? and stage='publish'",(key,)).fetchone(),"publish stage exists")
            require(not c.execute("""select 1 from native.fb_auto_task where id<>? and page_id=? and material_id=?
                and status in ('ready','running','preparing','publishing','submitted','published','unknown') limit 1""",(tid,t["page_id"],t["material_id"])).fetchone(),"competing material")
            require(not c.execute("""select 1 from native.fb_auto_publish_ledger where page_id=? and material_id=?
                and (graph_post_id<>'' or status in ('published','submitted','unknown','running') or unknown_outcome=1) limit 1""",(t["page_id"],t["material_id"])).fetchone(),"material already delivered")
            if a.apply:
                c.execute("""update native.fb_auto_task set status='preparing',lease_owner=?,lease_expires_at_utc=?,completed_at_utc='',error_code='',error_message=''
                     where id=? and status='failed'""",(frozen["lease_owner"],frozen["lease_expires_at_utc"],tid))
                FBAutoPostStore._refresh_run(c,t["run_id"],now)
                c.execute("update pipeline_job set source_status='preparing',updated_at=? where id=? and status='queued'",(now,jid))
                c.execute("insert into pipeline_event(job_id,event_type,details_json,created_at) values(?,?,?,?)",(jid,"cos_unsubmitted_handoff_restored",dumps({"previous_error":t["error_code"],"previous_completed_at":t["completed_at_utc"],"original_generation":f["generation"]}),now))
        if a.apply:c.commit()
        else:c.rollback()
        print("verified frozen preparation scope",a.ids,"apply",a.apply)
    finally:c.close();snap.close()


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("central","native","snapshot","cutover"):p.add_argument("--"+k,required=True)
    p.add_argument("--apply",action="store_true");p.add_argument("ids",type=int,nargs="+")
    main(p.parse_args())
