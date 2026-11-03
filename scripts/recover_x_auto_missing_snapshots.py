#!/usr/bin/env python3
"""Complete two never-frozen account snapshots; no selection or publication."""
import argparse
import contextlib
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_auto_posts.publisher import AutoPostExecutor
from features.x_auto_posts.service import XAutoPostService
from features.x_auto_posts.x_sidecar import XPostAutoBridgeClient, DEFAULT_X_POST_INTERNAL_URL
from features.x_posts.account_blockers import read_account_publish_blockers
from features.pipeline_dispatch.backlog_contract import disposition_record
from features.pipeline_dispatch.x_prepost_recovery import digest, encoded
from scripts.recover_pipeline_x_preparation import require, RecoveryRejected, read_connection, rows, utcnow, date, document_hash
from scripts.recover_x_prepost_failures import backup, writable
from scripts.rearm_x_auto_receipt_tracking import runtime_proof

AUDIT = 'x_auto_missing_snapshot_recovery'
SCOPE = {2658: (418, 2, 9, '7'), 2674: (419, 2, 10, '9')}
ORIGINAL_SHA = {
    2658: '821d70270e9a7868c61f81557839fbd430e25e516a36f3a14d2b1acf5c1e9479',
    2674: '74aee678c5857c0855bd91b4b0caa354c9272be31fd54ee3bbaa679eccb410cc',
}
ACCOUNT_FIELDS = frozenset(('id', 'x_user_id', 'username', 'display_name', 'status',
    'publish_approved', 'publish_eligible', 'access_token_expired', 'refresh_token_available',
    'authorization_refreshable', 'access_token_status', 'drama_language', 'subscription_type',
    'premium_subscriber', 'long_video_eligible', 'long_video_publish_eligible', 'protected'))
ACCOUNT_BOOLEANS = frozenset(('publish_approved', 'publish_eligible', 'access_token_expired',
    'refresh_token_available', 'authorization_refreshable', 'premium_subscriber',
    'long_video_eligible', 'long_video_publish_eligible', 'protected'))
EMPTY_SNAPSHOT = {'account_snapshot_failed': True, 'language_source': 'account'}


def one(conn, sql, params=()):
    found = rows(conn, sql, params)
    require(len(found) == 1, 'unique_identity_required')
    return found[0]


def capture_accounts(bridge, *, now_fn=utcnow):
    """Explicit standard account verification only. May refresh credentials.

    The sidecar, under its normal account lock/service identity, performs this
    verification. This helper never opens OAuth files or sends an X write.
    """
    result = {'kind': 'x_auto_initial_snapshot_accounts', 'version': 1, 'accounts': {}}
    for account_id in (7, 9):
        account = bridge.verify_account(account_id, only_refresh_required=False,
            preserve_transient_status=True, require_publish_approved=True)
        require(str(account.get('id')) == str(account_id), 'verified_account_identity_changed')
        safe = {key: account[key] for key in ACCOUNT_FIELDS if key in account}
        result['accounts'][str(account_id)] = {'verified_at': now_fn().isoformat(), 'item': safe}
    return result


def verified_snapshot(evidence, account_id, now):
    require(evidence.get('kind') == 'x_auto_initial_snapshot_accounts' and evidence.get('version') == 1
        and set(evidence.get('accounts', {})) == {'7', '9'}, 'verified_account_evidence_invalid')
    proof = evidence['accounts'][str(account_id)]
    require(timedelta(0) <= now - date(proof['verified_at']) <= timedelta(seconds=120), 'account_verification_not_fresh')
    account = dict(proof['item'])
    require(set(account) == ACCOUNT_FIELDS and str(account['id']) == str(account_id)
        and account['status'] == 'active' and account['publish_approved'] is True
        and account['publish_eligible'] is True, 'account_not_publishable')
    require(type(account['id']) is int and all(type(account[k]) is bool for k in ACCOUNT_BOOLEANS)
        and all(isinstance(account[k], str) for k in ACCOUNT_FIELDS - ACCOUNT_BOOLEANS - {'id'}),
        'verified_account_shape_invalid')
    require(account['x_user_id'] and account['username'] and str(account['drama_language'] or '').strip(),
        'verified_identity_or_language_missing')
    # Same first-snapshot language normalization and account checks as the
    # normal Auto service/executor. There is no existing language to replace.
    language = XAutoPostService._account_language(account)
    AutoPostExecutor._validate_account(SimpleNamespace(account_id=str(account_id), language=language), account)
    account.update(drama_language=language, language_source='account')
    return account


