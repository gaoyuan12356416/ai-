#!/usr/bin/env python3
"""Reconcile only audited q5524/generation 1, preserving its original unknown.

This is not an unknown retry policy. Exact historical worker, terminal ACK,
operation ledgers and frozen identity prove this one execution stopped before
Post. Admission uses the existing one-generation pre-Post runtime authority.
"""
import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.adapters import canonical_fingerprint
from features.pipeline_dispatch.x_prepost_recovery import (
    TABLE as AUTH, SCHEMA as AUTH_SCHEMA, digest, encoded, queue_hash, log_hash, no_post_operation,
)
from scripts.recover_x_prepost_failures import backup, media_facts, stable_job, writable
from scripts.recover_pipeline_x_preparation import RecoveryRejected, require, read_connection, rows, date, document_hash, utcnow

QID, LOG_ID, PREP_ID, JOB_ID, ACCOUNT_ID, PARENT_ID = 5524, 5475, 31955, 31993, 43, 1563
KEY = 'x:queue:5524'
WORKER = 'hk-gpu-publish-0-ff381fa0'
WORKER_PID = 198472
RELEASE = 'ec8984773c4bc33e832ec5ae483cb3dcad7482dc'
RECEIPT_ID = 'b52ca6ec1895491f972ee928b9b952d5'
RECEIPT_SHA = '5bdb598791564bc592abc33d4e74e8eba3d3b7eed788b0d65c84d7ed12c95d8a'
# Original packets rechecked read-only on HK GPU. Hash both the original packet
# and its credential-redacted representation; operator files contain no bearer.
EXPECTED_RECEIPTS = {
    '979229e188d0cfb36ef2e73450b9ffa1573255dee18660e5b1c82f67ccac0ef6': ('jobs/checkpoint', 'e11dc3d57c9cde5d9a421f790c1cae898d278e652617c09b465521787296099d', '7e4c87714ff8bc44212667e9544e5c4f8b00cef7fc165168f447f20057667b1e'),
    'ec897e1c688a466f9bff2885ba641cce': ('jobs/complete', '917bd55da36cc5343edf3cb74407c2320547668e7c7b42b84e233a22b1892afc', '53367c6201e9b4c40d779d8a2fc4d1e1a99d6a9c3c0a30f60986bc4850e477fa'),
    '7eb75d61d0e4ea716105cb53acb7b5bbf50402939baabedf469bfa313995bc95': ('jobs/checkpoint', '407ae42c91049004d45d502463643954fe5850dd15479509118fcd160da47bd5', 'bd11bf0ebaa241ac17d3e1b34b548a0867a64679f7dbf256c7bc017b4c263dc2'),
    RECEIPT_ID: ('jobs/fail', RECEIPT_SHA, '70638fc0411c336ca1da1a05d41ef81fadadfdb0329eb16eb72ea55169474f9b'),
}
CLAIM_SHA = '27123d5069926ac14bfc4095b147771d9b1b460bd87fd8798e9441bfc2be982b'
HISTORY_DIR = '/data/pipeline-dispatch/maintenance/gpu-offload-20261103T090605Z'
RELEASE_ROOT = '/opt/pipeline-dispatch/releases/' + RELEASE
HISTORY_FILES = {
    'audit.json': '1fa01009a25a8be040ea0219efa481fb2163f977668e2d77ddcca44102e203a0',
    'node.service.before': '7ffe0db0e10edfd1dbb4652edebe2e5bde93e36428688dbdf21ef81187f80e7e',
}
RELEASE_FILES = {
    'features/pipeline_dispatch/worker.py': '7abc1300e57bc515a504d9295708b3d449e40c1ad9ba1dff6e6eb76a8e160b06',
    'features/pipeline_dispatch/executors.py': '0def164d2639687c3b7af3440de00693122fdf4fcfd0b999c0b88f1a05c2885f',
    'features/x_posts/service.py': '62bc80ea7b1b552b9e95744b8d5757611b04504979b20728be4b93a863d4a163',
}
EXPECTED = {
    'queue': '6966f39c0e5c722de8f56f494373523da1893325f2487c0d48bcab48648ee9b8',
    'log': '34205863744b8daa1e90a0663eb774496059d9ae68fb6d808ea4d238068dfd07',
    'native_ops': 'ce0cd105810bc63dcfaee647e4a3a351bb4b571c21d12abebc34f98f6390e878',
    'central_ops': '6e633ac2c5955be7b281068fad952e693ae418b252f5f19f85e7c1276db69e5f',
    'history': 'a8228525f12ee2fdf733f807ee27d4afd9caba4644d6acfb095becc985cc03f9',
    'claims': 'de76a2158082694113b9db23b6a6ba98becab04998e67f77be798435b0483658',
}
HOLD = 'operator_5524_verified_prepost_pending'
AUDIT = 'pipeline_x_5524_no_post_audit'
JOURNAL = 'pipeline_x_5524_reconciliation_apply'
AUDIT_SCHEMA = 'CREATE TABLE IF NOT EXISTS '+AUDIT+' (job_id INTEGER PRIMARY KEY,plan_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)'
JOURNAL_SCHEMA = 'CREATE TABLE IF NOT EXISTS '+JOURNAL+' (job_id INTEGER PRIMARY KEY,plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,backups_json TEXT NOT NULL,updated_at TEXT NOT NULL)'


