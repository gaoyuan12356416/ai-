"""Reconcile the one expired, unbridged tag planner claim observed during audit."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess

IDENTITY = 9000000005265
ROOT = Path('/mnt/data-disk/production-audit-20261123')


def main():
    state = subprocess.check_output(['systemctl', 'show', 'x-tag-auto.service', '-p', 'MainPID', '-p', 'ActiveState'], universal_newlines=True)
    runner = subprocess.check_output(['systemctl', 'show', 'x-tag-auto-runner.service', '-p', 'MainPID'], universal_newlines=True)
    if 'MainPID=0' not in state or 'ActiveState=inactive' not in state or 'MainPID=0' not in runner:
        raise RuntimeError('original_planner_owner_has_not_drained')
    c = sqlite3.connect('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3', timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE '/mnt/data-disk/x-tag-publisher/posts.sqlite3' AS posts")
    c.execute("ATTACH DATABASE '/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3' AS central")
    c.execute('BEGIN IMMEDIATE')
    row = dict(c.execute('SELECT * FROM x_auto_task WHERE id=?', (IDENTITY,)).fetchone())
    job = dict(c.execute('SELECT * FROM central.pipeline_job WHERE id=93857').fetchone())
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    if (row['status'] != 'preparing' or row['error_code'] != 'x_accounts_unavailable'
            or row['lease_expires_at_utc'] > now or job['generation'] != 0
            or any(row.get(k) for k in ('publish_id', 'unknown_outcome', 'publish_attempt_count', 'execution_run_id', 'execution_queue_id', 'execution_log_id'))
            or c.execute('SELECT 1 FROM posts.x_post_manual_run WHERE external_task_key=?', ('x-auto-task-' + str(IDENTITY),)).fetchone()
            or c.execute('SELECT 1 FROM central.pipeline_operation WHERE job_id=93857').fetchone()):
        raise RuntimeError('orphan_identity_or_no_write_evidence_changed')
    evidence = ROOT / 'tag-orphan-before.json'
    with evidence.open('x') as f:
        os.chmod(str(evidence), 0o600)
        json.dump({'native': row, 'central': job, 'owner': state, 'runner': runner, 'at': now}, f)
        f.flush(); os.fsync(f.fileno())
    key = 'x:auto:' + str(IDENTITY)
    c.execute("INSERT OR IGNORE INTO pipeline_canonical_hold VALUES(?,?,'historical_preparation_only')", (key, now))
    c.execute("UPDATE x_auto_task SET status='reserved',claim_token='',claim_worker='',claim_phase='',lease_expires_at_utc='',updated_at=? WHERE id=?", (now, IDENTITY))
    c.execute("UPDATE central.pipeline_job SET status='backlog_paused',hold_reason='historical_preparation_only',source_status='reserved',updated_at=? WHERE id=93857 AND generation=0", (now,))
    c.execute('DELETE FROM central.pipeline_legacy_resource WHERE job_id=93857')
    c.execute('DELETE FROM central.pipeline_resource WHERE job_id=93857')
    c.execute('INSERT INTO central.pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
        (93857, 'orphan_selection_reconciled', json.dumps({'owner_drained': True, 'child_absent': True,
         'publication_authorized': False, 'native_attempt_count_preserved': row['preparation_attempt_count']}), now))
    c.commit()
    print(json.dumps({'source_key': key, 'unbridged_selection_preserved': True, 'historical_publication_held': True}))


if __name__ == '__main__':
    main()
