"""One broker serializes effect-template reuse across the two independent X lanes.

Publishing databases are read-only here. Durable grants survive timeouts; a
grant is never released because a network request timed out.
"""
from contextlib import closing
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
from .collector import readonly,request,LEDGERS
from .selection import config_enabled,POLICY
from .store import ROOT

AUTO_PATHS={'ordinary':'/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3',
            'tag':'/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3'}

def local_conflict(db,material_id,account_id):
    if db.execute('''SELECT 1 FROM x_post_material_pool p WHERE p.material_key=?
        AND NOT EXISTS(SELECT 1 FROM x_post_queue q WHERE q.pool_item_id=p.id) LIMIT 1''',(material_id,)).fetchone():return True
    return bool(db.execute('''SELECT 1 FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
        LEFT JOIN x_post_repost_ledger relay ON relay.queue_id=q.id
        WHERE q.material_key=? AND (q.account_id=? OR q.status NOT IN ('published','failed','cancelled') OR l.unknown_outcome=1
        OR l.status IN ('post_creating','repost_creating') OR relay.unknown_outcome=1 OR relay.status='needs_review') LIMIT 1''',
        (material_id,account_id)).fetchone())

def lane():return 'tag' if '/x-tag-publisher/' in os.environ.get('X_AUTO_POST_DB_PATH','') else 'ordinary'

def client(action,**payload):
    if action=='check' and len(payload.get('material_ids',[]))>500:
        ids=payload['material_ids'];blocked=set()
        for offset in range(0,len(ids),500):
            blocked.update(client(action,**dict(payload,material_ids=ids[offset:offset+500]))['blocked'])
        return dict(ok=True,blocked=sorted(blocked))
    token=os.environ.get('X_POST_AUTO_INTERNAL_TOKEN') or os.environ.get('X_AUTO_POST_INTERNAL_TOKEN') or os.environ.get('X_TAG_ACCOUNT_BROKER_TOKEN')
    if not token:raise RuntimeError('x_effect_broker_credentials_missing')
    result=request('http://127.0.0.1:8899/internal/x-material-effects/reuse',token,dict(action=action,**payload))
    if not result.get('ok'):raise RuntimeError('x_effect_material_reserved')
    return result

def task(lane_name,task_id):
    if lane_name not in AUTO_PATHS:raise ValueError('invalid_lane')
    with closing(readonly(AUTO_PATHS[lane_name])) as db:
        row=db.execute('SELECT * FROM x_auto_task WHERE id=?',(int(task_id),)).fetchone()
        if not row:raise ValueError('task_missing')
        data=dict(row)
        version=db.execute('SELECT config_json FROM x_auto_template_version WHERE template_id=? AND version=?',(row['template_id'],row['template_version'])).fetchone()
        if not version or not config_enabled(json.loads(version[0])):raise ValueError('effect_policy_not_frozen')
        return data

def conflicts(material_ids,account_id,*,own_lane='',own_task=0,own_external=''):
    blocked=set();ids=list(dict.fromkeys(str(v) for v in material_ids));marks=','.join('?' for _ in ids)
    if not ids:return blocked
    for lane_name,path in zip(('ordinary','tag'),LEDGERS):
        with closing(readonly(path)) as db:
            for r in db.execute(f'''SELECT q.material_key,q.account_id,q.status,l.unknown_outcome,mr.external_task_key,
                    l.status AS log_status,relay.unknown_outcome AS relay_unknown,relay.status AS relay_status
                    FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
                    LEFT JOIN x_post_repost_ledger relay ON relay.queue_id=q.id
                    LEFT JOIN x_post_manual_run mr ON mr.id=q.manual_run_id
                    WHERE q.material_key IN ({marks})''',ids):
                if lane_name==own_lane and own_external and r['external_task_key']==own_external:continue
                if (str(r['account_id'])==str(account_id) or r['status'] not in ('published','failed','cancelled') or r['unknown_outcome']
                    or r['log_status'] in ('post_creating','repost_creating') or r['relay_unknown'] or r['relay_status']=='needs_review'):
                    blocked.add(str(r['material_key']))
            for r in db.execute(f'''SELECT p.material_key FROM x_post_material_pool p WHERE p.material_key IN ({marks})
                    AND NOT EXISTS(SELECT 1 FROM x_post_queue q WHERE q.pool_item_id=p.id)''',ids):blocked.add(str(r[0]))
            for r in db.execute(f'''SELECT r.material_key,m.external_task_key FROM x_post_manual_material_reservation r
                    JOIN x_post_manual_run m ON m.id=r.manual_run_id WHERE r.state='active' AND r.material_key IN ({marks})''',ids):
                if lane_name!=own_lane or not own_external or r['external_task_key']!=own_external:blocked.add(str(r[0]))
    for name,path in AUTO_PATHS.items():
        with closing(readonly(path)) as db:
            for r in db.execute(f'SELECT id,account_id,material_id,status FROM x_auto_task WHERE material_id IN ({marks})',ids):
                if name==own_lane and int(r['id'])==int(own_task):continue
                if str(r['account_id'])==str(account_id) or r['status'] not in ('published','failed','canceled','no_candidate','skipped'):
                    blocked.add(str(r['material_id']))
    return blocked

