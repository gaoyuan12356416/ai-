"""Audited additions to exact future runs; existing publication facts never change."""
import hashlib
import json
from datetime import datetime,timedelta,timezone
from .core import StoreError,utc_iso
from .languages import candidate_snapshots_for_pages,page_language


def scope_fingerprint(pages):
    rows=sorted((p.page_id,p.group_id,page_language(p.language),p.eligible_token_count) for p in pages)
    return hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()


def extend_future_runs(store,run_ids,pages,materials,*,operation_id,expected_scope,expected_version,max_pages=200,max_daily_jobs=1000,publish_floor=''):
    """Caller owns approval, backup and live Page/legacy/capacity revalidation."""
    page_rows=list(pages)
    if len({p.page_id for p in page_rows})!=len(page_rows) or scope_fingerprint(page_rows)!=expected_scope:
        raise StoreError('fb_auto_extension_scope_changed','Page范围或语言已经变化',409)
    if not run_ids or len(set(run_ids))!=len(run_ids) or len(run_ids)>20:
        raise StoreError('fb_auto_extension_runs_invalid','追加运行范围无效',409)
    with store.connect() as conn:
        source=[dict(conn.execute('SELECT * FROM fb_auto_run WHERE id=?',(rid,)).fetchone() or {}) for rid in run_ids]
        has_receipts=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fb_auto_pool_extension'").fetchone()
        receipts=[conn.execute('SELECT scope_sha256 FROM fb_auto_pool_extension WHERE operation_id=? AND run_id=?',(operation_id,rid)).fetchone() for rid in run_ids] if has_receipts else []
        if receipts and all(receipts):
            if any(row['scope_sha256']!=expected_scope for row in receipts):
                raise StoreError('fb_auto_extension_scope_changed','操作范围变化',409)
            return [{'run_id':rid,'idempotent':True,'added':0} for rid in run_ids]
    if any(not r for r in source) or len({r['config_json'] for r in source})!=1:
        raise StoreError('fb_auto_extension_config_changed','追加运行配置不一致',409)
    config=json.loads(source[0]['config_json'])
    frequency=len(config['schedule']['times']) if config['schedule']['mode']=='fixed' else int(config['schedule']['daily_count'])
    if len(page_rows)>max_pages or len(page_rows)*frequency>max_daily_jobs:
        raise StoreError('fb_auto_capacity_exceeded','扩容超过配置容量',409)
    if any(not page_language(p.language) or p.eligible_token_count<=0 for p in page_rows):
        raise StoreError('fb_auto_extension_page_invalid','扩容Page缺少语言或授权',409)
    with store.connect() as conn:
        max_reserved=max((len(store._cooldown_material_ids(conn,p.page_id,int(config['cooldown_days']))) for p in page_rows),default=0)
    candidate_limit=min(5000,max(500,max_reserved+len(run_ids)+100))
    snapshots=candidate_snapshots_for_pages({**config,'_candidate_limit':candidate_limit},page_rows,materials)
    if any(not snapshots[page_language(p.language)].candidates for p in page_rows):
        raise StoreError('fb_auto_extension_material_shortage','扩容Page语言缺少可用素材',409)
    now=utc_iso(store.now_fn())
    cutoff=utc_iso(store.now_fn()+timedelta(minutes=10))
    report=[]
    with store._lock,store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS fb_auto_pool_extension(operation_id TEXT NOT NULL,run_id INTEGER NOT NULL,scope_sha256 TEXT NOT NULL,added_page_ids_json TEXT NOT NULL,metric_generations_json TEXT NOT NULL,created_at_utc TEXT NOT NULL,PRIMARY KEY(operation_id,run_id))')
        for original in source:
            rid=original['id'];run=dict(conn.execute('SELECT * FROM fb_auto_run WHERE id=?',(rid,)).fetchone())
            receipt=conn.execute('SELECT * FROM fb_auto_pool_extension WHERE operation_id=? AND run_id=?',(operation_id,rid)).fetchone()
            if receipt:
                if receipt['scope_sha256']!=expected_scope:raise StoreError('fb_auto_extension_scope_changed','操作范围变化',409)
                report.append({'run_id':rid,'idempotent':True,'added':0});continue
            template=conn.execute('SELECT status,current_version FROM fb_auto_template WHERE id=?',(run['template_id'],)).fetchone()
            if not template or template['status']!='enabled' or template['current_version']!=expected_version or run['template_version']!=expected_version or run['config_json']!=original['config_json']:
                raise StoreError('fb_auto_extension_version_changed','运行版本已变化',409)
            if run['trigger_type']!='auto' or run['planned_publish_at_utc']<cutoff:
                raise StoreError('fb_auto_extension_not_future','仅允许追加尚未到期的自动运行',409)
            existing={r[0] for r in conn.execute('SELECT page_id FROM fb_auto_task WHERE run_id=?',(rid,))}
            if not existing.issubset({p.page_id for p in page_rows}):
                raise StoreError('fb_auto_extension_membership_changed','原Page已不在当前池中',409)
            missing=[p for p in page_rows if p.page_id not in existing]
            publish_at=max(run['planned_publish_at_utc'],publish_floor) if publish_floor else run['planned_publish_at_utc']
            for page in missing:
                if conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND status IN ('running','submitted','unknown') LIMIT 1",(page.page_id,)).fetchone():
                    raise StoreError('fb_auto_page_unknown_block','扩容Page存在未确认发布',409)
                if conn.execute('SELECT 1 FROM fb_auto_task WHERE page_id=? AND planned_publish_at_utc=? LIMIT 1',(page.page_id,publish_at)).fetchone():
                    raise StoreError('fb_auto_extension_duplicate_slot','Page已存在该时隙任务',409)
                language=page_language(page.language)
                excluded=store._cooldown_material_ids(conn,page.page_id,int(config['cooldown_days']))
                if candidate_limit<5000 and len(excluded)>candidate_limit-50:
                    raise StoreError('fb_auto_candidate_reservations_changed','Page素材占用在计划期间变化',409)
                material=materials.choose_from(snapshots[language].candidates,excluded)
                if material is None:raise StoreError('fb_auto_extension_material_shortage','Page冷却后无可用素材',409)
                conn.execute('INSERT INTO fb_auto_run_page(run_id,page_id,group_id,group_ids_json,owner_user_id,timezone,language,eligible_token_count,snapshot_status,skip_reason) VALUES(?,?,?,?,?,?,?,?,?,?)',(rid,page.page_id,page.group_id,json.dumps(list(page.group_ids)),page.owner_user_id,page.timezone,language,page.eligible_token_count,'eligible',''))
                job='fb-page-'+hashlib.sha256(f"{run['template_id']}:{run['template_version']}:{run['slot_key']}:{page.page_id}".encode()).hexdigest()[:48]
                cur=conn.execute('INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,material_id,content_id,media_url,message_text,created_at_utc,planned_publish_at_utc,video_template,gpu_job_id,source_media_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(rid,run['template_id'],run['template_version'],page.page_id,page.group_id,'planned',material.material_id,material.content_id,'',store._message(config,material),now,publish_at,config['video_template'],job,material.media_url))
                tid=int(cur.lastrowid)
                if '{{url}}' in str(config['message_template']):
                    short,long=store._link_values(page,material,tid,int(store.now_fn().timestamp()))
                    conn.execute('UPDATE fb_auto_task SET message_text=?,short_url=?,long_url=? WHERE id=?',(store._message(config,material,short),short,long,tid))
            conn.execute('UPDATE fb_auto_run SET total_pages=total_pages+?,publishable_pages=publishable_pages+?,queued_tasks=queued_tasks+? WHERE id=?',(len(missing),len(missing),len(missing),rid))
            store._refresh_run(conn,rid,now)
            conn.execute('INSERT INTO fb_auto_pool_extension VALUES(?,?,?,?,?,?)',(operation_id,rid,expected_scope,json.dumps([p.page_id for p in missing]),json.dumps({k:list(v.metric_generation_ids) for k,v in snapshots.items()}),now))
            report.append({'run_id':rid,'added':len(missing),'total_pages':len(existing)+len(missing),'planned_publish_at_utc':publish_at})
        conn.commit()
    return report
