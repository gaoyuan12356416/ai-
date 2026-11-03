#!/usr/bin/env python3
"""Rearm only read-only Auto receipt tracking for this incident's restored queues."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.pipeline_dispatch.x_prepost_recovery import TABLE as AUTH, digest, encoded, queue_hash, log_hash
from features.x_auto_posts.receipt_reconciliation import _connect, _ids
from scripts.recover_x_prepost_failures import backup
from scripts.recover_pipeline_x_preparation import RecoveryRejected, require, read_connection, rows, document_hash, utcnow

DAYS = ('2026-11-02', '2026-11-03')
PREPOST = 'pipeline_x_prepost_recovery_apply'
AUDIT = 'x_auto_receipt_tracking_resume_audit'
UNIT = 'x-auto-post-service.service'
SPECIAL_TASK = {'id': 2395, 'run_id': 414, 'execution_queue_id': 5524, 'execution_run_id': 1563,
                'status': 'failed', 'unknown_outcome': 1}
SPECIAL_TASK_SHA = '564433d5f9647f26544313c2ab5af7646b6903eeeb7b152eae8286f62dc6332b'
SPECIAL_PLAN_SHA = '66167b334a1bb019987a4761a6d022bbf2f5f98b673b70f78262313797e02585'
SPECIAL_JOB_ID = 31993
SPECIAL_AUDIT = 'pipeline_x_5524_no_post_audit'
SPECIAL_JOURNAL = 'pipeline_x_5524_reconciliation_apply'
# Exact recovery plans already applied in this incident, not a failed-task policy.
PREPOST_PLANS = frozenset((
    '06c5dade7a8d954e9e33bbf5907c3f3280498b6978afbafd18f0c39f5ec75c34',
    '33d43d3482affb22451333bebd874a2f54c81bc511e8ccf3cdd5c37f40af2a3a',
    '4418becbb5292cfa113113c082b8e9a2b6a28c7b7f78d1c6bc7ba51ccd86ee08',
    '8f40e08ff78610dfa81f43444ee2d89c234d40fcf146e1d0d9ee9d3863f88b1d',
    'f4a542f860381a3b023894a6058e1e40b9080b138bfba82bc3654ed86c5ca127',
))
ATTRIBUTION_PLANS = frozenset((
    '9817bfc2f30a58ca83b14f0f74ed6f1f7b8ad58d5c8f3ac15bc5d6c211b5270c',
    '4ec5cb9f5f50714d86b6aa1bf5bfec282806e0804d962ff8472c7c939e83d6eb',
))
RUNTIME_HASHES = {
    'features/x_auto_posts/publisher.py': {'088258f9dc4f9a5228d76efa0c43d7147908ca7527b1642a7be822b83a07930b'},
    'features/x_auto_posts/core.py': {'0c627e6a8cc63de64ce6a1c757d177f9eae4bc08f8171f4299e585093f44d580'},
    'features/pipeline_dispatch/x_planning.py': {'e287c6e0e479d50a569f247ff2c2bc5fff07c23b9a9fc0a4473fe75aea4fd88b'},
    'scripts/x_auto_post_service.py': {'fcc27b512890fa38ebd7d5758ddf03151ad063c2f9a4500ae0a5824f4fd3e367'},
    # The running service includes independently reviewed account-list batching;
    # its executor construction and dispatch injection are unchanged.
    'features/x_auto_posts/service.py': {'bd3245fd3b46b71e074e0b19e23709c8203329d20d7fe26abb596b1638de4c06',
                                       '9cd5f2132ba3eafe2a36ee2835e87bd1ba9e213bb7ee99351ecf1b29daed364a'},
}


def main_pid():
    value = subprocess.check_output(['systemctl', 'show', UNIT, '-p', 'MainPID', '--value'], timeout=10).decode().strip()
    require(value.isdigit() and int(value) > 1, 'auto_runtime_not_running')
    return value


def runtime_proof(auto_db, proc_root='/proc'):
    pid = main_pid(); proc = Path(proc_root)/pid
    # Only non-secret control values leave this function.
    env = dict(part.split(b'=', 1) for part in (proc/'environ').read_bytes().split(b'\0') if b'=' in part)
    require(env.get(b'PIPELINE_X_DISPATCH_ENABLED') == b'1'
            and env.get(b'PIPELINE_X_CANONICAL_BRIDGE_ENABLED') == b'1', 'dispatch_runtime_not_enabled')
    configured = env.get(b'X_AUTO_POST_DB_PATH', b'').decode()
    require(configured and Path(configured).resolve() == Path(auto_db).resolve(), 'runtime_auto_database_mismatch')
    command = [x.decode() for x in (proc/'cmdline').read_bytes().split(b'\0') if x]
    require(command == ['/usr/bin/python3', '/opt/x-post-automation/current/scripts/x_auto_post_service.py'], 'runtime_entrypoint_changed')
    start = (proc/'stat').read_text().rsplit(')', 1)[1].split()[19]
    root = (proc/'cwd').resolve(strict=True)
    files = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in RUNTIME_HASHES}
    require(all(value in RUNTIME_HASHES[name] for name, value in files.items()), 'runtime_code_not_reviewed')
    require(main_pid() == pid and (proc/'stat').read_text().rsplit(')', 1)[1].split()[19] == start, 'runtime_changed_during_verification')
    return {'unit': UNIT, 'pid': int(pid), 'process_start_ticks': start, 'cwd': str(root),
            'command': command, 'dispatch_enabled': True, 'bridge_enabled': True, 'files': files,
            'auto_db': str(Path(auto_db).resolve())}


def exists(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def one(conn, sql, params=()):
    found = rows(conn, sql, params)
    require(len(found) == 1, 'unique_identity_required')
    return found[0]


def reconciled_5524_authority(c, n, queue, log):
    """Observe this reconciled native queue while retaining Auto's old unknown."""
    require(exists(c, SPECIAL_AUDIT) and exists(n, SPECIAL_AUDIT) and exists(c, SPECIAL_JOURNAL) and exists(n, AUTH),
            '5524_reconciliation_missing')
    journal = one(c, 'SELECT * FROM '+SPECIAL_JOURNAL+' WHERE job_id=?', (SPECIAL_JOB_ID,))
    require(journal['state'] == 'committed' and journal['plan_sha256'] == SPECIAL_PLAN_SHA, '5524_reconciliation_not_committed')
    audits = [one(conn, 'SELECT * FROM '+SPECIAL_AUDIT+' WHERE job_id=?', (SPECIAL_JOB_ID,)) for conn in (c, n)]
    plans = [json.loads(a['evidence_json'])['plan'] for a in audits]
    require(all(a['plan_sha256'] == SPECIAL_PLAN_SHA for a in audits) and plans[0] == plans[1]
            and plans[0]['kind'] == 'x_5524_verified_no_post' and document_hash(plans[0]) == SPECIAL_PLAN_SHA,
            '5524_reconciliation_evidence_changed')
    proof = plans[0]['proof']; prior = proof['log']
    key = 'x:queue:'+str(queue['id'])
    auth = one(n, 'SELECT * FROM '+AUTH+' WHERE source_key=?', (key,))
    require((auth['stage'], auth['from_generation'], auth['to_generation'], auth['prior_attempts']) == ('publish', 1, 2, 1)
            and auth['plan_sha256'] == SPECIAL_PLAN_SHA and auth['state'] in ('armed', 'consumed')
            and json.loads(auth['proof_json']) == {'reconciliation': SPECIAL_AUDIT, 'original_proof': proof}
            and prior['unknown_outcome'] == 1 and prior['attempt_count'] == 1 and prior['id'] == log['id']
            and proof['queue']['id'] == queue['id'] and queue_hash(queue) == queue_hash(proof['queue']) == auth['queue_sha256']
            and log_hash(log) == log_hash(prior) == auth['log_sha256'], '5524_reconciliation_authority_changed')
    return {'kind': '5524_reconciled_unknown_observation', 'journal': journal, 'audits': audits,
            'authorization': {k: v for k, v in auth.items() if k not in ('state', 'started_at')}}


