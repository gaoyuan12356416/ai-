"""Explicit one-off drama selection through the ordinary FB planner."""
import json
import re
from .core import StoreError
from .hit_material import _gates

MARKER = 'hit_drama_publish'


def normalize(payload):
    if set(payload) != {'expected_version', 'drama_id', 'id_type', 'operation_id'}:
        raise StoreError('invalid_request', '爆款剧发布请求无效', 400)
    if payload['id_type'] not in ('content_id', 'resource_id') or not re.fullmatch(r'[1-9][0-9]{0,30}', str(payload['drama_id'])):
        raise StoreError('invalid_request', '请输入一个有效的剧 ID 或资源 ID', 400)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,70}', str(payload['operation_id'])):
        raise StoreError('invalid_request', '操作标识无效', 400)
    if isinstance(payload['expected_version'], bool) or not re.fullmatch(r'[1-9][0-9]{0,8}', str(payload['expected_version'])):
        raise StoreError('invalid_request', '模板版本无效', 400)
    return dict(payload, drama_id=str(payload['drama_id']), expected_version=int(payload['expected_version']))


def receipt(conn, template_id, slot, request):
    row = conn.execute('SELECT id,config_json,queued_tasks,skipped_tasks,total_pages FROM fb_auto_run WHERE template_id=? AND slot_key=?', (template_id, slot)).fetchone()
    if not row:
        return None
    marker = json.loads(row['config_json']).get(MARKER, {})
    if marker.get('request') != request:
        raise StoreError('hit_drama_operation_conflict', '同一操作标识已用于不同剧目或模板版本', 409)
    return dict(ok=True, run_id=row['id'], idempotent=True, queued=row['queued_tasks'], skipped=row['skipped_tasks'], total_pages=row['total_pages'], content_id=marker['content_id'])


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
    content_id = request['drama_id']
    if request['id_type'] == 'resource_id':
        rows = mysql.select(f'SELECT DISTINCT content_id FROM `{mysql.schema}`.ads_drama_resource WHERE id=%s AND app_id=%s LIMIT 2', (content_id, config['app_id']))
        ids = {str(r['content_id']).strip() for r in rows if r.get('content_id')}
        if len(ids) != 1:
            raise StoreError('hit_drama_not_found', '资源 ID 未关联到目标产品下的唯一剧目', 409)
        content_id = ids.pop()
    rows = mysql.select(f'SELECT content_id FROM `{mysql.schema}`.ads_drama_info WHERE app_id=%s AND content_id=%s LIMIT 1', (config['app_id'], content_id))
    if not rows:
        raise StoreError('hit_drama_not_found', '目标产品下未找到该剧', 409)
    result = runtime.store.create_run(template_id, slot, 'manual', actor, runtime.pages, runtime.materials,
        expected_template_version=request['expected_version'], hit_drama=dict(request=request, content_id=content_id),
        max_publishable_pages=runtime.max_publishable_pages, max_jobs_per_slot=runtime.max_jobs_per_slot, max_daily_jobs=runtime.max_daily_jobs)
    summary = result.get('summary', {})
    return dict(result, queued=summary.get('queued_tasks', result.get('queued', 0)), skipped=summary.get('skipped_tasks', result.get('skipped', 0)), content_id=content_id)
