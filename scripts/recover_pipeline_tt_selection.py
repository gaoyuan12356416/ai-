#!/usr/bin/env python3
"""Plan/apply one reviewed, unexecuted TT selection handoff; never a retry API.

The private scope contains the exact reviewed sources and frozen hashes. No
production IDs, credentials or scope manifest belong in this repository.
Native ledgers are opened read-only. Apply changes dispatch admission only.
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
from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec, obj


class HandoffRejected(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise HandoffRejected(code)


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(value):
    return hashlib.sha256((value if isinstance(value, str) else dumps(value)).encode()).hexdigest()


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def date(value):
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'timezone_required')
    return parsed


def document_hash(value):
    return sha({key: item for key, item in value.items() if key != 'plan_sha256'})


def read_connection(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def private_json(path, value):
    with os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        f.flush(); os.fsync(f.fileno())


def validate_scope(scope, expected_sha):
    require(scope.get('kind') == 'review_only_tt_frozen_prepare_handoff' and scope.get('version') == 1
            and scope.get('apply') is False, 'scope_type_invalid')
    require(scope.get('plan_sha256') == expected_sha == document_hash(scope), 'scope_hash_mismatch')
    items = scope.get('items', [])
    require(len(items) == 8 and len({x.get('job_id') for x in items}) == 8
            and len({x.get('source_key') for x in items}) == 8, 'scope_must_match_reviewed_eight')
    require(sorted(scope.get('source_keys', [])) == sorted(x['source_key'] for x in items), 'scope_sources_mismatch')
    return items


def admission_off(c):
    config = obj(c.execute('SELECT config_json FROM pipeline_config WHERE id=1').fetchone()[0])
    require(config.get('enabled_channels') == [], 'all_channels_must_be_disabled')
    require(not c.execute("SELECT 1 FROM pipeline_job WHERE status IN ('running','claimed','leased') LIMIT 1").fetchone(), 'active_dispatch_work')
    require(len(config.get('hosts', {})) == 2, 'reviewed_two_nodes_required')
    require(config.get('cutovers', {}).get('tt'), 'tt_cutover_missing')
    return config


SCOPE_FIELDS = ('source_key', 'job_id', 'central_generation', 'central_error', 'central_fingerprint',
    'source_created_at', 'scheduled_at_utc', 'account_id', 'template_id', 'template_version', 'material_id', 'content_id',
    'gpu_job_id', 'profile', 'trim_tail_seconds', 'required_capabilities', 'descriptor_sha256',
    'request_sha256', 'source_url_sha256', 'native_token_sha256', 'binding_claim_hash')


def snapshot(c, native_path, scope, scope_sha, now):
    items = validate_scope(scope, scope_sha)
    config = admission_off(c)
    adapter = LedgerAdapter(LedgerSpec('tt', 'auto', str(native_path), 'tt_auto_task'))
    proofs = []
    with contextlib.closing(read_connection(native_path)) as n:
        n.execute('BEGIN')
        for item in items:
            job = c.execute('SELECT * FROM pipeline_job WHERE id=?', (item['job_id'],)).fetchone()
            require(job is not None, 'dispatch_job_missing'); job = dict(job)
            require((job['channel'], job['family'], job['stage'], job['status'], job['generation'], job['error_code'])
                    == ('tt', 'auto', 'prepare', 'failed', 2, 'canonical_identity_changed'), 'dispatch_state_not_exact')
            require(job['source_key'] == item['source_key'] == 'tt:auto:' + str(job['source_id']), 'source_identity_changed')
            require(date(job['source_created_at']) > date(config['cutovers']['tt']), 'precutover_source_forbidden')
            artifact = obj(job['artifact_json'])
            require(not job['external_intent'] and not job['hold_reason'] and not job['canonical_receipt']
                    and not any(artifact.get(k) for k in ('url', 'sha256', 'size')), 'dispatch_has_intent_hold_or_artifact')
            require(not c.execute("SELECT 1 FROM pipeline_job WHERE source_key=? AND id<>? AND (generation>0 OR external_intent<>0)",
                                  (job['source_key'], job['id'])).fetchone(), 'other_stage_execution_evidence')
            for table in ('pipeline_operation', 'pipeline_checkpoint', 'pipeline_receipt', 'pipeline_delivery', 'pipeline_phase_metric', 'pipeline_resource', 'pipeline_legacy_resource'):
                require(not c.execute('SELECT 1 FROM ' + table + ' WHERE job_id=? LIMIT 1', (job['id'],)).fetchone(), 'dispatch_execution_evidence')
            require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (job['source_key'],)).fetchone(), 'publication_evidence')
            history = [dict(r) for r in c.execute('SELECT * FROM pipeline_claim_history WHERE job_id=? ORDER BY generation', (job['id'],))]
            require([r['generation'] for r in history] == [1, 2], 'claim_history_changed')
            failures = [dict(r) for r in c.execute("SELECT * FROM pipeline_event WHERE job_id=? AND event_type='failed' ORDER BY id", (job['id'],))]
            require(failures, 'failed_event_missing')
            row = n.execute('SELECT * FROM tt_auto_task WHERE id=?', (job['source_id'],)).fetchone()
            require(row is not None, 'native_missing'); row = dict(row)
            require(row['status'] == 'preparing' and row['claim_phase'] in ('selection', 'prepare'), 'native_state_changed')
            require(row['claim_worker'] == 'pipeline:' + history[0]['node_id'] + ':1'
                    and row['claim_token'] and date(row['lease_expires_at_utc']) <= now, 'native_lease_changed_or_active')
            require(not any(row.get(k) for k in ('preparation_attempt_count', 'publish_attempt_count', 'publish_id', 'unknown_outcome',
                    'prepared_media_url', 'prepared_output_sha256', 'prepared_output_size', 'prepared_at_utc'))
                    and not obj(row.get('prepared_json')), 'native_execution_evidence')
            require(not n.execute('SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?', (job['source_key'],)).fetchone(), 'native_held')
            bindings = n.execute('SELECT * FROM pipeline_tt_claim WHERE source_key=? ORDER BY generation', (job['source_key'],)).fetchall()
            require(len(bindings) == 1 and bindings[0]['generation'] == 1 and bindings[0]['stage'] == 'prepare'
                    and not bindings[0]['receipt_json'] and bindings[0]['native_token'] == row['claim_token']
                    and bindings[0]['claim_hash'] == history[0]['claim_hash'], 'native_binding_changed')
            require(not n.execute('SELECT 1 FROM pipeline_tt_operation WHERE source_key=? LIMIT 1', (job['source_key'],)).fetchone(), 'native_operation_evidence')
            frozen = bindings[0]; descriptor = obj(frozen['descriptor_json']); request = obj(descriptor.get('request'))
            fingerprint = obj(job['source_meta_json']).get('canonical_fingerprint')
            require(adapter.owns_frozen_tt_selection(row, job, {}, fingerprint), 'frozen_selection_not_proven')
            reservation = n.execute('SELECT * FROM tt_auto_material_ledger WHERE task_id=?', (row['id'],)).fetchone()
            require(reservation and reservation['material_id'] == row['material_id'] and reservation['content_id'] == row['content_id'], 'material_reservation_changed')
            proof = {'source_key': job['source_key'], 'job_id': job['id'], 'central_generation': job['generation'], 'central_error': job['error_code'],
                'central_fingerprint': fingerprint, 'source_created_at': job['source_created_at'], 'scheduled_at_utc': row['scheduled_at_utc'], 'account_id': row['account_id'],
                'template_id': row['template_id'], 'template_version': row['template_version'], 'material_id': row['material_id'],
                'content_id': row['content_id'], 'gpu_job_id': row['gpu_job_id'], 'profile': request['expected_profile'],
                'trim_tail_seconds': request['source_trim_tail_seconds'], 'required_capabilities': descriptor.get('required_capabilities', []),
                'descriptor_sha256': sha(descriptor), 'request_sha256': sha(request), 'source_url_sha256': sha(request['source_url']),
                'native_token_sha256': sha(row['claim_token']), 'binding_claim_hash': frozen['claim_hash']}
            require(all(proof[k] == item[k] for k in SCOPE_FIELDS), 'reviewed_scope_identity_drift')
            require(proof['required_capabilities'] and 'prepare:tt' in proof['required_capabilities'], 'frozen_capabilities_missing')
            proof.update(scheduled_at=job['scheduled_at'], native_scheduled_at=row['scheduled_at_utc'],
                         failed_events_sha256=sha(failures), claim_history_sha256=sha(history),
                         material_reservation_sha256=sha(dict(reservation)), native_lease_until=row['lease_expires_at_utc'],
                         central_row_sha256=sha(job), native_row_sha256=sha(row))
            proofs.append(proof)
    return {'items': proofs, 'node_ids': sorted(config['hosts']), 'tt_cutoff': config['cutovers']['tt']}


def make_plan(central_path, native_path, scope, scope_sha, now=None):
    now = now or utcnow()
    with contextlib.closing(read_connection(central_path)) as c:
        c.execute('BEGIN')
        proof = snapshot(c, native_path, scope, scope_sha, now)
    plan = dict(proof, kind='tt_frozen_selection_admission_plan', version=1, scope_sha256=scope_sha,
                captured_at=now.isoformat(), scope=scope)
    plan['plan_sha256'] = document_hash(plan)
    return plan


def validate_nodes(plan, evidence, now):
    require(evidence.get('plan_sha256') == plan['plan_sha256'], 'node_evidence_wrong_plan')
    rows = evidence.get('nodes', [])
    require(len(rows) == 2 and sorted(x.get('node_id') for x in rows) == plan['node_ids'], 'node_evidence_scope')
    for row in rows:
        age = (now - date(row['captured_at'])).total_seconds()
        require(0 <= age <= 60, 'node_evidence_stale')
        require(row.get('unit') == 'pipeline-node-' + row['node_id'] + '.service'
                and row.get('active_state') == 'inactive' and row.get('main_pid') == 0, 'node_not_inactive')
        require(row.get('outbox_matching_count') == 0 and row.get('task_file_matches') == []
                and row.get('work_root_exists') is True and row.get('outbox_exists') is True, 'node_execution_evidence')


def apply_plan(central_path, native_path, plan, expected_sha, evidence, backup_dir, *, actor, clock=utcnow):
    require(plan.get('kind') == 'tt_frozen_selection_admission_plan' and plan.get('version') == 1, 'plan_type_invalid')
    require(plan.get('plan_sha256') == expected_sha == document_hash(plan), 'plan_hash_mismatch')
    require(bool(str(actor).strip()), 'audit_actor_required')
    validate_nodes(plan, evidence, clock())
    fresh = make_plan(central_path, native_path, plan['scope'], plan['scope_sha256'], clock())
    require(all(fresh[k] == plan[k] for k in ('items', 'node_ids', 'tt_cutoff')), 'plan_state_drift')
    backup_dir = Path(backup_dir); backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / ('tt-selection-' + expected_sha[:16] + '-' + uuid.uuid4().hex + '.sqlite3')
    with os.fdopen(os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb'):
        pass
    with contextlib.closing(read_connection(central_path)) as source, contextlib.closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
        require(destination.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'backup_integrity_failed')
    with backup.open('r+b') as saved:
        os.fsync(saved.fileno())
        digest = hashlib.sha256()
        for chunk in iter(lambda: saved.read(1024 * 1024), b''):
            digest.update(chunk)
        backup_sha = digest.hexdigest()
    c = sqlite3.connect(str(central_path), timeout=10, isolation_level=None); c.row_factory = sqlite3.Row
    try:
        c.execute('PRAGMA synchronous=FULL'); c.execute('BEGIN IMMEDIATE')
        validate_nodes(plan, evidence, clock())
        current = snapshot(c, native_path, plan['scope'], plan['scope_sha256'], clock())
        require(all(current[k] == plan[k] for k in ('items', 'node_ids', 'tt_cutoff')), 'plan_state_drift')
        instant = clock().isoformat()
        for item in plan['items']:
            changed = c.execute("UPDATE pipeline_job SET status='queued',capabilities_json=?,lease_expires_at='',updated_at=? WHERE id=? AND status='failed' AND generation=2 AND error_code='canonical_identity_changed'",
                                (dumps(item['required_capabilities']), instant, item['job_id'])).rowcount
            require(changed == 1, 'handoff_update_conflict')
            details = {'plan_sha256': expected_sha, 'scope_sha256': plan['scope_sha256'], 'actor': str(actor),
                       'previous_status': 'failed', 'generation': 2, 'request_sha256': item['request_sha256'],
                       'backup_sha256': backup_sha, 'reason': 'unexecuted_frozen_selection_handoff'}
            c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                      (item['job_id'], 'audited_preparation_handoff', dumps(details), instant))
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return {'applied': True, 'plan_sha256': expected_sha, 'job_ids': [x['job_id'] for x in plan['items']],
            'backup_path': str(backup), 'backup_sha256': backup_sha, 'native_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--scope'); parser.add_argument('--scope-sha256')
    parser.add_argument('--plan'); parser.add_argument('--plan-sha256')
    parser.add_argument('--node-evidence'); parser.add_argument('--backup-dir'); parser.add_argument('--actor')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(); config = json.loads(Path(args.config).read_text())
    # Reserve the private report before any mutation. An existing/unwritable
    # report path must never cause an already-applied operation to look failed.
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as output:
        if args.action == 'plan':
            require(args.scope and args.scope_sha256, 'scope_required')
            result = make_plan(config['db_path'], config['paths']['tt_auto'], json.loads(Path(args.scope).read_text()), args.scope_sha256)
        else:
            require(all((args.plan, args.plan_sha256, args.node_evidence, args.backup_dir, args.actor)), 'apply_arguments_required')
            result = apply_plan(config['db_path'], config['paths']['tt_auto'], json.loads(Path(args.plan).read_text()),
                args.plan_sha256, json.loads(Path(args.node_evidence).read_text()), args.backup_dir, actor=args.actor)
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        output.flush(); os.fsync(output.fileno())
    print(json.dumps({'ok': True, 'action': args.action, 'plan_sha256': result['plan_sha256']}))


if __name__ == '__main__':
    try:
        main()
    except HandoffRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