def one(c, sql, params=()):
    result = rows(c, sql, params)
    require(len(result) == 1, 'exact_row_missing')
    return result[0]


def exists(c, table):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def collect(c, n):
    q = one(n, 'SELECT * FROM x_post_queue WHERE id=?', (QID,))
    logs = rows(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (QID,))
    require(len(logs) == 1, 'log_not_unique')
    jobs = rows(c, 'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY stage', (KEY,))
    require(len(jobs) == 2 and {j['id'] for j in jobs} == {PREP_ID, JOB_ID}, 'job_scope_changed')
    for job in jobs:
        require(json.loads(job['source_meta_json']).get('canonical_fingerprint') == canonical_fingerprint(q, 'id'), 'canonical_identity_changed')
    proof = {'queue': q, 'log': logs[0], 'jobs': jobs,
        'parent': one(n, 'SELECT * FROM x_post_manual_run WHERE id=?', (PARENT_ID,)),
        'native_ops': rows(n, 'SELECT * FROM pipeline_x_operation WHERE source_key=? ORDER BY operation_id', (KEY,)),
        'claims': rows(n, 'SELECT * FROM pipeline_x_claim WHERE source_key=? ORDER BY stage,generation', (KEY,)),
        'native_checkpoints': rows(n, 'SELECT * FROM pipeline_x_checkpoint WHERE source_key=? ORDER BY generation,phase', (KEY,)),
        'media': media_facts(n, QID),
        'holds': rows(n, 'SELECT * FROM pipeline_canonical_hold WHERE source_key=?', (KEY,)),
        'reposts': rows(n, 'SELECT * FROM x_post_repost_ledger WHERE queue_id=?', (QID,)),
        'publications': rows(c, 'SELECT * FROM pipeline_publication WHERE source_key=?', (KEY,)),
        'siblings': rows(n, 'SELECT id FROM x_post_queue WHERE manual_run_id=? ORDER BY id', (PARENT_ID,)),
    }
    for name, table, order in (
        ('central_ops', 'pipeline_operation', 'operation_id'), ('history', 'pipeline_claim_history', 'job_id,generation'),
        ('central_checkpoints', 'pipeline_checkpoint', 'checkpoint_id'), ('receipts', 'pipeline_receipt', 'receipt_id'),
        ('deliveries', 'pipeline_delivery', 'delivery_id'), ('events', 'pipeline_event', 'id'),
        ('resources', 'pipeline_resource', 'resource_key'), ('legacy_resources', 'pipeline_legacy_resource', 'resource_key')):
        proof[name] = rows(c, 'SELECT * FROM '+table+' WHERE job_id IN (?,?) ORDER BY '+order, (PREP_ID, JOB_ID))
    return proof


