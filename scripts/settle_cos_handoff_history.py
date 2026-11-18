"""Converge stale queue projections with an existing pre-handoff FB terminal result."""
import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path


def main(args):
    mode="rw" if args.apply else "ro"
    c=sqlite3.connect(Path(args.central).resolve().as_uri()+"?mode="+mode,uri=True,timeout=8)
    c.row_factory=sqlite3.Row
    c.execute("attach database ? as native",(Path(args.native).resolve().as_uri()+"?mode=ro",))
    ledger=sqlite3.connect(Path(args.snapshot).resolve().as_uri()+"?mode=ro",uri=True)
    try:
        # Keep the native database read-only. Only the main database needs
        # to acquire a write lock when the verified queue row is updated.
        c.execute("begin")
        for jid in args.ids:
            j=c.execute("select * from pipeline_job where id=?",(jid,)).fetchone()
            event="cos_handoff_historical_failure_confirmed"
            if c.execute("select 1 from pipeline_event where job_id=? and event_type=?",(jid,event)).fetchone():
                continue
            if (j["node_id"],j["channel"],j["family"],j["stage"],j["status"],j["source_status"],j["error_code"])!=(
                    "cos-cloud","fb","auto","prepare","queued","failed","lease_expired") or j["external_intent"] or j["hold_reason"]:
                raise ValueError("dispatch state changed")
            w=ledger.execute("select paid,job from work where identity like ?",(str(jid)+":%",)).fetchall()
            if len(w)!=1 or w[0][0] or json.loads(w[0][1])["source_key"]!=j["source_key"]:
                raise ValueError("not an unpaid handoff row")
            t=c.execute("select * from native.fb_auto_task where id=?",(int(j["source_id"]),)).fetchone()
            if (not t or t["status"]!="failed" or not (""<t["completed_at_utc"]<args.cutover) or
                    t["error_code"]!="fb_auto_prepare_worker_interrupted" or
                    any(t[k] for k in ("graph_post_id","unknown_outcome","lease_owner","prepared_media_url"))):
                raise ValueError("prior failure not verified")
            for tab in ("pipeline_cloud_intent","pipeline_receipt","pipeline_operation","pipeline_resource","pipeline_legacy_resource","pipeline_publication","pipeline_checkpoint"):
                if c.execute("select 1 from "+tab+" where job_id=? limit 1",(jid,)).fetchone():
                    raise ValueError("active resource or external intent")
            if args.apply:
                now=dt.datetime.now(dt.timezone.utc).isoformat()
                proof=json.dumps({"source_key":j["source_key"],"source_status":"failed","error_code":t["error_code"],"completed_at":t["completed_at_utc"]})
                c.execute("""update pipeline_job set status='failed',error_code=?,error_message=?,finished_at=?,updated_at=?,
                              canonical_receipt=? where id=? and status='queued'""",(t["error_code"],t["error_message"],t["completed_at_utc"],now,proof,jid))
                c.execute("insert into pipeline_event(job_id,event_type,details_json,created_at) values(?,?,?,?)",(jid,event,proof,now))
        if args.apply:c.commit()
        else:c.rollback()
        print("verified pre-existing failures",args.ids,"apply",args.apply)
    finally:c.close();ledger.close()


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("central","native","snapshot","cutover"):p.add_argument("--"+k,required=True)
    p.add_argument("--apply",action="store_true");p.add_argument("ids",type=int,nargs="+")
    main(p.parse_args())
