#!/usr/bin/env python3
"""Fence queue 5452 after an unsafe reset of its acknowledged unknown outcome.

No publisher, bridge, migrations or platform requests are invoked. The native
queue/log and every historical attempt/receipt remain byte-for-byte unchanged.
Central unknown+hold commits first, so interruption cannot reopen admission.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_x_prepost_failures import backup, writable
from scripts.recover_pipeline_x_preparation import (
    RecoveryRejected, date, document_hash, read_connection, require, rows, utcnow,
)
from features.pipeline_dispatch.x_prepost_recovery import digest, encoded

QUEUE, JOB, GENERATION, ACCOUNT = 5452, 32064, 1, 63
KEY = 'x:queue:5452'
REASON = 'operator_unknown_receipt_reconciliation'
CODE = 'pipeline_lease_lost'


def historical_audit(path):
    raw = Path(path).read_bytes()
    body = json.loads(raw)
    matches = [r for r in body['items'] if r.get('id') == QUEUE]
    require(body.get('readonly') is True and len(matches) == 1, 'historical_audit_missing')
    row = matches[0]
    expected = {'status': 'failed', 'log_status': 'failed', 'attempt_count': 1,
                'unknown_outcome': 1, 'job_id': JOB, 'stage': 'publish',
                'generation': GENERATION, 'account_id': ACCOUNT,
                'central_status': 'unknown', 'error_code': CODE, 'node_id': 'cpu-main'}
    require(all(row.get(k) == v for k, v in expected.items()), 'historical_audit_identity')
    return {'file_sha256': hashlib.sha256(raw).hexdigest(), 'captured_at': body['at'], 'row': row}


def snapshot(c, n, o, now):
    config = c.execute('SELECT config_json FROM pipeline_config WHERE id=1').fetchone()
    require(config and 'x' not in json.loads(config[0])['enabled_channels'], 'x_admissions_must_be_paused')
    q = rows(n, 'SELECT * FROM x_post_queue WHERE id=?', (QUEUE,))
    logs = rows(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (QUEUE,))
    require(len(q) == len(logs) == 1, 'native_identity_missing')
    q, log = q[0], logs[0]
    require(q['account_id'] == log['account_id'] == ACCOUNT and q['status'] == 'queued'
            and log['status'] == 'reserved' and log['attempt_count'] == 0 and not log['unknown_outcome'],
            'reset_native_state_changed')
    require(not any(log.get(k) for k in ('x_post_id', 'published_at')), 'native_success_present')
    jobs = rows(c, 'SELECT * FROM pipeline_job WHERE source_key=? ORDER BY id', (KEY,))
    chosen = [r for r in jobs if r['id'] == JOB]
    require(len(chosen) == 1 and chosen[0]['stage'] == 'publish'
            and chosen[0]['source_id'] == str(QUEUE) and chosen[0]['channel'] == 'x'
            and chosen[0]['family'] == 'queue' and chosen[0]['generation'] == GENERATION,
            'central_identity_changed')
    require(all(j['status'] not in ('running', 'legacy_running', 'published')
                and (not j['lease_expires_at'] or date(j['lease_expires_at']) <= now) for j in jobs),
            'source_running_leased_or_published')
    require(not chosen[0]['canonical_receipt'], 'canonical_receipt_present')
    require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (KEY,)).fetchone(),
            'central_success_present')
    ids = [j['id'] for j in jobs]
    placeholders = ','.join('?' for _ in ids)
    central = {}
    for table in ('pipeline_operation', 'pipeline_checkpoint', 'pipeline_receipt',
                  'pipeline_delivery', 'pipeline_claim_history', 'pipeline_resource', 'pipeline_legacy_resource'):
        central[table] = rows(c, 'SELECT * FROM ' + table + ' WHERE job_id IN (' + placeholders + ') ORDER BY rowid', ids)
    events = rows(c, 'SELECT * FROM pipeline_event WHERE job_id IN (' + placeholders + ') ORDER BY id', ids)
    own = [r for r in events if r['event_type'] == REASON]
    require(len(own) <= 1, 'multiple_operator_events')
    central['pipeline_event'] = [r for r in events if r['event_type'] != REASON]
    require(any(r['job_id'] == JOB and r['event_type'] == 'failed'
                and json.loads(r['details_json']).get('unknown') is True
                and json.loads(r['details_json']).get('code') == CODE for r in events),
            'actual_unknown_failure_event_required')
    native = {table: rows(n, 'SELECT * FROM ' + table + ' WHERE source_key=? ORDER BY rowid', (KEY,))
              for table in ('pipeline_x_claim', 'pipeline_x_operation', 'pipeline_x_checkpoint')}
    # This incident has initialize/append history only. Any later Post evidence
    # or additional generation needs a separate reconciliation, never this fix.
    from features.pipeline_dispatch.x_prepost_recovery import no_post_operation
    require(all(no_post_operation(r['operation']) and r['generation'] == GENERATION
                for r in central['pipeline_operation'] + native['pipeline_x_operation']),
            'unexpected_operation_or_generation')
    require(all(r['phase'] in ('x_preflight', 'x_media_initialized') for r in
                central['pipeline_checkpoint'] + native['pipeline_x_checkpoint']), 'later_checkpoint_present')
    outbox, proof = [], []
    for row in rows(o, 'SELECT * FROM receipt_outbox ORDER BY id'):
        payload = json.loads(row['payload'])
        if payload.get('job_id') not in ids:
            continue
        require(row['acked_at'] is not None, 'pending_node_receipt')
        require(not (payload.get('result') or {}).get('post_id')
                and payload.get('phase') not in ('x_source_published', 'x_target_reposted')
                and not (row['route'] == 'jobs/complete' and payload['job_id'] == JOB), 'node_success_present')
        # Claim tokens never enter the plan/audit, only their cryptographic hash.
        safe = {k: v for k, v in row.items() if k != 'payload'}
        safe['payload_sha256'] = digest(payload)
        outbox.append(safe)
        if (row['route'] != 'jobs/fail' or payload.get('job_id') != JOB
                or payload.get('generation') != GENERATION or payload.get('node_id') != 'cpu-main'
                or payload.get('unknown_outcome') is not True or payload.get('error_code') != CODE):
            continue
        claim_hash = hashlib.sha256(payload.get('claim_token', '').encode()).hexdigest()
        history = [r for r in central['pipeline_claim_history'] if r['job_id'] == JOB and r['generation'] == GENERATION]
        deliveries = [r for r in central['pipeline_delivery'] if r['delivery_id'] == row['id']]
        claims = [r for r in native['pipeline_x_claim'] if r['stage'] == 'publish' and r['generation'] == GENERATION]
        require(len(history) == len(deliveries) == len(claims) == 1, 'ack_identity_history_missing')
        h, d, claim = history[0], deliveries[0], claims[0]
        require(payload.get('receipt_id') == row['id']
                and h['claim_hash'] == d['claim_hash'] == claim['claim_hash'] == claim_hash
                and h['node_id'] == 'cpu-main' and h['worker_id'] == payload.get('worker_id')
                and claim['queue_id'] == QUEUE and claim['log_id'] == log['id']
                and d['job_id'] == JOB and d['generation'] == GENERATION
                and d['request_sha256'] == digest(payload)
                and json.loads(d['response_json']).get('accepted') is True
                and json.loads(d['response_json']).get('status') == 'unknown', 'acked_unknown_receipt_mismatch')
        proof.append({'receipt_id': row['id'], 'payload_sha256': digest(payload),
                      'claim_hash': claim_hash, 'acked_at': row['acked_at']})
    require(proof, 'acked_unknown_receipt_required')
    hold = rows(n, 'SELECT * FROM pipeline_canonical_hold WHERE source_key=?', (KEY,))
    return {'queue': q, 'log': log, 'jobs': jobs, 'central': central, 'native': native,
            'outbox': outbox, 'unknown_receipts': proof}, own, hold


def make_plan(central, native, outbox, audit, now=None):
    now = now or utcnow()
    with contextlib.closing(read_connection(central)) as c, contextlib.closing(read_connection(native)) as n, contextlib.closing(read_connection(outbox)) as o:
        c.execute('BEGIN'); n.execute('BEGIN'); o.execute('BEGIN')
        frozen, own, hold = snapshot(c, n, o, now)
    job = next(j for j in frozen['jobs'] if j['id'] == JOB)
    require(job['status'] == 'ready' and not job['hold_reason'] and not own and not hold,
            'already_held_or_not_reset_ready')
    p = {'kind': 'hold_x_reset_unknown_5452', 'version': 1, 'captured_at': now.isoformat(),
         'central': str(Path(central).resolve()), 'native': str(Path(native).resolve()),
         'outbox': str(Path(outbox).resolve()), 'audit': historical_audit(audit), 'before': frozen}
    p['plan_sha256'] = document_hash(p)
    return p


def apply_plan(plan, backup_dir, actor, now=None, stop_after=None):
    now = now or utcnow()
    require(plan.get('kind') == 'hold_x_reset_unknown_5452' and plan.get('version') == 1
            and document_hash(plan) == plan.get('plan_sha256'), 'plan_hash_changed')
    require(bool(str(actor).strip()), 'actor_required')
    backups = [backup(plan[key], backup_dir, key) for key in ('central', 'native', 'outbox')]
    with contextlib.closing(writable(plan['central'])) as c, contextlib.closing(writable(plan['native'])) as n, contextlib.closing(read_connection(plan['outbox'])) as o:
        c.execute('BEGIN IMMEDIATE'); n.execute('BEGIN IMMEDIATE'); o.execute('BEGIN')
        try:
            frozen, own, hold = snapshot(c, n, o, now)
            if own:
                event = json.loads(own[0]['details_json'])
                require(event['plan_sha256'] == plan['plan_sha256'], 'different_operator_plan')
                job = next(j for j in frozen['jobs'] if j['id'] == JOB)
                require(job['status'] == 'unknown' and job['hold_reason'] == REASON
                        and job['error_code'] == CODE and job['finished_at'] == '', 'operator_fence_changed')
                previous = next(j for j in plan['before']['jobs'] if j['id'] == JOB)
                for key in ('status', 'hold_reason', 'error_code', 'finished_at', 'updated_at'):
                    job[key] = previous[key]
                applied_at = own[0]['created_at']
            else:
                require(not hold, 'unrelated_native_hold')
                applied_at = now.isoformat()
            require(frozen == plan['before'], 'plan_state_drift')
            expected_hold = {'source_key': KEY, 'cutoff': applied_at, 'reason': REASON}
            require(not hold or hold == [expected_hold], 'native_hold_changed')
            if not own:
                c.execute("UPDATE pipeline_job SET status='unknown',hold_reason=?,error_code=?,finished_at='',updated_at=? WHERE id=?",
                          (REASON, CODE, applied_at, JOB))
                details = {'plan_sha256': plan['plan_sha256'], 'actor': str(actor),
                           'historical_audit': plan['audit'], 'original_rows': plan['before'],
                           'native_attempts_unchanged': True, 'platform_called': False}
                c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                          (JOB, REASON, encoded(details), applied_at))
            c.commit()  # The durable central fence precedes the native fence.
            if stop_after == 'central':
                raise RecoveryRejected('injected_stop_after_central')
            n.execute('INSERT OR IGNORE INTO pipeline_canonical_hold(source_key,cutoff,reason) VALUES(?,?,?)',
                      (KEY, applied_at, REASON))
            n.commit()
        except BaseException:
            c.rollback(); n.rollback(); raise
    return {'queue_id': QUEUE, 'job_id': JOB, 'status': 'held_unknown',
            'already_applied': bool(own and hold), 'plan_sha256': plan['plan_sha256'],
            'backups': backups, 'native_attempts_unchanged': True, 'platform_called': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('plan', 'apply'))
    for name in ('central', 'native', 'outbox', 'audit', 'plan', 'backup-dir', 'actor'):
        p.add_argument('--' + name)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    with os.fdopen(os.open(a.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8') as output:
        if a.action == 'plan':
            result = make_plan(a.central, a.native, a.outbox, a.audit)
        else:
            result = apply_plan(json.loads(Path(a.plan).read_text(encoding='utf-8')), a.backup_dir, a.actor)
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        output.flush(); os.fsync(output.fileno())
    print(json.dumps({'ok': True, 'queue_id': QUEUE, 'action': a.action, 'plan_sha256': result['plan_sha256']}))


if __name__ == '__main__':
    try:
        main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
