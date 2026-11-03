#!/usr/bin/env python3
"""Audited first publication of six existing X final artifacts; native DB is read-only.

Plan is the default. Apply never renders, uploads, calls a platform, resets a
native attempt, or removes an account fence. Normal publisher admission remains
responsible for account approval, credentials, chronology and unknown holds.
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
from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.x_posts.random_template import validate_receipt
from scripts.recover_pipeline_x_preparation import (
    RecoveryRejected, date, document_hash, dumps, read_connection, require, rows, sha, utcnow,
)

QUEUE_IDS = (5392, 5394, 5395, 5396, 5398, 5458)
FAILED_PREPARATIONS = frozenset((5392, 5394))
FAILURES = frozenset(('canonical_bridge_unavailable', 'x_dispatch_failed'))


def artifact_from_receipt(request, result):
    try:
        validate_receipt(request, result)
    except Exception as exc:
        raise RecoveryRejected('native_final_receipt_invalid') from exc
    return {'url': result['output_url'], 'sha256': result['output_sha256'], 'size': result['output_size'],
            'mime': 'video/mp4', 'duration_seconds': result['probe']['duration'],
            'profile': result['profile'], 'recipe': result['recipe']}


def history(c, job):
    found = rows(c, 'SELECT * FROM pipeline_claim_history WHERE job_id=? ORDER BY generation', (job['id'],))
    require([r['generation'] for r in found] == list(range(1, job['generation'] + 1)), 'claim_history_missing')
    require(found and (found[-1]['claim_hash'], found[-1]['node_id'], found[-1]['worker_id']) ==
            (job['claim_hash'], job['node_id'], job['worker_id']), 'claim_identity_changed')
    return found


def snapshot(c, n, now):
    items, artifacts = [], {}
    for qid in QUEUE_IDS:
        key = 'x:queue:' + str(qid)
        found = rows(n, 'SELECT * FROM x_post_queue WHERE id=?', (qid,))
        require(len(found) == 1, 'native_queue_missing'); queue = found[0]
        require(queue['status'] == 'queued' and queue['random_template_enabled']
                and queue['media_validation_mode'] == 'preflight', 'native_queue_not_prepared')
        logs = rows(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (qid,))
        require(len(logs) == 1 and logs[0]['status'] == 'reserved' and not any(logs[0][k] for k in
                ('attempt_count', 'unknown_outcome', 'x_media_id', 'x_post_id', 'started_at', 'published_at')),
                'native_publish_evidence')
        for table, column, value in (('pipeline_x_operation', 'source_key', key),
                ('x_post_repost_ledger', 'queue_id', qid), ('pipeline_canonical_hold', 'source_key', key)):
            require(not n.execute('SELECT 1 FROM ' + table + ' WHERE ' + column + '=?', (value,)).fetchone(),
                    'native_write_or_hold_evidence')
        preparations = rows(n, 'SELECT * FROM x_post_random_template_preparation WHERE queue_id=?', (qid,))
        require(len(preparations) == 1 and preparations[0]['status'] == 'ready', 'native_final_not_ready')
        prepared = preparations[0]
        request, result = json.loads(prepared['request_json']), json.loads(prepared['result_json'])
        artifact = artifact_from_receipt(request, result)
        source_proofs = rows(n, 'SELECT * FROM pipeline_x_artifact WHERE queue_id=?', (qid,))
        require(len(source_proofs) == 1, 'native_source_proof_missing')
        source_artifact = json.loads(source_proofs[0]['artifact_json'])
        require(request['queue_id'] == qid and request.get('content_id') == str(queue['content_id'])
                and request['source_sha256'] == queue['preflight_sha256']
                and request['source_size'] == queue['preflight_size']
                and source_proofs[0]['source_url'] == queue['material_url']
                and request['source_url'] == source_artifact.get('url')
                and request['source_sha256'] == source_artifact.get('sha256')
                and request['source_size'] == source_artifact.get('size')
                and abs(float(request['source_duration']) - float(queue['preflight_duration'])) <= .05,
                'native_final_source_mismatch')
        require(date(prepared['updated_at']) <= now and date(prepared['updated_at']) >= date(queue['created_at']),
                'native_final_time_invalid')
        jobs = rows(c, 'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY stage', (key,))
        require(len(jobs) == 2 and {j['stage'] for j in jobs} == {'prepare', 'publish'}, 'dispatch_stages_changed')
        prep, pub = ({j['stage']: j for j in jobs}[stage] for stage in ('prepare', 'publish'))
        require(pub['status'] == 'failed' and pub['error_code'] in FAILURES
                and pub['generation'] > 0 and pub['finished_at']
                and date(pub['finished_at']) <= now - dt.timedelta(seconds=120), 'publish_failure_not_settled')
        require(prep['status'] == ('failed' if qid in FAILED_PREPARATIONS else 'completed')
                and prep['generation'] > 0, 'prepare_status_changed')
        if qid in FAILED_PREPARATIONS:
            require(prep['error_code'] == 'canonical_bridge_unavailable' and not prep['canonical_receipt'],
                    'prepare_failure_not_exact')
        else:
            previous = json.loads(prep['artifact_json'])
            require(all(previous.get(k) == v for k, v in artifact.items()), 'completed_artifact_changed')
        require(canonical_fingerprint(queue, 'id') == json.loads(pub['source_meta_json']).get('canonical_fingerprint'),
                'native_frozen_identity_changed')
        histories, related = {}, {}
        for job in jobs:
            require((job['channel'], job['family'], job['source_id']) == ('x', 'queue', str(qid))
                    and not job['external_intent'] and not job['hold_reason']
                    and (not job['lease_expires_at'] or date(job['lease_expires_at']) <= now), 'dispatch_write_or_hold_evidence')
            meta = json.loads(job['source_meta_json'])
            require(not any(meta.get(k) for k in ('unknown_outcome', 'reconcile_only', 'source_attempt_count', 'publish_id')),
                    'dispatch_unknown_evidence')
            for table in ('pipeline_operation', 'pipeline_resource', 'pipeline_legacy_resource'):
                require(not c.execute('SELECT 1 FROM ' + table + ' WHERE job_id=?', (job['id'],)).fetchone(),
                        'dispatch_operation_or_active_fence')
            if job['stage'] == 'publish':
                require(not job['canonical_receipt'], 'publish_receipt_present')
                for table in ('pipeline_checkpoint', 'pipeline_receipt'):
                    require(not c.execute('SELECT 1 FROM ' + table + ' WHERE job_id=?', (job['id'],)).fetchone(),
                            'publish_receipt_present')
            histories[job['stage']] = history(c, job)
            related[job['stage']] = {table: rows(c, 'SELECT * FROM ' + table + ' WHERE job_id=?', (job['id'],))
                for table in ('pipeline_checkpoint', 'pipeline_receipt', 'pipeline_delivery', 'pipeline_phase_metric')}
        require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (key,)).fetchone(), 'publication_present')
        bindings = rows(n, 'SELECT * FROM pipeline_x_claim WHERE source_key=? ORDER BY stage,generation', (key,))
        for binding in bindings:
            stage, gen = binding['stage'], binding['generation']
            require(stage in histories and 1 <= gen <= len(histories[stage])
                    and binding['claim_hash'] == histories[stage][gen - 1]['claim_hash']
                    and binding['queue_id'] == qid and binding['log_id'] == logs[0]['id'], 'native_binding_changed')
            desc = json.loads(binding['descriptor_json'])
            if stage == 'publish':
                require(desc.get('kind') == 'x_publish' and all(desc.get('artifact', {}).get(k) == v for k, v in artifact.items()),
                        'frozen_publish_artifact_changed')
        checkpoints = rows(n, 'SELECT * FROM pipeline_x_checkpoint WHERE source_key=? ORDER BY generation,phase', (key,))
        require(all(r['phase'] == 'x_preflight' for r in checkpoints), 'native_publish_checkpoint_present')
        parent = []
        for field, table in (('manual_run_id', 'x_post_manual_run'), ('schedule_run_id', 'x_post_schedule_run')):
            if queue.get(field):
                parent.extend(rows(n, 'SELECT * FROM ' + table + ' WHERE id=?', (queue[field],)))
        failures = rows(c, "SELECT * FROM pipeline_event WHERE job_id IN (?,?) AND event_type='failed' ORDER BY id", (prep['id'], pub['id']))
        require(any(r['job_id'] == pub['id'] and json.loads(r['details_json']).get('code') == pub['error_code']
                    and not json.loads(r['details_json']).get('unknown') for r in failures), 'failure_fact_missing')
        stable = [{k: v for k, v in j.items() if k not in ('updated_at', 'source_status')} for j in jobs]
        item = {'queue_id': qid, 'source_key': key, 'job_id': pub['id'], 'generation': pub['generation'],
                'prepare_job_id': prep['id'], 'prepare_generation': prep['generation'],
                'correct_prepare_outcome': qid in FAILED_PREPARATIONS,
                'central_rows_sha256': sha(stable), 'native_queue_sha256': sha(queue),
                'native_logs_sha256': sha(logs), 'native_parents_sha256': sha(parent),
                'native_preparation_sha256': sha(preparations), 'native_bindings_sha256': sha(bindings),
                'native_source_proof_sha256': sha(source_proofs),
                'native_checkpoints_sha256': sha(checkpoints), 'claim_history_sha256': sha(histories),
                'related_receipts_sha256': sha(related), 'failed_events_sha256': sha(failures),
                'artifact_sha256': sha(artifact), 'final_sha256': artifact['sha256'], 'final_size': artifact['size'],
                'final_profile': artifact['profile'], 'native_finished_at': prepared['updated_at'],
                'scheduled_at': pub['scheduled_at'],
                'previous_execution': {j['stage']: {k: j[k] for k in ('status', 'generation', 'node_id', 'worker_id',
                    'started_at', 'finished_at', 'lease_expires_at', 'error_code')} for j in jobs}}
        items.append(item); artifacts[qid] = artifact
    node_ids = [r[0] for r in c.execute('SELECT node_id FROM pipeline_node ORDER BY node_id')]
    require(node_ids, 'node_inventory_missing')
    proof = {'items': items, 'node_ids': node_ids,
             'evidence_job_ids': sorted(j for i in items for j in (i['job_id'], i['prepare_job_id']))}
    return proof, artifacts


def make_plan(central_path, native_path, now=None):
    now = now or utcnow()
    with contextlib.closing(read_connection(central_path)) as c, contextlib.closing(read_connection(native_path)) as n:
        c.execute('BEGIN'); n.execute('BEGIN'); proof, _ = snapshot(c, n, now)
    plan = dict(proof, kind='x_existing_artifact_first_publish_plan', version=1, captured_at=now.isoformat(),
                central_path=str(Path(central_path).resolve()), native_path=str(Path(native_path).resolve()))
    plan['plan_sha256'] = document_hash(plan)
    return plan


def validate_nodes(plan, evidence, now):
    require(evidence.get('plan_sha256') == plan['plan_sha256'], 'node_evidence_wrong_plan')
    nodes = evidence.get('nodes', [])
    require(sorted(n.get('node_id') for n in nodes) == plan['node_ids'], 'node_evidence_scope')
    for node in nodes:
        require(0 <= (now - date(node['captured_at'])).total_seconds() <= 60, 'node_evidence_stale')
        require(node.get('scope_job_ids') == plan['evidence_job_ids'] and node.get('outbox_pending_for_scope') == 0
                and node.get('active_scope_job_ids') == [], 'node_execution_evidence')


def apply_plan(central_path, native_path, plan, expected_sha, evidence, backup_dir, *, actor, clock=utcnow):
    require(plan.get('kind') == 'x_existing_artifact_first_publish_plan' and plan.get('version') == 1
            and plan.get('plan_sha256') == expected_sha == document_hash(plan), 'plan_hash_mismatch')
    require(plan['central_path'] == str(Path(central_path).resolve()) and plan['native_path'] == str(Path(native_path).resolve()),
            'database_path_changed')
    require(bool(str(actor).strip()), 'audit_actor_required')
    validate_nodes(plan, evidence, clock())
    fresh = make_plan(central_path, native_path, clock())
    keys = ('items', 'node_ids', 'evidence_job_ids')
    require(all(fresh[k] == plan[k] for k in keys), 'plan_state_drift')
    backup_dir = Path(backup_dir); backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / ('x-prepared-publish-' + expected_sha[:16] + '-' + uuid.uuid4().hex + '.sqlite3')
    with os.fdopen(os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb'): pass
    with contextlib.closing(read_connection(central_path)) as source, contextlib.closing(sqlite3.connect(backup)) as dest:
        source.backup(dest); require(dest.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'backup_integrity_failed')
    with backup.open('r+b') as f:
        os.fsync(f.fileno()); digest = hashlib.sha256()
        for chunk in iter(lambda: f.read(1024 * 1024), b''): digest.update(chunk)
    backup_sha = digest.hexdigest()
    c = sqlite3.connect(str(central_path), timeout=10, isolation_level=None); c.row_factory = sqlite3.Row
    try:
        with contextlib.closing(read_connection(native_path)) as n:
            c.execute('PRAGMA synchronous=FULL'); c.execute('BEGIN IMMEDIATE'); n.execute('BEGIN')
            validate_nodes(plan, evidence, clock()); current, artifacts = snapshot(c, n, clock())
            require(all(current[k] == plan[k] for k in keys), 'plan_state_drift')
            instant = clock().isoformat()
            for item in plan['items']:
                artifact = artifacts[item['queue_id']]
                if item['correct_prepare_outcome']:
                    proof = dumps({'source_key': item['source_key'], 'kind': 'audited_native_final_artifact',
                        'artifact_sha256': item['artifact_sha256'], 'native_preparation_sha256': item['native_preparation_sha256']})
                    count = c.execute("UPDATE pipeline_job SET status='completed',artifact_json=?,canonical_receipt=?,"
                        "finished_at=?,started_at=CASE WHEN julianday(started_at)<=julianday(?) THEN started_at ELSE '' END,"
                        "lease_expires_at='',claim_hash='',error_code='',error_message='',updated_at=? "
                        "WHERE id=? AND stage='prepare' AND status='failed' AND generation=? AND external_intent=0",
                        (dumps(artifact), proof, item['native_finished_at'], item['native_finished_at'], instant,
                         item['prepare_job_id'], item['prepare_generation'])).rowcount
                    require(count == 1, 'prepare_correction_conflict')
                count = c.execute("UPDATE pipeline_job SET status='ready',artifact_json=?,started_at='',finished_at='',"
                    "lease_expires_at='',claim_hash='',node_id='',worker_id='',updated_at=? "
                    "WHERE id=? AND stage='publish' AND status='failed' AND generation=? AND external_intent=0",
                    (dumps(artifact), instant, item['job_id'], item['generation'])).rowcount
                require(count == 1, 'publish_recovery_conflict')
                audit = {'plan_sha256': expected_sha, 'actor': str(actor), 'backup_sha256': backup_sha,
                    'source_key': item['source_key'], 'artifact_sha256': item['artifact_sha256'],
                    'final_sha256': item['final_sha256'], 'final_size': item['final_size'], 'final_profile': item['final_profile'],
                    'central_rows_sha256': item['central_rows_sha256'], 'native_preparation_sha256': item['native_preparation_sha256'],
                    'previous_execution': item['previous_execution'], 'reason': 'existing_final_artifact_first_publication'}
                if item['correct_prepare_outcome']:
                    c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                        (item['prepare_job_id'], 'audited_native_preparation_confirmed', dumps(audit), instant))
                c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                    (item['job_id'], 'audited_x_first_publish_recovery', dumps(audit), instant))
            c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return {'applied': True, 'plan_sha256': expected_sha, 'job_ids': [i['job_id'] for i in plan['items']],
            'prepare_confirmed_ids': [i['prepare_job_id'] for i in plan['items'] if i['correct_prepare_outcome']],
            'backup_path': str(backup), 'backup_sha256': backup_sha, 'native_modified': False, 'platform_called': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply'), nargs='?', default='plan')
    parser.add_argument('--config', required=True); parser.add_argument('--plan'); parser.add_argument('--plan-sha256')
    parser.add_argument('--node-evidence'); parser.add_argument('--backup-dir'); parser.add_argument('--actor')
    parser.add_argument('--output', required=True); args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as f:
        if args.action == 'plan': result = make_plan(config['db_path'], config['paths']['x'])
        else:
            require(all((args.plan, args.plan_sha256, args.node_evidence, args.backup_dir, args.actor)), 'apply_arguments_required')
            result = apply_plan(config['db_path'], config['paths']['x'], json.loads(Path(args.plan).read_text()),
                args.plan_sha256, json.loads(Path(args.node_evidence).read_text()), args.backup_dir, actor=args.actor)
        f.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n'); f.flush(); os.fsync(f.fileno())
    print(json.dumps({'ok': True, 'action': args.action, 'plan_sha256': result['plan_sha256']}))


if __name__ == '__main__':
    try: main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
