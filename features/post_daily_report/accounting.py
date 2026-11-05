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
    'backlog_archived': '历史任务已按保留记录归档，不再计入待处理',
    'failure_reason_unrecorded': '原任务失败；投影未保留错误码，需查看原记录',
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


def _native_reasons(paths, result, cutoff):
    specs = [('fb','fb_auto_task','id','fb:auto:'), ('fb','fb_video_task','task_id','fb:video:'),
             ('tt_auto','tt_auto_task','id','tt:auto:'), ('tt','tt_post_queue','id','tt:queue:'),
             ('x_auto','x_auto_task','id','x:auto:'), ('x','x_post_publish_log','queue_id','x:queue:')]
    for name,table,identity,prefix in specs:
        if not paths.get(name):continue
        wanted={key for key,rows in result['jobs'].items() if key.startswith(prefix) and any(r['status']=='failed' and not r['error_code'] for r in rows)}
        if not wanted:continue
        try:
            with read_db(paths[name]) as db:
                columns={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
                if not {identity,'error_code'}<=columns:continue
                updated=next((c for c in ('updated_at_utc','updated_at','created_at_utc','created_at') if c in columns),None)
                fields=identity+',error_code'+(','+updated if updated else '')
                for row in db.execute('SELECT '+fields+' FROM '+table+" WHERE error_code<>''"):
                    key=prefix+str(row[identity]);at=parse_time(row[updated]) if updated else None
                    if key not in wanted or at and at>=cutoff:continue
                    for job in result['jobs'][key]:
                        if job['status']=='failed' and not job['error_code']:job['error_code']=row['error_code']
        except (OSError,sqlite3.Error):
            result['warnings'].append(name.upper()+' 原始错误码暂不可读，保留调度状态并标注原因未记录')


def load_dispatch(paths, cutoff=None, conn=None, *, source_keys=None, skip_confirmed=False):
    """Load display facts only; never expose payloads, credentials or raw errors."""
    result = {'available': False, 'jobs': {}, 'publications': {}, 'blocked_resources': set(), 'warnings': [], 'aliases': {}}
    if conn is None:
        path = paths.get('dispatch')
        if not path:
            return result
        try:
            with read_db(path) as db:
                return load_dispatch(paths, cutoff, db, source_keys=source_keys, skip_confirmed=skip_confirmed)
        except (OSError, sqlite3.Error):
            result['warnings'].append('调度状态读取失败，原始任务仍保留；跨日期未完成数量未知')
            return result
    present = tables(conn)
    if 'pipeline_job' not in present:
        return result
    cutoff = parse_time(cutoff) or datetime.now(timezone.utc)
    selected = None if source_keys is None else set(source_keys)

    def selected_rows(table, fields):
        where, values = [], []
        if table == 'pipeline_job' and skip_confirmed and 'pipeline_publication' in present:
            # A confirmed publication cannot contribute to unfinished/closed
            # counts. Let the indexed receipt lookup exclude it before Python
            # decodes historical task metadata. Match parse_time's UTC cutoff.
            where.append("NOT EXISTS (SELECT 1 FROM pipeline_publication p WHERE p.source_key=pipeline_job.source_key "
                         "AND p.publish_id<>'' AND julianday(p.published_at)<julianday(?))")
            values.append(cutoff.isoformat())
        query = 'SELECT ' + fields + ' FROM ' + table
        if selected is None:
            yield from conn.execute(query + (' WHERE ' + ' AND '.join(where) if where else ''), values)
        else:
            keys = sorted(selected)
            for offset in range(0, len(keys), 400):
                chunk = keys[offset:offset + 400]
                clause = where + ['source_key IN (' + ','.join('?' * len(chunk)) + ')']
                yield from conn.execute(query + ' WHERE ' + ' AND '.join(clause), values + chunk)

    fields = 'id,source_key,channel,family,stage,status,source_status,hold_reason,scheduled_at,created_at,updated_at,finished_at,error_code,resource_keys_json,source_meta_json'
    for r in selected_rows('pipeline_job', fields):
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
        for ancestor in (object_value(row.get('source_meta_json'), {}) or {}).get('ancestor_source_keys', []):
            result['aliases'][ancestor] = row['source_key']
    alias_keys = set(result['jobs']) if skip_confirmed else selected
    if alias_keys:
        # A successor may lie outside this page. Keep its suppression of the
        # original target without loading every historical task and receipt.
        for row in conn.execute("SELECT source_key,source_meta_json,created_at FROM pipeline_job WHERE source_meta_json LIKE '%\"ancestor_source_keys\"%'"):
            created = parse_time(row['created_at'])
            if created and created >= cutoff:
                continue
            for ancestor in (object_value(row['source_meta_json'], {}) or {}).get('ancestor_source_keys', []):
                if ancestor in alias_keys:
                    result['aliases'][ancestor] = row['source_key']
    if 'pipeline_publication' in present:
        for row in selected_rows('pipeline_publication', 'source_key,publish_id,published_at'):
            at = parse_time(row['published_at'])
            if row['publish_id'] and at and at < cutoff:
                result['publications'][row['source_key']] = dict(row)
    for table in ('pipeline_resource', 'pipeline_legacy_resource'):
        if table not in present:
            continue
        quarantine = ' OR r.quarantined=1' if table == 'pipeline_resource' else ''
        for r in conn.execute('SELECT r.resource_key FROM ' + table + " r JOIN pipeline_job j ON j.id=r.job_id WHERE j.status='unknown'" + quarantine):
            result['blocked_resources'].add(r[0])
    _native_reasons(paths, result, cutoff)
    result['available'] = True
    return result


def effective_state(context, source_key, now=None):
    now = parse_time(now) or datetime.now(timezone.utc)
    if source_key in context.get('aliases', {}):
        return None  # The canonical descendant represents this same target.
    if source_key in context.get('publications', {}):
        return {'state': 'published', 'code': '', 'stage': 'publish'}
    all_rows = context.get('jobs', {}).get(source_key, [])
    rows = [r for r in all_rows if r.get('hold_reason') != 'metadata_only']
    if not rows:
        rows = [r for r in all_rows if r.get('source_status') in CLOSED_NATIVE]
        if not rows:
            return None
    # The publish stage owns delivery after a successful preparation handoff.
    row = max(rows, key=lambda r: (r['stage'] == 'publish', r['id']))
    status, hold, code = row['status'], row.get('hold_reason') or '', row.get('error_code') or ''
    if row.get('channel') == 'youtube' and row['stage'] == 'prepare' and status == 'completed':
        return None  # A completed creation job is not a promised publication.
    if status == 'unknown':
        state, code = 'unknown', code or 'unknown_outcome'
    elif status == 'archived':
        state, code = 'closed', 'backlog_archived'
    elif row.get('source_status') in CLOSED_NATIVE and status not in {'running', 'legacy_running'}:
        state, code = 'closed', code or 'source_closed'
    elif status == 'backlog_paused':
        state, code = 'paused', hold or 'cutover_overdue'
    elif status == 'failed':
        code = code or 'failure_reason_unrecorded'
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


def backlog(paths, *, context=None, cutoff=None, visible_keys=None, include_plans=True, channels=None, async_plans=False):
    cutoff = parse_time(cutoff) or datetime.now(timezone.utc)
    context = context if context is not None else load_dispatch(paths, cutoff)
    channels = list(channels or ('tt', 'fb', 'x', 'youtube'))
    result = {'as_of': cutoff.isoformat(), 'available': context['available'],
              'warnings': list(context['warnings']), 'channels': [], 'scope_note': '跨日期未完成；已取消/策略关闭不计入待处理，成功以发布回执为准'}
    if not context['available']:
        result.update(unfinished=None, instantiated_unfinished=None, unqueued=None)
        return result
    from .plans import collect_plan_gaps, live_plan_gaps
    plans = (live_plan_gaps(paths, channels) if async_plans else collect_plan_gaps(paths, cutoff, channels)) if include_plans else {'available': False, 'items': [], 'warnings': ['未入队计划缺少可靠的个人归属；仅全站管理员汇总，不按零处理']}
    result['plans_as_of'] = plans.get('as_of', cutoff.isoformat())
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
        dates = [p['due_at'] for p in missing if p.get('due_at')]
        group['plans_oldest_at'] = min(dates, default=None)
        group['plans_latest_at'] = max(dates, default=None)
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