def inspect(a, n, c, task_id, evidence, now):
    require(task_id in SCOPE, 'task_outside_exact_scope')
    task = one(a, 'SELECT * FROM x_auto_task WHERE id=?', (task_id,))
    require(digest(task) == ORIGINAL_SHA[task_id], 'original_task_changed')
    require(tuple(task[k] for k in ('run_id', 'template_id', 'template_version', 'account_id')) == SCOPE[task_id], 'task_identity_changed')
    require(task['status'] == 'failed' and task['error_code'] == 'x_accounts_unavailable'
        and task['account_snapshot_version'] == 2 and json.loads(task['account_snapshot_json']) == EMPTY_SNAPSHOT,
        'not_initial_account_snapshot_failure')
    require(not any(task[k] for k in ('language', 'account_username', 'account_display_name', 'material_id',
        'content_id', 'series_code', 'execution_run_id', 'execution_queue_id', 'execution_log_id', 'publish_id',
        'publish_url', 'unknown_outcome', 'preparation_attempt_count', 'publish_attempt_count', 'claim_phase',
        'claim_worker', 'claim_token', 'lease_expires_at_utc', 'selected_at_utc', 'reserved_at_utc', 'bridged_at_utc',
        'published_at_utc')) and json.loads(task['selection_json']) == {}, 'task_has_execution_or_frozen_snapshot')
    run = one(a, 'SELECT * FROM x_auto_run WHERE id=?', (task['run_id'],))
    require(run['status'] == 'running' and run['shanghai_date'] == '2026-11-03'
        and (run['template_id'], run['template_version']) == (task['template_id'], task['template_version']), 'parent_not_running_original_run')
    template = one(a, 'SELECT * FROM x_auto_template_version WHERE template_id=? AND version=?',
        (task['template_id'], task['template_version']))
    config = json.loads(template['config_json'])
    require(config.get('language_source') == 'account' and task['account_id'] in [str(v) for v in config['account_ids']]
        and config['body_template'] == task['body_template'], 'frozen_template_changed')
    require(one(a, 'SELECT enabled FROM x_auto_template WHERE id=?', (task['template_id'],))['enabled'] == 1,
        'template_disabled')
    events = rows(a, 'SELECT * FROM x_auto_event WHERE task_id=? ORDER BY id', (task_id,))
    require([r['event_type'] for r in events] == ['task_created', 'task_account_snapshot_failed'], 'unexpected_task_history')
    require(not rows(a, 'SELECT * FROM x_auto_material_ledger WHERE task_id=?', (task_id,)), 'material_ledger_exists')
    key = 'x:auto:' + str(task_id)
    require(not a.execute('SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?', (key,)).fetchone()
        and disposition_record(a, key, task) not in ('archive', 'blocked'), 'task_held')
    require(not n.execute('SELECT 1 FROM x_post_manual_run WHERE external_task_key=?', ('x-auto-task-' + str(task_id),)).fetchone(),
        'native_execution_exists')
    job = one(c, 'SELECT * FROM pipeline_job WHERE source_key=?', (key,))
    require((job['channel'], job['family'], job['stage'], job['status'], job['generation'], job['hold_reason'])
        == ('x', 'auto', 'prepare', 'failed', 0, 'adapter_unavailable')
        and not any(job[k] for k in ('worker_id', 'node_id', 'claim_hash', 'lease_expires_at', 'external_intent', 'canonical_receipt')),
        'central_control_projection_has_execution')
    for table in ('pipeline_claim_history', 'pipeline_operation', 'pipeline_receipt', 'pipeline_resource', 'pipeline_legacy_resource'):
        require(not c.execute('SELECT 1 FROM ' + table + ' WHERE job_id=?', (job['id'],)).fetchone(), 'central_execution_evidence_exists')
    require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?', (key,)).fetchone(), 'central_publication_exists')
    snapshot = verified_snapshot(evidence, task['account_id'], now)
    native_account = one(n, 'SELECT * FROM x_authorized_account WHERE id=?', (task['account_id'],))
    for key in ('id', 'x_user_id', 'username', 'status', 'drama_language'):
        require(str(native_account[key]) == str(snapshot[key]), 'native_account_changed_' + key)
    require(native_account['publish_approved'] == 1 and not read_account_publish_blockers(n, [int(task['account_id'])]), 'native_account_blocked')
    return {'task': task, 'run': run, 'template': template, 'events': events,
        'central_job_id': job['id'], 'snapshot': snapshot, 'native_account_sha256': digest(native_account)}


