"""Explicit one-off drama selection through the ordinary FB planner."""
import json
import re
from .core import StoreError
from .hit_material import _gates
from features.hit_drama_target import identifier, resolve_target, content_ids
from .hit_profile import freeze_match

MARKER = 'hit_drama_publish'


def run_summary(conn, run_id):
    return [dict(reason=row[0], count=row[1]) for row in conn.execute(
        "SELECT COALESCE(NULLIF(skip_reason,''),NULLIF(error_code,''),'unknown') reason,COUNT(*) n "
        "FROM fb_auto_task WHERE run_id=? AND status IN ('skipped','failed') GROUP BY reason ORDER BY n DESC,reason", (run_id,))]


def normalize(payload):
    if set(payload) != {'expected_version', 'drama_id', 'id_type', 'operation_id'}:
        raise StoreError('invalid_request', '爆款剧发布请求无效', 400)
    try:
        value = identifier(payload['id_type'], payload['drama_id'])
    except ValueError as exc:
        raise StoreError('invalid_request', str(exc), 400) from None
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,70}', str(payload['operation_id'])):
        raise StoreError('invalid_request', '操作标识无效', 400)
    if isinstance(payload['expected_version'], bool) or not re.fullmatch(r'[1-9][0-9]{0,8}', str(payload['expected_version'])):
        raise StoreError('invalid_request', '模板版本无效', 400)
    return dict(payload, drama_id=value, expected_version=int(payload['expected_version']))


def receipt(conn, template_id, slot, request):
    row = conn.execute('SELECT id,config_json,queued_tasks,skipped_tasks,total_pages FROM fb_auto_run WHERE template_id=? AND slot_key=?', (template_id, slot)).fetchone()
    if not row:
        return None
    marker = json.loads(row['config_json']).get(MARKER, {})
    if marker.get('request') != request:
        raise StoreError('hit_drama_operation_conflict', '同一操作标识已用于不同剧目或模板版本', 409)
    return dict(ok=True, run_id=row['id'], idempotent=True, queued=row['queued_tasks'], skipped=row['skipped_tasks'], total_pages=row['total_pages'],
                content_id=marker.get('content_id',''), content_ids=content_ids(marker), languages=marker.get('languages',[]),
                matched_pages=len(marker.get('page_ids', [])) if 'page_ids' in marker else None,
                match_policy=marker.get('match_policy', 'legacy_language_only'),
                skipped_pages=marker.get('skipped_pages', []), skip_summary=run_summary(conn, row['id']))


def publish_drama(runtime, template_id, actor, payload):
    request = normalize(payload)
    slot = 'manual:hit-drama:' + request['operation_id']
    with runtime.store.connect() as conn:
        template = dict(runtime.store._template_row(conn, template_id, actor))
        existing = receipt(conn, template_id, slot, request)
        if existing:
            return existing
    _gates(runtime)
    if template['current_version'] != request['expected_version'] or template['status'] != 'enabled':
        raise StoreError('fb_auto_version_conflict', '模板已停用或版本已变化，请刷新后重试', 409)
    config = json.loads(template['config_json'])
    mysql = runtime.materials.mysql
    try:
        target = resolve_target(mysql.select, mysql.schema, config['app_id'], request)
    except ValueError as exc:
        raise StoreError('hit_drama_not_found', str(exc), 409) from None
    page_rows = runtime.pages.list_pages(config['group_ids'], is_admin=bool(template['scope_is_admin']),
                                        owner_user_id=str(template['owner_user_id']))
    target = freeze_match(page_rows, target, mysql, [template_id, request['expected_version'], slot],
                          app_id=config['app_id'])
    if not target['page_ids']:
        error = StoreError('hit_drama_no_matching_page', '没有语言、受众和题材收入画像同时匹配的可发布Page，未创建发布任务', 409)
        error.conflicts = target['skipped_pages']
        raise error
    from features.hit_publish import release_times, POLICY
    target['release_times'] = release_times(mysql.select, mysql.schema, config['app_id'], content_ids(target))
    target['timing_policy'] = POLICY
    result = runtime.store.create_run(template_id, slot, 'manual', actor, runtime.pages, runtime.materials,
        expected_template_version=request['expected_version'], hit_drama=dict(target, request=request),
        max_publishable_pages=runtime.max_publishable_pages, max_jobs_per_slot=runtime.max_jobs_per_slot, max_daily_jobs=runtime.max_daily_jobs)
    with runtime.store.connect() as conn:
        saved = receipt(conn, template_id, slot, request)
    return dict(saved, idempotent=bool(result.get('idempotent')))