def initial_proof(c, n, now):
    p = collect(c, n); q, log = p['queue'], p['log']
    require(queue_hash(q) == EXPECTED['queue'] and digest(log) == EXPECTED['log'], 'exact_original_identity_changed')
    require((q['status'], q['account_id'], q['manual_run_id'], q['delivery_mode']) == ('failed', ACCOUNT_ID, PARENT_ID, 'direct')
            and (log['id'], log['status'], log['attempt_count'], log['unknown_outcome']) == (LOG_ID, 'failed', 1, 1)
            and log['error_code'] == log['error_message'] == 'pipeline_lease_lost'
            and not any(log[k] for k in ('x_media_id', 'x_post_id', 'published_at'))
            and all(log[k] for k in ('post_text', 'long_url', 'short_url')), 'not_exact_original_unknown')
    jobs = {j['stage']: j for j in p['jobs']}; pub, prep = jobs['publish'], jobs['prepare']
    require(prep['id'] == PREP_ID and prep['status'] == 'completed' and pub['id'] == JOB_ID
            and pub['generation'] == 1 and pub['status'] == 'unknown' and pub['node_id'] == 'hk-gpu'
            and pub['worker_id'] == WORKER and pub['claim_hash'] == CLAIM_SHA and pub['external_intent'] == 1
            and pub['error_code'] == 'pipeline_lease_lost' and not pub['hold_reason'] and not pub['canonical_receipt'], 'original_execution_changed')
    require(all(not j['lease_expires_at'] or date(j['lease_expires_at']) < now for j in p['jobs'])
            and date(log['updated_at']) < now - dt.timedelta(seconds=120), 'execution_not_settled')
    for name in ('native_ops', 'central_ops', 'claims', 'history'):
        require(digest(p[name]) == EXPECTED[name], 'historical_'+name+'_changed')
    require(len(p['native_ops']) == 83 and all(r['generation'] == 1 and no_post_operation(r['operation']) for r in p['native_ops']), 'not_exact_media_only_operations')
    fields = ('operation_id', 'generation', 'operation', 'request_sha256')
    require({tuple(r[k] for k in fields) for r in p['native_ops']} == {tuple(r[k] for k in fields) for r in p['central_ops']}, 'operation_ledgers_disagree')
    require(not p['holds'] and not p['reposts'] and not p['publications'] and not p['legacy_resources'], 'other_hold_or_publication')
    require(p['resources'] == [{'resource_key': 'x:account:'+str(ACCOUNT_ID), 'job_id': JOB_ID, 'generation': 1, 'quarantined': 1, 'legacy': 0}], 'quarantine_resource_changed')
    require(all(r['phase'] in ('x_preflight', 'x_media_initialized') for r in p['native_checkpoints']+p['central_checkpoints']), 'later_checkpoint')
    delivery = [r for r in p['deliveries'] if r['delivery_id'] == RECEIPT_ID]
    require(len(delivery) == 1 and delivery[0]['job_id'] == JOB_ID and delivery[0]['generation'] == 1
            and delivery[0]['claim_hash'] == CLAIM_SHA and delivery[0]['request_sha256'] == RECEIPT_SHA
            and json.loads(delivery[0]['response_json']) == {'accepted': True, 'status': 'unknown'}, 'original_terminal_delivery_missing')
    failures = [r for r in p['events'] if r['event_type'] == 'failed']
    require(len(failures) == 1 and failures[0]['job_id'] == JOB_ID
            and json.loads(failures[0]['details_json']) == {'code': 'pipeline_lease_lost', 'unknown': True}, 'historical_failure_changed')
    parent = p['parent']
    require(parent['trigger_source'] == 'auto_template' and parent['status'] == 'needs_review'
            and (parent['expected_count'], parent['queued_count'], parent['unknown_count'], parent['published_count'], parent['failed_count']) == (1, 1, 1, 0, 0)
            and not parent['error_code'] and not parent['error_message'] and p['siblings'] == [{'id': QID}], 'parent_scope_changed')
    descriptor = json.loads(next(r['descriptor_json'] for r in p['claims'] if r['stage'] == 'publish'))
    artifact = descriptor['artifact']
    require(descriptor['kind'] == 'x_publish' and descriptor['request']['body'] == log['post_text']
            and descriptor['account_resource'] == 'x:account:'+str(ACCOUNT_ID), 'frozen_descriptor_changed')
    if q['random_template_enabled']:
        from features.x_posts.random_template import validate_receipt
        render = p['media']['x_post_random_template_preparation']
        require(len(render) == 1 and render[0]['status'] == 'ready', 'render_not_ready')
        request, result = json.loads(render[0]['request_json']), json.loads(render[0]['result_json'])
        validate_receipt(request, result)
        expected = {'url': result['output_url'], 'sha256': result['output_sha256'], 'size': result['output_size'],
            'mime': 'video/mp4', 'duration_seconds': result['probe']['duration'], 'profile': result['profile'], 'recipe': result['recipe']}
        require(expected == artifact and request['queue_id'] == QID, 'render_identity_changed')
    require(artifact.get('sha256') and artifact.get('size'), 'artifact_missing')
    require(not (exists(n, AUTH) and n.execute('SELECT 1 FROM '+AUTH+' WHERE source_key=?', (KEY,)).fetchone()), 'already_authorized')
    return p


