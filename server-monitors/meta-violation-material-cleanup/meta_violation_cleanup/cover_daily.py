"""One frozen 09:30 Beijing report; reads execution ledgers without mutations."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import subprocess

from .notifications import Notifier
from .runtime import CST, process_lock


def window(at=None):
    at = (at or datetime.now(CST)).astimezone(CST)
    end = at.replace(hour=9, minute=30, second=0, microsecond=0)
    if at < end:
        end -= timedelta(days=1)
    return end - timedelta(days=1), end


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
        failed_scans = db.execute("SELECT count(*) FROM batches WHERE status='failed' AND created_at>=? AND created_at<?", bounds).fetchone()[0]
        return {'window_start': start.isoformat(), 'window_end': end.isoformat(),
                'snapshot_at': datetime.now(CST).isoformat(timespec='seconds'),
                'new_ads': added_ads, 'new_materials': added_materials,
                'scan_completed': len(batches), 'scan_failed': failed_scans,
                'tasks': dict(tasks), 'deletions': dict(deletions),
                'current_tasks': current_tasks, 'current_deletions': current_deletions,
                'timings': {k: summary(v) for k, v in (
                    ('scan', scans), ('resource_acceptance', resources),
                    ('enqueue_to_bind', before_bind), ('bind_verification', binds),
                    ('replacement_total', total), ('cleanup_latency', cleanup))}}
    finally:
        db.close()


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
    lines = ['Meta 违规素材封面替换｜每日汇总',
             '统计窗口：%s 至 %s（北京时间，前含后不含）' % (start, end),
             '范围：DramaWave / HotDrama；每天 09:30 播报一次',
             '新增达标素材 %d；新增关联广告 %d' % (data['new_materials'], data['new_ads']),
             '扫描：完成 %d 批，异常 %d 批' % (data['scan_completed'], data['scan_failed']),
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
    lines.extend(['各行样本不同且包含等待/重叠，不可相加；纯API及前置校验耗时尚无独立埋点。',
                  '明确失败不自动重试；未知结果只读核实。'])
    return '\n'.join(lines)


def notify_daily(config, audit, at=None, dry_run=False):
    root = Path(config['runtime_dir'])
    start, end = window(at)
    run_id = 'cover-daily-' + end.strftime('%Y%m%d')
    with process_lock(root / 'cover-notify.lock'):
        existing = audit.notice(run_id, 'end')
        if not dry_run and existing and existing['status'] in ('sent', 'sending', 'unknown'):
            return {'run_id': run_id, 'delivery': existing['status']}
        try:
            frozen = audit.load_artifact(run_id, 'daily-summary.json')
        except ValueError:
            if existing:
                raise  # Never silently replace an already attempted body.
            data = collect(root / 'cover.sqlite3', start, end)
            data['worker_state'] = worker_state()
            frozen = {'data': data, 'body': report_body(data)}
            if not dry_run:
                audit.begin(run_id, 'run', {'policy_version': '20261029-cover-daily'})
                audit.artifact(run_id, 'daily-summary.json', frozen)
        if dry_run:
            return dict(frozen, run_id=run_id, delivery='dry_run')
        receipt = Notifier(audit, config['feishu_config']).send(run_id, 'end', frozen['body'])
        audit.update(run_id, status='completed', window_start=start.isoformat(), window_end=end.isoformat())
        return {'run_id': run_id, 'delivery': 'sent', 'receipt': receipt}
