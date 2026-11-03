#!/usr/bin/env python3
"""Close only the audited historical TT 987 creator-info rejection, never retry it.

Default: read-only plan. Apply requires the exact plan hash, fresh GPU absence
evidence, a native backup, and unchanged task/event evidence under a transaction.
The coordinator remains read-only and observes the owning ledger on its next scan.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_pipeline_x_preparation import (RecoveryRejected, date, document_hash,
    dumps, read_connection, require, rows, sha, utcnow)
from scripts.reconcile_fb_legacy_receipts import validate_backup_location
from features.tt_auto_posts.core import TERMINAL_TASK_STATUSES

CODE = 'spam_risk_user_banned_from_posting'


def snapshot(central, native, task_id=987, account_id='643'):
    found = rows(native, 'SELECT * FROM tt_auto_task WHERE id=?', (task_id,))
    require(len(found) == 1, 'tt_task_missing')
    task = found[0]; key = 'tt:auto:' + str(task_id)
    require(str(task['account_id']) == str(account_id) and task['status'] == 'retry_wait'
            and task['claim_phase'] == 'publish' and task['error_code'] == 'tt_upstream_rejected'
            and task['publish_attempt_count'] > 0, 'tt_task_not_preflight_rejection')
    require(not any(task.get(k) for k in ('unknown_outcome', 'publish_id', 'publish_url',
            'published_at_utc', 'claim_worker', 'claim_token', 'lease_expires_at_utc')),
            'tt_native_write_or_active_evidence')
    events = rows(native, 'SELECT * FROM tt_auto_event WHERE task_id=? ORDER BY id', (task_id,))
    allowed = {'task_created','task_claimed_selection','task_transient_failure','material_reserved',
               'task_preparation_started','task_preparation_ready','task_claimed_publish'}
    require(events and all(e['event_type'] in allowed and e['to_status'] not in
            ('unknown','reconciling','published') for e in events), 'tt_event_write_or_unknown_evidence')
    attempts = [i for i,e in enumerate(events) if e['event_type'] == 'task_claimed_publish']
    require(len(attempts) == task['publish_attempt_count'], 'tt_attempt_history_incomplete')
    for index in attempts:
        require(index + 1 < len(events), 'tt_unfinished_attempt')
        end = events[index + 1]
        require(end['event_type'] == 'task_transient_failure' and end['from_status'] == 'publishing'
                and end['to_status'] == 'retry_wait'
                and ('code=' + CODE + ',') in end['message'] and '[HTTP 200,' in end['message'],
                'tt_attempt_not_definitely_rejected_before_init')
    tables = {r[0] for r in native.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('pipeline_tt_claim', 'pipeline_tt_operation', 'pipeline_tt_checkpoint', 'pipeline_canonical_hold'):
        if table in tables:
            require(not native.execute('SELECT 1 FROM '+table+' WHERE source_key=? LIMIT 1',(key,)).fetchone(),
                    'tt_native_pipeline_evidence')
    jobs = rows(central, 'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY id',(key,))
    require(len(jobs) == 1 and jobs[0]['stage'] == 'publish' and jobs[0]['status'] == 'unknown'
            and jobs[0]['generation'] == 0, 'tt_central_scope_changed')
    job = jobs[0]
    require(not any(job.get(k) for k in ('external_intent','claim_hash','lease_expires_at','canonical_receipt',
                                       'node_id','worker_id')), 'tt_central_execution_evidence')
    for table in ('pipeline_operation','pipeline_checkpoint','pipeline_receipt','pipeline_delivery',
                  'pipeline_claim_history'):
        require(not central.execute('SELECT 1 FROM '+table+' WHERE job_id=? LIMIT 1',(job['id'],)).fetchone(),
                'tt_central_execution_evidence')
    require(not central.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?',(key,)).fetchone(),
            'tt_publication_evidence')
    material = rows(native,'SELECT * FROM tt_auto_material_ledger WHERE task_id=?',(task_id,))
    require(len(material) == 1 and material[0]['material_id'] == task['material_id']
            and material[0]['content_id'] == task['content_id'], 'tt_material_reservation_changed')
    return {'task_id':task_id,'account_id':str(account_id),'source_key':key,'job_id':job['id'],
        'gpu_job_id':task['gpu_job_id'],'task_sha256':sha(task),'events_sha256':sha(events),
        'material_ledger_sha256':sha(material),
        'event_count':len(events),'preflight_rejected_attempts':len(attempts),
        'central_sha256':sha({k:v for k,v in job.items() if k not in ('updated_at',)})}


def check_gpu(proof, evidence, now):
    require(evidence.get('gpu_job_id') == proof['gpu_job_id'] and evidence.get('node_id') == 'hk-gpu'
            and evidence.get('publish_ledger_matches') == [] and evidence.get('read_only') is True
            and evidence.get('checked_roots') == ['/data/tt-post-publisher/publishes',
                                                 '/data/tt-post-publisher/direct-outro-work/publishes'],
            'tt_gpu_evidence_invalid')
    age = (now - date(evidence.get('captured_at'))).total_seconds()
    require(-5 <= age <= 120, 'tt_gpu_evidence_stale')


def make_plan(central_path, native_path, evidence, now=None, task_id=987, account_id='643'):
    now = now or utcnow()
    with contextlib.closing(read_connection(central_path)) as c, contextlib.closing(read_connection(native_path)) as n:
        c.execute('BEGIN'); n.execute('BEGIN'); proof = snapshot(c,n,task_id,account_id)
    check_gpu(proof,evidence,now)
    plan = {'kind':'tt987_preflight_rejection_closure','version':1,'captured_at':now.isoformat(),
            'paths':[str(Path(p).resolve()) for p in (central_path,native_path)],'proof':proof}
    plan['plan_sha256'] = document_hash(plan)
    return plan


def refresh_run(conn, run_id, stamp):
    """Apply AutoPostExecutor._update_run's existing rule in this same transaction."""
    states = [r[0] for r in conn.execute('SELECT status FROM tt_auto_task WHERE run_id=?',(run_id,))]
    run = conn.execute('SELECT * FROM tt_auto_run WHERE id=?',(run_id,)).fetchone()
    require(run is not None and states, 'tt_run_missing')
    if not all(state in TERMINAL_TASK_STATUSES for state in states):
        if run['status'] != 'queued': return
        target = 'running'
    else:
        if run['status'] in {'completed','partial_failed','failed','canceled'}: return
        failed = sum(state in {'failed','canceled'} for state in states)
        target = 'failed' if failed == len(states) else 'partial_failed' if failed else 'completed'
    started = run['started_at_utc'] or (stamp if target == 'running' else '')
    finished = run['finished_at_utc'] or (stamp if target != 'running' else '')
    conn.execute("UPDATE tt_auto_run SET status=?,error_code='',error_message='',updated_at=?,started_at_utc=?,finished_at_utc=? WHERE id=?",
                 (target,stamp,started,finished,run_id))
    conn.execute("INSERT INTO tt_auto_event(run_id,event_type,from_status,to_status,created_at) VALUES(?,'run_status_changed',?,?,?)",
                 (run_id,run['status'],target,stamp))


