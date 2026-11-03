#!/usr/bin/env python3
"""Settle only q5620's proven PNG/random-video preflight failure; no network/ACK.

The current claim is fenced before native settlement. The original hold, all
claim history and all 34 pending node receipts survive. Resume the SAME plan
after interruption; never restore the old claim or invent a receipt delivery.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_prepost_recovery import digest, encoded
from features.x_posts.service import XPostStore
from scripts.recover_pipeline_x_preparation import (
    RecoveryRejected, date, document_hash, read_connection, require, rows, utcnow,
)
from scripts.recover_x_prepost_failures import backup, writable

KEY, QID, LOG_ID, JOB_ID, PARENT = 'x:queue:5620', 5620, 5543, 32153, 1039
HOLD = 'operator_invalid_video_input_reconciliation'
CODE = 'x_random_requires_video'
MESSAGE = 'Frozen random-video template requires video; verified source is PNG.'
JOURNAL = 'pipeline_x_png_5620_settlement'
AUDIT = 'pipeline_x_png_5620_audit'
MANIFEST = json.loads(Path(__file__).with_name('x_png_5620_incident.json').read_text(encoding='utf-8'))
PARENT_MUTABLE = {'status', 'published_count', 'failed_count', 'unknown_count',
                  'updated_at', 'finished_at', 'error_code', 'error_message'}
JOB_PROJECTION = {'updated_at', 'source_status', 'source_meta_json'}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def one(conn, sql, args=()):
    result = rows(conn, sql, args)
    require(len(result) == 1, 'exact_one_row_required')
    return result[0]


def stable_job(row):
    return {k: v for k, v in row.items() if k not in JOB_PROJECTION}


def parent_identity(row):
    return {k: v for k, v in row.items() if k not in PARENT_MUTABLE}


def node_evidence(path, node_id, *, now=None):
    """Read-only; export hashes and payloads with claim tokens removed."""
    require(node_id in ('cpu-main', 'hk-gpu'), 'node_scope')
    found = []
    with contextlib.closing(read_connection(path)) as c:
        c.execute('BEGIN')
        for row in rows(c, 'SELECT id,route,payload,created_at,acked_at FROM receipt_outbox ORDER BY created_at,id'):
            payload = json.loads(row.pop('payload'))
            if payload.get('job_id') != JOB_ID:
                continue
            # Retain the exact original bytes' fingerprint, not reserialized JSON.
            raw = c.execute('SELECT payload FROM receipt_outbox WHERE id=?', (row['id'],)).fetchone()[0]
            claim = payload.pop('claim_token', '')
            require(isinstance(claim, str) and claim, 'receipt_claim_missing')
            found.append(dict(row, raw_sha256=sha(raw), claim_sha256=sha(claim),
                              safe_payload=payload, safe_sha256=digest(payload)))
    return {'node_id': node_id, 'at': (now or utcnow()).isoformat(), 'receipts': found}


def validate_nodes(evidence, history, now):
    require(sorted(e['node_id'] for e in evidence) == ['cpu-main', 'hk-gpu'], 'both_nodes_required')
    actual = []
    bygen = {r['generation']: r for r in history}
    for e in evidence:
        require(0 <= (now - date(e['at'])).total_seconds() <= 120, 'stale_node_evidence')
        if e['node_id'] == 'cpu-main':
            require(e['receipts'] == [], 'unexpected_cpu_receipt')
            continue
        for r in e['receipts']:
            p = r['safe_payload']; old = bygen.get(p.get('generation'))
            require(old and p.get('job_id') == JOB_ID and p.get('node_id') == 'hk-gpu'
                    and p.get('worker_id') == old['worker_id'] and r['claim_sha256'] == old['claim_hash'], 'receipt_claim_mismatch')
            require('claim_token' not in p and digest(p) == r['safe_sha256'] and r['acked_at'] is None, 'receipt_changed_or_acked')
            if r['route'] == 'jobs/checkpoint':
                require(p.get('phase') == 'x_preflight' and digest(p.get('receipt')) == MANIFEST['receipt_sha256'], 'not_original_png_preflight')
            else:
                require(r['route'] == 'jobs/fail' and p.get('unknown_outcome') is False
                        and p.get('error_code') == 'coordinator_operation_failed', 'not_known_preflight_failure')
            actual.append({k: r[k] for k in ('id', 'route', 'raw_sha256', 'safe_sha256', 'claim_sha256')})
    require(sorted(actual, key=lambda r: r['id']) == MANIFEST['receipts'], 'original_34_packets_required')


def collect(c, n):
    q = one(n, 'SELECT * FROM x_post_queue WHERE id=?', (QID,))
    log = one(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (QID,))
    parent = one(n, 'SELECT * FROM x_post_schedule_run WHERE id=?', (PARENT,))
    job = one(c, 'SELECT * FROM pipeline_job WHERE source_key=?', (KEY,))
    claims = rows(n, 'SELECT * FROM pipeline_x_claim WHERE source_key=? ORDER BY stage,generation', (KEY,))
    history = rows(c, 'SELECT * FROM pipeline_claim_history WHERE job_id=? ORDER BY generation', (JOB_ID,))
    events = rows(c, 'SELECT * FROM pipeline_event WHERE job_id=? ORDER BY id', (JOB_ID,))
    for table in ('pipeline_operation', 'pipeline_checkpoint', 'pipeline_receipt', 'pipeline_delivery', 'pipeline_resource', 'pipeline_legacy_resource'):
        require(not c.execute('SELECT 1 FROM '+table+' WHERE job_id=?', (JOB_ID,)).fetchone(), 'central_execution_fact_' + table)
    require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (KEY,)).fetchone(), 'central_publication')
    for table in ('pipeline_x_operation', 'pipeline_x_checkpoint', 'pipeline_canonical_hold'):
        require(not n.execute('SELECT 1 FROM '+table+' WHERE source_key=?', (KEY,)).fetchone(), 'native_execution_fact_' + table)
    for table in ('pipeline_x_artifact', 'x_post_random_template_preparation', 'x_post_repost_ledger'):
        require(not n.execute('SELECT 1 FROM '+table+' WHERE queue_id=?', (QID,)).fetchone(), 'native_media_fact_' + table)
    return {'queue': q, 'log': log, 'parent': parent, 'job': job,
            'native_claims': claims, 'history': history, 'events': events}


def verify_original(facts, now):
    q, log, job = (facts[k] for k in ('queue', 'log', 'job'))
    expected = MANIFEST['hashes']
    for k in ('queue', 'log', 'native_claims', 'history', 'events'):
        require(digest(facts[k]) == expected[k], 'original_' + k + '_changed')
    require(digest(stable_job(job)) == expected['stable_job'], 'original_job_changed')
    require(digest(parent_identity(facts['parent'])) == expected['parent_identity'], 'parent_identity_changed')
    require(facts['parent']['status'] not in ('stopped', 'cancelled', 'canceled'), 'parent_stopped')
    require((q['account_id'], q['schedule_run_id'], q['material_id'], q['status'], q['source_type'], q['delivery_mode'])
            == (63, PARENT, '7676040', 'queued', 'material', 'direct') and q['random_template_enabled'] == 1, 'queue_identity')
    require(log['id'] == LOG_ID and log['status'] == 'reserved' and not any(log[k] for k in
            ('attempt_count', 'unknown_outcome', 'x_post_id', 'x_media_id', 'started_at', 'published_at')), 'native_not_zero_write')
    require((job['id'], job['generation'], job['status'], job['hold_reason']) == (JOB_ID, 17, 'queued', HOLD)
            and job['claim_hash'] and date(job['lease_expires_at']) < now
            and not job['external_intent'] and not job['canonical_receipt'], 'central_not_held_expired')
    require([h['generation'] for h in facts['history']] == list(range(1, 18))
            and [h['generation'] for h in facts['native_claims']] == list(range(1, 18)), 'seventeen_generations_required')
    for claim, old in zip(facts['native_claims'], facts['history']):
        require(claim['stage'] == 'prepare' and claim['queue_id'] == QID and claim['log_id'] == LOG_ID
                and claim['claim_hash'] == old['claim_hash'] and old['node_id'] == 'hk-gpu', 'claim_ledgers_disagree')
        desc = json.loads(claim['descriptor_json'])
        require(digest(desc) == MANIFEST['descriptor_sha256'] and desc['kind'] == 'x_preflight'
                and sha(desc['request']['source_url']) == MANIFEST['source_url_sha256']
                and desc['request'].get('random_request'), 'frozen_input_changed')


def make_plan(central, native, evidence, *, now=None):
    now = now or utcnow()
    with contextlib.closing(read_connection(central)) as c, contextlib.closing(read_connection(native)) as n:
        c.execute('BEGIN'); n.execute('BEGIN')
        facts = collect(c, n); verify_original(facts, now); validate_nodes(evidence, facts['history'], now)
    plan = {'kind': 'x_png_preflight_5620_settlement', 'version': 1, 'created_at': now.isoformat(),
            'central': str(Path(central).resolve()), 'native': str(Path(native).resolve()),
            'manifest_sha256': digest(MANIFEST), 'before': facts, 'node_evidence': evidence}
    return dict(plan, plan_sha256=document_hash(plan))


def audit_row(c, table):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return None
    found = rows(c, 'SELECT * FROM '+table+' WHERE queue_id=?', (QID,))
    return found[0] if found else None


def install_audit(c, table, journal=False):
    c.execute('CREATE TABLE IF NOT EXISTS '+table+' (queue_id INTEGER PRIMARY KEY CHECK(queue_id=5620),'
              'plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,proof_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)')
    c.execute('CREATE TRIGGER IF NOT EXISTS '+table+'_no_delete BEFORE DELETE ON '+table+" BEGIN SELECT RAISE(ABORT,'settlement_audit_immutable'); END")
    c.execute('CREATE TRIGGER IF NOT EXISTS '+table+'_no_replace BEFORE INSERT ON '+table+
              ' WHEN EXISTS(SELECT 1 FROM '+table+" WHERE queue_id=NEW.queue_id) BEGIN SELECT RAISE(ABORT,'settlement_audit_immutable'); END")
    condition = (" WHEN OLD.queue_id<>NEW.queue_id OR OLD.plan_sha256<>NEW.plan_sha256 OR OLD.proof_json<>NEW.proof_json"
                 " OR OLD.created_at<>NEW.created_at OR NOT (OLD.state='fenced' AND NEW.state='committed')") if journal else ''
    c.execute('CREATE TRIGGER IF NOT EXISTS '+table+'_no_update BEFORE UPDATE ON '+table+condition+
              " BEGIN SELECT RAISE(ABORT,'settlement_audit_immutable'); END")


def verify_after(facts, saved, state, native_done):
    require(state in ('fenced', 'committed'), 'journal_state_invalid')
    old = saved['before']
    for k in ('native_claims', 'history'):
        require(facts[k] == old[k], 'history_changed')
    job = dict(old['job']); job['claim_hash'] = ''
    if state == 'committed':
        job.update(status='failed', error_code=CODE, error_message=MESSAGE, finished_at=facts['job']['finished_at'])
        require(bool(job['finished_at']), 'central_finish_missing')
    require(stable_job(facts['job']) == stable_job(job), 'fenced_job_drift')
    require(digest(parent_identity(facts['parent'])) == MANIFEST['hashes']['parent_identity'], 'parent_identity_changed')
    require(native_done or facts['parent']['status'] not in ('stopped', 'cancelled', 'canceled'), 'parent_stopped')
    for k in ('queue', 'log'):
        expected = dict(old[k]); current = facts[k]
        if native_done:
            expected.update(status='failed', updated_at=current['updated_at'])
            if k == 'log': expected.update(error_code=CODE, error_message=MESSAGE)
        require(current == expected, 'native_' + k + '_drift')
    previous = old['events']
    require(facts['events'][:len(previous)] == previous, 'event_history_changed')
    tail = facts['events'][len(previous):]
    required = ['operator_png_preflight_claim_fenced'] + (['operator_png_preflight_settled'] if state == 'committed' else [])
    require([e['event_type'] for e in tail] == required and all(json.loads(e['details_json']) ==
            {'plan_sha256': saved['plan_sha256'], 'platform_called': False} for e in tail), 'unexpected_new_event')


def event(c, kind, saved, stamp):
    c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
              (JOB_ID, kind, encoded({'plan_sha256': saved['plan_sha256'], 'platform_called': False}), stamp))


def apply(saved, evidence, backup_dir, actor, *, now=None, stop_after=None):
    fixed_now = now
    now = now or utcnow(); stamp = now.isoformat()
    require(saved.get('kind') == 'x_png_preflight_5620_settlement' and saved.get('version') == 1
            and saved.get('manifest_sha256') == digest(MANIFEST) and document_hash(saved) == saved['plan_sha256']
            and str(actor).strip(), 'plan_invalid')
    central_path, native_path = Path(saved['central']), Path(saved['native'])
    require(central_path.is_file() and native_path.is_file()
            and central_path.resolve() != native_path.resolve(), 'existing_distinct_databases_required')
    verify_original(saved['before'], date(saved['created_at']))
    with contextlib.closing(writable(saved['central'])) as c, contextlib.closing(writable(saved['native'])) as n:
        prior = audit_row(c, JOURNAL)
        if prior:
            require(prior['plan_sha256'] == saved['plan_sha256'], 'different_plan_already_started')
            require(json.loads(prior['proof_json'])['plan'] == saved, 'journal_proof_conflict')
            if prior['state'] != 'committed':
                validate_nodes(evidence, saved['before']['history'], now)
        else:
            validate_nodes(evidence, saved['before']['history'], now)
            backups = [backup(saved['central'], backup_dir, 'dispatch'), backup(saved['native'], backup_dir, 'native')]
        # Central lock first matches coordinator->native order. The native lock
        # also excludes a late native write during the initial exact CAS.
        c.execute('BEGIN IMMEDIATE'); n.execute('BEGIN IMMEDIATE')
        try:
            prior = audit_row(c, JOURNAL); native_audit = audit_row(n, AUDIT)
            facts = collect(c, n)
            if prior:
                require(prior['plan_sha256'] == saved['plan_sha256'], 'different_plan_already_started')
                require(json.loads(prior['proof_json'])['plan'] == saved, 'journal_proof_conflict')
                require(not native_audit or (native_audit['plan_sha256'] == saved['plan_sha256']
                        and native_audit['proof_json'] == prior['proof_json'] and native_audit['state'] == 'committed'), 'native_audit_conflict')
                verify_after(facts, saved, prior['state'], bool(native_audit))
                require(prior['state'] != 'committed' or native_audit, 'native_audit_missing')
                if prior['state'] == 'committed':
                    c.commit(); n.commit()
                    return {'already_applied': True, 'queue_id': QID, 'plan_sha256': saved['plan_sha256'], 'publish_requests': 0, 'pending_receipts_preserved': 34}
            else:
                require(not native_audit, 'orphan_native_audit')
                verify_original(facts, fixed_now or utcnow())
                validate_nodes(evidence, facts['history'], fixed_now or utcnow())
                require(all(facts[k] == saved['before'][k] for k in ('queue', 'log', 'native_claims', 'history', 'events'))
                        and stable_job(facts['job']) == stable_job(saved['before']['job']), 'plan_drift')
                install_audit(c, JOURNAL, journal=True)
                proof = {'plan': saved, 'actor': actor, 'backups': backups, 'evidence': evidence, 'parent_at_apply': facts['parent']}
                c.execute('INSERT INTO '+JOURNAL+' VALUES(?,?,?,?,?,?)', (QID, saved['plan_sha256'], 'fenced', encoded(proof), stamp, stamp))
                require(c.execute("UPDATE pipeline_job SET claim_hash='',updated_at=? WHERE id=? AND generation=17 AND status='queued' AND hold_reason=? AND claim_hash=?",
                        (stamp, JOB_ID, HOLD, saved['before']['job']['claim_hash'])).rowcount == 1, 'claim_fence_cas')
                event(c, 'operator_png_preflight_claim_fenced', saved, stamp)
            c.commit(); n.commit()
        except BaseException:
            c.rollback(); n.rollback(); raise
        if stop_after == 'fenced': return {'state': 'fenced'}
        # The fence is durable now, so no old receipt can enter the native bridge.
        c.execute('BEGIN IMMEDIATE'); n.execute('BEGIN IMMEDIATE')
        try:
            prior = audit_row(c, JOURNAL); native_audit = audit_row(n, AUDIT)
            facts = collect(c, n)
            require(prior and prior['plan_sha256'] == saved['plan_sha256']
                    and json.loads(prior['proof_json'])['plan'] == saved, 'journal_proof_conflict')
            require(not native_audit or (native_audit['plan_sha256'] == saved['plan_sha256']
                    and native_audit['proof_json'] == prior['proof_json']), 'native_audit_conflict')
            verify_after(facts, saved, 'fenced', bool(native_audit))
            if not native_audit:
                install_audit(n, AUDIT)
                n.execute('INSERT INTO '+AUDIT+' VALUES(?,?,?,?,?,?)', (QID, saved['plan_sha256'], 'committed', prior['proof_json'], stamp, stamp))
                require(n.execute("UPDATE x_post_publish_log SET status='failed',error_code=?,error_message=?,updated_at=? WHERE id=? AND status='reserved' AND attempt_count=0 AND unknown_outcome=0",
                        (CODE, MESSAGE, stamp, LOG_ID)).rowcount == 1, 'native_log_cas')
                require(n.execute("UPDATE x_post_queue SET status='failed',updated_at=? WHERE id=? AND status='queued'", (stamp, QID)).rowcount == 1, 'native_queue_cas')
                XPostStore._sync_run(n, QID, stamp)
            n.commit(); c.commit()
        except BaseException:
            n.rollback(); c.rollback(); raise
        if stop_after == 'native': return {'state': 'native'}
        c.execute('BEGIN IMMEDIATE'); n.execute('BEGIN IMMEDIATE')
        try:
            verify_after(collect(c, n), saved, 'fenced', True)
            require(audit_row(n, AUDIT)['plan_sha256'] == saved['plan_sha256'], 'native_audit_missing')
            require(c.execute("UPDATE pipeline_job SET status='failed',error_code=?,error_message=?,finished_at=?,updated_at=? WHERE id=? AND generation=17 AND status='queued' AND hold_reason=? AND claim_hash=''",
                    (CODE, MESSAGE, stamp, stamp, JOB_ID, HOLD)).rowcount == 1, 'central_terminal_cas')
            event(c, 'operator_png_preflight_settled', saved, stamp)
            require(c.execute("UPDATE "+JOURNAL+" SET state='committed',updated_at=? WHERE queue_id=? AND state='fenced' AND plan_sha256=?", (stamp, QID, saved['plan_sha256'])).rowcount == 1, 'journal_cas')
            c.commit(); n.commit()
        except BaseException:
            c.rollback(); n.rollback(); raise
        return {'already_applied': False, 'queue_id': QID, 'plan_sha256': saved['plan_sha256'],
                'backups': json.loads(audit_row(c, JOURNAL)['proof_json'])['backups'], 'publish_requests': 0, 'pending_receipts_preserved': 34}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('node-evidence', 'plan', 'apply'))
    for name in ('central', 'native', 'outbox', 'node-id', 'plan', 'backup-dir', 'actor'): p.add_argument('--'+name)
    p.add_argument('--evidence', nargs='+'); p.add_argument('--output', required=True)
    a = p.parse_args()
    evidence = [json.loads(Path(v).read_text(encoding='utf-8')) for v in (a.evidence or [])]
    with os.fdopen(os.open(a.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8') as f:
        if a.action == 'node-evidence':
            require(a.outbox and a.node_id, 'node_arguments_required')
            result = node_evidence(a.outbox, a.node_id)
        elif a.action == 'plan':
            require(a.central and a.native, 'plan_arguments_required')
            result = make_plan(a.central, a.native, evidence)
        else:
            require(a.plan and a.backup_dir and a.actor, 'apply_arguments_required')
            result = apply(json.loads(Path(a.plan).read_text(encoding='utf-8')), evidence, a.backup_dir, a.actor)
        json.dump(result, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
    print(json.dumps({'ok': True, 'action': a.action, 'queue_id': QID, 'publish_requests': 0}))


if __name__ == '__main__':
    try: main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
