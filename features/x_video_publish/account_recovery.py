"""Cancel only settled, unpublished personal Excel work on explicitly locked X accounts.

No tokens, platform writes, account reauthorization or history deletion. A new
healthy target keeps the ordinary account/material duplicate and ownership guards.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from features.x_posts.account_blockers import read_account_publish_blockers

CODE = 'x_video_account_unavailable_cancelled'
MESSAGE = '原账号被X锁定或暂停，未发任务已撤销；可选择新的正常账号重新上传Excel'
TERMINAL = {'completed', 'completed_with_errors', 'failed_preflight', 'stopped'}


def connect(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def locked_accounts(conn, ids):
    """Use platform evidence only, never disabled switches, token age or cooldown."""
    blockers = read_account_publish_blockers(conn, ids)
    locked = {aid: dict(value) for aid, value in blockers.items()
              if value['code'] == 'x_post_account_locked'}
    if ids:
        # /users/me verifies a suspended account with this exact safe message.
        for row in conn.execute("SELECT id,last_error FROM x_authorized_account WHERE id IN (%s)"
                                % ','.join('?' for _ in ids), ids):
            if str(row['last_error'] or '') == 'X账号已被平台暂停，请登录X处理申诉后重新校验':
                locked.setdefault(row['id'], {'code': 'x_account_suspended', 'queue_id': 0, 'log_id': 0})
    return locked


def queue_safe(conn, run_id):
    rows = conn.execute('''SELECT q.id,q.status,l.status AS log_status,
      l.unknown_outcome,l.x_post_id,r.status AS relay_status,r.unknown_outcome AS relay_unknown,
      r.source_post_id FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
      LEFT JOIN x_post_repost_ledger r ON r.queue_id=q.id WHERE q.manual_run_id=?''', (run_id,)).fetchall()
    # Never interrupt an upload, reservation, platform write or ambiguous result.
    return all(row['status'] in {'queued', 'failed', 'cancelled'}
               and row['log_status'] in {None, 'failed'}
               and not row['unknown_outcome'] and not row['x_post_id']
               and row['relay_status'] in {None, 'failed'}
               and not row['relay_unknown'] and not row['source_post_id'] for row in rows)


def release_settled(conn, batch_id, mid, timestamp):
    rows = conn.execute('''SELECT t.manual_run_id,r.status,r.unknown_count,
      q.id AS queue_id,q.status AS queue_status,l.status AS log_status,l.unknown_outcome,
      z.status AS relay_status,z.unknown_outcome AS relay_unknown
      FROM x_video_publish_task t JOIN x_post_manual_run r ON r.id=t.manual_run_id
      LEFT JOIN x_post_queue q ON q.manual_run_id=r.id
      LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
      LEFT JOIN x_post_repost_ledger z ON z.queue_id=q.id
      WHERE t.batch_id=? AND t.material_id=?''', (batch_id, mid)).fetchall()
    if any(row['unknown_count'] or row['unknown_outcome'] or row['relay_unknown']
           or row['log_status'] in {'reserved', 'media_uploading', 'post_creating', 'repost_creating'}
           or row['relay_status'] in {'source_publishing', 'reposting', 'needs_review'}
           or (row['status'] not in TERMINAL and row['queue_status'] not in {'published', 'failed', 'cancelled'})
           for row in rows):
        return 0
    return conn.execute('''UPDATE x_post_manual_material_reservation
      SET state=?,release_reason=?,updated_at=? WHERE state='active' AND material_key=?
      AND manual_run_id IN (SELECT manual_run_id FROM x_video_publish_task WHERE batch_id=? AND material_id=?)''',
      ('consumed' if any(row['queue_id'] for row in rows) else 'released', CODE, timestamp, mid, batch_id, mid)).rowcount


def reconcile(conn, *, apply=False, batch_id=None, now=None):
    """Caller holds BEGIN IMMEDIATE, serializing with queue admission and log writes."""
    if not conn.in_transaction:
        raise RuntimeError('recovery_requires_transaction')
    timestamp = now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    where, args = (' AND t.batch_id=?', (int(batch_id),)) if batch_id is not None else ('', ())
    rows = conn.execute('''SELECT t.id,t.manual_run_id,t.batch_id,t.account_id,t.material_id,
      r.status,r.unknown_count FROM x_video_publish_task t
      JOIN x_video_publish_batch b ON b.id=t.batch_id
      JOIN x_post_manual_run r ON r.id=t.manual_run_id
      WHERE b.source='excel' AND r.status IN ('queued','running')''' + where + ' ORDER BY t.id', args).fetchall()
    ids = sorted({row['account_id'] for row in rows})
    locks = locked_accounts(conn, ids)
    result = dict(candidate_tasks=len(rows), locked_accounts=sorted(locks), cancelled_tasks=0,
                  cancelled_queues=0, released_reservations=0, retained_inflight_or_unknown=0, task_ids=[])
    selected = []
    for row in rows:
        if row['account_id'] not in locks:
            continue
        if row['unknown_count'] or not queue_safe(conn, row['manual_run_id']):
            result['retained_inflight_or_unknown'] += 1
            continue
        selected.append(row)
    result['task_ids'] = [row['id'] for row in selected]
    if not apply:
        result['would_cancel_tasks'] = len(selected)
        return result
    conn.execute('''CREATE TABLE IF NOT EXISTS x_video_account_cancellation_audit(
      manual_run_id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL,batch_id INTEGER NOT NULL,
      account_id INTEGER NOT NULL,material_id TEXT NOT NULL,previous_run_status TEXT NOT NULL,
      evidence_code TEXT NOT NULL,evidence_queue_id INTEGER NOT NULL,evidence_log_id INTEGER NOT NULL,
      cancelled_queue_ids_json TEXT NOT NULL,created_at TEXT NOT NULL)''')
    for row in selected:
        run_id = row['manual_run_id']; evidence = locks[row['account_id']]
        queues = [r[0] for r in conn.execute("SELECT id FROM x_post_queue WHERE manual_run_id=? AND status='queued'", (run_id,))]
        conn.execute('INSERT INTO x_video_account_cancellation_audit VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     (run_id,row['id'],row['batch_id'],row['account_id'],row['material_id'],row['status'],
                      evidence['code'],evidence['queue_id'],evidence['log_id'],json.dumps(queues),timestamp))
        result['cancelled_queues'] += conn.execute("UPDATE x_post_queue SET status='cancelled',updated_at=? WHERE manual_run_id=? AND status='queued'", (timestamp,run_id)).rowcount
        result['cancelled_tasks'] += conn.execute('''UPDATE x_post_manual_run SET status='stopped',error_code=?,
          error_message=?,finished_at=?,updated_at=? WHERE id=? AND status IN ('queued','running')''',
          (CODE,MESSAGE,timestamp,timestamp,run_id)).rowcount
    # Revisit audited siblings: healthy pending siblings can settle after cancellation.
    for row in conn.execute('SELECT DISTINCT batch_id,material_id FROM x_video_account_cancellation_audit'):
        result['released_reservations'] += release_settled(conn,row['batch_id'],row['material_id'],timestamp)
    return result


def sweep(path, *, apply=False, batch_id=None):
    with contextlib.closing(connect(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            result = reconcile(conn, apply=apply, batch_id=batch_id)
            conn.commit() if apply else conn.rollback()
            return result
        except Exception:
            conn.rollback()
            raise
