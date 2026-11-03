#!/usr/bin/env python3
"""Expire one proven orphan X Auto planning lease; do not execute its task.

The existing planner subsequently confirms the unique existing queue. This
tool changes no central/X queue state and never renders, creates a queue, or
calls a platform. An exact private scope, fresh process evidence and CAS are
required. The default action produces a read-only plan.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_pipeline_x_preparation import (RecoveryRejected, date, document_hash,
    dumps, read_connection, require, rows, sha, utcnow)

SERVICE_UNITS = ('x-auto-post-service.service', 'x-post-automation.service')


def capture_scope(auto_path, task_id, run_id, queue_id, central_job_ids):
    """Private exact-identity manifest; the full row (including token) is hashed."""
    with contextlib.closing(read_connection(auto_path)) as c:
        found = rows(c, 'SELECT * FROM x_auto_task WHERE id=?', (task_id,))
    require(len(found) == 1, 'orphan_task_missing')
    task = found[0]
    scope = {'kind': 'x_orphan_planner_lease_scope', 'version': 1, 'task_id': task_id,
        'execution_run_id': run_id, 'queue_id': queue_id, 'central_job_ids': sorted(central_job_ids),
        'account_id': str(task['account_id']), 'material_id': task['material_id'], 'content_id': task['content_id'],
        'external_task_key': 'x-auto-task-' + str(task_id), 'task_row_sha256': sha(task)}
    scope['scope_sha256'] = document_hash(scope)
    return scope


def snapshot(c, a, x, scope, expected_sha, now):
    require(scope.get('kind') == 'x_orphan_planner_lease_scope' and scope.get('version') == 1
            and scope.get('scope_sha256') == expected_sha == document_hash(scope), 'orphan_scope_hash_mismatch')
    tid, rid, qid = (scope.get(k) for k in ('task_id', 'execution_run_id', 'queue_id'))
    require(all(type(v) is int and v > 0 for v in (tid, rid, qid)), 'orphan_identity_invalid')
    task_rows = rows(a, 'SELECT * FROM x_auto_task WHERE id=?', (tid,))
    require(len(task_rows) == 1 and sha(task_rows[0]) == scope['task_row_sha256'], 'orphan_task_changed')
    task = task_rows[0]
    require(task['status'] == 'preparing' and task['claim_phase'] in ('selection', 'prepare')
            and task['execution_run_id'] == rid and not task['execution_queue_id'] and task['claim_token']
            and date(task['lease_expires_at_utc']) > now and date(task['updated_at']) < now - dt.timedelta(seconds=120),
            'orphan_task_not_stale_planning')
    require(str(task['account_id']) == scope['account_id'] and task['material_id'] == scope['material_id']
            and task['content_id'] == scope['content_id'] and not any(task.get(k) for k in
                ('preparation_attempt_count', 'publish_attempt_count', 'unknown_outcome', 'execution_log_id',
                 'publish_id', 'published_at_utc', 'finished_at_utc')), 'orphan_native_execution_evidence')
    reservation = rows(a, 'SELECT * FROM x_auto_material_ledger WHERE task_id=?', (tid,))
    require(len(reservation) == 1 and reservation[0]['material_id'] == task['material_id']
            and reservation[0]['content_id'] == task['content_id'] and reservation[0]['run_id'] == task['run_id']
            and reservation[0]['template_id'] == task['template_id'] and not reservation[0]['canonical_queue_id']
            and not reservation[0]['confirmed_at_utc'], 'orphan_reservation_changed')
    run_rows = rows(x, 'SELECT * FROM x_post_manual_run WHERE id=?', (rid,))
    require(len(run_rows) == 1, 'orphan_run_missing'); run = run_rows[0]
    require(run['trigger_source'] == 'auto_template' and run['external_task_key'] == scope['external_task_key'] == 'x-auto-task-' + str(tid)
            and run['template_ref'] == 'x-auto-template-' + str(task['template_id'])
            and run['template_version'] == task['template_version']
            and json.loads(run['account_ids_json']) == [int(task['account_id'])]
            and json.loads(run['material_ids_json']) == [str(task['material_id'])], 'orphan_run_identity_changed')
    require(run['status'] == 'running' and run['expected_count'] == run['queued_count'] == 1
            and not any(run[k] for k in ('published_count', 'failed_count', 'unknown_count')), 'orphan_run_not_untouched')
    queues = rows(x, 'SELECT * FROM x_post_queue WHERE manual_run_id=? ORDER BY id', (rid,))
    require(len(queues) == 1 and queues[0]['id'] == qid, 'orphan_queue_not_unique')
    queue = queues[0]
    require(queue['status'] == 'queued' and queue['media_validation_mode'] == 'deferred'
            and str(queue['account_id']) == scope['account_id'] and queue['material_id'] == scope['material_id']
            and queue['content_id'] == scope['content_id'] and not any(queue[k] for k in
                ('preflight_sha256', 'preflight_size', 'preflight_duration')), 'orphan_queue_identity_changed')
    key = 'x:queue:' + str(qid)
    for table, field, identity in (('x_post_publish_log', 'queue_id', qid), ('x_post_repost_ledger', 'queue_id', qid),
            ('x_post_random_template_preparation', 'queue_id', qid), ('pipeline_x_claim', 'source_key', key),
            ('pipeline_x_checkpoint', 'source_key', key), ('pipeline_x_artifact', 'queue_id', qid),
            ('pipeline_x_operation', 'source_key', key), ('pipeline_canonical_hold', 'source_key', key)):
        require(not x.execute('SELECT 1 FROM ' + table + ' WHERE ' + field + '=? LIMIT 1', (identity,)).fetchone(), 'orphan_child_execution_evidence')
    source_keys = ['x:auto:' + str(tid), key]
    central = rows(c, 'SELECT * FROM pipeline_job WHERE source_key IN (?,?) ORDER BY id', source_keys)
    require(len(central) == 2 and [r['id'] for r in central] == scope['central_job_ids']
            and sorted(r['source_key'] for r in central) == sorted(source_keys), 'orphan_central_scope_changed')
    for job in central:
        require(job['stage'] == 'prepare' and job['generation'] == 0 and job['status'] == 'legacy_running'
                and not any(job[k] for k in ('node_id', 'worker_id', 'claim_hash', 'lease_expires_at', 'external_intent', 'canonical_receipt')),
                'orphan_dispatch_execution_evidence')
        for table in ('pipeline_claim_history', 'pipeline_operation', 'pipeline_checkpoint', 'pipeline_receipt',
                      'pipeline_delivery', 'pipeline_phase_metric', 'pipeline_resource'):
            require(not c.execute('SELECT 1 FROM ' + table + ' WHERE job_id=? LIMIT 1', (job['id'],)).fetchone(), 'orphan_dispatch_execution_evidence')
        require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (job['source_key'],)).fetchone(), 'orphan_publication_evidence')
    return {'task_row_sha256': sha(task), 'claim_token_sha256': hashlib.sha256(task['claim_token'].encode()).hexdigest(),
        'old_lease_expires_at': task['lease_expires_at_utc'], 'reservation_sha256': sha(reservation),
        'execution_run_sha256': sha(run), 'queue_sha256': sha(queue),
        'central_rows_sha256': sha([{k: v for k, v in row.items() if k not in ('updated_at', 'source_status')} for row in central]),
        'node_ids': [r[0] for r in c.execute('SELECT node_id FROM pipeline_node ORDER BY node_id')]}


def make_plan(central_path, auto_path, x_path, scope, scope_sha, now=None):
    with contextlib.closing(read_connection(central_path)) as c, contextlib.closing(read_connection(auto_path)) as a, contextlib.closing(read_connection(x_path)) as x:
        for db in (c, a, x): db.execute('BEGIN')
        proof = snapshot(c, a, x, scope, scope_sha, now or utcnow())
    plan = {'kind': 'x_orphan_planner_lease_plan', 'version': 1, 'scope': scope, 'proof': proof,
        'captured_at': (now or utcnow()).isoformat(), 'paths': [str(Path(p).resolve()) for p in (central_path, auto_path, x_path)]}
    plan['plan_sha256'] = document_hash(plan)
    return plan


def validate_evidence(plan, evidence, now):
    require(evidence.get('plan_sha256') == plan['plan_sha256'], 'orphan_evidence_plan_changed')
    services = evidence.get('services', [])
    require(sorted(r['unit'] for r in services) == sorted(SERVICE_UNITS), 'orphan_services_missing')
    for service in services:
        require(0 <= (now - date(service['captured_at'])).total_seconds() <= 60
                and service.get('main_pid', 0) > 0 and service.get('threads') == 1
                and service.get('accepted_connections') == 0, 'orphan_service_not_idle')
    nodes = evidence.get('nodes', [])
    require(sorted(r['node_id'] for r in nodes) == plan['proof']['node_ids'] and nodes, 'orphan_nodes_missing')
    for node in nodes:
        require(0 <= (now - date(node['captured_at'])).total_seconds() <= 60
                and node.get('scope_job_ids') == plan['scope']['central_job_ids'] and node.get('outbox_matching_count') == 0
                and node.get('active_scope_job_ids') == [] and node.get('task_file_matches') == [], 'orphan_node_execution_evidence')


def apply_plan(central_path, auto_path, x_path, plan, expected_sha, evidence, backup_dir, *, actor, clock=utcnow):
    require(plan.get('kind') == 'x_orphan_planner_lease_plan' and plan.get('version') == 1
            and plan.get('plan_sha256') == expected_sha == document_hash(plan), 'orphan_plan_hash_mismatch')
    require(plan['paths'] == [str(Path(p).resolve()) for p in (central_path, auto_path, x_path)] and str(actor).strip(), 'orphan_path_or_actor_invalid')
    validate_evidence(plan, evidence, clock())
    fresh = make_plan(central_path, auto_path, x_path, plan['scope'], plan['scope']['scope_sha256'], clock())
    require(fresh['proof'] == plan['proof'], 'orphan_plan_state_drift')
    directory = Path(backup_dir); directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = directory / ('x-orphan-' + expected_sha[:16] + '-' + uuid.uuid4().hex + '.sqlite3')
    with os.fdopen(os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb'): pass
    with contextlib.closing(read_connection(auto_path)) as source, contextlib.closing(sqlite3.connect(backup)) as dest:
        source.backup(dest)
        require(dest.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'orphan_backup_invalid')
    h = hashlib.sha256()
    with backup.open('r+b') as f:
        os.fsync(f.fileno())
        for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
    a = sqlite3.connect(str(auto_path), timeout=10, isolation_level=None); a.row_factory = sqlite3.Row
    try:
        with contextlib.closing(read_connection(central_path)) as c, contextlib.closing(read_connection(x_path)) as x:
            a.execute('PRAGMA synchronous=FULL'); a.execute('BEGIN IMMEDIATE'); c.execute('BEGIN'); x.execute('BEGIN')
            now = clock(); validate_evidence(plan, evidence, now)
            require(snapshot(c, a, x, plan['scope'], plan['scope']['scope_sha256'], now) == plan['proof'], 'orphan_plan_state_drift')
            task = a.execute('SELECT * FROM x_auto_task WHERE id=?', (plan['scope']['task_id'],)).fetchone()
            expired = (now - dt.timedelta(seconds=1)).isoformat(timespec='seconds')
            changed = a.execute("UPDATE x_auto_task SET lease_expires_at_utc=?,updated_at=? WHERE id=? AND status='preparing' AND lease_expires_at_utc=? AND claim_token=?",
                (expired, now.isoformat(timespec='seconds'), task['id'], task['lease_expires_at_utc'], task['claim_token'])).rowcount
            require(changed == 1, 'orphan_lease_cas_failed')
            details = {'plan_sha256': expected_sha, 'scope_sha256': plan['scope']['scope_sha256'], 'actor': str(actor),
                'old_lease_expires_at': task['lease_expires_at_utc'], 'new_lease_expires_at': expired,
                'task_row_sha256': plan['proof']['task_row_sha256'], 'backup_sha256': h.hexdigest(),
                'execution_run_id': task['execution_run_id'], 'existing_queue_id': plan['scope']['queue_id']}
            a.execute("INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,details_json,created_at) VALUES(?,?,'pipeline_orphan_planning_lease_expired','preparing','preparing',?,?)",
                (task['run_id'], task['id'], dumps(details), now.isoformat(timespec='seconds')))
            a.commit()
    except Exception:
        a.rollback(); raise
    finally:
        a.close()
    return {'applied': True, 'plan_sha256': expected_sha, 'task_id': plan['scope']['task_id'],
        'backup_path': str(backup), 'backup_sha256': h.hexdigest(), 'central_modified': False, 'x_queue_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply'), nargs='?', default='plan')
    for name in ('config', 'output'): parser.add_argument('--' + name, required=True)
    for name in ('scope', 'scope-sha256', 'plan', 'plan-sha256', 'node-evidence', 'backup-dir', 'actor'): parser.add_argument('--' + name)
    args = parser.parse_args(); cfg = json.loads(Path(args.config).read_text())
    paths = (cfg['db_path'], cfg['paths']['x_auto'], cfg['paths']['x'])
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as f:
        if args.action == 'plan':
            require(args.scope and args.scope_sha256, 'orphan_scope_required')
            result = make_plan(*paths, json.loads(Path(args.scope).read_text()), args.scope_sha256)
        else:
            require(all((args.plan, args.plan_sha256, args.node_evidence, args.backup_dir, args.actor)), 'orphan_apply_arguments_required')
            result = apply_plan(*paths, json.loads(Path(args.plan).read_text()), args.plan_sha256,
                json.loads(Path(args.node_evidence).read_text()), args.backup_dir, actor=args.actor)
        f.write(json.dumps(result, indent=2) + '\n'); f.flush(); os.fsync(f.fileno())
    print(json.dumps({'ok': True, 'action': args.action, 'plan_sha256': result['plan_sha256']}))


if __name__ == '__main__':
    try:
        main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
