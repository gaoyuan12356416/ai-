#!/usr/bin/env python3
"""Audited disposal of a frozen paused backlog, never a general retry command.

Plan is read-only. Apply touches only sources in the hash-approved plan and
rechecks each source under its database write lock. It never calls a service,
renderer or platform. Native and central commits are journalled separately so
a crash cannot cause a second native action. Private plans must not enter Git.
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
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.adapters import AdapterRegistry
from features.pipeline_dispatch.backlog_contract import SCHEMA, authorization_identity, source_fingerprint


JOURNAL = """CREATE TABLE IF NOT EXISTS pipeline_backlog_apply_journal(
 plan_sha256 TEXT NOT NULL, source_key TEXT NOT NULL, phase TEXT NOT NULL,
 action TEXT NOT NULL, item_sha256 TEXT NOT NULL, before_sha256 TEXT NOT NULL,
 after_sha256 TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(plan_sha256,source_key,phase))"""
KINDS = {
    ('x', 'auto'): ('x_auto', 'x_auto_task', 'id', {'pending', 'retry_wait'}, 'canceled'),
    ('x', 'queue'): ('x', 'x_post_queue', 'id', {'queued', 'waiting_relay'}, 'cancelled'),
    ('tt', 'auto'): ('tt_auto', 'tt_auto_task', 'id', {'pending', 'ready'}, 'canceled'),
    ('fb', 'video'): ('fb', 'fb_video_task', 'task_id', {'queued'}, 'cancelled'),
    ('youtube', 'publish'): ('youtube', 'drama_youtube_publish', 'id', {'queued'}, 'cancelled'),
}
CENTRAL_EVIDENCE = ('pipeline_claim_history', 'pipeline_operation', 'pipeline_checkpoint',
                    'pipeline_receipt', 'pipeline_delivery', 'pipeline_phase_metric',
                    'pipeline_resource', 'pipeline_legacy_resource')
PUBLISH_FIELDS = ('publish_id', 'x_post_id', 'graph_post_id', 'video_id', 'source_post_id', 'repost_id',
                  'x_media_id', 'resumable_session_uri')
ATTEMPT_FIELDS = ('publish_attempt_count', 'video_attempt_count', 'comment_attempt_count',
                  'attempt_count', 'source_attempt_count', 'repost_attempt_count', 'definite_attempts')
LEASE_FIELDS = ('lease_expires_at_utc', 'prepare_until', 'link_until', 'execution_until')


class DisposalRejected(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise DisposalRejected(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def sha(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def without_hash(value, name):
    return sha({k: v for k, v in value.items() if k != name})


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def date(value):
    try:
        result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        require(result.tzinfo is not None, 'timezone_required')
        return result.astimezone(dt.timezone.utc)
    except (ValueError, TypeError):
        raise DisposalRejected('invalid_timestamp') from None


def day(value):
    return date(value).astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()


def connection(path, *, write=False):
    path = Path(path).resolve()
    require(path.is_file(), 'database_missing')
    c = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True,
                        timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA busy_timeout=15000')
    c.execute('PRAGMA synchronous=FULL' if write else 'PRAGMA query_only=ON')
    return c


def exists(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def rows(c, table, column, value):
    require(re.fullmatch(r'[a-z_]+', table) and re.fullmatch(r'[a-z_]+', column), 'invalid_table')
    if not exists(c, table):
        return []
    return sorted((dict(r) for r in c.execute('SELECT * FROM ' + table + ' WHERE ' + column + '=?', (value,))), key=encoded)


def native_connection(config, key, *, write=False):
    n = connection(config['paths'][key], write=write)
    try:
        if key == 'x' and config['paths'].get('x_auto'):
            # BEGIN IMMEDIATE reserves attached databases too. The attachment
            # is read-only in plan, writable-capable in apply solely so SQLite
            # can lock it. Every mutation still explicitly targets main.
            mode = '?mode=rw' if write else '?mode=ro'
            n.execute('ATTACH DATABASE ? AS pipeline_x_auto', (Path(config['paths']['x_auto']).resolve().as_uri() + mode,))
    except BaseException:
        n.close(); raise
    return n


def central_snapshot(c, key):
    jobs = rows(c, 'pipeline_job', 'source_key', key)
    evidence = {table: {str(j['id']): rows(c, table, 'job_id', j['id']) for j in jobs} for table in CENTRAL_EVIDENCE}
    return {'jobs': jobs, 'evidence': evidence, 'publication': rows(c, 'pipeline_publication', 'source_key', key)}


def central_guard_sha(proof):
    # The collector may refresh these two observations while admission is
    # paused. Ownership, material/payload, stage, schedule and all write proof
    # remain part of the CAS; do not broaden this exception to source metadata.
    value = dict(proof)
    value['jobs'] = [{k: v for k, v in j.items() if k not in ('updated_at', 'source_status')} for j in proof['jobs']]
    return sha(value)


def native_snapshot(n, item):
    _, table, pk, _, _ = KINDS[(item['channel'], item['family'])]
    source = rows(n, table, pk, item['source_id'])
    require(len(source) == 1, 'native_source_missing')
    key = item['source_key']
    evidence = {}
    for record in n.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pipeline_%'"):
        name = record[0]
        if not re.fullmatch(r'[a-z_]+', name) or not name.endswith(('_claim', '_operation', '_checkpoint')):
            continue
        if 'source_key' in {r[1] for r in n.execute('PRAGMA table_info(' + name + ')')}:
            evidence[name] = rows(n, name, 'source_key', key)
    related = {
        ('x', 'queue'): (('x_post_publish_log', 'queue_id'), ('x_post_repost_ledger', 'queue_id'),
                         ('x_post_random_template_preparation', 'queue_id'), ('pipeline_x_artifact', 'queue_id')),
        ('x', 'auto'): (('x_auto_material_ledger', 'task_id'),),
        ('tt', 'auto'): (('tt_auto_material_ledger', 'task_id'),),
        ('fb', 'video'): (('fb_video_attempt', 'task_id'), ('fb_video_link', 'task_id')),
        ('youtube', 'publish'): (('drama_youtube_publish_event', 'task_id'),),
    }
    for name, column in related[(item['channel'], item['family'])]:
        evidence[name] = rows(n, name, column, item['source_id'])
    if (item['channel'], item['family']) == ('x', 'queue'):
        for field, parent in (('manual_run_id', 'x_post_manual_run'), ('schedule_run_id', 'x_post_schedule_run')):
            if source[0].get(field):
                # Aggregate run status can change when a different child runs;
                # retain only the exact immutable schedule/owner lineage here.
                evidence[parent] = [{k: v for k, v in r.items() if k in ('id', 'created_at', 'created_at_utc', 'scheduled_at', 'scheduled_at_utc', 'template_ref', 'template_version', 'external_task_key', 'owner_user_id', 'owner_tenant_key')}
                                    for r in rows(n, parent, 'id', source[0][field])]
        if 'pipeline_x_auto' in {r[1] for r in n.execute('PRAGMA database_list')}:
            evidence['origin_auto'] = [dict(r) for r in n.execute('SELECT * FROM pipeline_x_auto.x_auto_task WHERE execution_queue_id=?', (item['source_id'],))]
    return {'source': source[0], 'hold': rows(n, 'pipeline_canonical_hold', 'source_key', key), 'evidence': evidence}


def require_no_execution(cproof, nproof, item, now):
    paused_ids = item['paused_job_ids']
    require(paused_ids and set(paused_ids) == {j['id'] for j in cproof['jobs'] if j['status'] == 'backlog_paused'}, 'paused_set_changed')
    for j in cproof['jobs']:
        if j['id'] not in paused_ids:
            require(item['ghost'] and j['stage'] == 'publish' and j['status'] == 'published', 'other_stage_not_terminal')
            continue
        require(j['generation'] == 0 and not any(j.get(k) for k in (
            'external_intent', 'canonical_receipt', 'claim_hash', 'lease_expires_at', 'node_id', 'worker_id')),
            'central_execution_evidence')
        require(j['hold_reason'] in ('cutover_overdue', 'cutover_backlog'), 'unexpected_hold_reason')
        for evidence in cproof['evidence'].values():
            require(not evidence[str(j['id'])], 'central_execution_evidence')
    source = nproof['source']
    require(not source.get('unknown_outcome'), 'native_unknown')
    if item['ghost']:
        require((item['channel'], item['family']) == ('x', 'queue') and source['status'] == 'published', 'ghost_source_not_published')
        logs = nproof['evidence'].get('x_post_publish_log', [])
        require(len(logs) == 1 and logs[0]['status'] == 'published' and logs[0]['x_post_id']
                and not logs[0]['unknown_outcome'], 'ghost_receipt_missing')
        require(len(cproof['publication']) == 1 and cproof['publication'][0]['publish_id'] == logs[0]['x_post_id'], 'ghost_receipt_mismatch')
        require(all(j['stage'] == 'prepare' for j in cproof['jobs'] if j['id'] in paused_ids), 'ghost_stage_not_prepare')
        return
    require(not cproof['publication'], 'publication_exists')
    require(source['status'] in KINDS[(item['channel'], item['family'])][3], 'native_not_untouched')
    require(len(nproof['hold']) == 1, 'native_hold_missing')
    for field in LEASE_FIELDS:
        if source.get(field):
            require(date(source[field]) <= now, 'native_active_lease')
    require(not any(source.get(k) for k in ('claim_worker', 'claim_token', 'lease_owner', 'execution_lease', 'prepare_lease', 'link_lease')), 'native_claim_present')
    for row in [source] + nproof['evidence'].get('x_post_publish_log', []) + nproof['evidence'].get('x_post_repost_ledger', []):
        require(not row.get('unknown_outcome') and not any(row.get(k) for k in PUBLISH_FIELDS + ATTEMPT_FIELDS), 'native_publish_evidence')
    require(not nproof['evidence'].get('fb_video_attempt'), 'native_publish_evidence')
    for table, values in nproof['evidence'].items():
        if table.startswith('pipeline_') and table.endswith(('_claim', '_operation', '_checkpoint')):
            require(not values, 'native_dispatch_evidence')
    require(not source.get('execution_queue_id'), 'auto_child_exists')


def registry(config):
    from types import SimpleNamespace
    drivers = {(v['channel'], family): SimpleNamespace(control_only=v.get('control_only', False))
               for v in config.get('drivers', []) for family in v['families']}
    return AdapterRegistry.from_paths(config['paths'], drivers, preparation_policy=config.get('preparation_policy'))


def frozen_item(c, n, adapter, scoped, target_date, now):
    key = scoped['source_key']
    channel, family, source_id = key.split(':', 2)
    require((channel, family) in KINDS and scoped['channel'] == channel and scoped['family'] == family
            and str(scoped['source_id']) == source_id, 'scope_identity_invalid')
    scheduled = scoped['scheduled_at']
    require(day(scheduled) == scoped['scheduled_day_bjt'] <= target_date, 'scope_day_invalid')
    item = {'source_key': key, 'channel': channel, 'family': family, 'source_id': source_id,
            'action': 'resume' if day(scheduled) == target_date else 'archive',
            'ghost': scoped['native_status'] == 'published', 'scheduled_at': scheduled,
            'paused_job_ids': sorted(j['id'] for j in scoped['jobs'])}
    if item['ghost']:
        require(item['action'] == 'archive', 'published_source_cannot_resume')
    cp, np = central_snapshot(c, key), native_snapshot(n, item)
    require(sha(np['source']) == scoped['native_row_sha256'] and np['hold'] == ([scoped['native_hold']] if scoped.get('native_hold') else []), 'snapshot_native_drift')
    expected = {j['id']: j for j in scoped['jobs']}
    scope_fields = ('stage', 'status', 'generation', 'hold_reason', 'scheduled_at', 'source_created_at',
                    'external_intent', 'owner_user_id', 'owner_tenant_key', 'required_module')
    require(all(all(j.get(k) == expected[j['id']].get(k) for k in scope_fields)
                for j in cp['jobs'] if j['id'] in expected), 'snapshot_central_drift')
    require_no_execution(cp, np, item, now)
    enriched = adapter.enrich(n, np['source'])
    # Preview exactly the projection after this source's hold is removed. No
    # database mutation occurs in plan, and other business gates remain set.
    if item['action'] == 'resume':
        enriched['canonical_held'] = False
    offer = adapter.project(enriched)
    require(offer is not None and not offer.get('legacy_inflight'), 'native_projection_unavailable')
    actual_schedule = offer.get('cutover_due_at') or offer.get('scheduled_at')
    require(actual_schedule and date(actual_schedule) == date(scheduled), 'original_schedule_changed')
    if item['action'] == 'resume':
        require(any(j['stage'] == offer['stage'] for j in cp['jobs'] if j['id'] in item['paused_job_ids']), 'requested_stage_missing')
    identity_row = dict(np['source'])
    # X queue dates live in its canonical parent; immediate legacy YT uses its
    # original enqueue timestamp. Neither case changes the stored schedule.
    identity_row['scheduled_at'] = scheduled
    identity = authorization_identity(channel, family, identity_row)
    require(identity['source_key'] == key, 'authorization_identity_mismatch')
    item.update(identity=identity, source_fingerprint=source_fingerprint(identity), requested_stage=offer['stage'],
                native_db_key=KINDS[(channel, family)][0], native_sha256=sha(np), central_sha256=sha(cp),
                central_guard_sha256=central_guard_sha(cp),
                native_row_sha256=sha(np['source']), hold=np['hold'],
                central_rows_sha256={str(j['id']): sha(j) for j in cp['jobs']},
                paused_jobs=[dict(j, row_sha256=sha(j)) for j in cp['jobs'] if j['id'] in item['paused_job_ids']],
                current_offer=offer)
    item['item_sha256'] = without_hash(item, 'item_sha256')
    return item


def make_plan(config, snapshot, expected_snapshot_sha, now=None):
    now = now or utcnow()
    require(snapshot.get('snapshot_sha256') == expected_snapshot_sha == without_hash(snapshot, 'snapshot_sha256'), 'snapshot_hash_mismatch')
    require(snapshot.get('read_only') is True and snapshot.get('day_basis') == 'original_publish_schedule_Asia/Shanghai', 'snapshot_kind_invalid')
    require(snapshot['target_date'] < day(now.isoformat()), 'target_day_not_historical')
    scopes = snapshot['items']
    require(0 < len(scopes) <= 5000 and len({i['source_key'] for i in scopes}) == len(scopes), 'invalid_scope_size')
    reg = registry(config)
    result = {'kind': 'paused_backlog_disposal_plan', 'version': 1, 'snapshot_sha256': expected_snapshot_sha,
              'captured_at': now.isoformat(), 'target_date': snapshot['target_date'],
              'central_path': str(Path(config['db_path']).resolve()), 'native_paths': {}, 'items': [], 'blocked': []}
    with contextlib.ExitStack() as stack:
        c = stack.enter_context(contextlib.closing(connection(config['db_path']))); c.execute('BEGIN')
        native = {}
        for scoped in scopes:
            try:
                kind = (scoped['channel'], scoped['family'])
                require(kind in KINDS, 'unsupported_scope_family')
                key = KINDS[kind][0]
                if key not in native:
                    native[key] = stack.enter_context(contextlib.closing(native_connection(config, key))); native[key].execute('BEGIN')
                    result['native_paths'][key] = str(Path(config['paths'][key]).resolve())
                result['items'].append(frozen_item(c, native[key], reg.adapters[kind], scoped, snapshot['target_date'], now))
            except DisposalRejected as exc:
                result['blocked'].append({'source_key': scoped['source_key'], 'code': str(exc)})
            except sqlite3.Error as exc:
                # A table with a different legacy shape must block this exact
                # source, never silently omit its evidence or abort all scopes.
                result['blocked'].append({'source_key': scoped['source_key'], 'code': 'native_schema_or_database_conflict',
                                          'sqlite_code': getattr(exc, 'sqlite_errorname', 'SQLITE_ERROR')})
    result['plan_sha256'] = without_hash(result, 'plan_sha256')
    return result


def private_json(path, value):
    with os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n'); f.flush(); os.fsync(f.fileno())


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def backup_databases(plan, directory):
    directory = Path(directory).resolve()
    require(Path(plan['central_path']).parent in directory.parents, 'backup_must_stay_on_central_data_directory')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = directory / ('backups-' + plan['plan_sha256'] + '.json')
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        require(saved['plan_sha256'] == plan['plan_sha256'], 'backup_plan_mismatch')
        require(all(Path(x['path']).is_file() and file_sha(x['path']) == x['sha256'] for x in saved['databases']), 'backup_hash_mismatch')
        return saved
    entries = []
    for index, path in enumerate(sorted({plan['central_path'], *plan['native_paths'].values()})):
        dest = directory / ('ledger-' + plan['plan_sha256'][:16] + '-' + str(index) + '-' + uuid.uuid4().hex + '.sqlite3')
        with os.fdopen(os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb'):
            pass
        with contextlib.closing(connection(path)) as source, contextlib.closing(sqlite3.connect(str(dest))) as target:
            source.backup(target)
            require([tuple(r) for r in target.execute('PRAGMA quick_check')] == [('ok',)], 'backup_integrity_failed')
        with dest.open('r+b') as f:
            os.fsync(f.fileno())
        entries.append({'source_path': path, 'path': str(dest), 'sha256': file_sha(dest)})
    saved = {'plan_sha256': plan['plan_sha256'], 'databases': entries}
    private_json(manifest, saved)
    return saved


def journal(c, plan_sha, item, phase, before, after, now, details):
    c.execute('INSERT INTO pipeline_backlog_apply_journal VALUES(?,?,?,?,?,?,?,?,?)',
              (plan_sha, item['source_key'], phase, item['action'], item['item_sha256'], before, after, encoded(details), now))


def disposition(c, plan_sha, item, now, actor):
    existing = c.execute('SELECT * FROM pipeline_backlog_disposition WHERE source_key=?', (item['source_key'],)).fetchone()
    require(existing is None, 'disposition_already_exists')
    identity = item['identity']
    c.execute('INSERT INTO pipeline_backlog_disposition VALUES(?,?,?,?,?,?,?,?)',
              (item['source_key'], plan_sha, item['action'], 'committed', item['source_fingerprint'],
               identity['scheduled_at'], identity['created_at'], encoded({'identity': identity, 'actor': actor, 'approved_at': now})))


def native_action(n, item, plan_sha, now, actor):
    if item['action'] == 'archive':
        _, table, pk, _, cancelled = KINDS[(item['channel'], item['family'])]
        columns = {r[1] for r in n.execute('PRAGMA table_info(' + table + ')')}
        changes = {'status': cancelled}
        for field in ('finished_at_utc', 'completed_at_utc', 'updated_at', 'updated_at_utc'):
            if field in columns:
                changes[field] = now
        require(len(changes) > 1, 'native_cancel_timestamp_unavailable')
        n.execute('UPDATE ' + table + ' SET ' + ','.join(k + '=?' for k in changes) + ' WHERE ' + pk + '=?', [*changes.values(), item['source_id']])
    disposition(n, plan_sha, item, now, actor)
    if item['action'] == 'resume':
        require(n.execute('DELETE FROM pipeline_canonical_hold WHERE source_key=?', (item['source_key'],)).rowcount == 1, 'native_hold_delete_conflict')


def central_commit(c, item, plan_sha, now):
    # The coordinator owns stage selection and successor authorization. Do not
    # construct a DispatchStore (which would run migrations) inside this tool.
    from features.pipeline_dispatch.backlog_release import commit_backlog_item
    try:
        return commit_backlog_item(c, item, plan_sha, now)
    except ValueError as exc:
        if re.fullmatch(r'backlog_[a-z_]+', str(exc)):
            raise DisposalRejected(str(exc)) from None
        raise


PLANNER_UNITS = ('x-auto-post-service.service', 'x-auto-post-runner.service', 'x-auto-post-scheduler.service',
                 'x-auto-post-runner.timer', 'x-auto-post-scheduler.timer', 'x-auto-post-runner.path')


def planner_quiescent():
    """Observe admission only. This tool never stops any unit."""
    try:
        output = subprocess.check_output(['systemctl', 'show', *PLANNER_UNITS, '--property=Id,LoadState,ActiveState,MainPID'], text=True, timeout=15)
        states = [dict(line.split('=', 1) for line in block.splitlines() if '=' in line) for block in output.strip().split('\n\n')]
        require({s.get('Id') for s in states} == set(PLANNER_UNITS), 'x_auto_planner_inventory_incomplete')
        require(all(s.get('LoadState') == 'loaded' and s.get('ActiveState') == 'inactive'
                    and (not s['Id'].endswith('.service') or s.get('MainPID') == '0') for s in states), 'x_auto_planner_not_quiescent')
        return states
    except (OSError, subprocess.SubprocessError):
        raise DisposalRejected('x_auto_planner_probe_failed') from None


def apply_item(config, plan, item, actor, *, clock=utcnow, commit=central_commit, crash_hook=None, planner_guard=planner_quiescent):
    require(item['item_sha256'] == without_hash(item, 'item_sha256'), 'item_hash_mismatch')
    stamp = clock().isoformat(); plan_sha = plan['plan_sha256']; key = item['source_key']
    c = connection(config['db_path'], write=True)
    try:
        n = native_connection(config, item['native_db_key'], write=not item['ghost'])
    except BaseException:
        c.close(); raise
    try:
        c.execute('BEGIN IMMEDIATE'); c.execute(JOURNAL); c.execute(SCHEMA)
        done = c.execute("SELECT * FROM pipeline_backlog_apply_journal WHERE plan_sha256=? AND source_key=? AND phase='completed'", (plan_sha, key)).fetchone()
        if done:
            require(done['item_sha256'] == item['item_sha256'] and done['action'] == item['action'], 'completed_journal_conflict')
            authorization = c.execute('SELECT * FROM pipeline_backlog_disposition WHERE source_key=?', (key,)).fetchone()
            require(authorization and authorization['plan_sha256'] == plan_sha and authorization['state'] == 'committed'
                    and authorization['action'] == item['action'] and authorization['source_fingerprint'] == item['source_fingerprint'],
                    'completed_authorization_conflict')
            c.rollback()
            return {'source_key': key, 'status': 'already_committed', 'action': item['action']}
        if (item['channel'], item['family']) == ('x', 'auto'):
            planner_guard()
        cp = central_snapshot(c, key)
        require(central_guard_sha(cp) == item['central_guard_sha256'], 'central_plan_drift')
        if item['ghost']:
            n.execute('BEGIN'); np = native_snapshot(n, item)
            require(sha(np) == item['native_sha256'], 'native_plan_drift')
            require_no_execution(cp, np, item, clock())
        else:
            n.execute('BEGIN IMMEDIATE'); n.execute(JOURNAL); n.execute(SCHEMA)
            previous = n.execute("SELECT * FROM pipeline_backlog_apply_journal WHERE plan_sha256=? AND source_key=? AND phase='native_committed'", (plan_sha, key)).fetchone()
            np = native_snapshot(n, item)
            if previous:
                require(previous['item_sha256'] == item['item_sha256'] and previous['action'] == item['action'], 'native_journal_conflict')
                require(sha(np) == previous['after_sha256'], 'native_postcommit_drift')
                auth = n.execute('SELECT * FROM pipeline_backlog_disposition WHERE source_key=?', (key,)).fetchone()
                require(auth and auth['plan_sha256'] == plan_sha and auth['state'] == 'committed' and auth['action'] == item['action'] and auth['source_fingerprint'] == item['source_fingerprint'], 'native_authorization_conflict')
            else:
                require(sha(np) == item['native_sha256'], 'native_plan_drift')
                require_no_execution(cp, np, item, clock())
                native_action(n, item, plan_sha, stamp, actor)
                after = native_snapshot(n, item)
                journal(n, plan_sha, item, 'native_committed', sha(np), sha(after), stamp, {'actor': actor})
            n.commit()
            if crash_hook:
                crash_hook('after_native_commit', item)
        # Central admission is the final commit. Until this point a committed
        # native resume cannot authorize a new descendant in the coordinator.
        commit_item = item
        if item['action'] == 'resume':
            adapter = registry(config).adapters[(item['channel'], item['family'])]
            raw = native_snapshot(n, item)['source']
            current_offer = adapter.project(adapter.enrich(n, raw))
            require(current_offer is not None and current_offer['stage'] == item['requested_stage'], 'requested_stage_changed')
            commit_item = dict(item, current_offer=current_offer)
        result = commit(c, commit_item, plan_sha, stamp)
        journal(c, plan_sha, item, 'completed', item['central_sha256'], sha(central_snapshot(c, key)), stamp,
                {'actor': actor, 'native_modified': not item['ghost'], 'result': result})
        c.commit()
        return {'source_key': key, 'status': 'committed', 'action': item['action'], 'native_modified': not item['ghost']}
    except BaseException:
        c.rollback(); n.rollback(); raise
    finally:
        c.close(); n.close()


def apply_plan(config, plan, expected_sha, backup_dir, actor, *, clock=utcnow, commit=central_commit, crash_hook=None, planner_guard=planner_quiescent):
    require(plan.get('kind') == 'paused_backlog_disposal_plan' and plan.get('version') == 1, 'plan_kind_invalid')
    require(plan.get('plan_sha256') == expected_sha == without_hash(plan, 'plan_sha256'), 'plan_hash_mismatch')
    require(str(actor).strip(), 'audit_actor_required')
    require(str(Path(config['db_path']).resolve()) == plan['central_path'], 'central_path_changed')
    require(all(str(Path(config['paths'][key]).resolve()) == path for key, path in plan['native_paths'].items()), 'native_paths_changed')
    with contextlib.closing(connection(config['db_path'])) as c:
        pending_auto = [item for item in plan['items'] if (item['channel'], item['family']) == ('x', 'auto')
                        and (not exists(c, 'pipeline_backlog_apply_journal') or not c.execute("SELECT 1 FROM pipeline_backlog_apply_journal WHERE plan_sha256=? AND source_key=? AND phase='completed'", (expected_sha, item['source_key'])).fetchone())]
    if pending_auto:
        planner_guard()
    backup = backup_databases(plan, backup_dir)
    results = []
    for item in plan['items']:
        try:
            results.append(apply_item(config, plan, item, actor, clock=clock, commit=commit, crash_hook=crash_hook, planner_guard=planner_guard))
        except DisposalRejected as exc:
            results.append({'source_key': item['source_key'], 'status': 'blocked', 'code': str(exc)})
        except sqlite3.Error as exc:
            results.append({'source_key': item['source_key'], 'status': 'blocked', 'code': 'database_conflict',
                            'sqlite_code': getattr(exc, 'sqlite_errorname', 'SQLITE_ERROR')})
    return {'plan_sha256': expected_sha, 'at': clock().isoformat(), 'backup_manifest': backup, 'items': results, 'plan_blocked': plan.get('blocked', [])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'apply'))
    parser.add_argument('--config', required=True); parser.add_argument('--output', required=True)
    parser.add_argument('--snapshot'); parser.add_argument('--snapshot-sha256')
    parser.add_argument('--plan'); parser.add_argument('--plan-sha256')
    parser.add_argument('--backup-dir'); parser.add_argument('--actor')
    args = parser.parse_args(); config = json.loads(Path(args.config).read_text())
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as out:
        if args.action == 'plan':
            require(args.snapshot and args.snapshot_sha256, 'snapshot_required')
            result = make_plan(config, json.loads(Path(args.snapshot).read_text()), args.snapshot_sha256)
        else:
            require(args.plan and args.plan_sha256 and args.backup_dir and args.actor, 'apply_arguments_required')
            result = apply_plan(config, json.loads(Path(args.plan).read_text()), args.plan_sha256, args.backup_dir, args.actor)
        out.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n'); out.flush(); os.fsync(out.fileno())
    blocked = len(result.get('blocked', [])) + len(result.get('plan_blocked', [])) + sum(x.get('status') == 'blocked' for x in result.get('items', []))
    print(encoded({'action': args.action, 'plan_sha256': result['plan_sha256'], 'items': len(result.get('items', [])), 'blocked': blocked}))
    return 2 if blocked else 0


if __name__ == '__main__':
    raise SystemExit(main())
