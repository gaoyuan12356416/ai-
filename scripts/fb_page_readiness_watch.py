"""Read-only, finite production readiness audit. Never publishes or retries."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess


def assess(tasks, expected_pages, run_ids, now, seconds_per_task=114.24058 / 1.169, source_seconds_by_task=None):
    issues, groups, runs = [], defaultdict(list), defaultdict(list)
    for task in tasks:
        runs[task['run_id']].append(task)
        groups[task['planned_publish_at_utc']].append(task)
        if task['status'] in ('failed', 'failed_without_retry', 'unknown', 'skipped'):
            issues.append({'kind': 'task_terminal_issue', 'task_id': task['id'],
                           'page_id': task['page_id'], 'status': task['status'],
                           'reason': task.get('error_code') or task.get('skip_reason') or ''})
        if task.get('language') != expected_pages.get(task['page_id']):
            issues.append({'kind': 'page_language_mismatch', 'task_id': task['id']})
    for run_id in run_ids:
        rows = runs[run_id]
        if len(rows) != len(expected_pages) or {r['page_id'] for r in rows} != set(expected_pages):
            issues.append({'kind': 'run_page_coverage', 'run_id': run_id, 'task_count': len(rows)})
    pending_seconds = 0
    source_seconds_by_task = source_seconds_by_task or {}
    batches = []
    for planned, rows in sorted(groups.items()):
        counts = Counter(t['status'] for t in rows)
        unprepared = sum(t['status'] in ('planned', 'preparing') for t in rows)
        pending_seconds += sum(seconds_per_task * float(source_seconds_by_task.get(str(t['id']), 261.257874)) / 261.257874
                               for t in rows if t['status'] in ('planned', 'preparing'))
        deadline = datetime.fromisoformat(planned)
        slack = (deadline - now).total_seconds() - pending_seconds
        batches.append({'planned_at_utc': planned, 'tasks': len(rows), 'states': dict(counts),
                        'unprepared': unprepared, 'estimated_prepare_slack_seconds': round(slack)})
        if unprepared and slack < 900:
            issues.append({'kind': 'preparation_deadline_risk', 'planned_at_utc': planned,
                           'unprepared': unprepared, 'estimated_slack_seconds': round(slack)})
        if deadline + timedelta(minutes=30) < now and any(t['status'] in ('planned', 'preparing', 'ready') for t in rows):
            issues.append({'kind': 'publish_claim_overdue', 'planned_at_utc': planned})
        if deadline + timedelta(hours=2) < now and any(t['status'] in ('running', 'submitted') for t in rows):
            issues.append({'kind': 'publication_confirmation_overdue', 'planned_at_utc': planned})
    return {'batches': batches, 'states': dict(Counter(t['status'] for t in tasks)),
            'issues': issues, 'expected_pages': len(expected_pages), 'task_count': len(tasks),
            'estimated_seconds_per_finished_video': round(seconds_per_task, 2)}


def health(port):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
    try:
        connection.request('GET', '/health')
        response = connection.getresponse()
        payload = json.loads(response.read(16384))
        return response.status == 200 and payload.get('ok') is True
    except Exception:
        return False
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--scope', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--run-ids', default='119,120,121,122,123,124')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    run_ids = [int(x) for x in args.run_ids.split(',')]
    assert 1 <= len(run_ids) <= 20
    scope = json.loads(Path(args.scope).read_text())
    scope_pages = scope['pages']
    expected = {p['page_id']: p['language'] for p in scope_pages}
    assert len(scope_pages) == len(expected) == 145 and all(expected.values())
    output = Path(args.output_dir).resolve()
    assert str(output).startswith('/mnt/data-disk/') and os.path.ismount('/mnt/data-disk')
    output.mkdir(parents=True, exist_ok=True)
    report = {'observed_at_utc': now.isoformat(), 'watch_run_ids': run_ids}
    try:
        conn = sqlite3.connect('file:' + str(Path(args.db).resolve()) + '?mode=ro', uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        placeholders = ','.join('?' for _ in run_ids)
        tasks = [dict(r) for r in conn.execute('SELECT t.id,t.run_id,t.page_id,t.status,t.error_code,t.skip_reason,t.planned_publish_at_utc,p.language FROM fb_auto_task t JOIN fb_auto_run_page p ON p.run_id=t.run_id AND p.page_id=t.page_id WHERE t.run_id IN (' + placeholders + ') ORDER BY t.id', run_ids)]
        report.update(assess(tasks, expected, run_ids, now, source_seconds_by_task=scope.get('source_seconds_by_task')))
        # Plan-day coverage includes earlier successful slots for the same Pages.
        coverage = defaultdict(set)
        for row in conn.execute("SELECT page_id,planned_publish_at_utc FROM fb_auto_task WHERE template_id=1 AND status='published' AND planned_publish_at_utc>='2026-09-17T16:00:00+00:00' AND planned_publish_at_utc<'2026-09-19T16:00:00+00:00'"):
            if row['page_id'] in expected:
                day = datetime.fromisoformat(row['planned_publish_at_utc']).astimezone(timezone(timedelta(hours=8))).date().isoformat()
                coverage[day].add(row['page_id'])
        report['published_pages_by_plan_day'] = {day: len(coverage[day]) for day in ('2026-09-18', '2026-09-19')}
        conn.close()
    except Exception as exc:
        report['issues'] = [{'kind': 'audit_unavailable', 'error_type': type(exc).__name__}]
    report['service_health'] = {str(port): health(port) for port in (18835, 18836)}
    report['timers'] = {name: subprocess.run(['systemctl', 'is-active', '--quiet', 'fb-auto-post-' + name + '.timer'], timeout=3).returncode == 0 for name in ('scheduler', 'plan', 'prepare', 'runner', 'reconcile')}
    report['data_disk_free_bytes'] = shutil.disk_usage('/mnt/data-disk').free
    if not all(report['service_health'].values()) or not all(report['timers'].values()):
        report['issues'].append({'kind': 'service_or_timer_unhealthy'})
    if report['data_disk_free_bytes'] < 20 * 1024 ** 3:
        report['issues'].append({'kind': 'cpu_data_disk_low'})
    report['status'] = 'attention' if report['issues'] else 'healthy'
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True)
    temporary = output / 'latest.tmp'
    temporary.write_text(raw, encoding='utf-8')
    temporary.replace(output / 'latest.json')
    events = output / 'observations.jsonl'
    if events.exists() and events.stat().st_size > 4 * 1024 * 1024:
        events.replace(output / 'observations.previous.jsonl')
    with events.open('a', encoding='utf-8') as stream:
        stream.write(raw + '\n')
    print(json.dumps({'status': report['status'], 'issues': report['issues'], 'report': str(output / 'latest.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