def recovery_authority(c, n, queue, log, *, reconciled_5524=False):
    key = 'x:queue:'+str(queue['id'])
    jobs = rows(c, 'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY stage', (key,))
    require(jobs and all(j['channel'] == 'x' and j['family'] == 'queue' and j['source_id'] == str(queue['id'])
            and json.loads(j['source_meta_json']).get('canonical_fingerprint') == canonical_fingerprint(queue, 'id')
            and j['status'] != 'unknown' and not j['hold_reason'] for j in jobs), 'canonical_job_identity_or_hold')
    by_stage = {j['stage']: j for j in jobs}
    if reconciled_5524:
        require(by_stage['publish']['id'] == SPECIAL_JOB_ID, '5524_job_identity_changed')
        return reconciled_5524_authority(c, n, queue, log)
    matches = []
    if exists(c, PREPOST) and exists(n, AUTH):
        for journal in rows(c, 'SELECT * FROM '+PREPOST+' WHERE source_key=?', (key,)):
            if journal['plan_sha256'] not in PREPOST_PLANS or journal['state'] != 'committed': continue
            proof = json.loads(journal['proof_json'])
            auth = rows(n, 'SELECT * FROM '+AUTH+' WHERE source_key=? AND stage=? AND from_generation=?',
                        (key, journal['stage'], journal['from_generation']))
            require(len(auth) == 1, 'native_recovery_authorization_missing')
            auth = auth[0]
            require(auth['plan_sha256'] == journal['plan_sha256'] and auth['state'] in ('armed', 'consumed')
                    and json.loads(auth['proof_json']) == proof and auth['to_generation'] == journal['from_generation']+1
                    and proof['queue_id'] == queue['id'] and proof['source_key'] == key
                    and proof['prior_log']['id'] == log['id'] and proof['prior_log']['account_id'] == queue['account_id']
                    and proof['job_id'] == by_stage[journal['stage']]['id'], 'recovery_authorization_identity_changed')
            if journal['stage'] == 'publish':
                require(queue_hash(queue) == auth['queue_sha256'] and log_hash(log) == auth['log_sha256'],
                        'authorized_frozen_queue_or_body_changed')
            matches.append({'kind': 'prepost', 'journal': journal,
                'authorization': {k: v for k, v in auth.items() if k not in ('state', 'started_at')}})
    for event in rows(c, "SELECT e.* FROM pipeline_event e JOIN pipeline_job j ON j.id=e.job_id WHERE j.source_key=? AND e.event_type='audited_zero_write_attribution_recovery' ORDER BY e.id", (key,)):
        proof = json.loads(event['details_json'])
        if proof.get('plan_sha256') not in ATTRIBUTION_PLANS: continue
        previous = proof['previous_execution']
        require(event['job_id'] == by_stage['publish']['id'] == previous['id']
                and previous['source_key'] == key and previous['status'] == 'failed'
                and previous['error_code'] == 'x_attribution_resolution_unavailable'
                and not previous['external_intent'] and proof['resolver_context_sha256'], 'attribution_audit_identity_changed')
        matches.append({'kind': 'attribution', 'event': event})
    require(len(matches) == 1, 'exact_incident_recovery_required')
    return matches[0]


