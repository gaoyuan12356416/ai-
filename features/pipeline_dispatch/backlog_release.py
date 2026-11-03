"""Exact, audited backlog disposition; never resets an executed publication."""
import json
import re

from .backlog_contract import SCHEMA, source_fingerprint


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def offer_disposition(conn, offer):
    """Require native and central proofs, including an exact linked X child."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_backlog_disposition'").fetchone():
        return None
    direct = conn.execute("SELECT * FROM pipeline_backlog_disposition WHERE source_key=?", (offer['source_key'],)).fetchone()
    if direct and direct['action'] == 'archive':
        return 'archive'
    for field in ('backlog_authorization', 'backlog_ancestor_authorization'):
        proof = offer.get(field) or {}
        key = proof.get('source_key')
        if not key or (field == 'backlog_authorization' and key != offer['source_key']):
            continue
        if field == 'backlog_ancestor_authorization' and (
                offer.get('channel') != 'x' or offer.get('family') != 'queue'
                or not key.startswith('x:auto:') or key not in offer.get('ancestor_source_keys', [])):
            continue
        saved = conn.execute("SELECT * FROM pipeline_backlog_disposition WHERE source_key=?", (key,)).fetchone()
        if not saved:
            continue
        if saved['action'] == 'archive':
            return 'archive'
        try:
            identity = json.loads(saved['details_json'])['identity']
            if (saved['state'] == 'committed' and saved['action'] == proof.get('action') == 'resume'
                    and re.fullmatch('[a-f0-9]{64}', saved['plan_sha256'])
                    and saved['plan_sha256'] == proof.get('plan_sha256')
                    and saved['source_fingerprint'] == proof.get('source_fingerprint') == source_fingerprint(identity)
                    and identity == proof.get('identity')):
                return 'resume'
        except (ValueError, TypeError, KeyError):
            continue
    return 'blocked' if direct else None


def commit_backlog_item(conn, item, plan_sha256, now):
    """Called inside the operator's BEGIN IMMEDIATE after native readback.

    paused_jobs are full frozen central rows with an added row_sha256. Only
    source_status and updated_at may drift due to read-only collector refresh.
    The caller journals this result in this same transaction. No network calls.
    """
    if not conn.in_transaction:
        raise ValueError('backlog_transaction_required')
    if not re.fullmatch('[a-f0-9]{64}', str(plan_sha256)):
        raise ValueError('backlog_plan_invalid')
    key, action = item['source_key'], item['action']
    identity = item['identity']
    if action not in ('resume', 'archive') or identity.get('source_key') != key or source_fingerprint(identity) != item['source_fingerprint']:
        raise ValueError('backlog_identity_invalid')
    conn.execute(SCHEMA)
    previous = conn.execute('SELECT * FROM pipeline_backlog_disposition WHERE source_key=?', (key,)).fetchone()
    if previous:
        if (previous['plan_sha256'] == plan_sha256 and previous['action'] == action
                and previous['source_fingerprint'] == item['source_fingerprint'] and previous['state'] == 'committed'):
            return {'source_key': key, 'action': action, 'already_committed': True}
        raise ValueError('backlog_disposition_conflict')
    offer = item['current_offer']
    if offer.get('source_key') != key or item['requested_stage'] != offer.get('stage'):
        raise ValueError('backlog_offer_identity_changed')
    rows = []
    for frozen in item['paused_jobs']:
        row = conn.execute('SELECT * FROM pipeline_job WHERE id=?', (frozen['id'],)).fetchone()
        if not row or row['source_key'] != key or row['status'] != 'backlog_paused' or row['generation'] != 0:
            raise ValueError('backlog_central_state_changed')
        if any(row[k] != v for k, v in frozen.items() if k not in {'row_sha256', 'updated_at', 'source_status'}):
            raise ValueError('backlog_central_row_changed')
        if any(row[k] for k in ('external_intent', 'claim_hash', 'canonical_receipt', 'lease_expires_at', 'node_id', 'worker_id')):
            raise ValueError('backlog_executed_source')
        for table in ('pipeline_claim_history', 'pipeline_operation', 'pipeline_checkpoint', 'pipeline_receipt', 'pipeline_delivery', 'pipeline_resource', 'pipeline_legacy_resource'):
            if conn.execute('SELECT 1 FROM ' + table + ' WHERE job_id=? LIMIT 1', (row['id'],)).fetchone():
                raise ValueError('backlog_execution_history_present')
        rows.append(row)
    if not rows:
        raise ValueError('backlog_paused_rows_required')
    if item.get('ghost'):
        if action != 'archive' or any(r['stage'] != 'prepare' for r in rows) or not conn.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (key,)).fetchone():
            raise ValueError('backlog_ghost_proof_missing')
    elif conn.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (key,)).fetchone():
        raise ValueError('backlog_already_published')
    if action == 'resume':
        if (offer.get('legacy_inflight') or offer.get('source_attempt_count') or offer.get('unknown_outcome')
                or offer.get('reconcile_only') or offer.get('publish_id')):
            raise ValueError('backlog_native_execution_present')
        if not any(r['stage'] == offer['stage'] for r in rows):
            raise ValueError('backlog_stage_changed')
        planner_only = (offer.get('channel'), offer.get('family'), offer.get('stage')) == ('x', 'auto', 'prepare') and offer.get('execution_available') is False
        if not offer.get('metadata_only') and (offer.get('status') not in ('queued', 'ready') or (not offer.get('execution_available') and not planner_only)):
            raise ValueError('backlog_native_not_ready')
        proof = offer.get('backlog_authorization') or {}
        if (proof.get('source_fingerprint') != item['source_fingerprint'] or proof.get('plan_sha256') != plan_sha256
                or proof.get('action') != 'resume' or proof.get('identity') != identity):
            raise ValueError('backlog_native_authorization_missing')
    details = {'identity': identity, 'job_ids': [r['id'] for r in rows], 'requested_stage': item['requested_stage'], 'ghost': bool(item.get('ghost'))}
    conn.execute('INSERT INTO pipeline_backlog_disposition VALUES(?,?,?,?,?,?,?,?)',
                 (key, plan_sha256, action, 'committed', item['source_fingerprint'], identity['scheduled_at'], identity['created_at'], _json(details)))
    changed = []
    for row in rows:
        status, hold = 'archived', 'backlog_archived'
        if action == 'resume' and row['stage'] == offer['stage']:
            status, hold = ('completed', 'metadata_only') if offer.get('metadata_only') else (offer['status'], str(offer.get('hold_reason') or 'adapter_unavailable') if offer.get('execution_available') is False else '')
            meta = json.loads(row['source_meta_json'])
            meta.update({k: offer[k] for k in ('canonical_fingerprint','source_attempt_count','legacy_inflight','reconcile_only','execution_available','ancestor_source_keys','cutover_due_at') if k in offer})
            conn.execute('UPDATE pipeline_job SET payload_json=?,artifact_json=?,capabilities_json=?,resource_keys_json=?,source_meta_json=?,source_status=? WHERE id=?',
                         (_json(offer.get('payload', {})), _json(offer.get('artifact', {})), _json(offer.get('capabilities', [])), _json(offer.get('resource_keys', [])), _json(meta), offer.get('source_status', ''), row['id']))
        conn.execute('UPDATE pipeline_job SET status=?,hold_reason=?,updated_at=? WHERE id=?', (status, hold, now, row['id']))
        conn.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                     (row['id'], 'backlog_disposition_committed', _json({'action': action, 'plan_sha256': plan_sha256, 'status': status}), now))
        changed.append({'id': row['id'], 'status': status})
    return {'source_key': key, 'action': action, 'jobs': changed}
