"""Replay the scheduled planner on a private database; the only live accesses are reads."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--metric-delay',type=int,default=0)
    p.add_argument('--refresh-script',type=Path,required=True)
    a=p.parse_args()
    out=a.output.resolve()
    if not out.is_relative_to('/mnt/data-disk/fb-planner-hour-20261125') or out.exists():
        raise ValueError('use a new private benchmark directory')
    out.mkdir(mode=0o700)
    sys.path.insert(0,str(a.release.resolve()/'candidate'))
    from features.fb_auto_posts.core import FBAutoPostStore
    from features.fb_auto_posts.metric_mysql import metric_store_from_env
    from features.fb_auto_posts.repositories import PagePoolRepository,MaterialRepository
    from features.fb_video_publish.runtime_source import BrokerReadOnlyMySQL
    from features.fb_auto_posts.service import Runtime
    from features.fb_auto_posts import planning_catalog
    if os.environ.get('FB_AUTO_METRIC_STORAGE')!='mysql':
        raise ValueError('required production read-only metric broker unavailable')
    dbfile=out/'shadow.sqlite3'
    shutil.copyfile(a.release.resolve()/'ledger-before.sqlite3',dbfile)
    store=FBAutoPostStore(dbfile)
    mysql=BrokerReadOnlyMySQL()
    metrics=metric_store_from_env(os.environ)
    rt=Runtime(store,PagePoolRepository(mysql),MaterialRepository(mysql,metrics),
               SimpleNamespace(live_enabled=False),None,'',metric_store=metrics,prebuild_enabled=True)
    # A private flag allows validation before switching the production owner.
    (out/'enabled').touch()
    (out/'current.sqlite3').symlink_to(planning_catalog.ROOT/'current.sqlite3')
    open_generation=planning_catalog.open_for_plan
    def checked(config,now):
        result=open_generation(config,now,root=out)
        if config.get('_catalog_trigger') and result is None:
            raise RuntimeError('benchmark cannot silently switch to the full scan')
        return result
    planning_catalog.open_for_plan=checked
    with store.connect() as db:
        if db.execute("SELECT count(*) FROM fb_auto_due_slot WHERE status IN ('pending','preparing')").fetchone()[0]:
            raise ValueError('baseline contains unrelated pending due slots')
    started=time.time()
    day=(datetime.now(timezone(timedelta(hours=8)))+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    enqueued=store.enqueue_due_slots(live_enabled=True,at=day,prebuild_days_ahead=0)
    with store.connect() as db:
        due=[dict(r) for r in db.execute("SELECT id,template_id,trigger_type,created_at_utc FROM fb_auto_due_slot WHERE status='pending' ORDER BY id")]
    if len(due)!=3 or {i['template_id'] for i in due}!={1,3,8} or any(i['trigger_type']!='auto' for i in due):
        raise ValueError('unexpected scheduled fixture: '+str(due))
    print('scheduled',due,flush=True)
    # Force a fresh incremental generation after the three trigger receipts. Reuse
    # only the actual source mirror; this subprocess never sees the shadow tasks.
    with (out/'refresh.log').open('w') as log:
        child=subprocess.Popen([sys.executable,'-Bu',str(a.refresh_script),'--force'],stdout=log,stderr=log)
        child.wait()
    if child.returncode:raise RuntimeError('incremental refresh failed')
    refreshed=time.time()
    with closing(sqlite3.connect((out/'current.sqlite3').as_uri()+'?mode=ro&immutable=1',uri=True)) as conn:
        captured,finished=conn.execute('SELECT started,finished FROM generation').fetchone()
    assert captured>=int(started) and finished>=captured
    print('refresh_ready_seconds',round(refreshed-started,2),flush=True)
    time.sleep(max(0,started+a.metric_delay-time.time()))
    results=[]
    for entry in due:
        began=time.time()
        row=rt.plan_next('isolated-timing',7200)
        if not row.get('run_id'):raise RuntimeError('shadow planner failed: '+str(row))
        with store.connect() as db:
            record=db.execute("""SELECT d.template_id,d.status,d.created_at_utc,d.updated_at_utc,r.queued_tasks,
                r.skipped_tasks,r.total_pages,r.metric_generation_ids_json,(SELECT count(*) FROM fb_auto_task x WHERE x.run_id=r.id) AS tasks
                FROM fb_auto_due_slot d JOIN fb_auto_run r ON r.id=d.run_id WHERE r.id=?""",(row['run_id'],)).fetchone()
        data=dict(record)
        if data['status']!='prepared' or data['queued_tasks']<1 or data['tasks']!=data['total_pages'] or not json.loads(data['metric_generation_ids_json']):
            raise ValueError('incomplete shadow batch: '+str(data))
        data['plan_seconds']=round(time.time()-began,2)
        data['trigger_to_queue_seconds']=round(time.time()-started,2)
        results.append(data)
        print('batch_ready',json.dumps(data),flush=True)
    report={'triggered':due,'refresh_seconds':round(refreshed-started,2),'metric_delay_seconds':a.metric_delay,
            'batches':results,'end_to_end_seconds':round(time.time()-started,2)}
    (out/'receipt.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('complete',round(time.time()-started,2),flush=True)


if __name__=='__main__':
    main()