def inspect(auto, n, c, task_id):
    task = one(auto, 'SELECT * FROM x_auto_task WHERE id=?', (task_id,))
    special = all(task.get(k) == v for k, v in SPECIAL_TASK.items()) and digest(task) == SPECIAL_TASK_SHA
    require(task['status'] == 'failed' and (not task['unknown_outcome'] or special) and not task['publish_id'], 'auto_not_known_failed')
    require(not any(task[k] for k in ('claim_token', 'claim_worker', 'lease_expires_at_utc')), 'auto_claim_present')
    require(task['execution_run_id'] and task['execution_queue_id'], 'original_execution_missing')
    run = one(auto, 'SELECT * FROM x_auto_run WHERE id=?', (task['run_id'],))
    require(run['shanghai_date'] in DAYS and run['status'] in ('running', 'failed', 'partial_failed', 'completed')
            and run['template_id'] == task['template_id'] and run['template_version'] == task['template_version'], 'auto_run_scope_changed')
    queue = one(n, 'SELECT * FROM x_post_queue WHERE id=?', (task['execution_queue_id'],))
    log = one(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (queue['id'],))
    parent = one(n, 'SELECT * FROM x_post_manual_run WHERE id=?', (queue['manual_run_id'],))
    require(parent['id'] == task['execution_run_id'] and parent['trigger_source'] == 'auto_template'
            and parent['external_task_key'] == 'x-auto-task-'+str(task_id)
            and parent['template_ref'] == 'x-auto-template-'+str(task['template_id'])
            and parent['template_version'] == task['template_version'], 'native_parent_identity_changed')
    require(json.loads(parent['account_ids_json']) == [int(task['account_id'])]
            and json.loads(parent['material_ids_json']) == [str(task['material_id'])]
            and str(queue['account_id']) == str(log['account_id']) == str(task['account_id'])
            and str(queue['material_id']) == str(task['material_id']) and str(queue['content_id']) == str(task['content_id'])
            and (not task['execution_log_id'] or task['execution_log_id'] == log['id']), 'native_execution_identity_changed')
    require(n.execute('SELECT count(*) FROM x_post_queue WHERE manual_run_id=?', (parent['id'],)).fetchone()[0] == 1,
            'native_parent_queue_not_unique')
    require(task['body_template'] == parent['body_template'] == queue['body_template']
            and hashlib.sha256(task['body_template'].encode()).hexdigest() == task['body_sha256'], 'frozen_body_changed')
    require(queue['status'] in ('queued', 'reserved', 'preparing', 'publishing', 'waiting_relay')
            and log['status'] in ('reserved', 'media_uploading', 'post_creating', 'repost_creating')
            and not log['unknown_outcome'] and not log['x_post_id'] and not log['published_at'], 'native_not_active_known_recovery')
    require(not n.execute('SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?', ('x:queue:'+str(queue['id']),)).fetchone()
            and not auto.execute('SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?', ('x:auto:'+str(task_id),)).fetchone(), 'canonical_hold_present')
    ledger = one(auto, 'SELECT * FROM x_auto_material_ledger WHERE task_id=?', (task_id,))
    require(ledger['canonical_queue_id'] == queue['id'] and ledger['run_id'] == task['run_id']
            and ledger['template_id'] == task['template_id'] and str(ledger['material_id']) == str(task['material_id'])
            and str(ledger['content_id']) == str(task['content_id']), 'material_reservation_changed')
    authority = recovery_authority(c, n, queue, log, reconciled_5524=special)
    native_identity = {'fingerprint': canonical_fingerprint(queue, 'id'), 'log_id': log['id'],
        'parent_id': parent['id'], 'body_sha256': task['body_sha256']}
    return {'task_id': task_id, 'task': task, 'run': run, 'ledger': ledger, 'native_identity': native_identity,
            'authority': authority, 'native_snapshot': {'queue': queue, 'log': log, 'parent': parent}}


