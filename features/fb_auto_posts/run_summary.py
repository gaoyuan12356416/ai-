"""Read-only run projections. Never use display states to drive publication."""
import json
from datetime import timedelta, timezone

PENDING = {'planned', 'ready', 'queued'}
ACTIVE = {'preparing', 'running'}
TERMINAL = {'published', 'failed', 'failed_without_retry', 'skipped', 'cancelled', 'canceled'}
LABELS = {
    'running': '执行中', 'waiting': '等待执行', 'scheduled': '等待排期',
    'paused': '已暂停', 'stopped': '已停止', 'attention_required': '待处理',
    'completed': '已完成', 'partial_failed': '部分失败', 'failed': '失败',
    'cancelled': '已取消',
}


def annotate_runs(conn, runs, now):
    """Aggregate only the caller's authorized run IDs in its read snapshot."""
    if not runs:
        return
    now_text = now.isoformat()
    day_start = now.astimezone(timezone(timedelta(hours=8))).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    by_id = {int(run['id']): run for run in runs}
    summaries = {rid: {} for rid in by_id}
    waits = {}
    ids = list(by_id)
    # Keep below SQLite's legacy bind limit, even for internal callers.
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        marks = ','.join('?' for _ in batch)
        rows = conn.execute(f"""SELECT run_id,status,COUNT(*) n,
            SUM(CASE WHEN julianday(planned_publish_at_utc)>julianday(?) THEN 1 ELSE 0 END) future,
            SUM(CASE WHEN julianday(planned_publish_at_utc)<julianday(?) THEN 1 ELSE 0 END) overdue,
            SUM(CASE WHEN lease_owner<>'' AND julianday(lease_expires_at_utc)>julianday(?) THEN 1 ELSE 0 END) leased,
            MAX(NULLIF(completed_at_utc,'')) last_completed
            FROM fb_auto_task WHERE run_id IN ({marks}) GROUP BY run_id,status""",
            (now_text, day_start, now_text, *batch))
        for row in rows:
            summaries[row['run_id']][row['status']] = dict(row)
        for row in conn.execute(f'SELECT run_id,COUNT(*) n FROM fb_auto_mobo_wait WHERE run_id IN ({marks}) GROUP BY run_id', batch):
            waits[row['run_id']] = row['n']
    for rid, run in by_id.items():
        states = summaries[rid]
        count = lambda names: sum(states.get(name, {}).get('n', 0) for name in names)
        pending = count(PENDING)
        active = sum(states.get(name, {}).get('leased', 0) for name in ACTIVE)
        stale = count(ACTIVE) - active
        future = sum(states.get(name, {}).get('future', 0) for name in PENDING)
        overdue = sum(states.get(name, {}).get('overdue', 0) for name in PENDING)
        other = count(set(states) - PENDING - ACTIVE - TERMINAL - {'submitted', 'unknown'})
        review = count({'submitted', 'unknown'}) + stale + other
        counts = dict(total=count(states), published=count({'published'}),
                      pending=pending, active=active, review=review,
                      failed=count({'failed', 'failed_without_retry'}), skipped=count({'skipped'}),
                      cancelled=count({'cancelled', 'canceled'}),
                      scheduled=future, overdue=overdue, stale_active=stale,
                      submitted=count({'submitted'}), unknown=count({'unknown'}),
                      waiting_materials=int(waits.get(rid, 0)))
        template = run.get('template_status', '')
        try:
            config = json.loads(run.get('config_json') or '{}')
        except (ValueError, TypeError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        independent = run.get('trigger_type') == 'manual' and config.get('explicit_publish_independent') == 1
        paused = template == 'disabled' and not independent
        outstanding = pending or counts['waiting_materials']
        if active:
            status, reason = 'running', f'{active} 条任务正在制作或提交'
        elif review:
            status, reason = 'attention_required', f'{review} 条任务待核对结果或执行状态'
        elif outstanding and template == 'deleted':
            status, reason = 'stopped', '模板已删除，剩余任务未执行'
        elif outstanding and paused:
            status, reason = 'paused', '模板已停用，剩余任务等待恢复'
        elif overdue:
            status, reason = 'attention_required', f'{overdue} 条未完成任务的排期早于今天'
        elif outstanding:
            status = 'scheduled' if pending and future == pending and not counts['waiting_materials'] else 'waiting'
            reason = '尚未到计划发布时间' if status == 'scheduled' else '等待制作、发布或素材就绪'
        elif counts['failed']:
            status = 'partial_failed' if counts['published'] or counts['skipped'] else 'failed'
            reason = '全部任务已结束，存在发布失败'
        elif counts['cancelled'] and not counts['published']:
            status, reason = 'cancelled', '任务已取消'
        elif counts['total'] or run.get('status') == 'completed':
            status, reason = 'completed', '全部任务已结束'
        elif run.get('status') in {'failed', 'partial_failed', 'cancelled'}:
            status, reason = run['status'], '批次已结束，未生成子任务'
        else:
            status, reason = 'attention_required', '暂无子任务明细，请核对批次记录'
        # Show the pause/deletion even when unresolved receipts take precedence.
        if outstanding and status in {'running', 'attention_required'}:
            if template == 'deleted':
                reason += '；模板已删除，待执行任务已停止'
            elif paused:
                reason += '；模板已停用，待执行任务已暂停'
        completed = ''
        if not (outstanding or active or review):
            completed = run.get('completed_at_utc') or max((row['last_completed'] or '' for row in states.values()), default='')
        run.update(display_status=status, display_status_label=LABELS[status],
                   display_reason=reason, task_counts=counts,
                   display_completed_at_utc=completed, statistics_at_utc=now_text)
