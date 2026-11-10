"""Sanitized administrator view of the independent service health snapshot."""
import datetime as dt
import json
from pathlib import Path

SNAPSHOT = '/mnt/data-disk/publish-scheduler-health/latest-fb.json'
LABELS = {'fb-auto-post-service.service': 'Facebook 主服务',
          'fb-video-publish-worker.service': '短链生成与回执核对',
          'fb-video-pool-scheduler.service': 'Page 素材池调度',
          'fb-group-pool-scheduler.service': '分组素材池调度',
          'fb-auto-post-scheduler.timer': '发布定时器',
          'fb-auto-post-plan.timer': '计划定时器',
          'fb-auto-post-reconcile.timer': '回执核对定时器'}
COUNTS = ('due_pending', 'overdue_pending', 'future_scheduled', 'platform_processing',
          'outcome_unknown', 'executing', 'due_ready', 'preparing', 'schedule_unknown', 'held',
          'oldest_pending_seconds', 'threshold_seconds')


def read_snapshot(now, actor, channels, path=SNAPSHOT):
    # The snapshot has global counts. Never expose it to ordinary tenant users.
    if not actor.get('is_admin') or 'fb' not in channels:
        return None
    unavailable = {'available': False, 'ok': False, 'code': 'snapshot_unavailable',
                   'alerts': ['Facebook 辅助服务监测数据暂不可用，当前状态未知。']}
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        stamp = dt.datetime.fromisoformat(value['at_utc'])
        age = (now - stamp).total_seconds()
        if not 0 <= age <= 150:
            return dict(unavailable, code='snapshot_stale', collected_at=value['at_utc'],
                        alerts=['Facebook 辅助服务监测超过 150 秒未更新，当前状态未知。'])
        checks = {r['unit']: r for r in value['checks'] if r['lane'] == 'fb'}
        services = [{'name': name, 'ok': checks.get(unit, {}).get('ok') is True}
                    for unit, name in LABELS.items()]
        progress = value['fb_short_links']
        counters = {k: progress.get(k) if progress.get('available') else None for k in COUNTS}
        alerts = [r['name'] + '未正常运行，请检查服务状态。' for r in services if not r['ok']]
        # Include failed oneshots as well as daemons without leaking raw state.
        if any(not r.get('ok') and r['unit'] not in LABELS for r in checks.values()):
            alerts.append('Facebook 定时任务最近运行异常，请检查调度日志。')
        if not progress.get('available'):
            alerts.append('短链阶段数据读取失败，任务数量未知。')
        elif progress.get('overdue_pending', 0):
            alerts.append('已到期且素材就绪的任务持续等待短链超过 5 分钟。')
        return {'available': True, 'ok': not alerts, 'code': 'ok' if not alerts else 'attention',
                'collected_at': value['at_utc'], 'services': services, 'progress': counters, 'alerts': alerts}
    except (OSError, ValueError, TypeError, KeyError):
        return unavailable
