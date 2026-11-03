#!/usr/bin/env python3
"""Audited admission of exact X preparations that failed before node execution.

Default is read-only plan. Apply changes only central failed -> queued, never
native ledgers, publication jobs, identities, artifacts, or existing workers.
The operator supplies an exact hashed scope and fresh per-node scoped evidence.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.adapters import canonical_fingerprint


class RecoveryRejected(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise RecoveryRejected(code)


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def document_hash(value):
    return sha({k: v for k, v in value.items() if k not in ('scope_sha256', 'plan_sha256')})


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def date(value):
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'timezone_required')
    return parsed


def read_connection(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args)]


def validate_scope(scope, expected_sha):
    require(scope.get('kind') == 'x_unstarted_prepare_recovery_scope' and scope.get('version') == 1,
            'scope_type_invalid')
    require(scope.get('scope_sha256') == expected_sha == document_hash(scope), 'scope_hash_mismatch')
    items = scope.get('items', [])
    require(0 < len(items) <= 200 and len({x['job_id'] for x in items}) == len(items)
            and len({x['source_key'] for x in items}) == len(items), 'scope_duplicates_or_size')
    require(sorted(scope.get('source_keys', [])) == sorted(x['source_key'] for x in items), 'scope_sources_mismatch')
    for item in items:
        require(type(item['job_id']) is int and type(item['generation']) is int and item['generation'] > 0
                and re.fullmatch(r'x:queue:[1-9][0-9]*', item['source_key'])
                and re.fullmatch(r'[a-f0-9]{64}', item['canonical_fingerprint']), 'scope_identity_invalid')
    return items


def snapshot(c, n, scope, scope_sha, now):
    proofs = []
    for item in validate_scope(scope, scope_sha):
        found = rows(c, 'SELECT * FROM pipeline_job WHERE id=?', (item['job_id'],))
        require(len(found) == 1, 'dispatch_job_missing'); job = found[0]
        key = item['source_key']; qid = int(key.rsplit(':', 1)[1])
        require((job['channel'], job['family'], job['stage'], job['status'], job['error_code']) ==
                ('x', 'queue', 'prepare', 'failed', 'canonical_bridge_unavailable'), 'dispatch_state_not_exact')
        require(job['source_key'] == key and job['source_id'] == str(qid)
                and job['generation'] == item['generation'], 'dispatch_identity_changed')
        require(job['finished_at'] and date(job['finished_at']) <= now - dt.timedelta(seconds=120)
                and (not job['lease_expires_at'] or date(job['lease_expires_at']) <= now), 'dispatch_failure_not_settled')
        meta = json.loads(job['source_meta_json'])
        require(meta.get('canonical_fingerprint') == item['canonical_fingerprint'], 'fingerprint_changed')
        require(not job['external_intent'] and not job['hold_reason'] and not job['canonical_receipt']
                and not any(meta.get(k) for k in ('unknown_outcome', 'reconcile_only', 'source_attempt_count')),
                'dispatch_write_or_hold_evidence')
        require(not any(json.loads(job['artifact_json']).get(k) for k in ('url', 'sha256', 'size')), 'prepared_artifact_present')
        source_jobs = rows(c, 'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY id', (key,))
        require(not any(j['id'] != job['id'] and (j['generation'] or j['external_intent'] or j['canonical_receipt']
                    or j['status'] in ('running', 'legacy_running', 'unknown', 'published')) for j in source_jobs),
                'other_stage_execution_evidence')
        for other in source_jobs:
            for table in ('pipeline_operation', 'pipeline_checkpoint', 'pipeline_receipt', 'pipeline_delivery',
                          'pipeline_phase_metric', 'pipeline_resource', 'pipeline_legacy_resource'):
                require(not c.execute('SELECT 1 FROM ' + table + ' WHERE job_id=? LIMIT 1', (other['id'],)).fetchone(),
                        'dispatch_execution_evidence')
        require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (key,)).fetchone(), 'publication_evidence')
        history = rows(c, 'SELECT * FROM pipeline_claim_history WHERE job_id=? ORDER BY generation', (job['id'],))
        require([r['generation'] for r in history] == list(range(1, job['generation'] + 1)), 'claim_history_changed')
        require((history[-1]['claim_hash'], history[-1]['node_id'], history[-1]['worker_id']) ==
                (job['claim_hash'], job['node_id'], job['worker_id']), 'claim_identity_changed')
        failures = rows(c, "SELECT * FROM pipeline_event WHERE job_id=? AND event_type='failed' ORDER BY id", (job['id'],))
        require(failures and json.loads(failures[-1]['details_json']).get('code') == 'canonical_bridge_unavailable'
                and not json.loads(failures[-1]['details_json']).get('unknown'), 'failure_fact_missing')
        queues = rows(n, 'SELECT * FROM x_post_queue WHERE id=?', (qid,))
        require(len(queues) == 1 and queues[0]['status'] == 'queued', 'native_queue_not_queued')
        queue = queues[0]
        require(canonical_fingerprint(queue, 'id') == item['canonical_fingerprint'], 'native_frozen_identity_changed')
        require(not queue.get('preflight_sha256') and not queue.get('preflight_size'), 'native_preflight_already_done')
        parent_table, parent_id = ('x_post_manual_run', queue.get('manual_run_id')) if queue.get('manual_run_id') else ('x_post_schedule_run', queue.get('schedule_run_id'))
        parents = rows(n, 'SELECT * FROM ' + parent_table + ' WHERE id=?', (parent_id,)) if parent_id else []
        require(len(parents) == 1, 'native_parent_missing')
        parent_identity = {k: parents[0].get(k) for k in ('id', 'template_ref', 'template_version',
            'random_template_enabled', 'body_template', 'external_task_key', 'source_type', 'trigger_source')}
        logs = rows(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (qid,))
        require(len(logs) <= 1 and all(r['status'] == 'reserved' and not any(r[k] for k in
                ('attempt_count', 'unknown_outcome', 'x_media_id', 'x_post_id', 'started_at', 'published_at')) for r in logs),
                'native_publish_evidence')
        require(not n.execute('SELECT 1 FROM x_post_repost_ledger WHERE queue_id=?', (qid,)).fetchone(), 'native_repost_evidence')
        require(not n.execute('SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?', (key,)).fetchone(), 'native_held')
        require(not n.execute('SELECT 1 FROM pipeline_x_operation WHERE source_key=?', (key,)).fetchone(), 'native_operation_evidence')
        require(not n.execute('SELECT 1 FROM pipeline_x_checkpoint WHERE source_key=?', (key,)).fetchone(), 'native_checkpoint_present')
        require(not n.execute('SELECT 1 FROM pipeline_x_artifact WHERE queue_id=?', (qid,)).fetchone()
                and not n.execute('SELECT 1 FROM x_post_random_template_preparation WHERE queue_id=?', (qid,)).fetchone(),
                'native_preparation_present')
        bindings = rows(n, 'SELECT * FROM pipeline_x_claim WHERE source_key=? ORDER BY generation', (key,))
        for binding in bindings:
            generation = binding['generation']
            require(binding['stage'] == 'prepare' and 1 <= generation <= job['generation']
                    and binding['claim_hash'] == history[generation - 1]['claim_hash']
                    and binding['queue_id'] == qid and (not binding['log_id'] or logs and binding['log_id'] == logs[0]['id']),
                    'native_binding_identity_changed')
            descriptor = json.loads(binding['descriptor_json'])
            require(descriptor.get('kind') == 'x_preflight' and not descriptor.get('artifact'), 'native_descriptor_not_unstarted')
        require(len({sha(json.loads(b['descriptor_json'])) for b in bindings}) <= 1, 'native_descriptor_changed')
        # Ignore only collector timestamps/status refresh; all frozen fields,
        # descriptors, capabilities, history and native bytes remain CAS-bound.
        stable_job = {k: v for k, v in job.items() if k not in ('updated_at', 'source_status')}
        proofs.append({**item, 'central_row_sha256': sha(stable_job), 'claim_history_sha256': sha(history),
            'failed_events_sha256': sha(failures), 'native_queue_sha256': sha(queue), 'native_log_sha256': sha(logs),
            'native_parent_identity_sha256': sha(parent_identity),
            'native_bindings_sha256': sha(bindings), 'payload_sha256': sha(json.loads(job['payload_json'])),
            'artifact_sha256': sha(json.loads(job['artifact_json'])), 'capabilities_sha256': sha(json.loads(job['capabilities_json'])),
            'scheduled_at': job['scheduled_at'], 'native_prepare_binding_count': len(bindings),
            'previous_execution': {k: job[k] for k in ('status', 'generation', 'node_id', 'worker_id',
                'started_at', 'finished_at', 'lease_expires_at', 'error_code')}})
    node_ids = [r[0] for r in c.execute('SELECT node_id FROM pipeline_node ORDER BY node_id')]
    require(node_ids, 'node_inventory_missing')
    return {'items': proofs, 'node_ids': node_ids}


def make_plan(central_path, native_path, scope, scope_sha, now=None):
    now = now or utcnow()
    with contextlib.closing(read_connection(central_path)) as c, contextlib.closing(read_connection(native_path)) as n:
        c.execute('BEGIN'); n.execute('BEGIN')
        proof = snapshot(c, n, scope, scope_sha, now)
    plan = dict(proof, kind='x_unstarted_prepare_recovery_plan', version=1, captured_at=now.isoformat(), scope=scope,
                central_path=str(Path(central_path).resolve()), native_path=str(Path(native_path).resolve()))
    plan['plan_sha256'] = document_hash(plan)
    return plan


def validate_nodes(plan, evidence, now):
    require(evidence.get('plan_sha256') == plan['plan_sha256'], 'node_evidence_wrong_plan')
    nodes = evidence.get('nodes', [])
    require(sorted(n.get('node_id') for n in nodes) == plan['node_ids'], 'node_evidence_scope')
    job_ids = sorted(x['job_id'] for x in plan['items'])
    for node in nodes:
        require(0 <= (now - date(node['captured_at'])).total_seconds() <= 60, 'node_evidence_stale')
        require(node.get('scope_job_ids') == job_ids and node.get('outbox_pending_for_scope') == 0
                and node.get('active_scope_job_ids') == [] and node.get('task_file_matches') == [], 'node_execution_evidence')


def apply_plan(central_path, native_path, plan, expected_sha, evidence, backup_dir, *, actor, clock=utcnow):
    require(plan.get('kind') == 'x_unstarted_prepare_recovery_plan' and plan.get('version') == 1
            and plan.get('plan_sha256') == expected_sha == document_hash(plan), 'plan_hash_mismatch')
    require(plan['central_path'] == str(Path(central_path).resolve()) and plan['native_path'] == str(Path(native_path).resolve()), 'database_path_changed')
    require(bool(str(actor).strip()), 'audit_actor_required')
    validate_nodes(plan, evidence, clock())
    fresh = make_plan(central_path, native_path, plan['scope'], plan['scope']['scope_sha256'], clock())
    require(all(fresh[k] == plan[k] for k in ('items', 'node_ids')), 'plan_state_drift')
    backup_dir = Path(backup_dir); backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / ('x-prepare-' + expected_sha[:16] + '-' + uuid.uuid4().hex + '.sqlite3')
    with os.fdopen(os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb'):
        pass
    with contextlib.closing(read_connection(central_path)) as source, contextlib.closing(sqlite3.connect(backup)) as dest:
        source.backup(dest)
        require(dest.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'backup_integrity_failed')
    with backup.open('r+b') as f:
        os.fsync(f.fileno())
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
    backup_sha = h.hexdigest()
    c = sqlite3.connect(str(central_path), timeout=10, isolation_level=None); c.row_factory = sqlite3.Row
    try:
        with contextlib.closing(read_connection(native_path)) as n:
            c.execute('PRAGMA synchronous=FULL'); c.execute('BEGIN IMMEDIATE'); n.execute('BEGIN')
            validate_nodes(plan, evidence, clock())
            current = snapshot(c, n, plan['scope'], plan['scope']['scope_sha256'], clock())
            require(all(current[k] == plan[k] for k in ('items', 'node_ids')), 'plan_state_drift')
            instant = clock().isoformat()
            for item in plan['items']:
                count = c.execute("UPDATE pipeline_job SET status='queued',started_at='',finished_at='',lease_expires_at='',claim_hash='',node_id='',worker_id='',updated_at=? WHERE id=? AND stage='prepare' AND status='failed' AND generation=? AND error_code='canonical_bridge_unavailable' AND external_intent=0",
                                  (instant, item['job_id'], item['generation'])).rowcount
                require(count == 1, 'recovery_update_conflict')
                details = {'plan_sha256': expected_sha, 'scope_sha256': plan['scope']['scope_sha256'], 'actor': str(actor),
                    'previous_status': 'failed', 'generation': item['generation'], 'backup_sha256': backup_sha,
                    'central_row_sha256': item['central_row_sha256'], 'native_bindings_sha256': item['native_bindings_sha256'],
                    'previous_execution': item['previous_execution'],
                    'reason': 'unstarted_x_preparation_after_bridge_fix'}
                c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                          (item['job_id'], 'audited_x_preparation_recovery', dumps(details), instant))
            c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return {'applied': True, 'plan_sha256': expected_sha, 'job_ids': [x['job_id'] for x in plan['items']],
            'backup_path': str(backup), 'backup_sha256': backup_sha, 'native_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply'), nargs='?', default='plan')
    parser.add_argument('--config', required=True); parser.add_argument('--scope'); parser.add_argument('--scope-sha256')
    parser.add_argument('--plan'); parser.add_argument('--plan-sha256'); parser.add_argument('--node-evidence')
    parser.add_argument('--backup-dir'); parser.add_argument('--actor'); parser.add_argument('--output', required=True)
    args = parser.parse_args(); config = json.loads(Path(args.config).read_text())
    # Reserve a private audit file before mutation; report failures cannot hide
    # an already committed recovery. Console output contains hashes only.
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as f:
        if args.action == 'plan':
            require(args.scope and args.scope_sha256, 'scope_required')
            result = make_plan(config['db_path'], config['paths']['x'], json.loads(Path(args.scope).read_text()), args.scope_sha256)
        else:
            require(all((args.plan, args.plan_sha256, args.node_evidence, args.backup_dir, args.actor)), 'apply_arguments_required')
            result = apply_plan(config['db_path'], config['paths']['x'], json.loads(Path(args.plan).read_text()), args.plan_sha256,
                json.loads(Path(args.node_evidence).read_text()), args.backup_dir, actor=args.actor)
        f.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n'); f.flush(); os.fsync(f.fileno())
    print(json.dumps({'ok': True, 'action': args.action, 'plan_sha256': result['plan_sha256']}))


if __name__ == '__main__':
    try:
        main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
