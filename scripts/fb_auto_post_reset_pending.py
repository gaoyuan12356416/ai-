#!/usr/bin/env python3
"""Audited replacement of future, never-submitted FB runs after a Page-pool change.

This only cancels untouched tasks and enqueues replacement automatic slots.
It does not call Graph, rewind history, change template versions or rewrite Pages
on existing tasks. Pause the plan/prepare timers and drain their current calls
before applying; prepare/publish work is left to the normal runtime afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REASON = 'fb_auto_page_pool_reset'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot(conn, run_ids, *, now):
    """Fail closed even if an API attempt failed without returning a Graph ID."""
    if not run_ids or len(run_ids) != len(set(run_ids)):
        raise ValueError('explicit unique run IDs required')
    runs = []
    today = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
    for run_id in sorted(run_ids):
        row = conn.execute('SELECT * FROM fb_auto_run WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('run missing')
        run = dict(row)
        planned = datetime.fromisoformat(run['planned_publish_at_utc'])
        if planned <= now or planned.astimezone(ZoneInfo('Asia/Shanghai')).date() != today or run['trigger_type'] != 'auto':
            raise ValueError('only future automatic runs on the current Beijing date may be reset')
        template = dict(conn.execute('SELECT * FROM fb_auto_template WHERE id=?', (run['template_id'],)).fetchone())
        if template['status'] != 'enabled' or template['current_version'] != run['template_version']:
            raise ValueError('template version/status changed')
        tasks = [dict(x) for x in conn.execute('SELECT * FROM fb_auto_task WHERE run_id=? ORDER BY id', (run_id,))]
        if not tasks or any(t['status'] not in ('planned', 'ready') or t['graph_post_id'] or t['unknown_outcome'] or t['lease_owner'] or t['lease_expires_at_utc'] for t in tasks):
            raise ValueError('run contains an in-flight, terminal or ambiguous task')
        for table in ('fb_auto_publish_ledger', 'fb_auto_publish_attempt'):
            if conn.execute(f'SELECT 1 FROM {table} WHERE task_id IN (SELECT id FROM fb_auto_task WHERE run_id=?) LIMIT 1', (run_id,)).fetchone():
                raise ValueError('run already has publication evidence')
        due = conn.execute('SELECT * FROM fb_auto_due_slot WHERE run_id=?', (run_id,)).fetchall()
        if len(due) != 1 or due[0]['status'] != 'prepared':
            raise ValueError('original prepared due slot missing')
        pages = [dict(x) for x in conn.execute('SELECT * FROM fb_auto_run_page WHERE run_id=? ORDER BY page_id', (run_id,))]
        runs.append({'run': run, 'template': template, 'tasks': tasks, 'pages': pages, 'due': dict(due[0])})
    return runs


def reset_pending(store, expected, operation_id):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{8,48}', operation_id):
        raise ValueError('invalid operation ID')
    run_ids = [x['run']['id'] for x in expected]
    now = store.now_fn()
    stamp = now.isoformat(timespec='seconds')
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        # Idempotency is tied to exact original slot identities and this operation.
        keys = [x['run']['slot_key'] + ':reset:' + operation_id for x in expected]
        existing = [conn.execute('SELECT * FROM fb_auto_due_slot WHERE template_id=? AND slot_key=?', (x['run']['template_id'], key)).fetchone() for x, key in zip(expected, keys)]
        if any(existing):
            if not all(existing):
                raise ValueError('partial replacement identity conflict')
            for item in expected:
                tasks = conn.execute('SELECT id,status,skip_reason FROM fb_auto_task WHERE run_id=? ORDER BY id', (item['run']['id'],)).fetchall()
                if [t['id'] for t in tasks] != [t['id'] for t in item['tasks']] or any(t['status'] != 'skipped' or t['skip_reason'] != REASON for t in tasks):
                    raise ValueError('replacement history changed')
            return {'ok': True, 'idempotent': True, 'due_ids': [x['id'] for x in existing]}
        current = snapshot(conn, run_ids, now=now)
        if fingerprint(current) != fingerprint(expected):
            raise ValueError('preview changed; inspect again before resetting')
        due_ids = []
        for item, key in zip(current, keys):
            run = item['run']
            conn.execute("UPDATE fb_auto_task SET status='skipped',skip_reason=?,error_code=?,error_message=?,completed_at_utc=? WHERE run_id=?", (REASON, REASON, '主页池已更换，用户要求取消旧主页任务；替代批次按原时间发布到新主页', stamp, run['id']))
            store._refresh_run(conn, run['id'], stamp)
            conn.execute('UPDATE fb_auto_run SET skipped_tasks=?,queued_tasks=0 WHERE id=?', (len(item['tasks']), run['id']))
            cur = conn.execute("INSERT INTO fb_auto_due_slot(template_id,template_version,slot_key,planned_publish_at_utc,status,trigger_type,created_at_utc,updated_at_utc,available_at_utc) VALUES(?,?,?,?,'pending','auto',?,?,?)", (run['template_id'], run['template_version'], key, run['planned_publish_at_utc'], stamp, stamp, stamp))
            due_ids.append(cur.lastrowid)
        return {'ok': True, 'idempotent': False, 'due_ids': due_ids, 'cancelled_tasks': sum(len(x['tasks']) for x in current)}


class ExactPages:
    """Keep all normal live Page and legacy checks; refuse membership drift."""
    def __init__(self, delegate, groups, expected_ids):
        self.delegate, self.groups, self.ids = delegate, set(groups), set(expected_ids)

    def list_pages(self, groups, **kwargs):
        rows = self.delegate.list_pages(groups, **kwargs)
        if set(groups) == self.groups:
            if {p.page_id for p in rows} != self.ids or any(p.eligible_token_count <= 0 for p in rows):
                raise ValueError('current Page pool no longer matches approved replacement scope')
        return rows

    def __getattr__(self, name):
        return getattr(self.delegate, name)


def plan_replacements(runtime, due_ids, page_ids):
    from datetime import timedelta
    from features.fb_auto_posts.core import ActorScope
    results = []
    for due_id in due_ids:
        now = runtime.store.now_fn()
        owner = 'page-pool-reset-' + str(due_id)
        lease = (now + timedelta(minutes=30)).isoformat(timespec='seconds')
        with runtime.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            due = dict(conn.execute('SELECT * FROM fb_auto_due_slot WHERE id=?', (due_id,)).fetchone())
            if due['status'] == 'prepared':
                results.append({'run_id': due['run_id'], 'idempotent': True})
                continue
            if due['status'] != 'pending':
                raise ValueError('replacement slot is already being processed')
            conn.execute("UPDATE fb_auto_due_slot SET status='preparing',lease_owner=?,lease_expires_at_utc=?,updated_at_utc=? WHERE id=?", (owner, lease, now.isoformat(timespec='seconds'), due_id))
            template = dict(conn.execute('SELECT * FROM fb_auto_template WHERE id=?', (due['template_id'],)).fetchone())
            cfg = json.loads(conn.execute('SELECT config_json FROM fb_auto_template_version WHERE template_id=? AND version=?', (due['template_id'], due['template_version'])).fetchone()[0])
        actor = ActorScope('operator-page-reset', '用户授权更换主页', bool(template['scope_is_admin']), template['owner_user_id'])
        pages = ExactPages(runtime.pages, cfg['group_ids'], page_ids)
        try:
            result = runtime.store.create_run(due['template_id'], due['slot_key'], 'auto', actor, pages, runtime.materials, planned_publish_at_utc=due['planned_publish_at_utc'], expected_template_version=due['template_version'], expected_due_id=due_id, expected_due_lease_owner=owner, expected_due_lease_expires_at_utc=lease, max_publishable_pages=runtime.max_publishable_pages, max_jobs_per_slot=runtime.max_jobs_per_slot, max_daily_jobs=runtime.max_daily_jobs)
            runtime.store.complete_due_slot(due_id, run_id=result['run_id'], expected_lease_owner=owner, expected_lease_expires_at_utc=lease)
            results.append(result)
        except Exception:
            runtime.store.defer_due_slot(due_id, 'fb_auto_reset_planning_failed', expected_lease_owner=owner, expected_lease_expires_at_utc=lease)
            raise
    with runtime.store.connect() as conn:
        for result in results:
            rows = conn.execute('SELECT page_id,status FROM fb_auto_task WHERE run_id=?', (result['run_id'],)).fetchall()
            if {x['page_id'] for x in rows} != set(page_ids) or any(x['status'] != 'planned' for x in rows):
                raise ValueError('replacement plan must be verified before resuming prepare timer')
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', required=True)
    p.add_argument('--run-id', type=int, action='append', required=True)
    p.add_argument('--operation-id', required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--apply-fingerprint')
    args = p.parse_args()
    if not args.apply_fingerprint:
        with sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro', uri=True) as c:
            c.row_factory = sqlite3.Row
            data = snapshot(c, args.run_id, now=datetime.now(timezone.utc))
        payload = {'operation_id': args.operation_id, 'snapshot': data, 'fingerprint': fingerprint(data)}
        with args.manifest.open('x', encoding='utf-8') as f: json.dump(payload, f, ensure_ascii=False, indent=2)
        print(json.dumps({'fingerprint': payload['fingerprint'], 'run_ids': args.run_id, 'tasks': sum(len(x['tasks']) for x in data)}))
        return
    payload = json.loads(args.manifest.read_text(encoding='utf-8'))
    if payload['operation_id'] != args.operation_id or payload['fingerprint'] != args.apply_fingerprint or fingerprint(payload['snapshot']) != args.apply_fingerprint or sorted(args.run_id) != sorted(x['run']['id'] for x in payload['snapshot']):
        raise ValueError('manifest/fingerprint/scope mismatch')
    backup = args.manifest.with_suffix('.before.sqlite3')
    if not backup.exists():
        with sqlite3.connect(args.db) as src, sqlite3.connect(backup) as dest: src.backup(dest)
    from features.fb_auto_posts.core import FBAutoPostStore
    result = reset_pending(FBAutoPostStore(args.db), payload['snapshot'], args.operation_id)
    args.manifest.with_suffix('.result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