def make_plan(auto_db, native, central, task_ids):
    runtime = runtime_proof(auto_db); selected = _ids(task_ids); items = []; skipped = []
    with contextlib.closing(_connect(auto_db)) as a, contextlib.closing(read_connection(native)) as n, contextlib.closing(read_connection(central)) as c:
        a.execute('BEGIN'); n.execute('BEGIN'); c.execute('BEGIN')
        for identity in selected:
            try: items.append(inspect(a, n, c, identity))
            except RecoveryRejected as exc: skipped.append({'task_id': identity, 'reason': str(exc)})
    require(runtime_proof(auto_db) == runtime, 'runtime_changed')
    result = {'kind': 'x_auto_restored_receipt_tracking', 'version': 1, 'auto': str(Path(auto_db).resolve()),
        'native': str(Path(native).resolve()), 'central': str(Path(central).resolve()), 'runtime': runtime,
        'selected_task_ids': selected, 'items': items, 'skipped': skipped, 'captured_at': utcnow().isoformat()}
    result['plan_sha256'] = document_hash(result)
    return result


def create_audit(a):
    a.execute('CREATE TABLE IF NOT EXISTS '+AUDIT+' (task_id INTEGER PRIMARY KEY,plan_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)')
    for action in ('UPDATE', 'DELETE'):
        a.execute('CREATE TRIGGER IF NOT EXISTS '+AUDIT+'_no_'+action.lower()+' BEFORE '+action+' ON '+AUDIT+" BEGIN SELECT RAISE(ABORT,'auto_receipt_audit_append_only'); END")
    a.execute('CREATE TRIGGER IF NOT EXISTS '+AUDIT+'_no_replace BEFORE INSERT ON '+AUDIT+' WHEN EXISTS(SELECT 1 FROM '+AUDIT+" WHERE task_id=NEW.task_id) BEGIN SELECT RAISE(ABORT,'auto_receipt_audit_append_only'); END")


