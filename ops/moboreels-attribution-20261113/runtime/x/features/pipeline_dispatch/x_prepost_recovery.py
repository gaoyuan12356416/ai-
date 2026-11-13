"""Exact operator authorization for a known failure before any Post intent.

This module does no network I/O. A recovery never erases an attempt or permits
another generation implicitly; the operator utility records one next generation.
"""
import hashlib
import json
import re


TABLE = 'pipeline_x_prepost_recovery'
SCHEMA = '''CREATE TABLE IF NOT EXISTS pipeline_x_prepost_recovery (
 source_key TEXT NOT NULL,stage TEXT NOT NULL,from_generation INTEGER NOT NULL,
 to_generation INTEGER NOT NULL,prior_attempts INTEGER NOT NULL,
 queue_sha256 TEXT NOT NULL,log_sha256 TEXT NOT NULL,artifact_sha256 TEXT NOT NULL,
 plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,actor TEXT NOT NULL,
 created_at TEXT NOT NULL,started_at TEXT NOT NULL DEFAULT '',proof_json TEXT NOT NULL,
 PRIMARY KEY(source_key,stage,from_generation))'''


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def queue_hash(queue):
    return digest({k: v for k, v in dict(queue).items() if k not in ('status', 'updated_at')})


def log_hash(log):
    return digest({k: dict(log).get(k) for k in ('id', 'queue_id', 'account_id',
                   'long_url', 'short_url', 'post_text', 'created_at')})


def no_post_operation(name):
    # A FINALIZE is deliberately excluded as well. Recovery can be widened
    # only after a separate explicit assessment, never by treating unknown as false.
    return bool(re.fullmatch(r'x:POST:/2/media/upload(?:/initialize|/[A-Za-z0-9_.:-]+/append)', str(name)))


def authorization(conn, queue, log, job=None):
    """Return only a valid *unconsumed* authorization; absent schema is closed."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    # Public store DTOs contain derived fields. Hash the frozen database row,
    # identically to the operator's audited snapshot.
    raw = conn.execute('SELECT * FROM x_post_queue WHERE id=?', (queue['id'],)).fetchone()
    if not raw or not log:
        return None
    q, l = dict(raw), dict(log)
    key = 'x:queue:' + str(q['id'])
    row = conn.execute("SELECT * FROM " + TABLE + " WHERE source_key=? AND stage='publish' AND state='armed' ORDER BY from_generation DESC LIMIT 1", (key,)).fetchone()
    if not row:
        return None
    r = dict(row)
    if (q['status'] != 'queued' or l['status'] != 'reserved' or l['unknown_outcome']
            or l.get('x_post_id') or l.get('published_at') or l.get('x_media_id')
            or l['attempt_count'] != r['prior_attempts'] or r['prior_attempts'] < 0
            or r['to_generation'] != r['from_generation'] + 1
            or queue_hash(q) != r['queue_sha256'] or log_hash(l) != r['log_sha256']
            or q.get('delivery_mode', 'direct') != 'direct'):
        return None
    if job and (job.get('stage') != 'publish' or job.get('source_key') != key
                or job.get('generation') != r['to_generation']):
        return None
    ops = conn.execute('SELECT operation,generation FROM pipeline_x_operation WHERE source_key=?', (key,)).fetchall()
    if any(not no_post_operation(o['operation']) or o['generation'] > r['from_generation'] for o in ops):
        return None
    if conn.execute("SELECT 1 FROM pipeline_x_checkpoint WHERE source_key=? AND phase NOT IN ('x_preflight','x_media_initialized')", (key,)).fetchone():
        return None
    return r


def mark_retry_uploading(bridge, job, log_id):
    """Consume one explicit permission and increase the original attempt count."""
    from .adapters import AdapterError
    from features.x_posts import service as posts
    with bridge.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        log = conn.execute('SELECT * FROM x_post_publish_log WHERE id=?', (log_id,)).fetchone()
        queue = conn.execute('SELECT * FROM x_post_queue WHERE id=?', (log['queue_id'],)).fetchone()
        auth = authorization(conn, queue, log, job)
        if not auth:
            raise AdapterError('x_recovery_authorization_changed')
        # Native ownership, drama affinity and all unrelated account fences remain.
        bridge.store._assert_drama_queue_assignment(conn, queue)
        bridge.store._assert_account_publish_fence(conn, queue)
        if not all(log[k] for k in ('long_url', 'short_url', 'post_text')):
            raise AdapterError('x_post_log_not_prepared')
        now = posts.utc_now()
        changed = conn.execute("UPDATE " + TABLE + " SET state='consumed',started_at=? WHERE source_key=? AND stage='publish' AND from_generation=? AND state='armed'", (now, job['source_key'], auth['from_generation'])).rowcount
        if changed != 1:
            raise AdapterError('x_recovery_authorization_changed')
        conn.execute("UPDATE x_post_publish_log SET status='media_uploading',attempt_count=attempt_count+1,started_at=?,error_code='',error_message='',updated_at=? WHERE id=?", (now, now, log_id))
        conn.execute("UPDATE x_post_queue SET status='publishing',updated_at=? WHERE id=?", (now, queue['id']))
        bridge.store._sync_run(conn, queue['id'], now)