def handle(payload,root=ROOT):
    action=payload.get('action');name=payload.get('lane','ordinary');tid=int(payload.get('task_id',0));account=str(payload.get('account_id',''))
    if action=='check':
        ids=payload.get('material_ids',[])
        if not isinstance(ids,list) or len(ids)>1000 or any(not str(v).isdigit() for v in ids):raise ValueError('invalid_material_ids')
        # Preview uses synthetic positive task IDs. At selection time no real
        # material is frozen yet, so the read-only check excludes no task.
        return dict(ok=True,blocked=sorted(conflicts(ids,account,own_lane=name)))
    if action not in ('reserve','execution'):raise ValueError('invalid_action')
    source=task(name,tid);mid=str(payload.get('material_id',''))
    if not mid.isdigit() or str(source['account_id'])!=account:raise ValueError('identity_mismatch')
    if source.get('material_id') and str(source['material_id'])!=mid:raise ValueError('material_mismatch')
    if action=='execution' and str(source.get('material_id'))!=mid:raise ValueError('material_not_frozen')
    external=str(payload.get('external_task_key',''))
    if external:
        from types import SimpleNamespace
        from features.x_auto_posts.operator_retry import execution_key
        if external!=execution_key(SimpleNamespace(db_path=AUTO_PATHS[name]),SimpleNamespace(**source)):
            raise ValueError('external_identity_mismatch')
    now=datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(str(Path(root)/'reuse.sqlite3'),timeout=15)) as db:
        db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS grants(lane TEXT,task_id INTEGER,account_id TEXT,material_id TEXT,state TEXT,external_key TEXT,created_at TEXT,PRIMARY KEY(lane,task_id))')
        db.execute('CREATE INDEX IF NOT EXISTS grants_material ON grants(material_id,state,account_id)')
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT * FROM grants WHERE lane=? AND task_id=?',(name,tid)).fetchone()
        if existing and existing['account_id']!=account:raise ValueError('grant_identity_conflict')
        if existing and existing['material_id']!=mid:
            if action!='reserve' or source.get('material_id') or existing['external_key']:
                raise ValueError('grant_identity_conflict')
            # The caller holds this task's selection transaction. No material
            # has been frozen or sent to the publishing owner yet.
            db.execute('DELETE FROM grants WHERE lane=? AND task_id=?',(name,tid))
            existing=None
        for row in db.execute("SELECT * FROM grants WHERE material_id=? AND state='active'",(mid,)).fetchall():
            if row['lane']==name and row['task_id']==tid:continue
            prior=task(row['lane'],row['task_id'])
            if prior['status'] in ('published','failed','canceled','no_candidate','skipped'):
                state='finished' if prior.get('material_id') else 'released'
                db.execute('UPDATE grants SET state=? WHERE lane=? AND task_id=?',(state,row['lane'],row['task_id']))
        occupied=db.execute("SELECT 1 FROM grants WHERE material_id=? AND (state='active' OR (account_id=? AND state='finished')) AND NOT(lane=? AND task_id=?) LIMIT 1",(mid,account,name,tid)).fetchone()
        if occupied or conflicts([mid],account,own_lane=name,own_task=tid,own_external=external or (existing['external_key'] if existing else '')):
            db.rollback();return dict(ok=False,code='x_effect_material_reserved')
        if action=='execution' and not existing:raise ValueError('grant_missing')
        if existing:
            if external and existing['external_key'] not in ('',external):raise ValueError('external_identity_conflict')
            if external:db.execute('UPDATE grants SET external_key=? WHERE lane=? AND task_id=?',(external,name,tid))
        else:db.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?)',(name,tid,account,mid,'active',external,now))
        db.commit()
    return dict(ok=True,policy=POLICY,lane=name,task_id=tid,account_id=account,material_id=mid)
