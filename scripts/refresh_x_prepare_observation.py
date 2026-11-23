"""Refresh one ended preparation's dispatch observation from its owning ledger."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, '/opt/pipeline-dispatch/current')
from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore, utcnow
sys.path.insert(0, '/opt/x-tag-publisher/current')
from features.x_auto_posts.selection_recovery import file_lock

ROOT = Path('/mnt/data-disk/x-reuse-read-context-20261123')
TASK_ID = 9000000005278
AUTO = '/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3'
DISPATCH = '/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3'


def main():
    proof = json.loads((ROOT/('abandoned-prepare-'+str(TASK_ID)+'.json')).read_text())
    assert proof['task_id'] == TASK_ID and proof['execution_lock_exclusive']
    locks = str(Path(AUTO).parent/'execution-locks')
    with file_lock(locks, 'admission.lock', blocking=False) as admission:
        assert admission, 'admission_busy'
        with file_lock(locks, str(TASK_ID)+'.lock', blocking=False) as lifetime:
            assert lifetime, 'request_still_running'
            adapter = LedgerAdapter(LedgerSpec('x', 'auto', AUTO, 'x_auto_task'))
            task = adapter.read(TASK_ID)
            assert task['status'] == 'retry_wait' and not task['claim_token']
            assert str(task['material_id']) == str(proof['material_id'])
            assert str(task['account_id']) == str(proof['account_id'])
            assert not any(task.get(k) for k in ('execution_run_id', 'execution_queue_id',
                'execution_log_id', 'publish_id', 'publish_attempt_count', 'unknown_outcome'))
            for path in ('/mnt/data-disk/x-tag-publisher/posts.sqlite3', '/var/lib/x-post-automation/accounts.sqlite3'):
                with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True, timeout=8)) as db:
                    assert not db.execute('SELECT 1 FROM x_post_manual_run WHERE external_task_key=?',
                        ('x-auto-task-'+str(TASK_ID),)).fetchone(), 'canonical_execution_exists'
            offer = adapter.project(task)
            assert offer['status'] == 'queued' and offer['legacy_inflight'] is False
            assert offer['source_attempt_count'] == 0
            # Reuse the installed observation convergence without migrations,
            # new dispatch admission, or any platform action.
            store = object.__new__(DispatchStore)
            store.path, store.clock = DISPATCH, utcnow
            with store.transaction() as db:
                before = db.execute("SELECT * FROM pipeline_job WHERE source_key=? AND stage='prepare'",
                                    (offer['source_key'],)).fetchone()
                assert before and before['status'] == 'legacy_running' and before['generation'] == 0
                assert not any(before[k] for k in ('claim_hash', 'external_intent', 'canonical_receipt'))
                (ROOT/'dispatch-observation-before.json').write_text(json.dumps(dict(before), ensure_ascii=False))
                store._converge_idle_legacy(db, before, offer)
                after = db.execute('SELECT status,hold_reason,generation FROM pipeline_job WHERE id=?',
                                   (before['id'],)).fetchone()
                assert after['status'] in ('queued', 'ready', 'backlog_paused') and after['generation'] == 0
            print(json.dumps(dict(task_id=TASK_ID, observation_refreshed=True, **dict(after))))


if __name__ == '__main__':
    main()