def apply_plan(plan, expected_sha, evidence, backup_dir, now=None):
    now = now or utcnow()
    require(plan.get('kind') == 'tt987_preflight_rejection_closure' and plan.get('version') == 1
            and plan.get('plan_sha256') == expected_sha == document_hash(plan), 'tt_plan_hash_mismatch')
    check_gpu(plan['proof'], evidence, now)
    cpath,npath = plan['paths']; backup_dir = Path(backup_dir).resolve()
    backup = backup_dir / ('tt987-before-' + uuid.uuid4().hex + '.sqlite3')
    validate_backup_location(backup)
    backup_dir.mkdir(parents=True,exist_ok=True); os.chmod(str(backup_dir),0o700)
    fd = os.open(str(backup),os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600);os.close(fd)
    with contextlib.closing(read_connection(npath)) as source, contextlib.closing(sqlite3.connect(str(backup))) as target:
        source.backup(target)
        require(target.execute('PRAGMA quick_check').fetchone()[0] == 'ok','tt_backup_quick_check_failed')
    os.chmod(str(backup),0o600)
    with contextlib.closing(read_connection(cpath)) as c, contextlib.closing(sqlite3.connect(npath,timeout=10)) as n:
        n.row_factory = sqlite3.Row; n.execute('BEGIN IMMEDIATE'); c.execute('BEGIN')
        proof = snapshot(c,n,plan['proof']['task_id'],plan['proof']['account_id'])
        require(proof == plan['proof'], 'tt_evidence_changed')
        task = n.execute('SELECT * FROM tt_auto_task WHERE id=?',(proof['task_id'],)).fetchone()
        stamp = now.isoformat(timespec='seconds')
        changed = n.execute("UPDATE tt_auto_task SET status='failed',claim_phase='',next_attempt_at_utc='',"
            "error_code='tt_creator_posting_banned',error_message=?,updated_at=?,"
            "finished_at_utc=CASE WHEN finished_at_utc='' THEN ? ELSE finished_at_utc END WHERE id=? AND status='retry_wait'"
            " AND unknown_outcome=0 AND publish_id='' AND claim_token='' AND updated_at=?",
            ('TikTok creator-info rejected all recorded attempts before publication; historical task closed without retry',
             stamp,stamp,proof['task_id'],task['updated_at'])).rowcount
        require(changed == 1,'tt_task_cas_failed')
        n.execute("UPDATE tt_auto_material_ledger SET last_task_status='failed',updated_at=? WHERE task_id=?",
                  (stamp,proof['task_id']))
        details = {'plan_sha256':expected_sha,'before_task_sha256':proof['task_sha256'],
            'events_sha256':proof['events_sha256'],'preflight_rejected_attempts':proof['preflight_rejected_attempts'],
            'upstream_code':CODE,'no_platform_write':True,'no_retry':True}
        n.execute("INSERT INTO tt_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) "
                  "VALUES(?,?,'historical_creator_rejection_closed','retry_wait','failed',?,?,?)",
                  (task['run_id'],proof['task_id'],'Audited preflight rejection; no publish or retry',dumps(details),stamp))
        refresh_run(n,task['run_id'],stamp)
        n.commit()
    return {'applied':True,'task_id':proof['task_id'],'status':'failed','backup':str(backup),'plan_sha256':expected_sha}


