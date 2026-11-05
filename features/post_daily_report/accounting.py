"""Shared read-only publication state. Never initialize a publisher/dispatch store.

The execution queue is not a publication ledger: failed claims remain unfinished,
completed media is not a Post, and a confirmed target receipt wins over workflow
errors. Original states are retained alongside the effective display state.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from .common import parse_time, read_db

DEFAULT_PATHS = {
    'dispatch': '/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3',
    'x': '/var/lib/x-post-automation/accounts.sqlite3',
    'x_auto': '/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3',
    'fb': '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3',
    'tt_auto': '/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3',
    'tt': '/mnt/data-disk/tt-post-publisher/tt-post.sqlite3',
}
STATE_LABELS = {'waiting': '待执行', 'executing': '执行中', 'scheduled': '预约中',
                'blocked': '受阻', 'failed': '失败', 'unknown': '结果待核对',
                'paused': '历史暂停', 'published': '发布成功', 'closed': '已取消/策略关闭'}
REASONS = {
    'canonical_task_not_claimable': '原任务领取被拒绝，需核对模板启停、排期或占用',
    'canonical_account_busy': '等待同账号前序任务或有效占用',
    'adapter_unavailable': '当前执行适配器不可用',
    'canonical_link_pending': '短链尚未就绪，发布受阻',
    'blocked_by_unknown': '同账号历史发布结果待核对，后续任务受阻',
    'unknown_outcome': '发布结果待核对，不能认定成功或失败',
    'cutover_overdue': '历史任务暂停，保留原任务与回执',
    'canonical_review_or_enqueue': '等待审核或后续入队',
    'worker_offline': '执行器心跳失联，需核对在途任务',
    'schedule_missing': '缺少可核对的执行时间',
    'identity_missing': '缺少可核对的账号身份',
    'prepared_handoff_pending': '制作已完成，发布交接状态待核对',
    'cos_upload_failed': '成片上传 COS 失败，尚未确认发布',
    'fb_gpu_cos_upload_failed': '成片上传 COS 失败，尚未确认发布',
    'tt_upstream_rejected': 'TikTok 上游请求被拒绝，需核对原任务记录',
    'x_auto_no_eligible_material': '没有符合筛选条件的素材，本次计划已跳过',
    'x_post_pool_fifo_conflict': '素材选择顺序与冻结计划冲突，未形成队列',
    'x_post_schedule_partial_capacity': '部分计划账号未形成执行队列',
    'x_upstream_error': '上游预检失败，未形成执行队列',
    'plan_not_enqueued': '已到期的冻结计划尚未形成执行任务',
    'plan_cancelled': '未入队计划已明确取消，保留取消记录',
    'source_closed': '原任务已取消或正常策略跳过',
    'execution_pending': '任务待执行',
}
BLOCKED_CODES = {'canonical_task_not_claimable', 'canonical_account_busy', 'adapter_unavailable',
                 'canonical_link_pending', 'blocked_by_unknown', 'canonical_review_or_enqueue'}
CLOSED_NATIVE = {'skipped', 'cancelled', 'canceled', 'abandoned', 'duplicate', 'no_candidate'}


def reason_label(code):
    if code in REASONS:
        return REASONS[code]
    safe = str(code or '')
    return ('任务未完成，原错误码：' + safe) if re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', safe) else '任务未完成，具体原因待核对'


def object_value(raw, default=None):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw if raw is not None else default
    except (ValueError, TypeError):
        return default


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def read_rows(conn, table):
    return [dict(r) for r in conn.execute('SELECT * FROM ' + table)] if table in tables(conn) else []


def load_dispatch(paths, cutoff=None, conn=None):
    """Load display facts only; never expose payloads, credentials or raw errors."""
    result = {'available': False, 'jobs': {}, 'publications': {}, 'blocked_resources': set(), 'warnings': []}
    if conn is None:
        path = paths.get('dispatch')
        if not path:
            return result
        try:
            with read_db(path) as db:
                return load_dispatch(paths, cutoff, db)
        except (OSError, sqlite3.Error):
            result['warnings'].append('调度状态读取失败，原始任务仍保留；跨日期未完成数量未知')
            return result
    present = tables(conn)
    if 'pipeline_job' not in present:
        return result
    cutoff = parse_time(cutoff) or datetime.now(timezone.utc)
    fields = 'id,source_key,channel,family,stage,status,source_status,hold_reason,scheduled_at,created_at,updated_at,finished_at,error_code,resource_keys_json'
    for r in conn.execute('SELECT ' + fields + ' FROM pipeline_job'):
        row = dict(r)
        created = parse_time(row['created_at'])
        if created and created >= cutoff:
            continue
        # Do not backdate an event that happened after a frozen report cutoff.
        changed = parse_time(row['updated_at'])
        if changed and changed >= cutoff and 'pipeline_event' in present:
            event = conn.execute("SELECT event_type,details_json,created_at FROM pipeline_event WHERE job_id=? AND created_at<? AND event_type IN ('offered','claimed','failed','completed','published','paused') ORDER BY id DESC LIMIT 1", (row['id'], cutoff.isoformat())).fetchone()
            if event:
                detail = object_value(event['details_json'], {}) or {}
                row['status'] = {'claimed': 'running', 'paused': 'backlog_paused'}.get(event['event_type'], detail.get('status', event['event_type']))
                row['status'] = 'queued' if row['status'] == 'offered' else row['status']
                row['error_code'] = detail.get('code', '')
                row['updated_at'] = event['created_at']
                row['source_status'] = detail.get('source_status', '')
                if row['status'] != 'failed':
                    row['hold_reason'] = ''
            else:
                continue
        result['jobs'].setdefault(row['source_key'], []).append(row)
    if 'pipeline_publication' in present:
        for row in conn.execute('SELECT source_key,publish_id,published_at FROM pipeline_publication'):
            at = parse_time(row['published_at'])
            if row['publish_id'] and at and at < cutoff:
                result['publications'][row['source_key']] = dict(row)
    for table in ('pipeline_resource', 'pipeline_legacy_resource'):
        if table not in present:
            continue
        quarantine = ' OR r.quarantined=1' if table == 'pipeline_resource' else ''
        for r in conn.execute('SELECT r.resource_key FROM ' + table + " r JOIN pipeline_job j ON j.id=r.job_id WHERE j.status='unknown'" + quarantine):
            result['blocked_resources'].add(r[0])
    result['available'] = True
    return result


def effective_state(context, source_key, now=None):
    now = parse_time(now) or datetime.now(timezone.utc)
    if source_key in context.get('publications', {}):
        return {'state': 'published', 'code': '', 'stage': 'publish'}
    rows = [r for r in context.get('jobs', {}).get(source_key, []) if r.get('hold_reason') != 'metadata_only']
    if not rows:
        return None
    # The publish stage owns delivery after a successful preparation handoff.
    row = max(rows, key=lambda r: (r['stage'] == 'publish', r['id']))
    status, hold, code = row['status'], row.get('hold_reason') or '', row.get('error_code') or ''
    if status == 'unknown':
        state, code = 'unknown', code or 'unknown_outcome'
    elif row.get('source_status') in CLOSED_NATIVE and status not in {'running', 'legacy_running'}:
        state, code = 'closed', code or 'source_closed'
    elif status == 'backlog_paused':
        state, code = 'paused', hold or 'cutover_overdue'
    elif status == 'failed':
        state = 'blocked' if code in BLOCKED_CODES else 'failed'
    elif status in {'running', 'legacy_running'}:
        state = 'executing'
    elif hold:
        state, code = 'blocked', hold
    elif status in {'published'}:
        # A status without its canonical target receipt is not proof of delivery.
        state, code = 'unknown', 'unknown_outcome'
    elif status == 'completed':
        if row['stage'] == 'publish':
            state, code = 'unknown', 'unknown_outcome'
        else:
            state, code = 'blocked', 'prepared_handoff_pending'
    else:
        due = parse_time(row.get('scheduled_at'))
        resources = object_value(row.get('resource_keys_json'), []) or []
        if not due:
            state, code = 'blocked', 'schedule_missing'
        elif due > now:
            state, code = 'scheduled', ''
        elif any(r in context.get('blocked_resources', set()) for r in resources):
            state, code = 'blocked', 'blocked_by_unknown'
        else:
            state, code = 'waiting', code or 'execution_pending'
    return {'state': state, 'code': code, 'reason': reason_label(code) if code else STATE_LABELS[state],
            'stage': row['stage'], 'source_status': row.get('source_status', ''), 'dispatch_status': status,
            'source_key': source_key}


def daily_override(context, key, cutoff):
    value = effective_state(context, key, cutoff)
    if not value or value['state'] == 'published':
        return None
    bucket = {'blocked': 'blocked', 'paused': 'blocked', 'failed': 'failed', 'unknown': 'unknown',
              'closed': 'policy_skipped', 'waiting': 'pending', 'scheduled': 'pending', 'executing': 'pending'}[value['state']]
    return bucket, value['code'], value['reason']


def backlog(paths, *, context=None, cutoff=None, visible_keys=None, include_plans=True, channels=None):
    cutoff = parse_time(cutoff) or datetime.now(timezone.utc)
    context = context if context is not None else load_dispatch(paths, cutoff)
    channels = list(channels or ('tt', 'fb', 'x', 'youtube'))
    result = {'as_of': cutoff.isoformat(), 'available': context['available'],
              'warnings': list(context['warnings']), 'channels': [], 'scope_note': '跨日期未完成；已取消/策略关闭不计入待处理，成功以发布回执为准'}
    if not context['available']:
        result.update(unfinished=None, instantiated_unfinished=None, unqueued=None)
        return result
    from .plans import collect_plan_gaps
    plans = collect_plan_gaps(paths, cutoff, channels) if include_plans else {'available': False, 'items': [], 'warnings': ['未入队计划缺少可靠的个人归属；仅全站管理员汇总，不按零处理']}
    result['warnings'].extend(plans['warnings'])
    grouped = {c: {'channel': c, 'states': Counter(), 'reasons': Counter(), 'sample': [], 'closed': 0} for c in channels}
    for key in context['jobs']:
        channel = key.split(':', 1)[0]
        if channel not in grouped or visible_keys is not None and key not in visible_keys:
            continue
        state = effective_state(context, key, cutoff)
        if not state or state['state'] == 'published':
            continue
        group = grouped[channel]
        if state['state'] == 'closed':
            group['closed'] += 1
            continue
        group['states'][state['state']] += 1
        if state['code']:
            group['reasons'][state['code']] += 1
        if len(group['sample']) < 20:
            group['sample'].append(state)
    for channel, group in grouped.items():
        missing = [p for p in plans['items'] if p['channel'] == channel and not p.get('closed')]
        closed = [p for p in plans['items'] if p['channel'] == channel and p.get('closed')]
        known = plans.get('channel_available', {}).get(channel, plans['available'])
        group['scheduled'] = group['states'].get('scheduled', 0)
        group['instantiated_unfinished'] = sum(group['states'].values()) - group['scheduled']
        group['unqueued'] = len(missing) if known else None
        group['known_unqueued'] = len(missing)
        group['closed_unqueued'] = len(closed)
        group['unfinished'] = group['instantiated_unfinished'] + len(missing) if known else None
        group['states'] = dict(group['states'])
        group['reasons'] = [{'code': c, 'reason': reason_label(c), 'count': n} for c, n in group['reasons'].most_common()]
        group['plan_reasons'] = [{'code': c, 'reason': reason_label(c), 'count': n} for c,n in Counter(p['code'] for p in missing).most_common()]
        group['plan_sample'] = missing[:20]
        result['channels'].append(group)
    result['instantiated_unfinished'] = sum(g['instantiated_unfinished'] for g in result['channels'])
    result['unqueued'] = sum(g['unqueued'] for g in result['channels']) if all(g['unqueued'] is not None for g in result['channels']) else None
    result['unfinished'] = result['instantiated_unfinished'] + result['unqueued'] if result['unqueued'] is not None else None
    return result
