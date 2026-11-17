"""Explicit operator recovery of held, unpublished tasks from a saved audit manifest."""
import argparse
import contextlib
import json
from pathlib import Path
import re
import shlex
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.story_copy import model, sources, store
from features.fb_video_publish.sources import SourceRepository

NATIVE = Path(store.SOURCES['fv'][1])


def safe(c, task, record):
    key='fb:video:'+task['task_id']
    hold=c.execute('SELECT reason FROM pipeline_canonical_hold WHERE source_key=?',(key,)).fetchone()
    return (task['batch_id']==record['batch'] and task['status']=='queued'
            and not any(task[k] for k in ('graph_post_id','unknown_outcome','execution_lease'))
            and hold is not None and hold['reason']==record['reason']
            and not c.execute('SELECT 1 FROM fb_video_attempt WHERE task_id=? LIMIT 1',(task['task_id'],)).fetchone()
            and not c.execute('SELECT 1 FROM story_copy_receipt WHERE task_key=?',(key,)).fetchone())


@contextlib.contextmanager
def native():
    c=sqlite3.connect(str(NATIVE),timeout=10)
    c.row_factory=sqlite3.Row
    c.execute('ATTACH DATABASE ? AS copy',(str(store.DB),))
    try:
        yield c
        c.commit()
    finally:
        c.close()


def metadata(rows):
    # This explicit operator CLI uses one reserved read-only connection, never automation.
    import pymysql
    env={}
    for line in Path('/etc/fb-auto-post.env').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
            continue
        k,v=line.split('=',1)
        env[k.strip()]=' '.join(shlex.split(v))
    schema=env.get('FB_AUTO_MYSQL_DATABASE','kunlunads_dev')
    assert re.fullmatch('[A-Za-z0-9_]+',schema)
    c=pymysql.connect(host='101.32.56.53',port=63350,user=env['FB_AUTO_MYSQL_USER'],
        password=env['FB_AUTO_MYSQL_PASSWORD'],database=schema,charset='utf8mb4',
        connect_timeout=3,read_timeout=12,write_timeout=3,cursorclass=pymysql.cursors.DictCursor)
    try:
        with c.cursor() as q:
            q.execute('SELECT @@read_only ro')
            assert q.fetchone()['ro']==1
            q.execute('SET SESSION MAX_EXECUTION_TIME=8000')
            q.execute('START TRANSACTION READ ONLY')
        class Reader:
            def select(self,sql,params):
                with c.cursor() as q:
                    q.execute(sql,params)
                    return q.fetchall()
        reader=Reader();reader.schema=schema
        repository=SourceRepository(None,SimpleNamespace(mysql=reader))
        return repository._enrich(rows,strict=True,need_desc=True,manual=True)
    finally:
        c.rollback();c.close()


def enqueue(path,record):
    originals=record['tasks']
    materials=[json.loads(item['native']['snapshot_json'])['material'] for item in originals]
    enriched={m['material_id']:m for m in metadata(materials)}
    assert len(enriched)==len(originals)
    assert all(m['drama_name'] and m['drama_description'] and m['description_status']=='matched' for m in enriched.values())
    audit=path.with_name('recovery-enriched.json')
    if not audit.exists():
        audit.write_text(json.dumps(enriched,ensure_ascii=False,indent=2));audit.chmod(0o600)
    changed=[];existing=[]
    with native() as c:
        c.execute('BEGIN IMMEDIATE')
        for item in originals:
            old=item['native'];key='fb:video:'+old['task_id']
            row=dict(c.execute('SELECT rowid _ord,* FROM fb_video_task WHERE task_id=?',(old['task_id'],)).fetchone())
            assert safe(c,row,record), 'task no longer safely held: '+key
            assert row['material_id']==old['material_id'] and row['content_id']==old['content_id']
            job=c.execute('SELECT * FROM copy.jobs WHERE key=?',(key,)).fetchone()
            if job['state'] in ('pending','generating','ready') and json.loads(job['payload']).get('recovery')==record['reason']:
                existing.append(key);continue
            assert job['state']=='failed' and job['reason']=='insufficient_evidence' and not job['draft'] and not job['final']
            snap=json.loads(row['snapshot_json']);mat=snap['material'];new=enriched[row['material_id']]
            assert not snap.get('story_copy_manual') and mat['source_app_id']==3214
            for name in ('drama_name','drama_description','description_status'):
                mat[name]=new[name]
            snap['story_copy_metadata_recovery']=record['reason']
            row['snapshot_json']=store.dump(snap)
            task=sources.fb_task('fv',row);task['recovery']=record['reason']
            assert model.group_key(task)==model.group_key(json.loads(job['payload']))
            c.execute('UPDATE fb_video_task SET snapshot_json=? WHERE task_id=?',(row['snapshot_json'],row['task_id']))
            c.execute("UPDATE copy.jobs SET payload=?,state='pending',reason='',batch=NULL WHERE key=?",(store.dump(task),key))
            changed.append(key)
    print(json.dumps({'enqueued':changed,'already_recovered':existing}),flush=True)


def release(path,record):
    released=[];waiting=[]
    with native() as c:
        c.execute('BEGIN IMMEDIATE')
        for item in record['tasks']:
            key=item['copy']['key'];tid=item['native']['task_id']
            row=dict(c.execute('SELECT rowid _ord,* FROM fb_video_task WHERE task_id=?',(tid,)).fetchone())
            job=c.execute('SELECT * FROM copy.jobs WHERE key=?',(key,)).fetchone()
            if not c.execute('SELECT 1 FROM pipeline_canonical_hold WHERE source_key=? AND reason=?',(key,record['reason'])).fetchone():
                continue
            assert safe(c,row,record), 'held task acquired publication evidence: '+key
            if job['state']!='ready':
                waiting.append({'key':key,'state':job['state'],'reason':job['reason']});continue
            frozen=json.loads(job['payload']);current=sources.fb_task('fv',row)
            assert frozen.get('recovery')==record['reason'] and model.group_key(current)==model.group_key(frozen)
            assert current['material_id']==frozen['material_id']
            preview=model.render(frozen,json.loads(job['draft']),row['short_url'])
            assert frozen['facts'] and preview
            c.execute('DELETE FROM pipeline_canonical_hold WHERE source_key=? AND reason=?',(key,record['reason']))
            released.append({'key':key,'caption':preview})
    if released:
        log=path.with_name('recovery-released.json')
        previous=json.loads(log.read_text()) if log.exists() else []
        log.write_text(json.dumps(previous+released,ensure_ascii=False,indent=2));log.chmod(0o600)
    print(json.dumps({'released':len(released),'waiting':waiting}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('enqueue','release'))
    p.add_argument('audit',type=Path)
    args=p.parse_args()
    record=json.loads(args.audit.read_text())
    (enqueue if args.action=='enqueue' else release)(args.audit,record)
