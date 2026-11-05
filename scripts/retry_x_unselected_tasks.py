"""One operator-authorized reselection of original, never-selected X Auto tasks.

No new tasks, queues, account changes or platform calls. Each task is isolated;
normal frozen-template selection, account serialization and dedupe still apply.
"""
import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_auto_posts.core import XPostAutoStore
from scripts.recover_x_prepost_failures import backup, writable
from scripts.recover_x_unstarted_manual import connect, require, rows

TABLE = 'x_auto_unselected_retry_audit'
SCHEMA = '''CREATE TABLE IF NOT EXISTS x_auto_unselected_retry_audit (
 task_id INTEGER PRIMARY KEY,day TEXT NOT NULL,actor TEXT NOT NULL,
 created_at TEXT NOT NULL,backup_path TEXT NOT NULL,prior_task_json TEXT NOT NULL)'''


def snapshot(c, task_id, day):
    require(dt.date.fromisoformat(day).isoformat() == day, 'exact_day_required')
    found = rows(c, 'SELECT t.* FROM x_auto_task t JOIN x_auto_run r ON r.id=t.run_id '
        'WHERE t.id=? AND r.shanghai_date=?', (task_id, day))
    require(len(found) == 1, 'task_outside_day')
    task = found[0]
    require(task['status'] == 'no_candidate' and task['error_code'] == 'x_auto_no_eligible_material', 'not_unselected_failure')
    require(not any(task[k] for k in ('material_id','content_id','series_code',
        'execution_run_id','execution_queue_id','execution_log_id','publish_id','publish_url',
        'unknown_outcome','preparation_attempt_count','publish_attempt_count','claim_token',
        'claim_worker','claim_phase','lease_expires_at_utc','reserved_at_utc','bridged_at_utc','published_at_utc')),
        'task_has_work_or_publication_evidence')
    require(json.loads(task['selection_json']) == {}, 'task_selection_present')
    require(not c.execute('SELECT 1 FROM x_auto_material_ledger WHERE task_id=?', (task_id,)).fetchone(), 'material_held')
    require(not c.execute('SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?', ('x:auto:'+str(task_id),)).fetchone(), 'canonical_hold')
    return task


def external_check(native, dispatch, task_id):
    require(not native.execute('SELECT 1 FROM x_post_manual_run WHERE external_task_key=?',
        ('x-auto-task-'+str(task_id),)).fetchone(), 'native_execution_exists')
    jobs = rows(dispatch, 'SELECT * FROM pipeline_job WHERE source_key=?', ('x:auto:'+str(task_id),))
    require(all(j['generation'] == 0 and not j['external_intent'] and not j['canonical_receipt']
        and j['hold_reason'] == 'metadata_only' for j in jobs), 'dispatch_execution_exists')


def plan(auto_path, native_path, dispatch_path, day):
    with contextlib.closing(connect(auto_path)) as c, contextlib.closing(connect(native_path)) as n, contextlib.closing(connect(dispatch_path)) as d:
        ids = [r['id'] for r in rows(c, "SELECT t.id FROM x_auto_task t JOIN x_auto_run r ON r.id=t.run_id WHERE r.shanghai_date=? AND t.status='no_candidate' ORDER BY t.id", (day,))]
        items, skipped = [], []
        for task_id in ids:
            try:
                task = snapshot(c, task_id, day); external_check(n, d, task_id)
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
                    require(not c.execute('SELECT 1 FROM '+TABLE+' WHERE task_id=?', (task_id,)).fetchone(), 'already_retried')
                items.append(task)
            except ValueError as exc:
                skipped.append(dict(task_id=task_id, reason=str(exc)))
    result = dict(kind='x_unselected_retry',day=day,auto_path=str(auto_path),native_path=str(native_path),dispatch_path=str(dispatch_path),items=items,skipped=skipped)
    result['sha256'] = digest(result)
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',',':'), ensure_ascii=False).encode()).hexdigest()


def apply(saved, folder, actor):
    require(saved['kind'] == 'x_unselected_retry' and saved['sha256'] == digest({k:v for k,v in saved.items() if k!='sha256'}) and str(actor).strip(), 'plan_invalid')
    target = backup(saved['auto_path'], folder, 'unselected-auto')
    result = dict(retried=[],already_applied=[],skipped=[],backup=target,platform_called=False)
    with contextlib.closing(writable(saved['auto_path'])) as c, contextlib.closing(connect(saved['native_path'])) as n, contextlib.closing(connect(saved['dispatch_path'])) as d:
        c.execute(SCHEMA)
        for task in saved['items']:
            c.execute('BEGIN IMMEDIATE')
            try:
                task_id=task['id']
                if c.execute('SELECT 1 FROM '+TABLE+' WHERE task_id=?', (task_id,)).fetchone():
                    c.rollback();result['already_applied'].append(task_id);continue
                require(snapshot(c,task_id,saved['day'])==task,'task_changed')
                external_check(n,d,task_id)
                now=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
                c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?)', (task_id,saved['day'],actor,now,target,json.dumps(task,ensure_ascii=False)))
                c.execute("UPDATE x_auto_task SET status='pending',error_code='',error_message='',finished_at_utc='',updated_at=? WHERE id=? AND status='no_candidate'",(now,task_id))
                c.execute("UPDATE x_auto_run SET status='running',finished_at_utc='',updated_at=? WHERE id=? AND status IN ('completed','partial_failed','failed')",(now,task['run_id']))
                XPostAutoStore._event(c,event_type='operator_unselected_retry',created_at=now,run_id=task['run_id'],task_id=task_id,from_status='no_candidate',to_status='pending',details={'actor':actor,'plan_sha256':saved['sha256'],'prior_error_code':task['error_code']})
                c.commit();result['retried'].append(task_id)
            except ValueError as exc:
                c.rollback();result['skipped'].append(dict(task_id=task['id'],reason=str(exc)))
            except Exception:
                c.rollback();raise
    return result
