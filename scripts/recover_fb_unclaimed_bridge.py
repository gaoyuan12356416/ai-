"""Recover exact FB auto jobs whose owner was unreachable before native claim.

Only the central failed job is requeued. Native tasks, attempts, artifacts,
claims and receipts remain unchanged. Admission and unrelated work stay live.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3

EVENT = 'authorized_fb_unclaimed_bridge_recovery'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def require(value, code):
    if not value:
        raise ValueError(code)


def rows(conn, sql, args=()):
    return [dict(row) for row in conn.execute(sql, args)]


def instant(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def proof(conn, native, task_id):
    key = 'fb:auto:' + str(task_id)
    jobs = rows(conn, "SELECT * FROM pipeline_job WHERE source_key=? AND stage='publish'", (key,))
    require(len(jobs) == 1, 'publish_job_not_unique')
    job = jobs[0]
    prior = rows(conn, 'SELECT id FROM pipeline_event WHERE job_id=? AND event_type=?', (job['id'], EVENT))
    if prior:
        return {'task_id': task_id, 'job_id': job['id'], 'already_applied': True}
    require((job['channel'], job['family'], str(job['source_id'])) == ('fb', 'auto', str(task_id)), 'scope_changed')
    require(job['status'] == 'failed' and job['error_code'] == 'canonical_bridge_unavailable', 'failure_changed')
    require(not job['external_intent'] and not job['canonical_receipt'] and not job['hold_reason'], 'central_outcome_or_hold')
    now = dt.datetime.now(dt.timezone.utc)
    require(not job['lease_expires_at'] or instant(job['lease_expires_at']) < now, 'active_lease')
    for table in ('pipeline_operation', 'pipeline_receipt', 'pipeline_checkpoint', 'pipeline_resource', 'pipeline_legacy_resource'):
        require(not rows(conn, 'SELECT 1 FROM '+table+' WHERE job_id=? LIMIT 1', (job['id'],)), 'central_'+table+'_present')
    require(not rows(conn, 'SELECT 1 FROM pipeline_publication WHERE source_key=? LIMIT 1', (key,)), 'publication_present')
    tasks = rows(native, 'SELECT * FROM fb_auto_task WHERE id=?', (task_id,))
    require(len(tasks) == 1, 'native_task_missing')
    task = tasks[0]
    require(task['status'] == 'ready' and not task['unknown_outcome'] and not task['graph_post_id'], 'native_outcome_changed')
    require(not task['lease_owner'] and not task['lease_expires_at_utc'], 'native_claim_active')
    require(task['prepared_at_utc'] and task['prepared_media_url'] and task['prepared_sha256']
            and task['media_url'] == task['prepared_media_url'] and task['media_url'] != task['source_media_url'], 'artifact_not_ready')
    require(instant(task['planned_publish_at_utc']) <= now, 'not_due')
    for table in ('fb_auto_publish_attempt', 'fb_auto_publish_ledger'):
        require(not rows(native, 'SELECT 1 FROM '+table+' WHERE task_id=? LIMIT 1', (task_id,)), 'native_'+table+'_present')
    require(not rows(native, "SELECT 1 FROM pipeline_fb_claim WHERE source_key=? AND stage='publish' LIMIT 1", (key,)), 'native_publish_claim_present')
    for table in ('pipeline_fb_operation', 'pipeline_canonical_hold'):
        require(not rows(native, 'SELECT 1 FROM '+table+' WHERE source_key=? LIMIT 1', (key,)), 'native_'+table+'_present')
    return {'task_id': task_id, 'job_id': job['id'], 'source_key': key,
            'generation': job['generation'], 'job': job, 'native_sha256': digest(task)}


def plan(conn, native, ids):
    require(ids and len(ids) == len(set(ids)) and all(type(i) is int and i > 0 for i in ids), 'exact_unique_task_ids_required')
    items, excluded = [], []
    for task_id in sorted(ids):
        try:
            items.append(proof(conn, native, task_id))
        except ValueError as exc:
            excluded.append({'task_id': task_id, 'reason': str(exc)})
    body = {'items': items, 'excluded': excluded}
    return dict(body, plan_sha256=digest(body))


def write_private(path, value):
    path = Path(path)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))
        stream.flush()
        import os
        os.fsync(stream.fileno())
    path.chmod(0o600)


def apply(conn, prepared, backup):
    # Failed rows cannot be claimed. BEGIN IMMEDIATE makes verification and the
    # requeue atomic with coordinator writes; no channel-wide pause is needed.
    require(not Path(backup).exists(), 'backup_exists')
    write_private(backup, prepared)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')
    changed = []
    for item in prepared['items']:
        if item.get('already_applied'):
            continue
        before = item['job']
        changed_count = conn.execute("""UPDATE pipeline_job SET status='queued',source_status='ready',
            node_id='',worker_id='',claim_hash='',lease_expires_at='',started_at='',finished_at='',
            error_code='',error_message='',updated_at=?
            WHERE id=? AND status='failed' AND error_code='canonical_bridge_unavailable'
            AND generation=? AND external_intent=0""", (now, item['job_id'], item['generation'])).rowcount
        require(changed_count == 1, 'central_compare_and_swap_failed')
        details = {'actor': 'operator_authorized', 'task_id': item['task_id'],
                   'previous_generation': item['generation'], 'previous_error': before['error_code'],
                   'native_sha256': item['native_sha256'], 'plan_sha256': prepared['plan_sha256'],
                   'backup': str(backup)}
        conn.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                     (item['job_id'], EVENT, encoded(details), now))
        changed.append(item['task_id'])
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('central', 'native', 'task-ids', 'output'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--backup')
    args = parser.parse_args()
    ids = json.loads(Path(args.task_ids).read_text(encoding='utf-8-sig'))
    mode = 'rw' if args.apply else 'ro'
    with contextlib.ExitStack() as stack:
        conn = stack.enter_context(contextlib.closing(sqlite3.connect(Path(args.central).resolve().as_uri()+'?mode='+mode, uri=True, timeout=5)))
        native = stack.enter_context(contextlib.closing(sqlite3.connect(Path(args.native).resolve().as_uri()+'?mode=ro', uri=True, timeout=5)))
        conn.row_factory = sqlite3.Row
        native.row_factory = sqlite3.Row
        native.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN IMMEDIATE' if args.apply else 'BEGIN')
        native.execute('BEGIN')
        result = plan(conn, native, ids)
        if args.apply:
            require(args.backup and args.plan_sha256 == result['plan_sha256'], 'matching_plan_and_backup_required')
            changed = apply(conn, result, args.backup)
            conn.commit()
        else:
            changed = []
            conn.rollback()
    result.update(applied=args.apply, changed_task_ids=changed)
    write_private(args.output, result)
    print(encoded({'applied': args.apply, 'eligible': len(result['items']), 'excluded': result['excluded'],
                   'changed_task_ids': changed, 'plan_sha256': result['plan_sha256']}))


if __name__ == '__main__':
    main()
