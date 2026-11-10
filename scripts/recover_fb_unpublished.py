"""Explicit, exact-task recovery after proving every prior attempt did not publish.

Default: read-only plan. Apply requires that plan's hash, an online backup and
paused FB admission. Unknown results, published objects and unresolved native
write permits are never recovered. Historical attempts/claims are retained.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.core import dumps

PREWRITE = {'identity_unavailable', 'group_pool_source_unavailable', 'source_busy'}
REJECTED = {'fb_graph_190', 'fb_graph_368'}
EVENT = 'authorized_fb_unpublished_recovery'

def sha(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()

def require(condition, reason):
    if not condition:
        raise ValueError(reason)

def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args)]

def proof(c, job_id):
    j = rows(c, 'SELECT * FROM pipeline_job WHERE id=?', (job_id,))
    require(len(j) == 1, 'job_missing')
    j = j[0]
    require(j['channel'] == 'fb' and j['stage'] == 'publish' and j['family'] in {'video', 'auto'}, 'scope_invalid')
    require(j['source_key'] == 'fb:%s:%s' % (j['family'], j['source_id']), 'source_identity_changed')
    prior = rows(c, 'SELECT details_json FROM pipeline_event WHERE job_id=? AND event_type=?', (job_id, EVENT))
    if prior:
        return {'job_id': job_id, 'source_key': j['source_key'], 'already_recovered': True}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    require(j['status'] == 'failed' and not j['hold_reason'] and (not j['lease_expires_at'] or j['lease_expires_at'] < now), 'central_not_failed_idle')
    require(j['error_code'] in PREWRITE | {'canonical_execution_failed'}, 'failure_not_allowed')
    require(not rows(c, 'SELECT 1 FROM pipeline_publication WHERE source_key=?', (j['source_key'],)), 'publication_present')
    for table in ('pipeline_resource', 'pipeline_legacy_resource'):
        require(not rows(c, 'SELECT 1 FROM '+table+' WHERE job_id=?', (job_id,)), 'resource_still_owned')
    for table in ('pipeline_canonical_hold',):
        require(not rows(c, 'SELECT 1 FROM native.'+table+' WHERE source_key=?', (j['source_key'],)), 'native_hold_present')
    family = j['family']
    table, key, attempt_table = ('fb_video_task', 'task_id', 'fb_video_attempt') if family == 'video' else ('fb_auto_task', 'id', 'fb_auto_publish_attempt')
    t = rows(c, 'SELECT * FROM native.'+table+' WHERE '+key+'=?', (j['source_id'],))
    require(len(t) == 1, 'native_missing')
    t = t[0]
    require(t['status'] == 'failed' and not t['graph_post_id'] and not t['unknown_outcome'], 'native_outcome_not_definite_failure')
    require(t['error_code'] in PREWRITE | REJECTED, 'native_failure_not_allowed')
    if family == 'video':
        require(not t['execution_lease'] and not t['execution_until'] and t['prepare_state'] == 'ready' and t['link_state'] == 'ready', 'native_not_ready')
    else:
        require(not t['lease_owner'] and not t['lease_expires_at_utc'] and t['prepared_media_url'] and t['prepared_sha256'], 'native_not_ready')
    attempts = rows(c, 'SELECT * FROM native.'+attempt_table+' WHERE task_id=? ORDER BY id', (j['source_id'],))
    require(attempts, 'attempt_evidence_missing')
    for a in attempts:
        require(a['result_kind'] in {'definite_failure', 'credential_failure'} and not a.get('graph_post_id'), 'attempt_unknown_or_accepted')
        require(a['error_code'] in PREWRITE | REJECTED, 'attempt_failure_not_allowed')
    operations = rows(c, 'SELECT * FROM native.pipeline_fb_operation WHERE source_key=? ORDER BY sequence', (j['source_key'],))
    for o in operations:
        result = json.loads(o['result_json'] or '{}')
        checkpoint = json.loads(o['checkpoint_json'] or '{}')
        require(result.get('kind') in {'definite_failure', 'credential_failure'} and result.get('error_code') in REJECTED
                and not result.get('post_id') and not result.get('graph_post_id'), 'native_operation_unresolved')
        require(not checkpoint or checkpoint == result, 'native_receipt_conflict')
        if o['native_attempt_id'] is not None:
            require(any(a['id'] == o['native_attempt_id'] and a['credential_id'] == o['credential_id'] for a in attempts), 'attempt_binding_missing')
    central_ops = rows(c, 'SELECT * FROM pipeline_operation WHERE job_id=?', (job_id,))
    issued = {o['operation_id'] for o in operations}
    # No native permit means no credential envelope could have been returned.
    # The empty native operation plus only completed rejection/preflight rows
    # excludes a partial begin_attempt and every accepted/unknown response.
    missing = [o['operation_id'] for o in central_ops if o['operation_id'] not in issued]
    for table2, key2 in [('fb_video_task', 'task_id'), ('fb_auto_task', 'id')]:
        created = 'created_at_utc'
        competing = rows(c, 'SELECT '+key2+' FROM native.'+table2+" WHERE page_id=? AND material_id=? AND "+created+">=? AND (status IN ('published','submitted','unknown','running') OR graph_post_id<>'' OR unknown_outcome=1)" + (' AND '+key2+'<>?' if table2 == table else ''),
            [t['page_id'], t['material_id'], t['created_at_utc']] + ([j['source_id']] if table2 == table else []))
        require(not competing, 'same_material_has_competing_outcome')
    state = {'job': j, 'native': t, 'attempts': attempts, 'operations': operations, 'central_operations': central_ops}
    return {'job_id': job_id, 'source_key': j['source_key'], 'source_id': j['source_id'], 'family': family,
            'page_id': t['page_id'], 'previous_error': t['error_code'], 'generation': j['generation'],
            'attempts': len(attempts), 'explicit_rejections': len(operations), 'unissued_permits': missing,
            'state_sha256': sha(state)}

def plan(c, ids):
    require(ids and len(ids) == len(set(ids)) and all(type(x) is int and x > 0 for x in ids), 'exact_ids_required')
    items, excluded = [], []
    for job_id in sorted(ids):
        try:
            items.append(proof(c, job_id))
        except ValueError as exc:
            excluded.append({'job_id': job_id, 'reason': str(exc)})
    return {'items': items, 'excluded': excluded, 'plan_sha256': sha({'items': items, 'excluded': excluded})}

def apply(c, prepared):
    cfg = json.loads(c.execute('SELECT config_json FROM pipeline_config WHERE id=1').fetchone()[0])
    require('fb' not in cfg['enabled_channels'], 'pause_fb_admission_before_apply')
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')
    for row in prepared['items']:
        if row.get('already_recovered'):
            continue
        if row['family'] == 'video':
            c.execute("UPDATE native.fb_video_task SET status='queued',error_code='',error_message='',updated_at_utc=? WHERE task_id=? AND status='failed' AND graph_post_id='' AND unknown_outcome=0", (now, row['source_id']))
        else:
            until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).isoformat(timespec='seconds')
            c.execute("UPDATE native.fb_auto_task SET status='ready',error_code='',error_message='',completed_at_utc='',recovery_until_utc=? WHERE id=? AND status='failed' AND graph_post_id='' AND unknown_outcome=0", (until, row['source_id']))
        for operation_id in row['unissued_permits']:
            c.execute("UPDATE pipeline_operation SET status='confirmed_unissued',updated_at=? WHERE operation_id=? AND job_id=?", (now, operation_id, row['job_id']))
        c.execute("UPDATE pipeline_job SET status='queued',source_status=?,external_intent=0,node_id='',worker_id='',claim_hash='',lease_expires_at='',error_code='',error_message='',finished_at='',started_at='',updated_at=? WHERE id=? AND status='failed'",
                  ('ready' if row['family'] == 'auto' else 'queued', now, row['job_id']))
        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                  (row['job_id'], EVENT, dumps({**row, 'plan_sha256': prepared['plan_sha256'], 'actor': 'operator_authorized', 'recovered_at': now}), now))

def backup(source, target):
    require(not target.exists(), 'backup_exists')
    with contextlib.closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro', uri=True)) as src:
        with contextlib.closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            require(dst.execute('pragma quick_check').fetchone()[0] == 'ok', 'backup_integrity_failed')
    target.chmod(0o600)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('central', 'native', 'job-ids', 'output'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--apply', action='store_true'); p.add_argument('--plan-sha256'); p.add_argument('--backup-dir')
    a = p.parse_args(); ids = json.loads(Path(a.job_ids).read_text())
    if a.apply:
        require(a.plan_sha256 and a.backup_dir, 'apply_requires_plan_and_backup')
        folder = Path(a.backup_dir); folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup(a.central, folder/'dispatch.sqlite3'); backup(a.native, folder/'fb.sqlite3')
    mode = 'rw' if a.apply else 'ro'
    with contextlib.closing(sqlite3.connect(Path(a.central).resolve().as_uri()+'?mode='+mode, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        c.execute('ATTACH DATABASE ? AS native', (Path(a.native).resolve().as_uri()+'?mode='+mode,))
        c.execute('BEGIN IMMEDIATE' if a.apply else 'BEGIN')
        result = plan(c, ids)
        if a.apply:
            require(result['plan_sha256'] == a.plan_sha256, 'plan_changed')
            apply(c, result); c.commit()
        else:
            c.rollback()
    result['applied'] = a.apply
    target = Path(a.output); target.write_text(json.dumps(result, ensure_ascii=False, indent=2)); target.chmod(0o600)
    print(json.dumps({'applied': a.apply, 'eligible': len(result['items']), 'excluded': result['excluded'], 'plan_sha256': result['plan_sha256']}))

if __name__ == '__main__':
    main()