def plan(central, native, now=None):
    now = now or utcnow()
    with contextlib.closing(read_connection(central)) as c, contextlib.closing(read_connection(native)) as n:
        c.execute('BEGIN'); n.execute('BEGIN')
        require([r[0] for r in c.execute('SELECT node_id FROM pipeline_node ORDER BY node_id')] == ['cpu-main', 'hk-gpu'], 'node_inventory_changed')
        proof = initial_proof(c, n, now)
    result = {'kind': 'x_5524_verified_no_post', 'version': 1, 'central': str(Path(central).resolve()),
        'native': str(Path(native).resolve()), 'proof': proof, 'captured_at': now.isoformat()}
    result['plan_sha256'] = document_hash(result)
    return result


def validate_plan(saved):
    require(saved.get('kind') == 'x_5524_verified_no_post' and saved.get('version') == 1
            and saved.get('plan_sha256') == document_hash(saved)
            and saved['proof']['queue']['id'] == QID and saved['proof']['log']['id'] == LOG_ID, 'plan_changed')


def historical_worker(history_dir=HISTORY_DIR, release_root=RELEASE_ROOT):
    checked = {}
    for base, files in ((history_dir, HISTORY_FILES), (release_root, RELEASE_FILES)):
        for rel, expected in files.items():
            actual = hashlib.sha256((Path(base)/rel).read_bytes()).hexdigest()
            require(actual == expected, 'historical_worker_file_changed')
            checked[rel] = actual
    audit = json.loads((Path(history_dir)/'audit.json').read_text())
    require(audit['previous_sha'] == RELEASE and str(WORKER_PID) in audit['children_before'], 'worker_pid_release_not_bound')
    return {'release': RELEASE, 'worker_id': WORKER, 'pid': WORKER_PID, 'files': checked}


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def node_evidence(saved, node_id, outbox, now=None, *, history_dir=HISTORY_DIR, release_root=RELEASE_ROOT):
    validate_plan(saved); require(node_id in ('cpu-main', 'hk-gpu'), 'node_scope')
    result = {'node_id': node_id, 'plan_sha256': saved['plan_sha256'], 'captured_at': (now or utcnow()).isoformat(), 'receipts': []}
    with contextlib.closing(read_connection(outbox)) as c:
        for row in c.execute('SELECT id,route,payload,acked_at FROM receipt_outbox ORDER BY id'):
            payload = json.loads(row['payload'])
            if payload.get('job_id') not in (PREP_ID, JOB_ID): continue
            safe = dict(payload)
            if 'claim_token' in safe:
                safe['claim_token_sha256'] = hashlib.sha256(str(safe.pop('claim_token')).encode()).hexdigest()
            result['receipts'].append({'id': row['id'], 'route': row['route'], 'acked_at': row['acked_at'],
                'payload_sha256': digest(payload), 'payload': safe})
    if node_id == 'hk-gpu': result['historical_worker'] = historical_worker(history_dir, release_root)
    return result


