"""Profile-ranked, one-material/one-Page explicit batches."""
import hashlib
import json
from dataclasses import asdict
from datetime import timedelta

from features.hit_publish import POLICY, assign, material_ids, release_times, timing
from features.hit_publish import target_language
from .core import ActorScope, BEIJING, StoreError, utc_iso
from .manual_batch import canonical, digest
from .strategy import daily_capacity
from . import hit_material as legacy, page_caption
from .hit_profile import freeze_match, profileaudit


def publish_material(runtime, template_id, actor, payload):
    field = 'material_ids' if 'material_ids' in payload else 'material_id'
    if set(payload) != {'expected_version', field, 'operation_id'}:
        raise StoreError('invalid_request', '爆款素材发布请求字段无效', 400)
    try:
        ids = material_ids(payload[field] if field == 'material_ids' else [payload[field]])
    except ValueError as exc:
        raise StoreError('invalid_request', str(exc), 400) from None
    # Reuse exact legacy validation and preserve already accepted browser drafts.
    old_request = legacy.normalize_request(template_id, dict(payload, material_id=ids[0]) if field == 'material_id'
        else dict(expected_version=payload['expected_version'], material_id=ids[0], operation_id=payload['operation_id']))
    request = {key: value for key, value in old_request.items() if key != 'material_id'}
    request['material_ids'] = ids
    store = runtime.store
    with store.connect() as conn:
        template = dict(store._template_row(conn, template_id, actor))
        row = conn.execute('SELECT config_json FROM fb_auto_run WHERE template_id=? AND slot_key=?',
                           (template_id, 'manual:hit-material:'+request['operation_id'])).fetchone()
        if row and field == 'material_id' and json.loads(row[0]).get(legacy.MARKER, {}).get('manifest', {}).get('kind') == legacy.KIND:
            return legacy._existing(conn, old_request)
        existing = legacy._existing(conn, request)
        if existing:
            return existing
    legacy._validate_template(template, request)
    legacy._gates(runtime)
    config = json.loads(template['config_json'])
    scope = ActorScope(actor.user_id, actor.name, bool(template['scope_is_admin']), str(template['owner_user_id']))
    runtime.resolve_source(config, scope)
    pages = runtime.pages.list_pages(config['group_ids'], is_admin=scope.is_admin, owner_user_id=scope.owner_user_id)
    if not pages:
        raise StoreError('fb_auto_page_pool_empty', '模板当前没有有效 Page', 409)
    if len({p.page_id for p in pages}) != len(pages):
        raise StoreError('fb_auto_page_scope_invalid', 'Page 池返回重复 Page', 409)
    if runtime.pages.legacy_conflicts(config['group_ids']):
        raise StoreError('fb_auto_legacy_queue_conflict', '模板 Page 池仍被旧版自动发布队列占用', 409)
    fingerprint = {(int(template_id), request['expected_version'])}
    automatic_jobs = daily_capacity(config, pages)
    for other in store.enabled_template_sources(template_id):
        other_pages = runtime.pages.list_pages(other['config']['group_ids'], is_admin=other['scope_is_admin'], owner_user_id=other['owner_user_id'])
        automatic_jobs += daily_capacity(other['config'], other_pages)
        fingerprint.add((int(other['template_id']), int(other['template_version'])))
    with store.connect() as conn:
        captions = page_caption.load(conn, template['created_by'], [p.page_id for p in pages])
    source_config = page_caption.source_config(config, pages, captions)
    candidates, rejected, by_language = {}, [], {}
    for mid in ids:
        try:
            material = runtime.materials.exact_material(source_config, mid, allow_future=True)
            if str(material.material_id) != mid:
                raise StoreError('fb_auto_material_identity_invalid', '素材 ID 核对失败', 409)
            candidates[mid] = material
            by_language.setdefault(target_language(material.language), []).append(material.content_id)
        except Exception as exc:
            rejected.append(dict(material_id=mid, reason=getattr(exc, 'code', 'material_lookup_failed')))
    target = dict(content_ids=sorted({m.content_id for m in candidates.values()}), by_language=by_language)
    mysql = runtime.materials.mysql
    target = freeze_match(pages, target, mysql, [template_id, request['operation_id']], app_id=config['app_id'])
    times = release_times(mysql.select, mysql.schema, config['app_id'], target['content_ids'])
    with store._lock, store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = store._template_row(conn, template_id, actor)
        existing = legacy._existing(conn, request)
        if existing:
            conn.commit()
            return existing
        legacy._validate_template(current, request)
        legacy._gates(runtime)
        if current['config_sha256'] != template['config_sha256']:
            raise StoreError('fb_auto_version_conflict', '模板配置已变化，请刷新重试', 409)
        if {(int(r[0]), int(r[1])) for r in conn.execute("SELECT id,current_version FROM fb_auto_template WHERE status='enabled'")} != fingerprint:
            raise StoreError('fb_auto_capacity_snapshot_changed', '启用模板集合已变化，请重试', 409)
        now, scores, audits, plans, reasons = store.now_fn(), {}, {}, {}, {}
        stamp = utc_iso(now)
        for mid, material in candidates.items():
            try:
                plans[mid] = timing(times, material.content_id, material.language, now)
            except ValueError:
                reasons[mid] = 'drama_deploy_time_missing'
                continue
            for page in pages:
                audit = profileaudit(page.page_id, material.content_id, target)
                reason = legacy._skip_reason(conn, store, config, page, mid, plans[mid]['publish_at_utc'])
                if reason:
                    reasons.setdefault(mid, reason)
                    continue
                if not audit.get('matched') or target_language(page.language) != target_language(material.language):
                    continue
                if not page_caption.material_eligible(config, page.page_id, material, captions):
                    reasons.setdefault(mid, 'fb_auto_caption_source_missing')
                    continue
                scores[mid, page.page_id] = audit['match_score']
                audits[mid, page.page_id] = audit
        assignments = assign(ids, [p.page_id for p in pages], scores)
        rejected += [dict(material_id=mid, reason=reasons.get(mid, 'no_available_profile_match')) for mid in candidates if mid not in assignments]
        count = len(assignments)
        start = now.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
        manual_today = conn.execute("SELECT COUNT(*) FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id WHERE r.trigger_type='manual' AND x.status<>'skipped' AND x.created_at_utc>=? AND x.created_at_utc<?", (utc_iso(start), utc_iso(start+timedelta(days=1)))).fetchone()[0]
        if count > runtime.max_publishable_pages or count > runtime.max_jobs_per_slot or (count and automatic_jobs+manual_today+count > runtime.max_daily_jobs):
            raise StoreError('fb_auto_capacity_exceeded', '爆款素材发布超过当前单轮或每日容量', 409)
        page_by_id = {p.page_id:p for p in pages}
        entries = [dict(sequence=i+1, page_id=pid, group_id=page_by_id[pid].group_id, material_id=mid,
                        content_id=candidates[mid].content_id, source_media_url=candidates[mid].media_url,
                        timing=plans[mid], tag_profile=audits[mid,pid]) for i,(mid,pid) in enumerate(assignments.items())]
        result = dict(queued=count, skipped=len(rejected), total_pages=len(pages), requested_materials=len(ids),
                      skipped_materials=rejected, skipped_pages=target['skipped_pages'], assignments=entries,
                      scheduled=sum(e['timing']['publish_at_utc'] > stamp for e in entries))
        manifest = dict(kind=POLICY, request=request, template_config_sha256=template['config_sha256'],
                        actor=asdict(actor), entries=entries, result=result, cooldown_days=int(config['cooldown_days']))
        sha = digest(manifest)
        frozen_config = {**config, legacy.MARKER:dict(sha256=sha, manifest=manifest)}
        slot = 'manual:hit-material:'+request['operation_id']
        cur = conn.execute("INSERT INTO fb_auto_run(template_id,template_version,slot_key,trigger_type,status,config_json,total_pages,publishable_pages,missing_token_pages,queued_tasks,skipped_tasks,created_at_utc,completed_at_utc,planned_publish_at_utc,metric_generation_ids_json,video_template) VALUES(?,?,?,'manual',?,?,?,?,?,?,?,?,?,?,'[]','random_overlay')", (template_id,request['expected_version'],slot,'queued' if count else 'completed',canonical(frozen_config),len(pages),sum(p.eligible_token_count>0 for p in pages),sum(p.eligible_token_count<=0 for p in pages),count,0,stamp,'' if count else stamp,min((e['timing']['publish_at_utc'] for e in entries),default=stamp)))
        run_id = int(cur.lastrowid)
        for entry in entries:
            page, material = page_by_id[entry['page_id']], candidates[entry['material_id']]
            conn.execute("INSERT INTO fb_auto_run_page(run_id,page_id,group_id,group_ids_json,owner_user_id,timezone,language,eligible_token_count,snapshot_status,skip_reason) VALUES(?,?,?,?,?,?,?,?,'eligible','')", (run_id,page.page_id,page.group_id,canonical(page.group_ids),page.owner_user_id,page.timezone,page.language,page.eligible_token_count))
            job = 'fb-page-'+hashlib.sha256(f"{template_id}:{request['expected_version']}:{slot}:{page.page_id}".encode()).hexdigest()[:48]
            selection = {legacy.MARKER:sha, 'sequence':entry['sequence'], 'hit_publish_timing':entry['timing'], 'tag_profile':entry['tag_profile']}
            cur = conn.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,material_id,content_id,created_at_utc,planned_publish_at_utc,video_template,gpu_job_id,source_media_url,selection_json) VALUES(?,?,?,?,?,'planned',?,?,?,?,'random_overlay',?,?,?)", (run_id,template_id,request['expected_version'],page.page_id,page.group_id,material.material_id,material.content_id,stamp,entry['timing']['publish_at_utc'],job,material.media_url,canonical(selection)))
            selection['caption'] = page_caption.freeze(store,conn,config,page,material,int(cur.lastrowid),int(now.timestamp()),captions)
            conn.execute('UPDATE fb_auto_task SET selection_json=? WHERE id=?',(canonical(selection),int(cur.lastrowid)))
        conn.commit()
    return dict(ok=True,run_id=run_id,idempotent=False,operation_id=request['operation_id'],**result)