def apply(saved, backup_dir, actor):
    require(saved.get('kind') == 'x_auto_restored_receipt_tracking' and saved.get('version') == 1
            and document_hash(saved) == saved['plan_sha256'], 'plan_changed')
    require(actor and str(actor).strip(), 'actor_required')
    require(saved['items'] and _ids([item['task_id'] for item in saved['items']])
            == [item['task_id'] for item in saved['items']]
            and set(item['task_id'] for item in saved['items']) <= set(saved['selected_task_ids']), 'no_eligible_or_invalid_task_scope')
    require(runtime_proof(saved['auto']) == saved['runtime'], 'runtime_changed')
    saved_backup = backup(saved['auto'], backup_dir, 'auto-receipt-tracking')
    now = utcnow().isoformat(); changed = []; already = []; skipped = []
    with contextlib.closing(_connect(saved['auto'], writable=True)) as a, contextlib.closing(read_connection(saved['native'])) as n, contextlib.closing(read_connection(saved['central'])) as c:
        a.execute('PRAGMA synchronous=FULL'); a.execute('BEGIN IMMEDIATE'); n.execute('BEGIN'); c.execute('BEGIN')
        try:
            create_audit(a)
            current = []
            for expected in saved['items']:
                previous = a.execute('SELECT plan_sha256 FROM '+AUDIT+' WHERE task_id=?', (expected['task_id'],)).fetchone()
                if previous:
                    require(previous[0] == saved['plan_sha256'], 'different_tracking_recovery_recorded')
                    already.append(expected['task_id']); continue
                # A newly successful native queue belongs to receipt correction,
                # not rearming. Other drift is a hard CAS failure for this plan.
                q = n.execute('SELECT status FROM x_post_queue WHERE id=?', (expected['task']['execution_queue_id'],)).fetchone()
                if q and q['status'] == 'published':
                    skipped.append({'task_id': expected['task_id'], 'reason': 'native_published_use_receipt_reconciliation'}); continue
                actual = inspect(a, n, c, expected['task_id'])
                for key in ('task', 'run', 'ledger', 'native_identity', 'authority'):
                    require(actual[key] == expected[key], 'plan_state_drift_'+key)
                current.append(actual)
            require(runtime_proof(saved['auto']) == saved['runtime'], 'runtime_changed')
            for item in current:
                task = item['task']; identity = task['id']
                evidence = {'previous': item, 'runtime': saved['runtime'], 'actor': actor, 'backup': saved_backup,
                    'plan_sha256': saved['plan_sha256'], 'purpose': 'observe_existing_canonical_queue_only'}
                a.execute('INSERT INTO '+AUDIT+' VALUES(?,?,?,?)', (identity, saved['plan_sha256'], encoded(evidence), now))
                count = a.execute("UPDATE x_auto_task SET status='retry_wait',claim_phase='reconcile',next_attempt_at_utc=?,finished_at_utc='',updated_at=? WHERE id=? AND status='failed' AND unknown_outcome=? AND claim_token='' AND claim_worker='' AND lease_expires_at_utc=''", (now, now, identity, task['unknown_outcome'])).rowcount
                require(count == 1, 'auto_compare_and_swap_failed')
                a.execute("UPDATE x_auto_material_ledger SET last_task_status='retry_wait',updated_at=? WHERE task_id=?", (now, identity))
                a.execute("INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(?,?,'task_receipt_tracking_resumed','failed','retry_wait','Observe original restored X queue; no publication request',?,?)", (task['run_id'], identity, encoded({'plan_sha256': saved['plan_sha256'], 'queue_id': task['execution_queue_id']}), now))
                changed.append(identity)
            for run_id in sorted({item['task']['run_id'] for item in current}):
                original = next(item['run'] for item in current if item['task']['run_id'] == run_id)
                if original['status'] == 'running': continue
                a.execute("UPDATE x_auto_run SET status='running',finished_at_utc='',updated_at=? WHERE id=?", (now, run_id))
                a.execute("INSERT INTO x_auto_event(run_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(?,'run_receipt_tracking_resumed',?,'running','Observe original restored X queues',?,?)", (run_id, original['status'], encoded({'plan_sha256': saved['plan_sha256'], 'previous': original}), now))
            a.commit()
        except BaseException: a.rollback(); raise
    return {'changed_task_ids': changed, 'already_applied_task_ids': already, 'skipped': skipped,
        'plan_sha256': saved['plan_sha256'], 'backup': saved_backup, 'publish_requests': 0, 'native_modified': False, 'central_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply'))
    for name in ('auto', 'native', 'central', 'plan', 'backup-dir', 'actor'): parser.add_argument('--'+name)
    parser.add_argument('--task-ids', nargs='+', type=int); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with os.fdopen(os.open(args.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w', encoding='utf-8') as handle:
        result = make_plan(args.auto, args.native, args.central, args.task_ids) if args.action == 'plan' else apply(json.loads(Path(args.plan).read_text()), args.backup_dir, args.actor)
        handle.write(json.dumps(result, ensure_ascii=False, indent=2)+'\n'); handle.flush(); os.fsync(handle.fileno())
    print(json.dumps({'ok': True, 'action': args.action, 'publish_requests': 0}))


if __name__ == '__main__':
    try: main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