def validate_nodes(saved, evidence, now):
    require(sorted(e['node_id'] for e in evidence) == ['cpu-main', 'hk-gpu'], 'both_nodes_required')
    failures = []
    for e in evidence:
        require(e['plan_sha256'] == saved['plan_sha256'] and 0 <= (now-date(e['captured_at'])).total_seconds() <= 120, 'node_evidence_stale')
        expected = EXPECTED_RECEIPTS if e['node_id'] == 'hk-gpu' else {}
        require(len(e['receipts']) == len(expected) and {r['id'] for r in e['receipts']} == set(expected), 'historical_receipt_set_changed')
        for row in e['receipts']:
            p = row['payload']
            require((row['route'], row['payload_sha256'], digest(p)) == expected[row['id']], 'historical_receipt_changed')
            require(p['job_id'] in (PREP_ID, JOB_ID) and p['generation'] == 1 and row['acked_at'] is not None, 'pending_or_other_generation_receipt')
            require(p.get('phase') not in ('x_media_uploaded', 'x_source_published', 'x_target_reposted')
                    and not (p.get('result') or {}).get('post_id'), 'later_or_success_receipt')
            if row['route'] == 'jobs/fail' or p.get('unknown_outcome'):
                require(e['node_id'] == 'hk-gpu' and row['id'] == RECEIPT_ID and row['route'] == 'jobs/fail'
                        and row['payload_sha256'] == RECEIPT_SHA and p['job_id'] == JOB_ID
                        and p['unknown_outcome'] is True and p['error_code'] == 'pipeline_lease_lost'
                        and p['worker_id'] == WORKER and p['claim_token_sha256'] == CLAIM_SHA, 'not_original_unknown_receipt')
                failures.append(row)
    require(len(failures) == 1, 'original_ack_missing')
    worker = next(e for e in evidence if e['node_id'] == 'hk-gpu').get('historical_worker')
    require(worker == {'release': RELEASE, 'worker_id': WORKER, 'pid': WORKER_PID, 'files': dict(HISTORY_FILES, **RELEASE_FILES)}, 'historical_worker_proof_changed')


def comparable(proof):
    p = copy.deepcopy(proof)
    p['jobs'] = [stable_job(j) for j in p['jobs']]
    return p


def assert_state(saved, c, n, native_armed=False):
    old = saved['proof']; fresh = collect(c, n)
    pub = next(j for j in fresh['jobs'] if j['id'] == JOB_ID)
    require(pub['hold_reason'] == HOLD, 'central_reconciliation_hold_missing')
    pub['hold_reason'] = next(j for j in old['jobs'] if j['id'] == JOB_ID)['hold_reason']
    if native_armed:
        require(fresh['queue']['status'] == 'queued' and fresh['log']['status'] == 'reserved'
                and fresh['log']['unknown_outcome'] == 0 and fresh['log']['attempt_count'] == 1, 'armed_native_state_changed')
        for key in ('status', 'updated_at'): fresh['queue'][key] = old['queue'][key]
        for key in ('status', 'unknown_outcome', 'updated_at'): fresh['log'][key] = old['log'][key]
        parent = fresh['parent']
        require(parent['status'] == 'running' and parent['unknown_count'] == 0 and parent['published_count'] == 0
                and parent['failed_count'] == 0 and parent['queued_count'] == 1, 'armed_parent_changed')
        for key in ('status', 'unknown_count', 'updated_at', 'finished_at'):
            parent[key] = old['parent'][key]
    require(comparable(fresh) == comparable(old), 'proof_state_drift')


def current_authorization(saved, n, *, allow_missing=False):
    found = rows(n, 'SELECT * FROM '+AUTH+' WHERE source_key=?', (KEY,))
    if not found and allow_missing:
        return None
    require(len(found) == 1, 'authorization_changed')
    auth = found[0]
    p = saved['proof']
    descriptor = json.loads(next(r['descriptor_json'] for r in p['claims'] if r['stage'] == 'publish'))
    expected = {'source_key': KEY, 'stage': 'publish', 'from_generation': 1, 'to_generation': 2,
        'prior_attempts': 1, 'queue_sha256': queue_hash(p['queue']), 'log_sha256': log_hash(p['log']),
        'artifact_sha256': digest(descriptor['artifact']), 'plan_sha256': saved['plan_sha256'],
        'state': 'armed', 'started_at': '', 'proof_json': encoded({'reconciliation': AUDIT, 'original_proof': p})}
    require(all(auth[k] == value for k, value in expected.items()), 'authorization_changed')
    audit = one(n, 'SELECT * FROM '+AUDIT+' WHERE job_id=?', (JOB_ID,))
    require(audit['plan_sha256'] == saved['plan_sha256'] and auth['actor'] == json.loads(audit['evidence_json'])['actor']
            and auth['created_at'] == audit['created_at'], 'authorization_audit_changed')
    return auth


