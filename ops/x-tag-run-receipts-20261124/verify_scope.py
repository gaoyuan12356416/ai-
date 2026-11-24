"""Read back original attempts, immutable bindings, reconciled outcomes and noon queues."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path("/mnt/data-disk/x-tag-run-receipts-20261124")
LIVE = ("/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3",
        "/mnt/data-disk/x-tag-publisher/posts.sqlite3","/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3")


def open_db(which):
    paths = LIVE if which == "live" else [ROOT/which/file for file in ("auto.sqlite3","posts.sqlite3","dispatch.sqlite3")]
    urls = [Path(p).resolve(strict=True).as_uri()+"?mode=ro" for p in paths]
    db = sqlite3.connect(urls[0],uri=True,timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("ATTACH DATABASE ? AS p",(urls[1],))
    db.execute("ATTACH DATABASE ? AS d",(urls[2],))
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def digest(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def original(db, item):
    key = "x:queue:"+str(item["queue"])
    records = db.execute("""SELECT t.id,t.run_id,t.template_id,t.template_version,t.account_id,t.material_id,
        t.content_id,t.scheduled_at_utc,t.selection_json,t.body_sha256,t.execution_run_id,t.execution_queue_id,
        q.id,q.manual_run_id,q.idempotency_key,q.account_id,q.material_id,q.content_id,
        l.id,l.queue_id,l.account_id,j.id,j.source_key,j.scheduled_at,
        m.task_id,m.material_id,m.canonical_queue_id,m.reserved_at_utc
        FROM x_auto_task t JOIN p.x_post_queue q ON q.id=t.execution_queue_id
        JOIN p.x_post_publish_log l ON l.queue_id=q.id JOIN x_auto_material_ledger m ON m.task_id=t.id
        JOIN d.pipeline_job j ON j.source_key='x:queue:'||q.id AND j.stage='publish'
        WHERE t.id=? AND q.id=? AND j.id=?""",(item["task"],item["queue"],item["job"])).fetchall()
    if len(records)!=1:
        raise ValueError("original identity differs for "+str(item["task"]))
    return digest(tuple(records[0]))


def attempts(db, item):
    key = "x:queue:"+str(item["queue"])
    fields = "operation_id,generation,operation,request_sha256"
    ops = [list(map(tuple,db.execute("SELECT "+fields+" FROM "+table+" WHERE "+column+"=? ORDER BY operation_id",(param,))))
           for table,column,param in (("p.pipeline_x_operation","source_key",key),("d.pipeline_operation","job_id",item["job"]))]
    task = db.execute("SELECT publish_attempt_count FROM x_auto_task WHERE id=?",(item["task"],)).fetchone()
    log = db.execute("SELECT id,attempt_count,x_post_id,x_post_url,published_at FROM p.x_post_publish_log WHERE queue_id=?",(item["queue"],)).fetchone()
    pub = list(map(tuple,db.execute("SELECT job_id,publish_id FROM d.pipeline_publication WHERE source_key=?",(key,))))
    job = db.execute("SELECT id,generation FROM d.pipeline_job WHERE id=?",(item["job"],)).fetchone()
    posts = [sum(x[2]=="x:POST:/2/tweets" or x[2].endswith("/retweets") for x in ledger) for ledger in ops]
    return digest((tuple(task),tuple(log),tuple(job),pub,ops)),posts,pub


def states(db, item):
    return db.execute("""SELECT t.status AS task,t.unknown_outcome AS unknown,t.publish_id,t.scheduled_at_utc,
              q.status AS queue,l.status AS log,l.unknown_outcome AS native_unknown,l.x_post_id,l.attempt_count,
              j.status AS job,m.last_task_status AS reservation
              FROM x_auto_task t JOIN p.x_post_queue q ON q.id=t.execution_queue_id
              JOIN p.x_post_publish_log l ON l.queue_id=q.id JOIN d.pipeline_job j ON j.id=?
              JOIN x_auto_material_ledger m ON m.task_id=t.id WHERE t.id=?""",(item["job"],item["task"])).fetchone()


def check(condition, item, message):
    if not condition:
        raise ValueError(str(item["task"])+" "+message)


def audit(before, after, scope, noon):
    kinds=Counter()
    noon_states=Counter()
    posts=set()
    noon_posts=set()
    frozen=[]
    for i in scope+noon:
        check(original(before,i)==original(after,i),i,"original account/material/schedule/queue binding changed")
        st=states(after,i)
        current,operations,pub=attempts(after,i)
        if "kind" not in i:
            check(st["scheduled_at_utc"]=="2026-11-24T04:00:00+00:00",i,"noon schedule changed")
            check(max(operations)<=1 and len(pub)<=1,i,"multiple post operations or receipts on noon queue")
            if st["x_post_id"]:
                check(st["x_post_id"] not in noon_posts,i,"duplicate noon Post ID")
                noon_posts.add(st["x_post_id"])
            noon_states[(st["task"],st["queue"],st["job"],st["attempt_count"])]+=1
            continue
        baseline=attempts(before,i)[0]
        check(baseline==current,i,"publish attempt, operation, generation or original receipt changed")
        frozen.append((i["task"],current))
        want="published" if i["kind"]=="late" else "failed"
        check(st["task"]==st["queue"]==st["log"]==st["reservation"]==want and not st["unknown"] and not st["native_unknown"],i,"ending statuses disagree")
        if i["kind"]=="late":
            check(st["x_post_id"]==st["publish_id"]==i["post"] and pub==[(i["job"],i["post"])] and i["post"] not in posts,i,"original Post ID differs")
            posts.add(i["post"])
        else:
            check(st["job"]=="failed" and operations==[0,0] and not pub and not st["x_post_id"],i,"media failure has a post or is open")
        kinds[want]+=1
    return {"historical":dict(kinds),"original_historical_post_ids":len(posts),"historical_attempts_digest":digest(sorted(frozen)),
            "unchanged_historical_attempts":len(frozen),"fixed_noon_bindings":len(noon),"confirmed_noon_post_ids":len(noon_posts),
            "noon_states":[dict(task=k[0],queue=k[1],dispatch=k[2],native_attempts=k[3],count=n) for k,n in sorted(noon_states.items())]}


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("target",choices=("rehearsal","live"))
    args=parser.parse_args()
    scope=json.loads((ROOT/"proof/scope.json").read_text())
    noon=json.loads((ROOT/"proof/noon.json").read_text())
    with closing(open_db("backup")) as b, closing(open_db(args.target)) as a:
        print(json.dumps(audit(b,a,scope,noon),ensure_ascii=False,indent=2))