def make_plan(auto, native, central, evidence, *, now=None):
    now = now or utcnow()
    runtime = runtime_proof(auto)
    with contextlib.closing(read_connection(auto)) as a, contextlib.closing(read_connection(native)) as n, contextlib.closing(read_connection(central)) as c:
        for conn in (a, n, c): conn.execute('BEGIN')
        items = [inspect(a, n, c, task_id, evidence, now) for task_id in sorted(SCOPE)]
    result = {'kind': 'x_auto_missing_snapshot_recovery', 'version': 1,
        'auto': str(Path(auto).resolve()), 'native': str(Path(native).resolve()), 'central': str(Path(central).resolve()),
        'created_at': now.isoformat(), 'runtime': runtime, 'account_evidence': evidence, 'items': items}
    return dict(result, plan_sha256=document_hash(result))


def existing(a):
    if not a.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', AUDIT)).fetchone():
        return []
    return rows(a, 'SELECT task_id,plan_sha256 FROM ' + AUDIT + ' ORDER BY task_id')


def install_audit(a):
    a.execute('CREATE TABLE IF NOT EXISTS ' + AUDIT + ' (task_id INTEGER PRIMARY KEY CHECK(task_id IN (2658,2674)),plan_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)')
    for action in ('UPDATE', 'DELETE'):
        a.execute('CREATE TRIGGER IF NOT EXISTS ' + AUDIT + '_no_' + action.lower() + ' BEFORE ' + action + ' ON ' + AUDIT
            + " BEGIN SELECT RAISE(ABORT,'initial_snapshot_audit_immutable'); END")
    a.execute('CREATE TRIGGER IF NOT EXISTS ' + AUDIT + '_no_replace BEFORE INSERT ON ' + AUDIT
        + ' WHEN EXISTS(SELECT 1 FROM ' + AUDIT + " WHERE task_id=NEW.task_id) BEGIN SELECT RAISE(ABORT,'initial_snapshot_audit_immutable'); END")


