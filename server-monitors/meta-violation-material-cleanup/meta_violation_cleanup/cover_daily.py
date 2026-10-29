"""Frozen Beijing reporting windows; reads execution ledgers without mutations."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import subprocess

from .notifications import Notifier, recipient
from .runtime import CST, process_lock


def report_settings(config):
    hours = config.get('cover_report_hours', 24)
    if type(hours) is not int or hours not in (4, 24):
        raise ValueError('invalid_cover_report_hours')
    lookback_hours = config.get('cover_report_lookback_hours', 24)
    if type(lookback_hours) is not int or lookback_hours not in (4, 24):
        raise ValueError('invalid_cover_report_lookback_hours')
    return hours, lookback_hours, recipient(config.get('cover_report_recipient'))


def window(at=None, hours=24, lookback_hours=24):
    if type(hours) is not int or hours not in (4, 24):
        raise ValueError('invalid_cover_report_hours')
    if type(lookback_hours) is not int or lookback_hours not in (4, 24):
        raise ValueError('invalid_cover_report_lookback_hours')
    at = (at or datetime.now(CST)).astimezone(CST)
    anchor = at.replace(hour=9, minute=30, second=0, microsecond=0)
    interval = timedelta(hours=hours)
    end = anchor + ((at - anchor) // interval) * interval
    return end - timedelta(hours=lookback_hours), end


def summary(values):
    values = sorted(v for v in values if v >= 0)
    if not values:
        return {'n': 0, 'mean_seconds': None, 'p95_seconds': None}
    return {'n': len(values), 'mean_seconds': round(sum(values) / len(values), 3),
            'p95_seconds': round(values[math.ceil(len(values) * .95) - 1], 3)}


def elapsed(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def worker_state():
    try:
        result = subprocess.run(['systemctl', 'show', 'meta-cover-worker.service',
                                 '-p', 'ActiveState', '-p', 'Result'],
                                capture_output=True, text=True, timeout=5, check=True)
        return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    except (OSError, subprocess.SubprocessError):
        return {'ActiveState': 'unavailable'}


def collect(path, start, end):
    """Outcome events use [start,end); current backlog is labelled separately.

    Historical records do not measure individual GETs or preflight. Timings
    below are explicitly ledger stage elapsed times, including relevant waits.
    """
    bounds = tuple(t.astimezone(timezone.utc).isoformat(timespec='microseconds') for t in (start, end))
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        # Read one consistent snapshot; never sum mutable batch totals.
        outcomes, intents, successes = {}, {}, {}
        for row in db.execute('''SELECT object_key,action,created_at FROM events
            WHERE created_at<? ORDER BY id''', (bounds[1],)):
            if row['action'] == 'write_intent':
                intents.setdefault(row['object_key'], row['created_at'])
            if row['created_at'] < bounds[0]:
                continue
            if row['action'] == 'succeeded':
                successes[row['object_key']] = row['created_at']
            if row['action'] in ('succeeded', 'failed', 'unknown', 'obsolete', 'blocked',
                                 'deleted_verified', 'previous_deleted', 'delete_failed', 'delete_unknown'):
                outcomes[row['object_key']] = row['action']
        tasks = Counter(v for k, v in outcomes.items() if k.startswith('task:'))
        deletions = Counter(v for k, v in outcomes.items() if k.startswith('creative:'))
        current_tasks = dict(db.execute('SELECT status,count(*) FROM tasks GROUP BY status').fetchall())
        current_deletions = dict(db.execute('SELECT status,count(*) FROM deletions GROUP BY status').fetchall())
        batches = list(db.execute('''SELECT status,meta,created_at,sealed_at FROM batches
            WHERE sealed_at>=? AND sealed_at<?''', bounds))
        scans = [json.loads(r['meta']).get('seconds') for r in batches if r['status'] == 'sealed']
        scans = [x for x in scans if isinstance(x, (int, float))]
        resources = [elapsed(r[0], r[1]) for r in db.execute('''SELECT created_at,updated_at FROM resources
            WHERE status='accepted' AND updated_at>=? AND updated_at<?''', bounds)]
        binds, before_bind, total = [], [], []
        # The first intent is durable and never replayed. A later read-only
        # reconciliation is deliberately included in the verification latency.
        for row in db.execute('SELECT id,created_at FROM tasks'):
            key = 'task:' + str(row['id'])
            if key not in successes:
                continue
            total.append(elapsed(row['created_at'], successes[key]))
            if key in intents:
                before_bind.append(elapsed(row['created_at'], intents[key]))
                binds.append(elapsed(intents[key], successes[key]))
        cleanup = [elapsed(r[0], r[1]) for r in db.execute('''SELECT created_at,updated_at FROM deletions
            WHERE status='deleted_verified' AND updated_at>=? AND updated_at<?''', bounds)]
        added_ads = db.execute('SELECT count(*) FROM tasks WHERE created_at>=? AND created_at<?', bounds).fetchone()[0]
        added_materials = db.execute('''SELECT count(*) FROM (SELECT material_id,min(first_seen) first_seen
            FROM materials GROUP BY material_id) WHERE first_seen>=? AND first_seen<?''', bounds).fetchone()[0]
        failed_scans = db.execute("SELECT count(*) FROM batches WHERE status='failed' AND id NOT LIKE 'cover-error-%' AND created_at>=? AND created_at<?", bounds).fetchone()[0]
        worker_errors = db.execute("SELECT count(*) FROM batches WHERE status='failed' AND id LIKE 'cover-error-%' AND created_at>=? AND created_at<?", bounds).fetchone()[0]
        return {'window_start': start.isoformat(), 'window_end': end.isoformat(),
                'snapshot_at': datetime.now(CST).isoformat(timespec='seconds'),
                'new_ads': added_ads, 'new_materials': added_materials,
                'scan_completed': sum(r['status'] == 'sealed' for r in batches),
                'scan_failed': failed_scans, 'worker_errors': worker_errors,
                'tasks': dict(tasks), 'deletions': dict(deletions),
                'current_tasks': current_tasks, 'current_deletions': current_deletions,
                'timings': {k: summary(v) for k, v in (
                    ('scan', scans), ('resource_acceptance', resources),
                    ('enqueue_to_bind', before_bind), ('bind_verification', binds),
                    ('replacement_total', total), ('cleanup_latency', cleanup))}}
    finally:
        db.close()


def _active_seconds(intervals):
    """Union wall intervals: concurrent ads and cleanup are never added twice."""
    seconds = 0.
    previous = None
    for left, right in sorted(intervals):
        if previous is None:
            previous = [left, right]
        elif left <= previous[1]:
            previous[1] = max(previous[1], right)
        else:
            seconds += (previous[1] - previous[0]).total_seconds()
            previous = [left, right]
    if previous is not None:
        seconds += (previous[1] - previous[0]).total_seconds()
    return seconds


def collect_performance(root, start, end):
    """Completed-cycle samples; throughput uses the union of active intervals."""
    keys = ('source_prepare', 'reference_scan', 'ad_attempt_succeeded',
            'ad_preflight', 'creative_prepare_verify', 'ad_bind_verify',
            'creative_delete_verify', 'graph_budget_wait', 'http_get', 'http_post', 'http_delete')
    totals = {key: [0, 0.] for key in keys}
    cycles = successes = errors = 0
    intervals, lanes = [], {}
    day = start.date()
    while day <= end.date():
        path = Path(root) / 'performance' / (day.strftime('%Y%m%d') + '.jsonl')
        day += timedelta(days=1)
        if not path.exists():
            continue
        try:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        cycle = json.loads(line)
                        at = datetime.fromisoformat(cycle['at'])
                        if at.tzinfo is None:
                            raise ValueError('naive_cycle_time')
                        if not start <= at < end:
                            continue
                        result = cycle['result']
                        # Empty polls do not count as active execution.
                        counts = list(result.get('tasks', {}).values()) + list(result.get('deletions', {}).values())
                        if not any(type(value) is int and value > 0 for value in counts):
                            continue
                        duration_s, successful = result['seconds'], result.get('tasks', {}).get('succeeded', 0)
                        deleted = result.get('deletions', {}).get('deleted_verified', 0)
                        lane = cycle.get('lane', 'legacy')
                        if (type(duration_s) not in (int, float) or not math.isfinite(duration_s) or duration_s <= 0
                                or type(successful) is not int or successful < 0
                                or type(deleted) is not int or deleted < 0
                                or lane not in ('legacy', 'ads', 'cleanup')):
                            raise ValueError('invalid_cycle_metrics')
                        # Old started_at includes credential/lease/reconciliation
                        # time, so anchor legacy durations at completion instead.
                        left = (datetime.fromisoformat(cycle['execution_started_at'])
                                if lane != 'legacy' else at - timedelta(seconds=duration_s))
                        if left.tzinfo is None or left >= at:
                            raise ValueError('invalid_cycle_interval')
                        metrics = {}
                        for key in keys:
                            metric = result.get('timings', {}).get(key, {})
                            n, total = metric.get('count', 0), metric.get('total_seconds', 0)
                            if (type(n) is not int or n < 0 or type(total) not in (int, float)
                                    or not math.isfinite(total) or total < 0):
                                raise ValueError('invalid_phase_metrics')
                            metrics[key] = (n, total)
                        cycles += 1
                        successes += successful
                        intervals.append((left, at))
                        item = lanes.setdefault(lane, {'cycles': 0, 'successes': 0, 'deleted': 0, 'seconds': 0.})
                        item['cycles'] += 1
                        item['successes'] += successful
                        item['deleted'] += deleted
                        item['seconds'] += duration_s
                        for key, (n, total) in metrics.items():
                            totals[key][0] += n
                            totals[key][1] += total
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                        errors += 1
        except (OSError, UnicodeError):
            errors += 1
    seconds = _active_seconds(intervals)
    for item in lanes.values():
        item['success_per_hour'] = round(item['successes'] * 3600 / item['seconds'], 2)
        item['deletions_per_hour'] = round(item['deleted'] * 3600 / item['seconds'], 2)
        item['seconds'] = round(item['seconds'], 3)
    return {'cycles': cycles, 'successes': successes, 'seconds': round(seconds, 3),
            'success_per_hour': round(successes * 3600 / seconds, 2) if seconds else None,
            'throughput_basis': 'active_interval_union', 'lanes': lanes, 'read_errors': errors,
            'timings': {key: {'n': n, 'mean_seconds': round(total / n, 3) if n else None}
                        for key, (n, total) in totals.items()}}


def duration(seconds):
    if seconds is None:
        return '无样本'
    if seconds >= 3600:
        return '%.2f 小时' % (seconds / 3600)
    if seconds >= 60:
        return '%.2f 分钟' % (seconds / 60)
    return '%.2f 秒' % seconds


def report_body(data):
    t, d = data['tasks'], data['deletions']
    ct, cd = data['current_tasks'], data['current_deletions']
    start, end = (datetime.fromisoformat(data[k]).strftime('%m-%d %H:%M')
                  for k in ('window_start', 'window_end'))
    hours = data.get('report_hours', 24)
    lookback_hours = data.get('report_lookback_hours',
                             int(elapsed(data['window_start'], data['window_end']) / 3600))
    title = '每日汇总' if hours == 24 else '每%d小时汇总' % hours
    cadence = ('每天 09:30 播报一次' if hours == 24 else '每%d小时播报一次' % hours)
    cadence += '，汇总近%d小时' % lookback_hours
    lines = ['Meta 违规素材封面替换｜' + title,
             '统计窗口：%s 至 %s（北京时间，前含后不含）' % (start, end),
             '范围：DramaWave / HotDrama；' + cadence,
             '新增达标素材 %d；新增关联广告 %d' % (data['new_materials'], data['new_ads']),
             '扫描：完成 %d 批，异常 %d 批' % (data['scan_completed'], data['scan_failed']),
             '执行异常批次：%d' % data['worker_errors'],
             '广告结果：成功 %d，失败 %d，未知 %d，跳过 %d' % (
                 t.get('succeeded', 0), t.get('failed', 0), t.get('unknown', 0),
                 t.get('obsolete', 0) + t.get('blocked', 0)),
             '旧Creative：确认删除 %d，历史已删 %d，失败 %d，未知 %d' % (
                 d.get('deleted_verified', 0), d.get('previous_deleted', 0),
                 d.get('failed', 0) + d.get('delete_failed', 0), d.get('unknown', 0) + d.get('delete_unknown', 0)),
             '结果按窗口内最后一次回执去重；包含此前批次在本窗口完成的任务。',
             '当前快照：积压 %d，执行中 %d，等待审核 %d，未决未知 %d；引用/读取暂缓 %d' % (
                 ct.get('pending', 0), ct.get('in_progress', 0), ct.get('waiting_review', 0),
                 ct.get('unknown', 0), cd.get('waiting_references', 0) + cd.get('reference_scan_incomplete', 0)),
             '快照时间：' + data['snapshot_at'],
             '执行进程：' + data.get('worker_state', {}).get('ActiveState', 'unavailable'),
             '流程耗时（均值 / P95 / 样本数）：']
    for key, label in (
        ('scan', '① 发现与入队，每批'),
        ('enqueue_to_bind', '② 入队→绑定提交，含排队/审核/准备'),
        ('resource_acceptance', '　其中：新Creative准备→验收，含限流/暂缓'),
        ('bind_verification', '③ 绑定提交→确认，含只读核实'),
        ('cleanup_latency', '④ 清理入队→确认删除，含引用等待'),
        ('replacement_total', '广告入队→替换成功，总历时')):
        metric = data['timings'][key]
        lines.append('%s：%s / %s / %d' % (
            label, duration(metric['mean_seconds']), duration(metric['p95_seconds']), metric['n']))
    lines.append('上表为账本阶段历时，各行样本不同且包含等待/重叠，不可相加。')
    performance = data.get('performance')
    if performance and performance['cycles']:
        lines.append('实际执行样本：%d 个完成周期，成功 %d 条；活跃执行时间吞吐 %.2f 条/小时。' % (
            performance['cycles'], performance['successes'], performance['success_per_hour']))
        lane_labels = {'ads': '广告替换', 'cleanup': '旧Creative清理', 'legacy': '旧串行执行'}
        for lane, metric in performance.get('lanes', {}).items():
            lines.append('%s：%d 个周期，执行 %s，广告成功 %d，确认删除 %d。' % (
                lane_labels[lane], metric['cycles'], duration(metric['seconds']),
                metric['successes'], metric['deleted']))
        if performance.get('throughput_basis') == 'active_interval_union':
            lines.append('广告与清理独立执行；吞吐分母取活跃周期时间并集，并行耗时不重复相加。')
        for key, label in (('source_prepare', '来源准备，每波'),
                           ('reference_scan', '旧Creative引用扫描，每账户'),
                           ('ad_attempt_succeeded', '成功广告工作线程处理，不含排队/来源准备/后续清理')):
            metric = performance['timings'][key]
            lines.append('%s：平均 %s，样本 %d' % (label, duration(metric['mean_seconds']), metric['n']))
        lines.append('按本窗口内完成且有任务/清理结果的周期纳入，周期可能跨窗；成功数与上方事件窗口口径不同，吞吐不含周期间等待。')
    else:
        lines.append('实际执行样本：本窗口暂无完整周期计时，不能据此推算吞吐。')
    if performance and performance['read_errors']:
        lines.append('性能记录读取异常 %d 次，执行样本可能不完整。' % performance['read_errors'])
    lines.append('明确失败不自动重试；未知结果只读核实。')
    return '\n'.join(lines)


def notify_daily(config, audit, at=None, dry_run=False):
    root = Path(config['runtime_dir'])
    hours, lookback_hours, destination = report_settings(config)
    start, end = window(at, hours, lookback_hours)
    # Keep every old daily notice/UUID intact. The periodic namespace cannot
    # collide with daily receipts or another completed window on the same day.
    run_id = ('cover-daily-' + end.strftime('%Y%m%d') if hours == 24 else
              'cover-periodic-%dh-' % hours + end.strftime('%Y%m%dT%H%M'))
    with process_lock(root / 'cover-notify.lock'):
        existing = audit.notice(run_id, 'end')
        if not dry_run and existing and existing['status'] in ('sent', 'sending', 'unknown'):
            try:
                frozen = audit.load_artifact(run_id, 'daily-summary.json')
                delivered_to = (recipient(frozen.get('recipient'))
                                if 'recipient' in frozen or hours == 24 else None)
            except (ValueError, TypeError, AttributeError):
                # Historical daily sends were hard-bound to the personal ID.
                # A periodic receipt without its artifact cannot prove a target.
                delivered_to = recipient() if hours == 24 else None
            return {'run_id': run_id, 'delivery': existing['status'],
                    'recipient': delivered_to, 'requested_recipient': destination,
                    'recipient_matches_requested': delivered_to == destination}
        try:
            frozen = audit.load_artifact(run_id, 'daily-summary.json')
        except ValueError:
            if existing:
                raise  # Never silently replace an already attempted body.
            data = collect(root / 'cover.sqlite3', start, end)
            data['worker_state'] = worker_state()
            data['report_hours'] = hours
            data['report_lookback_hours'] = lookback_hours
            data['performance'] = collect_performance(root, start, end)
            frozen = {'data': data, 'body': report_body(data), 'recipient': destination}
            if not dry_run:
                audit.begin(run_id, 'run', {'policy_version': '20261029-cover-periodic',
                                           'report_hours': hours, 'report_lookback_hours': lookback_hours,
                                           'recipient': destination})
                audit.artifact(run_id, 'daily-summary.json', frozen)
        # Existing daily artifacts predate destination metadata and belong to
        # the original personal recipient. Never reroute a frozen/failed send.
        if recipient(frozen.get('recipient')) != destination:
            raise ValueError('frozen_notification_recipient_changed')
        if dry_run:
            return dict(frozen, run_id=run_id, delivery='dry_run')
        feishu_config = config.get('cover_report_feishu_config', config['feishu_config'])
        receipt = Notifier(audit, feishu_config, destination).send(run_id, 'end', frozen['body'])
        audit.update(run_id, status='completed', window_start=start.isoformat(), window_end=end.isoformat())
        return {'run_id': run_id, 'delivery': 'sent', 'receipt': receipt}
