"""Read-only audit of the exact operator-authorized recovery scope."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

ROOT = Path('/mnt/data-disk/x-reuse-read-context-20261123')


def read(path):
    db = sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True, timeout=8)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def audit():
    scope = json.loads((ROOT/'recovery-scope.json').read_text())['tasks']
    frozen = {row['id']: row for row in scope}
    marks = ','.join('?' for _ in frozen)
    with closing(read('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')) as db:
        tasks = [dict(row) for row in db.execute('SELECT id,status,error_code,error_message,account_id,material_id,execution_run_id,execution_queue_id,publish_id,unknown_outcome,publish_attempt_count,preparation_attempt_count,updated_at FROM x_auto_task WHERE id IN ('+marks+')', list(frozen))]
    keys = {'x-auto-task-'+str(tid): tid for tid in frozen}
    with closing(read('/mnt/data-disk/x-tag-publisher/posts.sqlite3')) as db:
        runs = [dict(row) for row in db.execute('SELECT id,external_task_key,status FROM x_post_manual_run WHERE external_task_key IN ('+marks+')', list(keys))]
        queues = []
        for run in runs:
            for row in db.execute('''SELECT q.id,q.account_id,q.material_key,q.status,l.id log_id,l.status log_status,
                    l.x_post_id,l.x_post_url,l.unknown_outcome FROM x_post_queue q
                    LEFT JOIN x_post_publish_log l ON l.queue_id=q.id WHERE q.manual_run_id=?''', (run['id'],)):
                queues.append(dict(row, task_id=keys[run['external_task_key']], execution_run_id=run['id']))
    errors = []
    for row in tasks:
        before = frozen[row['id']]
        if row['account_id'] != before['account_id'] or (row['material_id'] and row['material_id'] != before['material_id']):
            errors.append(dict(task_id=row['id'], error='task_identity_changed'))
    for row in queues:
        before = frozen[row['task_id']]
        if str(row['account_id']) != before['account_id'] or str(row['material_key']) != before['material_id']:
            errors.append(dict(task_id=row['task_id'], error='queue_identity_changed'))
    # Detect a second queue for the same account/material outside these keys,
    # including the other publishing lane. Cross-account material reuse is valid.
    expected = {(str(t['account_id']), str(t['material_id'])): t['id'] for t in scope}
    material_ids = sorted({key[1] for key in expected})
    for lane, path in [('tag', '/mnt/data-disk/x-tag-publisher/posts.sqlite3'),
                       ('ordinary', '/var/lib/x-post-automation/accounts.sqlite3')]:
        with closing(read(path)) as db:
            for row in db.execute('''SELECT q.id,q.account_id,q.material_key,m.external_task_key
                    FROM x_post_queue q LEFT JOIN x_post_manual_run m ON m.id=q.manual_run_id
                    WHERE q.material_key IN ('''+','.join('?' for _ in material_ids)+')', material_ids):
                tid = expected.get((str(row['account_id']), str(row['material_key'])))
                if tid and (lane != 'tag' or row['external_task_key'] != 'x-auto-task-'+str(tid)):
                    errors.append(dict(task_id=tid, lane=lane, queue_id=row['id'], error='other_account_material_queue'))
    for label, identities in [('execution_key', [r['external_task_key'] for r in runs]),
                              ('task_queue', [q['task_id'] for q in queues]),
                              ('post_id', [q['x_post_id'] for q in queues if q['x_post_id']])]:
        errors.extend(dict(error='duplicate_'+label, identity=key, count=count) for key, count in Counter(identities).items() if count>1)
    confirmed = [q for q in queues if q['log_status']=='published' and q['x_post_id'] and not q['unknown_outcome']]
    unknown = {t['id'] for t in tasks if t['unknown_outcome']} | {q['task_id'] for q in queues if q['unknown_outcome']}
    creating = {q['task_id'] for q in queues if q['log_status']=='post_creating' and not q['unknown_outcome']}
    return dict(at=datetime.now(timezone.utc).isoformat(), total=len(tasks), original_scope=len(frozen),
                task_status=dict(Counter(t['status'] for t in tasks)),
                task_errors=dict(Counter(t['error_code'] for t in tasks if t['error_code'])),
                canonical_runs=len(runs), canonical_queues=len(queues),
                linked_tasks=sum(bool(t['execution_run_id'] and t['execution_queue_id']) for t in tasks),
                queue_status=dict(Counter(q['status'] for q in queues)), confirmed_posts=len(confirmed),
                unknown_tasks=sorted(unknown), post_creation_inflight=sorted(creating), duplicate_or_identity_errors=errors,
                tasks=tasks, queues=queues)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()
    report = audit()
    if args.save:
        (ROOT/'recovery-latest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ('tasks','queues')}, ensure_ascii=False))