def append_audit(c, saved, evidence, backups, actor, now):
    c.execute(AUDIT_SCHEMA)
    for action in ('UPDATE', 'DELETE'):
        c.execute('CREATE TRIGGER IF NOT EXISTS '+AUDIT+'_no_'+action.lower()+' BEFORE '+action+' ON '+AUDIT+" BEGIN SELECT RAISE(ABORT,'5524_audit_append_only'); END")
    c.execute('CREATE TRIGGER IF NOT EXISTS '+AUDIT+'_no_replace BEFORE INSERT ON '+AUDIT+
              ' WHEN EXISTS(SELECT 1 FROM '+AUDIT+" WHERE job_id=NEW.job_id) BEGIN SELECT RAISE(ABORT,'5524_audit_append_only'); END")
    original = c.execute('SELECT plan_sha256 FROM '+AUDIT+' WHERE job_id=?', (JOB_ID,)).fetchone()
    if original:
        require(original[0] == saved['plan_sha256'], 'different_reconciliation_audit')
        return
    body = {'plan': saved, 'node_evidence': evidence, 'backups': backups, 'actor': actor,
        'finding': 'exact_generation_stopped_in_media_upload_before_Post', 'original_unknown_preserved': True}
    c.execute('INSERT INTO '+AUDIT+' VALUES(?,?,?,?)', (JOB_ID, saved['plan_sha256'], encoded(body), now))


