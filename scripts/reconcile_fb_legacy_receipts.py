#!/usr/bin/env python3
"""Plan/apply the two independently GET-confirmed legacy FB receipts.

This is not a retry or unlock API. It contains no HTTP client and cannot post.
Private input files are produced by the bounded Page/videos + object GET audit;
review the plan hash before applying. The unrelated unknown 29147 is excluded.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.core import FBAutoPostStore

TARGETS = {29162: ('910860148774020', '2290597594846430'),
           29303: ('873564875837952', '1045420691853155')}
KIND = 'fb_legacy_confirmed_receipt_reconciliation_v1'
AUDIT = '''CREATE TABLE IF NOT EXISTS fb_legacy_receipt_reconciliation(
 task_id INTEGER PRIMARY KEY,plan_sha256 TEXT NOT NULL,video_id TEXT NOT NULL,
 before_sha256 TEXT NOT NULL,after_sha256 TEXT NOT NULL,
 evidence_json TEXT NOT NULL,created_at_utc TEXT NOT NULL)'''


def require(value, code):
    if not value:
        raise ValueError(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf8')


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def text_sha(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def parsed(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'timestamp_timezone_missing')
    return result.astimezone(timezone.utc)


def connect(path, readonly=True):
    path = Path(path).resolve(strict=True)
    c = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    if readonly:
        c.execute('PRAGMA query_only=ON')
    return c


def tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def snapshot(c, tid):
    task = c.execute('SELECT * FROM fb_auto_task WHERE id=?', (tid,)).fetchone()
    ledger = c.execute('SELECT * FROM fb_auto_publish_ledger WHERE task_id=?', (tid,)).fetchone()
    require(task is not None and ledger is not None, 'native_receipt_missing')
    attempts = [dict(r) for r in c.execute('SELECT * FROM fb_auto_publish_attempt WHERE task_id=? ORDER BY id', (tid,))]
    return {'task': dict(task), 'ledger': dict(ledger), 'attempts': attempts}


def untouched(c, state):
    t, ledger = state['task'], state['ledger']
    require(t['id'] in TARGETS, 'task_outside_exact_allowlist')
    require(t['page_id'] == TARGETS[t['id']][0], 'page_identity_changed')
    require(t['status'] == ledger['status'] == 'unknown' and
            t['unknown_outcome'] == ledger['unknown_outcome'] == 1 and
            not t['graph_post_id'] and not ledger['graph_post_id'], 'native_not_unknown_without_id')
    require(not t['lease_owner'] and not t['lease_expires_at_utc'], 'native_lease_present')
    require(t['material_id'] == ledger['material_id'] and t['page_id'] == ledger['page_id'], 'ledger_identity_changed')
    require(t['prepared_sha256'] and t['prepared_size_bytes'] > 0 and t['prepared_at_utc'] and t['short_url'] and t['message_text'], 'frozen_identity_missing')
    require(state['attempts'] and state['attempts'][-1]['result_kind'] == 'unknown', 'unknown_attempt_missing')
    require(not any(r['result_kind'] == 'accepted' for r in state['attempts']), 'conflicting_accepted_attempt')
    key = 'fb:auto:' + str(t['id'])
    existing = tables(c)
    for table in ('pipeline_fb_claim', 'pipeline_fb_operation', 'pipeline_canonical_hold', 'pipeline_backlog_disposition'):
        if table in existing:
            require(c.execute('SELECT 1 FROM ' + table + ' WHERE source_key=? LIMIT 1', (key,)).fetchone() is None,
                    'native_claim_operation_or_hold_present')
    require(c.execute('SELECT 1 FROM fb_auto_run WHERE id=?', (t['run_id'],)).fetchone() is not None, 'run_missing')
    video_id = TARGETS[t['id']][1]
    require(c.execute('SELECT 1 FROM fb_auto_task WHERE graph_post_id=? AND id<>? LIMIT 1',
                      (video_id, t['id'])).fetchone() is None, 'video_already_bound_elsewhere')
    require(c.execute('SELECT 1 FROM fb_auto_publish_ledger WHERE graph_post_id=? AND task_id<>? LIMIT 1',
                      (video_id, t['id'])).fetchone() is None, 'video_already_bound_elsewhere')
    if 'fb_video_task' in existing:
        require(c.execute('SELECT 1 FROM fb_video_task WHERE graph_post_id=? LIMIT 1', (video_id,)).fetchone() is None,
                'video_already_bound_elsewhere')


def evidence_for(state, page_evidence, object_evidence, now):
    tid, page_id = state['task']['id'], state['task']['page_id']
    video_id = TARGETS[tid][1]
    for evidence in (page_evidence, object_evidence):
        require(evidence.get('read_only') is True and evidence.get('graph_methods') == ['GET'], 'not_get_only_evidence')
        age = (now - parsed(evidence['at'])).total_seconds()
        require(-60 <= age <= 6 * 3600, 'graph_evidence_stale')
    pages = [i for i in page_evidence['items'] if i['task_id'] == tid]
    objects = [i for i in object_evidence['items'] if i['task_id'] == tid]
    require(len(pages) == len(objects) == 1, 'evidence_task_ambiguous')
    p, o = pages[0], objects[0]
    require(p['page_id'] == o['page_id'] == page_id, 'evidence_page_mismatch')
    require(len(p.get('matches', [])) == 1, 'page_match_not_unique')
    require(any(q.get('pages') and all(x.get('http_status') == 200 for x in q['pages']) and
                q['pages'][-1].get('complete') is True for q in p['queries']), 'page_scan_incomplete')
    direct = [q for q in o['queries'] if q.get('kind') == 'object' and q.get('http_status') == 200]
    require(len(direct) == 1 and len(direct[0].get('rows', [])) == 1, 'object_get_missing')
    row = direct[0]['rows'][0]
    require(row.get('owner_page_id') == page_id, 'object_owner_mismatch')
    t = state['task']
    for r in (p['matches'][0], row):
        require(r.get('id') == video_id and r.get('exact_description') is True and
                r.get('exact_frozen_short_url') is True, 'object_identity_mismatch')
        require(r['description_sha256'] == text_sha(t['message_text']), 'description_hash_mismatch')
        duration = float(r['length'])
        require(math.isfinite(duration) and abs(duration - float(t['prepared_duration_seconds'])) <= .05,
                'video_duration_mismatch')
        status = r.get('status') or {}
        require(status.get('video_status') == 'ready' and
                (status.get('publishing_phase') or {}).get('publish_status') == 'published' and
                (status.get('publishing_phase') or {}).get('status') == 'complete', 'not_confirmed_published')
        published_at = parsed(status['publishing_phase']['publish_time'])
        require(parsed(r['created_time']) <= published_at <= now, 'invalid_platform_publish_time')
        # Both recovered videos were created during the final 120s request.
        end = parsed(state['attempts'][-1]['created_at_utc'])
        require(end - timedelta(minutes=10) <= parsed(r['created_time']) <= end + timedelta(minutes=5),
                'video_outside_original_request_window')
    require(row.get('frozen_short_url_sha256') == text_sha(t['short_url']), 'short_link_hash_mismatch')
    require(row['created_time'] == p['matches'][0]['created_time'], 'graph_receipt_changed')
    require(row['status']['publishing_phase']['publish_time'] == p['matches'][0]['status']['publishing_phase']['publish_time'],
            'graph_publish_time_changed')
    return {'page_id': page_id, 'video_id': video_id, 'created_time': row['created_time'],
            'published_at': parsed(row['status']['publishing_phase']['publish_time']).isoformat(timespec='seconds'),
            'description_sha256': row['description_sha256'], 'short_url_sha256': row['frozen_short_url_sha256'],
            'duration_seconds': row['length'], 'page_get_at': page_evidence['at'], 'object_get_at': object_evidence['at'],
            'page_evidence_sha256': sha(p), 'object_evidence_sha256': sha(o)}


def make_plan(db_path, page_evidence, object_evidence, now=None):
    now = now or datetime.now(timezone.utc)
    plan = {'kind': KIND, 'db_path': str(Path(db_path).resolve(strict=True)),
            'created_at': now.isoformat(), 'items': []}
    with closing(connect(db_path)) as c:
        c.execute('BEGIN')
        for tid in sorted(TARGETS):
            state = snapshot(c, tid)
            untouched(c, state)
            proof = evidence_for(state, page_evidence, object_evidence, now)
            plan['items'].append({'task_id': tid, 'before': state, 'before_sha256': sha(state), 'proof': proof})
    plan['plan_sha256'] = sha(plan)
    return plan


def validate_plan(plan, expected_sha):
    digest = sha({k: v for k, v in plan.items() if k != 'plan_sha256'})
    require(plan.get('kind') == KIND and digest == plan.get('plan_sha256') == expected_sha, 'plan_hash_mismatch')
    require([i['task_id'] for i in plan['items']] == sorted(TARGETS), 'plan_scope_mismatch')
    for item in plan['items']:
        require(sha(item['before']) == item['before_sha256'], 'before_hash_mismatch')
        require(item['proof']['video_id'] == TARGETS[item['task_id']][1], 'plan_video_mismatch')


def validate_backup_location(path, data_root=Path('/mnt/data-disk')):
    root = data_root.resolve(strict=True)
    require(root in path.parents, 'backup_must_use_data_disk')
    result = subprocess.run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', str(root)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=5)
    require(result.returncode == 0 and result.stdout.decode().strip() == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8',
            'expected_data_disk_not_mounted')


def apply_plan(plan, expected_sha, backup_path, now=None):
    validate_plan(plan, expected_sha)
    now = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    backup_path = Path(backup_path).resolve()
    require(not backup_path.exists() and backup_path.parent.is_dir(), 'backup_destination_invalid')
    validate_backup_location(backup_path)
    fd = os.open(str(backup_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    # SQLite backup takes a consistent snapshot without stopping unrelated work.
    with closing(connect(plan['db_path'])) as src, closing(sqlite3.connect(str(backup_path))) as dst:
        src.backup(dst)
        require(dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'backup_quick_check_failed')
    os.chmod(backup_path, 0o600)
    report = {'plan_sha256': expected_sha, 'backup_path': str(backup_path), 'items': [], 'platform_writes': 0}
    with closing(connect(plan['db_path'], readonly=False)) as c:
        try:
            c.execute('BEGIN IMMEDIATE')
            c.execute(AUDIT)
            # Validate both before changing either: no partial pair on CAS drift.
            pending = []
            for item in plan['items']:
                state = snapshot(c, item['task_id'])
                prior = c.execute('SELECT * FROM fb_legacy_receipt_reconciliation WHERE task_id=?', (item['task_id'],)).fetchone()
                if prior is not None:
                    require(prior['plan_sha256'] == expected_sha and prior['after_sha256'] == sha(state), 'committed_receipt_drift')
                    report['items'].append({'task_id': item['task_id'], 'status': 'already_committed'})
                    continue
                untouched(c, state)
                require(sha(state) == item['before_sha256'], 'native_compare_and_swap_failed')
                pending.append(item)
            for item in pending:
                t, proof = item['before']['task'], item['proof']
                c.execute("""UPDATE fb_auto_task SET status='published',graph_post_id=?,unknown_outcome=0,
                    error_code='',error_message='',lease_owner='',lease_expires_at_utc='',next_reconcile_at_utc='',
                    completed_at_utc=? WHERE id=? AND status='unknown' AND graph_post_id=''""",
                          (proof['video_id'], proof['published_at'], t['id']))
                # Use the same success ledger/run calculations as reconcile_task.
                FBAutoPostStore._upsert_ledger(c, t['id'], t['page_id'], t['material_id'], 'published', proof['video_id'], 0, False, '', now)
                FBAutoPostStore._refresh_run(c, t['run_id'], now)
                after = snapshot(c, t['id'])
                c.execute('INSERT INTO fb_legacy_receipt_reconciliation VALUES(?,?,?,?,?,?,?)',
                          (t['id'], expected_sha, proof['video_id'], item['before_sha256'], sha(after),
                           json.dumps({'action': 'confirmed_existing_publication', 'proof': proof,
                                       'original_error_code': t['error_code'], 'original_completed_at_utc': t['completed_at_utc']}, sort_keys=True), now))
                report['items'].append({'task_id': t['id'], 'status': 'published', 'video_id': proof['video_id']})
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return report


def write_private(path, value):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('plan')
    for flag in ('db', 'page-evidence', 'object-evidence', 'output'):
        p.add_argument('--' + flag, required=True)
    a = subs.add_parser('apply')
    for flag in ('plan', 'expected-sha256', 'backup', 'output'):
        a.add_argument('--' + flag, required=True)
    args = parser.parse_args()
    if args.command == 'plan':
        result = make_plan(args.db, json.loads(Path(args.page_evidence).read_text(encoding='utf8')),
                           json.loads(Path(args.object_evidence).read_text(encoding='utf8')))
    else:
        result = apply_plan(json.loads(Path(args.plan).read_text(encoding='utf8')), args.expected_sha256, args.backup)
    write_private(args.output, result)
    print(json.dumps({'ok': True, 'plan_sha256': result['plan_sha256'], 'count': len(result['items']), 'platform_writes': 0}))


if __name__ == '__main__':
    main()
