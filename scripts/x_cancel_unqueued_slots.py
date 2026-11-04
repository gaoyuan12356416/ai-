"""Cancel a frozen set of unmaterialized X schedule targets; never publish.

Preview first, then apply the SHA-bound manifest. Existing queues, logs, pool
reservations, account credentials and future schedules are not changed.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3


TABLE = 'x_post_unqueued_cancellation'
BATCH = 'x_post_unqueued_cancellation_batch'
PARENT_TABLES = ('x_post_schedule_codefix_compensation_audit',
                 'x_post_schedule_drama_scope_compensation_audit')
EDGES = ' UNION '.join('SELECT original_schedule_run_id parent, compensation_schedule_run_id child FROM ' + t for t in PARENT_TABLES)
EDGES += " UNION SELECT CAST(identity AS INTEGER), CAST(json_extract(evidence_json,'$.child_run_id') AS INTEGER) FROM x_post_operator_gap_recovery_audit WHERE json_valid(evidence_json) AND json_extract(evidence_json,'$.child_run_id') IS NOT NULL"


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def sha(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def connect(path, readonly=False):
    c = sqlite3.connect(Path(path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=20)
    c.row_factory = sqlite3.Row
    if readonly:
        c.execute('PRAGMA query_only=ON')
    return c


def descendants(c, run_id):
    return [r[0] for r in c.execute('WITH RECURSIVE edges(parent,child) AS (' + EDGES + '), ids(id) AS (SELECT ? UNION SELECT e.child FROM edges e JOIN ids i ON e.parent=i.id) SELECT id FROM ids', (run_id,))]


def queues(c, run_id):
    ids = descendants(c, run_id)
    return [dict(r) for r in c.execute('SELECT id,account_id,schedule_run_id FROM x_post_queue WHERE schedule_run_id IN (' + ','.join('?' for _ in ids) + ') ORDER BY id', ids)]


def identity(row):
    return {k: row[k] for k in ('id', 'source_type', 'run_date', 'publish_time', 'account_ids_json', 'config_version', 'created_at')}


def preview(c, run_ids):
    runs, targets = [], []
    for run_id in run_ids:
        row = c.execute('SELECT * FROM x_post_schedule_run WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('run_missing')
        existing = queues(c, run_id)
        occupied = {r['account_id'] for r in existing}
        account_ids = json.loads(row['account_ids_json'])
        if len(account_ids) != len(set(account_ids)):
            raise ValueError('duplicate_account_scope')
        missing = [a for a in account_ids if a not in occupied]
        runs.append({'identity': identity(row), 'existing_queues': existing, 'cancel_account_ids': missing})
        targets.extend({'run_id': run_id, 'account_id': a} for a in missing)
    return {'version': 1, 'runs': runs, 'targets': targets, 'count': len(targets)}


def install_schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS ' + BATCH + ' (manifest_sha256 TEXT PRIMARY KEY, actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, backup_path TEXT NOT NULL, previous_runs_json TEXT NOT NULL, count INTEGER NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS ' + TABLE + ' (run_id INTEGER NOT NULL, account_id INTEGER NOT NULL, manifest_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,account_id))')
    ancestors = 'WITH RECURSIVE edges(parent,child) AS (' + EDGES + '), ancestors(id) AS (SELECT NEW.schedule_run_id UNION SELECT e.parent FROM edges e JOIN ancestors a ON e.child=a.id) SELECT 1 FROM ' + TABLE + ' c JOIN ancestors a ON a.id=c.run_id WHERE c.account_id=NEW.account_id'
    for suffix, action in (('insert', 'INSERT'), ('update', 'UPDATE OF schedule_run_id,account_id')):
        c.execute('CREATE TRIGGER IF NOT EXISTS x_unqueued_cancel_queue_' + suffix + ' BEFORE ' + action + ' ON x_post_queue WHEN EXISTS (' + ancestors + ") BEGIN SELECT RAISE(ABORT,'x_schedule_target_cancelled_by_operator'); END")
    c.execute('CREATE TRIGGER IF NOT EXISTS x_unqueued_cancel_run_resume BEFORE UPDATE OF status ON x_post_schedule_run WHEN OLD.status=\'cancelled\' AND NEW.status<>\'cancelled\' AND EXISTS (SELECT 1 FROM ' + TABLE + ' WHERE run_id=OLD.id) BEGIN SELECT RAISE(ABORT,\'x_schedule_cancelled_by_operator\'); END')
    # A recovery may insert queues before recording its parent link. Refuse
    # that late linkage too, within the recovery caller's transaction.
    for i, table in enumerate(PARENT_TABLES):
        c.execute('CREATE TRIGGER IF NOT EXISTS x_unqueued_cancel_comp_' + str(i) + ' BEFORE INSERT ON ' + table + ' WHEN EXISTS (SELECT 1 FROM ' + TABLE + ' c JOIN x_post_queue q ON q.account_id=c.account_id WHERE c.run_id=NEW.original_schedule_run_id AND q.schedule_run_id=NEW.compensation_schedule_run_id) BEGIN SELECT RAISE(ABORT,\'x_schedule_target_cancelled_by_operator\'); END')
    c.execute('CREATE TRIGGER IF NOT EXISTS x_unqueued_cancel_gap BEFORE INSERT ON x_post_operator_gap_recovery_audit WHEN json_valid(NEW.evidence_json) AND EXISTS (SELECT 1 FROM ' + TABLE + " c JOIN x_post_queue q ON q.account_id=c.account_id WHERE c.run_id=CAST(NEW.identity AS INTEGER) AND q.schedule_run_id=CAST(json_extract(NEW.evidence_json,'$.child_run_id') AS INTEGER)) BEGIN SELECT RAISE(ABORT,'x_schedule_target_cancelled_by_operator'); END")


def apply(c, manifest, expected_sha, *, actor, reason, backup_path):
    if sha(manifest) != expected_sha or not actor or not reason or not manifest['targets']:
        raise ValueError('invalid_manifest')
    c.execute('BEGIN IMMEDIATE')
    try:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (BATCH,)).fetchone()
        if exists and c.execute('SELECT 1 FROM ' + BATCH + ' WHERE manifest_sha256=?', (expected_sha,)).fetchone():
            count = c.execute('SELECT count(*) FROM ' + TABLE + ' WHERE manifest_sha256=?', (expected_sha,)).fetchone()[0]
            if count != manifest['count']:
                raise ValueError('incomplete_prior_application')
            c.rollback()
            return {'already_applied': True, 'cancelled': count, 'manifest_sha256': expected_sha}
        run_ids = [r['identity']['id'] for r in manifest['runs']]
        if preview(c, run_ids) != manifest:
            raise ValueError('scope_changed_repreview_required')
        before = [dict(c.execute('SELECT * FROM x_post_schedule_run WHERE id=?', (i,)).fetchone()) for i in run_ids]
        closed = []
        for entry, row in zip(manifest['runs'], before):
            if not entry['existing_queues']:
                if row['status'] not in ('failed_preflight', 'stopped', 'failed'):
                    raise ValueError('planner_may_be_inflight')
                closed.append(row['id'])
        install_schema(c)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        c.execute('INSERT INTO ' + BATCH + ' VALUES(?,?,?,?,?,?,?)', (expected_sha, actor, reason, now, backup_path, encoded(before), manifest['count']))
        c.executemany('INSERT INTO ' + TABLE + ' VALUES(?,?,?,?)', [(t['run_id'], t['account_id'], expected_sha, now) for t in manifest['targets']])
        for i in closed:
            c.execute("UPDATE x_post_schedule_run SET status='cancelled',error_code='operator_cancelled_unqueued',error_message=?,finished_at=?,updated_at=? WHERE id=?", (reason, now, now, i))
        # Updates above must not change existing task identities or frozen scope.
        if preview(c, run_ids) != manifest:
            raise ValueError('unexpected_scope_change')
        c.commit()
        return {'cancelled': manifest['count'], 'closed_runs': closed, 'preserved_queues': sum(len(r['existing_queues']) for r in manifest['runs']), 'manifest_sha256': expected_sha}
    except BaseException:
        c.rollback()
        raise


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--db', required=True)
    p.add_argument('--manifest', required=True)
    p.add_argument('--run-ids', nargs='+', type=int)
    p.add_argument('--expect-count', type=int)
    p.add_argument('--apply-sha')
    p.add_argument('--backup')
    p.add_argument('--actor')
    p.add_argument('--reason')
    a = p.parse_args()
    if not a.apply_sha:
        with connect(a.db, True) as c:
            c.execute('BEGIN')
            manifest = preview(c, a.run_ids)
        if a.expect_count is not None and manifest['count'] != a.expect_count:
            raise ValueError('unexpected_target_count')
        dest = Path(a.manifest)
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with dest.open('x', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2)
        os.chmod(dest, 0o600)
        print(encoded({'count': manifest['count'], 'manifest_sha256': sha(manifest), 'by_run': {r['identity']['id']: len(r['cancel_account_ids']) for r in manifest['runs']}}))
        return
    manifest = json.loads(Path(a.manifest).read_text(encoding='utf-8'))
    if sha(manifest) != a.apply_sha:
        raise ValueError('manifest_changed')
    if not a.backup or not a.actor or not a.reason:
        raise ValueError('backup_actor_reason_required')
    backup = Path(a.backup)
    if backup.exists():
        raise ValueError('backup_exists')
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old_mask = os.umask(0o077)
    try:
        with connect(a.db, True) as src, sqlite3.connect(str(backup)) as dst:
            src.backup(dst, pages=256, sleep=.05)
        with connect(a.db) as c:
            result = apply(c, manifest, a.apply_sha, actor=a.actor, reason=a.reason, backup_path=str(backup))
    finally:
        os.umask(old_mask)
    print(encoded(result))


if __name__ == '__main__':
    main()
