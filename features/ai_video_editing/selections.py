"""Durable ordered source intent, independent of browser lifetime or readiness."""
import hashlib
import json
import logging
import re
import threading
import time

from .runtime import check, ident, owner, packed, public_asset
from . import catalog_imports, smart_api, source_jobs
from .catalog import decode_key

LOG = logging.getLogger(__name__)
_threads = {}
_lock = threading.Lock()


def setup(rt):
    catalog_imports.setup(rt)
    with rt.db(True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS studio_source_batches(
          id TEXT PRIMARY KEY,session TEXT NOT NULL,owner TEXT NOT NULL,revision INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',analysis_id TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL,updated REAL NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS studio_source_items(
          id TEXT PRIMARY KEY,batch TEXT NOT NULL,position INTEGER NOT NULL,kind TEXT NOT NULL,
          product TEXT NOT NULL DEFAULT '',source_key TEXT NOT NULL DEFAULT '',episode INTEGER NOT NULL DEFAULT 0,
          name TEXT NOT NULL DEFAULT '',import_id TEXT NOT NULL DEFAULT '',asset_id TEXT NOT NULL DEFAULT '',
          source_job TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',UNIQUE(batch,position))''')
        db.execute('CREATE INDEX IF NOT EXISTS studio_source_batch_owner ON studio_source_batches(owner,status,created)')
        cols={r[1] for r in db.execute('PRAGMA table_info(studio_sessions)')}
        for name,definition in [('selection_id',"TEXT NOT NULL DEFAULT ''"),('selection_revision','INTEGER NOT NULL DEFAULT 0')]:
            if name not in cols:
                db.execute('ALTER TABLE studio_sessions ADD COLUMN '+name+' '+definition)


def normalize(rt, actor, values):
    check(isinstance(values,list) and 1<=len(values)<=200,'selection_invalid','请选择 1–20 个素材')
    items=[]; seen=set()
    for value in values:
        check(isinstance(value,dict),'selection_invalid','素材选择格式无效')
        kind=value.get('kind','asset')
        item={'kind':kind,'product':'','source_key':'','episode':0,'name':'','asset_id':'','import_id':''}
        if kind=='asset':
            asset=rt.asset(value.get('asset_id',''),actor)
            item.update(asset_id=asset['id'],name=asset['name'])
            identity='asset:'+asset['id']
        else:
            check(kind=='catalog','selection_invalid','素材来源无效')
            product,key,episode=value.get('product'),value.get('key'),value.get('episode')
            decode_key(key,product)
            check(type(episode) is int and 0<episode<=100000,'selection_invalid','集号无效')
            with rt.db() as db:
                previous=db.execute('SELECT * FROM catalog_imports WHERE owner=? AND product=? AND source_key=? AND episode=?',(owner(actor),product,key,episode)).fetchone()
            item.update(product=product,source_key=key,episode=episode,name=str(value.get('name') or '第 %s 集'%episode)[:240])
            if previous:
                item.update(import_id=previous['id'],asset_id=previous['asset_id'] if previous['status']=='ready' else '')
            identity='asset:'+item['asset_id'] if item['asset_id'] else packed([product,key,episode])
        if identity in seen:
            continue
        seen.add(identity);items.append(item)
    check(1<=len(items)<=20,'selection_limit','一次创作最多选择 20 个不同素材')
    return items


def operation(rt, actor, op):
    with rt.db() as db:
        row=db.execute("SELECT receipt FROM operations WHERE owner=? AND kind='source-selection' AND op=?",(owner(actor),op)).fetchone()
    return json.loads(row['receipt']) if row else None


def submit(rt, actor, sid, body):
    from .studio import configuration
    op=body.get('operation_id','')
    check(isinstance(op,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',op),'operation_required','缺少本次选择操作编号')
    values=body.get('items')
    # Hash the submitted intent, never a later download result or generated ID.
    digest=hashlib.sha256(packed([sid,values,body.get('version'),body.get('config')]).encode()).hexdigest()
    with rt.db() as db:
        prior=db.execute("SELECT * FROM operations WHERE owner=? AND kind='source-selection' AND op=?",(owner(actor),op)).fetchone()
    if prior:
        check(prior['digest']==digest,'operation_conflict','本次操作编号已用于另一份选择',409)
        return json.loads(prior['receipt'])
    items=normalize(rt,actor,values)
    now=time.time();who=owner(actor)
    with rt.db(True) as db:
        prior=db.execute("SELECT * FROM operations WHERE owner=? AND kind='source-selection' AND op=?",(who,op)).fetchone()
        if prior:
            check(prior['digest']==digest,'operation_conflict','本次操作编号已用于另一份选择',409)
            return json.loads(prior['receipt'])
        if sid:
            session=db.execute('SELECT * FROM studio_sessions WHERE id=? AND owner=?',(sid,who)).fetchone()
            check(session,'session_missing','创作不存在',404)
            check(body.get('version')==session['version'],'version_conflict','素材选择已更新，请刷新后合并你的选择',409)
            cfg=configuration(body.get('config',json.loads(session['config'])))
            revision=session['selection_revision']+1
        else:
            sid=ident('ss');cfg=configuration(body.get('config',{}));revision=1
            db.execute('INSERT INTO studio_sessions(id,owner,title,sources,config,updated) VALUES(?,?,?,?,?,?)',(sid,who,items[0]['name'][:180], '[]',packed(cfg),now))
            session=db.execute('SELECT * FROM studio_sessions WHERE id=?',(sid,)).fetchone()
        bid=ident('sb')
        db.execute('INSERT INTO studio_source_batches(id,session,owner,revision,created,updated) VALUES(?,?,?,?,?,?)',(bid,sid,who,revision,now,now))
        for position,item in enumerate(items):
            if item['kind']=='catalog':
                job=catalog_imports.submit_rows(db,actor,{'product':item['product'],'key':item['source_key'],'episodes':[item['episode']]})['items'][0]
                item['import_id']=job['id']
                item['asset_id']=job['asset_id'] if job['status']=='ready' else ''
            db.execute('INSERT INTO studio_source_items(id,batch,position,kind,product,source_key,episode,name,import_id,asset_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (ident('si'),bid,position,item['kind'],item['product'],item['source_key'],item['episode'],item['name'],item['import_id'],item['asset_id']))
        ready=[i['asset_id'] for i in items if i['asset_id']]
        db.execute("UPDATE studio_sessions SET selection_id=?,selection_revision=?,sources=?,analysis_id='',config=?,version=version+1,updated=? WHERE id=?",(bid,revision,packed(ready),packed(cfg),now,sid))
        # Only not-yet-started creation intent follows an explicit new selection.
        for run in db.execute("SELECT * FROM studio_runs WHERE session=? AND status IN ('waiting_sources','needs_attention')",(sid,)).fetchall():
            frozen=json.loads(run['snapshot'])
            db.execute("UPDATE studio_runs SET status='superseded',error='已按新的素材选择继续',updated=? WHERE id=?",(now,run['id']))
            frozen.update(selection_id=bid,selection_revision=revision,source_ids=ready)
            rid=ident('sr');replacement='selection-'+run['id']+'-'+bid
            db.execute('INSERT INTO studio_runs(id,session,owner,operation,digest,snapshot,status,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (rid,sid,who,replacement,hashlib.sha256(packed(frozen).encode()).hexdigest(),packed(frozen),'waiting_sources','{}',now,now))
        if session['selection_id']:
            db.execute("UPDATE studio_source_batches SET status='superseded',updated=? WHERE id=? AND status IN ('queued','preparing','needs_attention')",(now,session['selection_id']))
        receipt={'session_id':sid,'selection_id':bid,'selection_revision':revision,'operation_id':op}
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',(who,'source-selection',op,digest,packed(receipt)))
    source_jobs.notify(rt)
    return receipt


def progress(rt, actor, sid, bid=None):
    who=owner(actor)
    with rt.db() as db:
        session=db.execute('SELECT * FROM studio_sessions WHERE id=? AND owner=?',(sid,who)).fetchone()
        check(session,'session_missing','创作不存在',404)
        bid=bid or session['selection_id']
        if not bid:
            return None
        batch=db.execute('SELECT * FROM studio_source_batches WHERE id=? AND session=? AND owner=?',(bid,sid,who)).fetchone()
        check(batch,'selection_missing','素材选择不存在',404)
        rows=db.execute('SELECT * FROM studio_source_items WHERE batch=? ORDER BY position',(bid,)).fetchall()
        items=[]
        for row in rows:
            item=dict(row);item.pop('batch')
            item['key']=item.pop('source_key')
            imp=db.execute('SELECT * FROM catalog_imports WHERE id=? AND owner=?',(row['import_id'],who)).fetchone() if row['import_id'] else None
            job=db.execute('SELECT * FROM editor_source_jobs WHERE id=? AND owner=?',(row['source_job'],who)).fetchone() if row['source_job'] else None
            import_status=imp['status'] if imp else ('ready' if row['asset_id'] else 'failed')
            item.update(import_status=import_status,import_stage=(imp['stage'] if imp and import_status=='downloading' else import_status),
                        bytes=imp['bytes'] if imp else 0,total=imp['total'] if imp else 0,speed=round(imp['speed']) if imp else 0,
                        analysis_status=job['status'] if job else ('queued' if import_status=='ready' else 'waiting_import'),
                        error=row['error'] or (imp['error'] if imp else '') or (job['error'] if job else ''),
                        warnings=json.loads(job['result']).get('warnings',[]) if job and job['status']=='succeeded' else [])
            if row['error']:
                item['analysis_status']='failed'
            if imp and imp['status']=='ready':
                item['asset_id']=imp['asset_id']
            item['asset']=None
            if item['asset_id']:
                assetrow=db.execute('SELECT * FROM assets WHERE id=? AND owner=?',(item['asset_id'],who)).fetchone()
                if assetrow:
                    asset=public_asset(rt.asset_info(assetrow))
                    item['name']=asset['name'];item['asset']=asset
                else:
                    item.update(import_status='failed',error='素材不存在或归属已变化',analysis_status='failed')
            items.append(item)
        runs=[dict(r) for r in db.execute('SELECT id,status,updated FROM studio_runs WHERE session=? ORDER BY created',(sid,))]
    imported=sum(i['import_status']=='ready' for i in items)
    analyzed=sum(i['analysis_status']=='succeeded' for i in items)
    failed=sum(bool(i['error']) or i['import_status']=='failed' or i['analysis_status']=='failed' for i in items)
    active=any(i['import_status'] in ('queued','downloading') or (i['import_status']=='ready' and i['analysis_status'] in ('queued','running')) for i in items)
    active=active or any(r['status'] in ('queued','waiting_sources','analyzing','preparing','rendering') for r in runs)
    active=active or any(i['asset'] and i['asset'].get('preview_status') in ('queued','running') for i in items)
    result={'selection_id':bid,'selection_revision':batch['revision'],'version':session['version'],
            'status':batch['status'],'analysis_id':batch['analysis_id'],'items':items,
            'summary':{'total':len(items),'imported':imported,'analyzed':analyzed,'failed':failed},'active':active,
            'session_revision':hashlib.sha256(packed([session['version'],session['updated'],runs,batch['analysis_id']]).encode()).hexdigest()[:24]}
    result['etag']='"'+hashlib.sha256(packed(result).encode()).hexdigest()+'"'
    return result


def retry(rt, actor, sid, iid):
    current=progress(rt,actor,sid)
    item=next((i for i in current['items'] if i['id']==iid),None) if current else None
    check(item,'selection_item_missing','该素材不在当前选择中',404)
    if item['import_status']=='failed' and item['import_id']:
        catalog_imports.retry(rt,actor,item['import_id'])
    elif item['analysis_status']=='failed' and item['asset_id']:
        source_jobs.enqueue(rt,actor,item['asset_id'],retry=True)
    elif item['error'] and item['asset_id']:
        rt.asset(item['asset_id'],actor)
    else:
        return {'ok':True}
    with rt.db(True) as db:
        db.execute("UPDATE studio_source_items SET error='' WHERE id=?",(iid,))
        db.execute("UPDATE studio_source_batches SET status='queued',updated=? WHERE id=?",(time.time(),current['selection_id']))
        db.execute("UPDATE studio_runs SET status='waiting_sources',error='',expires=0 WHERE session=? AND status='needs_attention'",(sid,))
    source_jobs.notify(rt)
    return {'ok':True}


def advance(rt, batch):
    tenant,user=json.loads(batch['owner']);actor={'tenant_key':tenant,'user_id':user}
    with rt.db() as db:
        rows=db.execute('SELECT * FROM studio_source_items WHERE batch=? ORDER BY position',(batch['id'],)).fetchall()
    for row in rows:
        aid=row['asset_id']
        if not aid and row['import_id']:
            with rt.db() as db:
                imp=db.execute('SELECT * FROM catalog_imports WHERE id=? AND owner=?',(row['import_id'],batch['owner'])).fetchone()
            if imp and imp['status']=='ready':
                aid=imp['asset_id']
        if not aid:
            continue
        try:
            if row['source_job']:
                job=source_jobs.read(rt,actor,row['source_job'])
            else:
                job=source_jobs.enqueue(rt,actor,aid)
            if row['asset_id']!=aid or not row['source_job'] or row['error']:
                with rt.db(True) as db:
                    db.execute("UPDATE studio_source_items SET asset_id=?,source_job=?,error='' WHERE id=?",(aid,job['id'],row['id']))
                try:
                    from . import previews
                    previews.request(rt,actor,aid,priority=20-row['position'])
                except Exception:
                    LOG.warning('Optional preview enqueue delayed for %s',aid)
        except Exception as exc:
            with rt.db(True) as db:
                db.execute('UPDATE studio_source_items SET error=? WHERE id=?',(getattr(exc,'message','素材准备失败，请重试')[:400],row['id']))
    state=progress(rt,actor,batch['session'],batch['id'])
    summary=state['summary'];ready=[i['asset_id'] for i in state['items'] if i['asset']]
    analysis_id='';status='preparing'
    if summary['analyzed']==summary['total'] and not summary['failed']:
        snapshots=smart_api.source_snapshots(rt,actor,ready)
        for snap,item in zip(snapshots,state['items']):
            snap.update(json.loads(source_jobs.read(rt,actor,item['source_job'])['result']))
            snap['status']='succeeded'
        analysis_id=smart_api.assemble(rt,actor,snapshots)['analysis_id'];status='succeeded'
    elif summary['failed'] and not any(i['import_status'] in ('queued','downloading') or (i['import_status']=='ready' and i['analysis_status'] in ('queued','running') and not i['error']) for i in state['items']):
        status='needs_attention'
    with rt.db(True) as db:
        db.execute("UPDATE studio_source_batches SET status=?,analysis_id=?,updated=? WHERE id=? AND status IN ('queued','preparing')",(status,analysis_id,time.time(),batch['id']))
        db.execute('UPDATE studio_sessions SET sources=?,analysis_id=? WHERE id=? AND selection_id=?',(packed(ready),analysis_id,batch['session'],batch['id']))
    event=getattr(rt,'_studio_wake',None)
    if event and status!=batch['status']:event.set()


def tick(rt):
    with rt.db() as db:
        rows=db.execute("SELECT b.* FROM studio_source_batches b JOIN studio_sessions s ON s.selection_id=b.id WHERE b.status IN ('queued','preparing') ORDER BY b.created").fetchall()
        legacy={r['owner']:r['n'] for r in db.execute("SELECT owner,count(*) n FROM ai_edit_smart_analysis WHERE status IN ('queued','running') GROUP BY owner")}
    counts=dict(legacy)
    for batch in rows:
        if counts.get(batch['owner'],0)>=3:
            continue
        counts[batch['owner']]=counts.get(batch['owner'],0)+1
        try:
            advance(rt,batch)
        except Exception:
            LOG.exception('Selection preparation delayed %s',batch['id'])


def start(rt):
    setup(rt);source_jobs.start(rt)
    with _lock:
        if str(rt.db_path) in _threads:
            return
        rt._selection_wake=threading.Event()
        def loop():
            while True:
                try:
                    tick(rt)
                except Exception:
                    LOG.exception('Selection dispatcher delayed')
                rt._selection_wake.wait(1);rt._selection_wake.clear()
        thread=threading.Thread(target=loop,name='editor-selections',daemon=True)
        _threads[str(rt.db_path)]=thread;thread.start()
