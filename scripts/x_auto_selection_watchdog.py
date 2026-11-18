#!/usr/bin/env python3
"""Independent, bounded recovery of ended selection requests, including drains."""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@contextlib.contextmanager
def readonly(path):
    with contextlib.closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=2)) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        yield c


def external_proof(task_id, posts, dispatch):
    if posts.execute('SELECT 1 FROM x_post_manual_run WHERE external_task_key=? LIMIT 1',
                     ('x-auto-task-' + str(task_id),)).fetchone():
        return 'native_execution_exists'
    jobs = dispatch.execute('SELECT * FROM pipeline_job WHERE source_key=?',
                            ('x:auto:' + str(task_id),)).fetchall()
    for row in jobs:
        if any(row[k] for k in ('generation', 'claim_hash', 'external_intent', 'canonical_receipt')):
            return 'dispatch_execution_exists'
        if row['stage'] != 'prepare' or row['status'] not in ('queued', 'ready', 'legacy_running'):
            return 'dispatch_state_requires_review'
        for table in ('pipeline_operation', 'pipeline_receipt', 'pipeline_claim_history'):
            if dispatch.execute('SELECT 1 FROM ' + table + ' WHERE job_id=? LIMIT 1',
                                (row['id'],)).fetchone():
                return 'dispatch_history_exists'
    return ''


def recover(args):
    from features.x_auto_posts.selection_recovery import file_lock, pure_selection
    if args.apply:
        pid = int(subprocess.check_output(
            ['systemctl', 'show', 'x-tag-auto.service', '-p', 'MainPID', '--value'], text=True).strip())
        if pid:
            environment = Path('/proc/' + str(pid) + '/environ').read_bytes().split(b'\0')
            if ('X_AUTO_EXECUTION_LOCK_DIR=' + args.locks).encode() not in environment:
                raise RuntimeError('executor_lifetime_guard_not_enabled')
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(seconds=60)).isoformat(timespec='seconds')
    results = []
    # Exclusive admission prevents a new claim between proof and native CAS.
    # It does not wait for, stop, or take over existing request locks.
    with file_lock(args.locks, 'admission.lock', blocking=False) as admission:
        if not admission:
            return [{'reason': 'claim_admission_busy'}]
        with readonly(args.native) as native, readonly(args.posts) as posts, readonly(args.dispatch) as dispatch:
            rows = native.execute("SELECT * FROM x_auto_task WHERE status='selecting' "
                                  "AND updated_at<=? ORDER BY updated_at,id LIMIT 32", (cutoff,)).fetchall()
            for row in rows:
                item = {'task_id': row['id']}
                results.append(item)
                with file_lock(args.locks, str(row['id']) + '.lock', blocking=False) as execution:
                    if not execution:
                        item['reason'] = 'execution_still_active'
                        continue
                    if not pure_selection(row) or native.execute(
                            'SELECT 1 FROM x_auto_material_ledger WHERE task_id=?', (row['id'],)).fetchone():
                        item['reason'] = 'reservation_or_attempt_exists'
                        continue
                    reason = external_proof(row['id'], posts, dispatch)
                    if reason:
                        item['reason'] = reason
                        continue
                    item['reason'] = 'ended_selection_without_external_effects'
                    if not args.apply:
                        item['recoverable'] = True
                        continue
                    proof = {'task_id': row['id'], 'expected_updated_at': row['updated_at'],
                             'proof': {'checked_at': now.isoformat(), 'executor_ended': True,
                                       'material_ledger_empty': True, 'external_execution_empty': True,
                                       'previous_error_code': row['error_code'],
                                       'method': 'exclusive_admission_and_request_lock'}}
                    result = subprocess.run(
                        ['runuser', '-u', 'x-post-daily', '--', sys.executable, '-I', __file__,
                         '--apply-proof', '--runtime', args.runtime, '--native', args.native],
                        input=json.dumps(proof), text=True, capture_output=True, timeout=40, check=True)
                    item['recovered'] = json.loads(result.stdout)['recovered']
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--apply-proof', action='store_true')
    parser.add_argument('--runtime', default='/opt/x-tag-publisher/current')
    parser.add_argument('--native', default='/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')
    parser.add_argument('--posts', default='/mnt/data-disk/x-tag-publisher/posts.sqlite3')
    parser.add_argument('--dispatch', default='/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')
    parser.add_argument('--locks', default='/mnt/data-disk/x-tag-publisher/auto/execution-locks')
    parser.add_argument('--diagnostics', default='/run/pipeline-service-drain/diagnostics.json')
    args = parser.parse_args()
    if args.apply_proof:
        sys.path.insert(0, args.runtime)
        from features.x_auto_posts.core import XAutoPostStore
        from features.x_auto_posts.selection_recovery import recover_proven_selection
        proof = json.load(sys.stdin)
        changed = recover_proven_selection(XAutoPostStore(args.native), **proof)
        print(json.dumps({'recovered': changed}))
        return
    from features.pipeline_dispatch.drain_diagnostics import collect, save
    results = []
    error = None
    try:
        results = recover(args)
    except Exception as exc:
        # A failed check is not evidence for recovery. Still refresh diagnostics.
        error = type(exc).__name__
    snapshot = collect(args.dispatch, args.native, args.locks)
    snapshot['selection_recovery'] = {'checked_at': snapshot['checked_at'], 'items': results, 'error': error}
    save(args.diagnostics, snapshot)
    print(json.dumps({'checked_at': snapshot['checked_at'], 'recovery': results,
                      'error': error, 'drains': [{'unit': h['unit'], 'pending_count': h['pending_count'],
                      'elapsed_seconds': h['elapsed_seconds']} for h in snapshot['holds']]}), flush=True)
    if error:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