def write_private(path, data):
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with open(path,'x',encoding='utf-8') as stream: json.dump(data,stream,indent=2)
    os.chmod(str(path),0o600)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--central-db');p.add_argument('--native-db');p.add_argument('--gpu-evidence',required=True)
    p.add_argument('--output');p.add_argument('--apply');p.add_argument('--plan-sha');p.add_argument('--backup-dir')
    args=p.parse_args()
    try:
        evidence=json.loads(Path(args.gpu_evidence).read_text(encoding='utf-8-sig'))
        if args.apply:
            require(args.plan_sha and args.backup_dir,'tt_apply_arguments_missing')
            plan=json.loads(Path(args.apply).read_text(encoding='utf-8-sig'))
            require(plan.get('proof',{}).get('task_id') == 987 and plan['proof'].get('account_id') == '643','tt_exact_scope_required')
            result=apply_plan(plan,args.plan_sha,evidence,args.backup_dir)
        else:
            require(args.central_db and args.native_db and args.output,'tt_plan_arguments_missing')
            plan=make_plan(args.central_db,args.native_db,evidence);write_private(args.output,plan)
            result={'applied':False,'task_id':987,'plan_sha256':plan['plan_sha256'],'plan':args.output}
        print(dumps(result));return 0
    except RecoveryRejected as exc:
        print(dumps({'applied':False,'error_code':str(exc)}));return 2
    except Exception:
        print(dumps({'applied':False,'error_code':'tt_closure_failed'}));return 2


if __name__ == '__main__':
    raise SystemExit(main())