def apply(saved, evidence, backup_dir, actor, now=None, stop_after=None):
    clock = (lambda: now) if now is not None else utcnow
    validate_plan(saved); started = clock(); timestamp = started.isoformat()
    require(isinstance(actor, str) and actor.strip(), 'actor_required')
    with contextlib.closing(read_connection(saved['central'])) as c:
        journal = c.execute('SELECT * FROM '+JOURNAL+' WHERE job_id=?', (JOB_ID,)).fetchone() if exists(c, JOURNAL) else None
        if journal:
            require(journal['plan_sha256'] == saved['plan_sha256'], 'different_reconciliation_plan')
            if journal['state'] == 'committed': return {'already_applied': True, 'queue_id': QID, 'platform_called': False}
    validate_nodes(saved, evidence, clock())
    backups = json.loads(journal['backups_json']) if journal else []
    if not journal:
        for label in ('central', 'native'):
            path = backup(saved[label], backup_dir, '5524-'+label)
            backups.append({'database': label, 'path': path, 'sha256': file_hash(path)})
    with contextlib.closing(writable(saved['central'])) as c, contextlib.closing(writable(saved['native'])) as n:
        c.execute('BEGIN IMMEDIATE'); n.execute('BEGIN')
        try:
            validate_nodes(saved, evidence, clock())
            c.execute(JOURNAL_SCHEMA)
            journal = c.execute('SELECT * FROM '+JOURNAL+' WHERE job_id=?', (JOB_ID,)).fetchone()
            if not journal:
                require(comparable(initial_proof(c, n, clock())) == comparable(saved['proof']), 'plan_state_drift')
                append_audit(c, saved, evidence, backups, actor, timestamp)
                c.execute('INSERT INTO '+JOURNAL+' VALUES(?,?,?,?,?)', (JOB_ID, saved['plan_sha256'], 'staged', encoded(backups), timestamp))
                changed = c.execute("UPDATE pipeline_job SET hold_reason=? WHERE id=? AND generation=1 AND status='unknown' AND hold_reason=''", (HOLD, JOB_ID)).rowcount
                require(changed == 1, 'central_stage_cas_failed')
            else:
                require(journal['plan_sha256'] == saved['plan_sha256'] and journal['state'] == 'staged', 'journal_state_changed')
            c.commit(); n.rollback()
        except BaseException: c.rollback(); n.rollback(); raise
        if stop_after == 'central_staged': raise RecoveryRejected('injected_after_central_stage')
        # Hold the central write lock while arming native. No coordinator permit,
        # new claim or quarantine change can race the two-ledger comparison.
        c.execute('BEGIN IMMEDIATE'); n.execute('BEGIN IMMEDIATE')
        try:
            validate_nodes(saved, evidence, clock())
            n.execute(AUTH_SCHEMA)
            auth = current_authorization(saved, n, allow_missing=True)
            assert_state(saved, c, n, native_armed=bool(auth))
            if not auth:
                append_audit(n, saved, evidence, backups, actor, timestamp)
                p = saved['proof']; descriptor = json.loads(next(r['descriptor_json'] for r in p['claims'] if r['stage'] == 'publish'))
                n.execute('INSERT INTO '+AUTH+' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (KEY, 'publish', 1, 2, 1, queue_hash(p['queue']), log_hash(p['log']), digest(descriptor['artifact']),
                     saved['plan_sha256'], 'armed', actor, timestamp, '', encoded({'reconciliation': AUDIT, 'original_proof': p})))
                changed = n.execute("UPDATE x_post_publish_log SET status='reserved',unknown_outcome=0,updated_at=? WHERE id=? AND status='failed' AND attempt_count=1 AND unknown_outcome=1", (timestamp, LOG_ID)).rowcount
                require(changed == 1, 'native_log_cas_failed')
                require(n.execute("UPDATE x_post_queue SET status='queued',updated_at=? WHERE id=? AND status='failed'", (timestamp, QID)).rowcount == 1, 'native_queue_cas_failed')
                from features.x_posts.service import XPostStore
                XPostStore.__new__(XPostStore)._sync_run(n, QID, timestamp)
                n.execute("UPDATE x_post_manual_run SET status='running',updated_at=? WHERE id=? AND trigger_source='auto_template' AND status='queued'", (timestamp, PARENT_ID))
            n.commit(); c.rollback()
        except BaseException: n.rollback(); c.rollback(); raise
        if stop_after == 'native_armed': raise RecoveryRejected('injected_after_native_arm')
        c.execute('BEGIN IMMEDIATE'); n.execute('BEGIN IMMEDIATE')
        try:
            validate_nodes(saved, evidence, clock())
            assert_state(saved, c, n, native_armed=True)
            current_authorization(saved, n)
            current = one(c, 'SELECT * FROM pipeline_job WHERE id=?', (JOB_ID,))
            meta = json.loads(current['source_meta_json']); meta.update(reconcile_only=False, legacy_inflight=False, unknown_outcome=False)
            changed = c.execute("UPDATE pipeline_job SET status='ready',hold_reason='',external_intent=0,started_at='',finished_at='',lease_expires_at='',claim_hash='',node_id='',worker_id='',source_meta_json=?,updated_at=? WHERE id=? AND generation=1 AND status='unknown' AND hold_reason=?", (encoded(meta), timestamp, JOB_ID, HOLD)).rowcount
            require(changed == 1, 'central_admission_cas_failed')
            require(c.execute('DELETE FROM pipeline_resource WHERE resource_key=? AND job_id=? AND generation=1 AND quarantined=1 AND legacy=0', ('x:account:'+str(ACCOUNT_ID), JOB_ID)).rowcount == 1, 'quarantine_cas_failed')
            c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                (JOB_ID, 'audited_5524_verified_no_post', encoded({'plan_sha256': saved['plan_sha256'], 'original_receipt_sha256': RECEIPT_SHA, 'actor': actor, 'original_attempts': 1, 'from_generation': 1, 'to_generation': 2}), timestamp))
            c.execute("UPDATE "+JOURNAL+" SET state='committed',updated_at=? WHERE job_id=?", (timestamp, JOB_ID))
            c.commit(); n.rollback()
        except BaseException: c.rollback(); n.rollback(); raise
    return {'already_applied': False, 'queue_id': QID, 'log_id': LOG_ID, 'plan_sha256': saved['plan_sha256'],
        'from_generation': 1, 'to_generation': 2, 'preserved_attempt_count': 1, 'platform_called': False, 'backups': backups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'node-evidence', 'apply'))
    for name in ('central', 'native', 'plan', 'node-id', 'outbox', 'backup-dir', 'actor'):
        parser.add_argument('--'+name)
    parser.add_argument('--node-evidence', nargs='+'); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with os.fdopen(os.open(args.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w', encoding='utf-8') as handle:
        if args.action == 'plan': result = plan(args.central, args.native)
        else:
            saved = json.loads(Path(args.plan).read_text(encoding='utf-8'))
            if args.action == 'node-evidence': result = node_evidence(saved, args.node_id, args.outbox)
            else: result = apply(saved, [json.loads(Path(x).read_text(encoding='utf-8')) for x in args.node_evidence], args.backup_dir, args.actor)
        handle.write(json.dumps(result, ensure_ascii=False, indent=2)+'\n'); handle.flush(); os.fsync(handle.fileno())
    print(json.dumps({'ok': True, 'action': args.action, 'queue_id': QID, 'platform_called': False}))


if __name__ == '__main__':
    try: main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
