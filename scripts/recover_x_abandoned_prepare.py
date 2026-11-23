"""Recover one proven ended pre-queue request while its publisher is draining."""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0, '/opt/x-tag-publisher/current')
from features.x_auto_posts.core import XAutoPostStore
from features.x_auto_posts.selection_recovery import file_lock

ROOT = Path('/mnt/data-disk/x-reuse-read-context-20261123')
AUTO = Path('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')
LOCKS = str(AUTO.parent/'execution-locks')


def read(path):
    db = sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True, timeout=8)
    db.row_factory = sqlite3.Row
    return db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-id', type=int, required=True)
    parser.add_argument('--updated-at', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    state = subprocess.check_output(['systemctl', 'show', 'x-tag-post.service', '-p', 'ActiveState', '--value'], text=True).strip()
    assert state == 'deactivating', 'publisher_not_draining'
    with file_lock(LOCKS, 'admission.lock', blocking=False) as admission:
        assert admission, 'admission_busy'
        with file_lock(LOCKS, str(args.task_id)+'.lock', blocking=False) as lifetime:
            assert lifetime, 'request_still_running'
            with closing(read(AUTO)) as db:
                task = dict(db.execute('SELECT * FROM x_auto_task WHERE id=?', (args.task_id,)).fetchone())
                assert task['status'] == 'preparing' and task['claim_phase'] == 'prepare'
                assert task['updated_at'] == args.updated_at and task['material_id']
                assert not any(task[k] for k in ('execution_run_id', 'execution_queue_id', 'execution_log_id', 'publish_id', 'unknown_outcome', 'publish_attempt_count'))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='x_auto_operator_retry'").fetchone():
                    assert not db.execute('SELECT 1 FROM x_auto_operator_retry WHERE task_id=?', (args.task_id,)).fetchone()
            for path in ('/mnt/data-disk/x-tag-publisher/posts.sqlite3', '/var/lib/x-post-automation/accounts.sqlite3'):
                with closing(read(path)) as db:
                    assert not db.execute('SELECT 1 FROM x_post_manual_run WHERE external_task_key=?', ('x-auto-task-'+str(args.task_id),)).fetchone(), 'canonical_execution_exists'
            proof = dict(task_id=args.task_id, previous_updated_at=task['updated_at'],
                         account_id=task['account_id'], material_id=task['material_id'],
                         claim_sha256=hashlib.sha256(task['claim_token'].encode()).hexdigest(),
                         admission_exclusive=True, execution_lock_exclusive=True, canonical_runs=0,
                         publish_attempts=0, publisher_state=state)
            if args.apply:
                (ROOT/('abandoned-prepare-'+str(args.task_id)+'.json')).write_text(json.dumps(proof, indent=2))
                # Use the existing fenced state transition without rerunning schema migrations.
                store = object.__new__(XAutoPostStore)
                store.db_path = str(AUTO)
                store._now_fn = lambda: datetime.now(timezone.utc)
                owner = AUTO.stat()
                original_uid, original_gid = os.geteuid(), os.getegid()
                try:
                    os.setegid(owner.st_gid)
                    os.seteuid(owner.st_uid)
                    row = store.transition_task(args.task_id, 'retry_wait', expected_statuses={'preparing'},
                        claim_token=task['claim_token'], updates=dict(claim_phase='prepare', claim_worker='',
                            claim_token='', lease_expires_at_utc='',
                            next_attempt_at_utc=datetime.now(timezone.utc)+timedelta(minutes=2)),
                        event_type='task_prepare_owner_recovered',
                        message='Ended preparation request returned to its original retry; selected material and execution identity retained',
                        details=proof)
                finally:
                    os.seteuid(original_uid)
                    os.setegid(original_gid)
                assert row.material_id == task['material_id'] and row.account_id == task['account_id']
            print(json.dumps(dict(proven=True, applied=args.apply, task_id=args.task_id, preserved_material=task['material_id'])))


if __name__ == '__main__':
    main()