def apply(saved, backup_dir, actor, *, now=None):
    fixed_now = now is not None
    now = now or utcnow()
    require(saved.get('kind') == 'x_auto_missing_snapshot_recovery' and saved.get('version') == 1
        and document_hash(saved) == saved['plan_sha256'] and str(actor).strip(), 'plan_or_actor_invalid')
    require([item['task']['id'] for item in saved['items']] == sorted(SCOPE), 'exact_two_tasks_required')
    expected_prior = [{'task_id': tid, 'plan_sha256': saved['plan_sha256']} for tid in sorted(SCOPE)]
    with contextlib.closing(read_connection(saved['auto'])) as a:
        prior = existing(a)
        if prior:
            require(prior == expected_prior, 'different_or_incomplete_recovery')
            return {'already_applied': True, 'task_ids': sorted(SCOPE), 'plan_sha256': saved['plan_sha256'], 'publish_requests': 0}
    require(runtime_proof(saved['auto']) == saved['runtime'], 'runtime_changed')
    saved_backup = backup(saved['auto'], backup_dir, 'auto-initial-snapshots')
    if not fixed_now: now = utcnow()
    with contextlib.closing(writable(saved['auto'])) as a, contextlib.closing(read_connection(saved['native'])) as n, contextlib.closing(read_connection(saved['central'])) as c:
        a.execute('BEGIN IMMEDIATE'); n.execute('BEGIN'); c.execute('BEGIN')
        try:
            require(not existing(a), 'concurrent_recovery')
            current = [inspect(a, n, c, tid, saved['account_evidence'], now) for tid in sorted(SCOPE)]
            require(current == saved['items'], 'plan_state_drift')
            require(runtime_proof(saved['auto']) == saved['runtime'], 'runtime_changed')
            install_audit(a)
            for item in current:
                task = item['task']; snapshot = item['snapshot']
                proof = {'previous': item, 'actor': actor, 'backup': saved_backup,
                    'account_evidence': saved['account_evidence'], 'runtime': saved['runtime'], 'plan_sha256': saved['plan_sha256']}
                a.execute('INSERT INTO ' + AUDIT + ' VALUES(?,?,?,?)', (task['id'], saved['plan_sha256'], encoded(proof), now.isoformat()))
                count = a.execute("UPDATE x_auto_task SET status='pending',language=?,account_username=?,account_display_name=?,"
                    "account_snapshot_json=?,error_code='',error_message='',finished_at_utc='',updated_at=? WHERE id=? AND status='failed' AND language=''",
                    (snapshot['drama_language'], snapshot['username'], snapshot['display_name'], encoded(snapshot), now.isoformat(), task['id'])).rowcount
                require(count == 1, 'task_compare_and_swap_failed')
                a.execute("INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(?,?,'initial_account_snapshot_completed','failed','pending','Complete missing first account snapshot before material selection',?,?)",
                    (task['run_id'], task['id'], encoded({'plan_sha256': saved['plan_sha256'], 'language': snapshot['drama_language']}), now.isoformat()))
            a.commit()
        except BaseException:
            a.rollback(); raise
    return {'already_applied': False, 'task_ids': sorted(SCOPE), 'plan_sha256': saved['plan_sha256'], 'backup': saved_backup,
        'publish_requests': 0, 'native_modified': False, 'central_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capture-accounts', 'plan', 'apply'))
    for name in ('auto', 'native', 'central', 'accounts', 'plan', 'backup-dir', 'actor'): parser.add_argument('--' + name)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8') as out:
        if args.action == 'capture-accounts':
            bridge = XPostAutoBridgeClient(DEFAULT_X_POST_INTERNAL_URL, os.environ.get('X_POST_AUTO_INTERNAL_TOKEN', ''))
            result = capture_accounts(bridge)
        elif args.action == 'plan':
            require(all((args.auto, args.native, args.central, args.accounts)), 'plan_arguments_required')
            result = make_plan(args.auto, args.native, args.central, json.loads(Path(args.accounts).read_text(encoding='utf-8')))
        else:
            require(all((args.plan, args.backup_dir, args.actor)), 'apply_arguments_required')
            result = apply(json.loads(Path(args.plan).read_text(encoding='utf-8')), args.backup_dir, args.actor)
        json.dump(result, out, ensure_ascii=False, indent=2); out.flush(); os.fsync(out.fileno())
    print(json.dumps({'ok': True, 'action': args.action, 'task_ids': sorted(SCOPE), 'publish_requests': 0}))


if __name__ == '__main__':
    try: main()
    except RecoveryRejected as exc:
        print(json.dumps({'ok': False, 'code': str(exc)})); raise SystemExit(2)
