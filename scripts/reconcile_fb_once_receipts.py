#!/usr/bin/env python3
"""Apply a frozen, bounded GET audit to FB native receipts; never call a platform.

Only independently confirmed published objects qualify. Native attempts and
reservations are retained, unknown fences are left to the normal collector.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.core import FBAutoPostStore

KIND = 'fb_once_get_reconciliation_20261104_v1'
AUDIT = '''CREATE TABLE IF NOT EXISTS fb_once_get_reconciliation(
 source_key TEXT PRIMARY KEY,plan_sha256 TEXT NOT NULL,video_id TEXT NOT NULL,
 before_sha256 TEXT NOT NULL,after_sha256 TEXT NOT NULL,
 evidence_json TEXT NOT NULL,reconciled_at TEXT NOT NULL)'''


def require(value, code):
    if not value:
        raise ValueError(code)


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def text_sha(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def parsed(value):
    value = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', str(value).replace('Z', '+00:00'))
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, 'timezone_missing')
    return result.astimezone(timezone.utc)


def connect(path, readonly=True):
    c = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    if readonly:
        c.execute('PRAGMA query_only=ON')
    return c


def snapshot(c, source):
    parts = source.split(':')
    require(len(parts) == 3 and parts[0] == 'fb' and parts[1] in {'auto', 'video'}, 'source_invalid')
    family, tid = parts[1:]
    auto = family == 'auto'
    t = c.execute('SELECT * FROM ' + ('fb_auto_task WHERE id=?' if auto else 'fb_video_task WHERE task_id=?'), (tid,)).fetchone()
    require(t is not None, 'native_missing')
    attempts = [dict(r) for r in c.execute('SELECT * FROM '+('fb_auto_publish_attempt' if auto else 'fb_video_attempt')+' WHERE task_id=? ORDER BY id', (tid,))]
    ledger = c.execute('SELECT * FROM fb_auto_publish_ledger WHERE task_id=?', (tid,)).fetchone() if auto else None
    return {'task': dict(t), 'attempts': attempts, 'ledger': dict(ledger) if ledger else None}


def proof_for(state, result, manifest, now):
    t = state['task']
    require(result.get('direct') and any(q.get('http_status') == 200 and q.get('kind') in {'original_video', 'matched_video_confirmation'} for q in result['queries']), 'no_successful_object_get')
    require(-60 <= (now-parsed(result['checked_at'])).total_seconds() <= 21600, 'evidence_expired')
    row = result['direct']
    vid = row.get('id')
    require(isinstance(vid, str) and re.fullmatch(r'[0-9]{1,128}', vid), 'video_id_invalid')
    require(row.get('owner_page_id') == t['page_id'] == result['page_id'] == manifest['page_id'], 'page_mismatch')
    st = row.get('status') or {}
    phase = st.get('publishing_phase') or {}
    require(st.get('video_status') == 'ready' and phase.get('status') == 'complete' and phase.get('publish_status') == 'published', 'publication_unconfirmed')
    published_at, created = parsed(phase['publish_time']), parsed(row['created_time'])
    require(created <= published_at <= now, 'publish_time_invalid')
    require((t.get('graph_post_id') or '') == result['original_graph_id'], 'original_id_changed')
    if t.get('graph_post_id'):
        require(t['graph_post_id'] == vid, 'known_id_mismatch')
    else:
        require(result.get('scan_complete') is True and len(result.get('matches', [])) == 1 and result['matches'][0]['id'] == vid, 'page_match_not_unique')
        caption = t.get('message_text') if result['family'] == 'auto' else t.get('caption')
        require(caption and t['short_url'], 'frozen_identity_missing')
        for candidate in (row, result['matches'][0]):
            require(candidate.get('exact_description') is True and candidate.get('exact_frozen_short_url') is True and candidate['description_sha256'] == text_sha(caption), 'description_mismatch')
        require(manifest['description_sha256'] == text_sha(caption) and manifest['short_url_sha256'] == text_sha(t['short_url']), 'frozen_identity_changed')
        duration = t.get('prepared_duration_seconds')
        tolerance = .1
        if result['family'] == 'video':
            prepared = json.loads(t.get('prepared_json') or '{}')
            duration = prepared.get('duration_seconds') or prepared.get('prepared_duration_seconds') or prepared.get('duration')
            if t.get('processing_mode') == 'original':
                duration = json.loads(t['snapshot_json']).get('material', {}).get('duration_seconds')
                # Original-material catalog snapshots can have whole-second
                # precision; Graph reports fractional playback seconds.
                if duration and float(duration).is_integer():
                    tolerance = .5
        require(duration and math.isfinite(float(duration)) and abs(float(row['length'])-float(duration)) <= tolerance, 'duration_mismatch')
        require(manifest['start']-900 <= created.timestamp() <= manifest['end']+300, 'outside_original_request_time')
    return {'video_id': vid, 'page_id': t['page_id'], 'published_at': published_at.isoformat(timespec='seconds'), 'object': row, 'evidence_sha256': sha(result)}


def eligible(c, source, state, proof, now):
    t = state['task']; family = source.split(':')[1]
    require(t['status'] in {'unknown', 'submitted'}, 'native_not_unresolved')
    for key in ('lease_expires_at_utc', 'execution_until', 'prepare_until', 'link_until'):
        require(not t.get(key) or parsed(t[key]) <= now, 'active_native_lease')
    require(t.get('prepare_state') != 'preparing' and t.get('link_state') != 'writing', 'active_native_stage')
    vid = proof['video_id']
    auto_id = str(t.get('id')) if family == 'auto' else ''
    video_id = str(t.get('task_id')) if family == 'video' else ''
    require(not c.execute('SELECT 1 FROM fb_auto_task WHERE graph_post_id=? AND CAST(id AS TEXT)<>? LIMIT 1', (vid, auto_id)).fetchone(), 'video_bound_elsewhere')
    require(not c.execute('SELECT 1 FROM fb_video_task WHERE graph_post_id=? AND task_id<>? LIMIT 1', (vid, video_id)).fetchone(), 'video_bound_elsewhere')
    require(not c.execute('SELECT 1 FROM fb_auto_publish_ledger WHERE graph_post_id=? AND CAST(task_id AS TEXT)<>? LIMIT 1', (vid, auto_id)).fetchone(), 'ledger_video_bound_elsewhere')
    if family == 'auto':
        ledger = state['ledger']
        require(ledger and ledger['page_id'] == t['page_id'] and ledger['material_id'] == t['material_id'], 'ledger_identity_mismatch')
        require(ledger['status'] in {'unknown', 'submitted'} and (not ledger['graph_post_id'] or ledger['graph_post_id'] == vid), 'ledger_outcome_conflict')


def make_plan(db_path, evidence_paths):
    now = datetime.now(timezone.utc)
    evidence = {}; manifests = {}; scope = None
    for path in evidence_paths:
        data = json.loads(Path(path).read_text())
        require(data.get('read_only') is True and data.get('platform_methods') == ['GET'], 'not_readonly_evidence')
        if scope is None:
            scope = set(data['manifest']['scope'])
            require(len(scope) == 141, 'original_scope_not_141')
        require(set(data['manifest']['scope']) <= scope, 'scope_expanded')
        for r in data['items']:
            old = evidence.get(r['source_key'])
            require(old is None or (old['outcome'] == 'inspection_error' and not old['queries']), 'duplicate_platform_query')
            evidence[r['source_key']] = r
        manifests.update({x['source_key']: x for x in data['manifest']['items']})
    plan = {'kind': KIND, 'db_path': str(Path(db_path).resolve(strict=True)), 'created_at': now.isoformat(), 'items': [], 'unresolved': []}
    seen = set()
    with closing(connect(db_path)) as c:
        c.execute('BEGIN')
        for source in sorted(scope):
            r = evidence[source]
            state = snapshot(c, source)
            if state['task']['status'] == 'published':
                plan['unresolved'].append({'source_key': source, 'reason': 'already_published', 'video_id': state['task']['graph_post_id']}); continue
            try:
                proof = proof_for(state, r, manifests[source], now)
                eligible(c, source, state, proof, now)
                require(proof['video_id'] not in seen, 'duplicate_proof_video')
            except (ValueError, KeyError, TypeError) as exc:
                plan['unresolved'].append({'source_key': source, 'reason': str(exc) if isinstance(exc, ValueError) else type(exc).__name__, 'query_outcome': r['outcome']}); continue
            seen.add(proof['video_id'])
            plan['items'].append({'source_key': source, 'before': state, 'before_sha256': sha(state), 'proof': proof})
    plan['plan_sha256'] = sha(plan)
    return plan


def validate_backup_location(backup_path):
    require(Path('/mnt/data-disk') in backup_path.parents and not backup_path.exists(), 'backup_location_invalid')
    require(subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk']).decode().strip() == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8', 'data_disk_missing')


def apply_plan(plan, expected, backup_path):
    require(plan['kind'] == KIND and sha({k:v for k,v in plan.items() if k!='plan_sha256'}) == plan['plan_sha256'] == expected, 'plan_hash_mismatch')
    now = datetime.now(timezone.utc)
    require((now-parsed(plan['created_at'])).total_seconds() <= 21600, 'plan_expired')
    backup_path = Path(backup_path).resolve()
    validate_backup_location(backup_path)
    fd = os.open(str(backup_path), os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); os.close(fd)
    with closing(connect(plan['db_path'])) as src, closing(sqlite3.connect(str(backup_path))) as dst:
        src.backup(dst)
        require(dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'backup_invalid')
    report = {'plan_sha256': expected, 'backup': str(backup_path), 'platform_writes': 0, 'items': []}
    with closing(connect(plan['db_path'], False)) as c:
        try:
            c.execute('BEGIN IMMEDIATE'); c.execute(AUDIT)
            pending = []
            for item in plan['items']:
                source = item['source_key']; state = snapshot(c, source)
                prior = c.execute('SELECT * FROM fb_once_get_reconciliation WHERE source_key=?', (source,)).fetchone()
                if prior:
                    require(prior['plan_sha256'] == expected and prior['after_sha256'] == sha(state), 'committed_state_changed')
                    report['items'].append({'source_key': source, 'status': 'already_committed'}); continue
                eligible(c, source, state, item['proof'], now)
                require(sha(state) == item['before_sha256'], 'native_compare_and_swap_failed')
                pending.append(item)
            for item in pending:
                source, proof, t = item['source_key'], item['proof'], item['before']['task']
                if source.split(':')[1] == 'auto':
                    c.execute("""UPDATE fb_auto_task SET status='published',graph_post_id=?,unknown_outcome=0,
                        error_code='',error_message='',lease_owner='',lease_expires_at_utc='',next_reconcile_at_utc='',completed_at_utc=? WHERE id=?""", (proof['video_id'], proof['published_at'], t['id']))
                    FBAutoPostStore._upsert_ledger(c, t['id'], t['page_id'], t['material_id'], 'published', proof['video_id'], 0, False, '', now.isoformat(timespec='seconds'))
                    FBAutoPostStore._refresh_run(c, t['run_id'], now.isoformat(timespec='seconds'))
                else:
                    # Native DTO/collector uses updated_at for final publish time;
                    # the audit below independently records reconciliation time.
                    c.execute("""UPDATE fb_video_task SET status='published',graph_post_id=?,unknown_outcome=0,
                        error_code='',error_message='',execution_lease='',execution_until='',reconcile_at='',updated_at_utc=? WHERE task_id=?""", (proof['video_id'], proof['published_at'], t['task_id']))
                after = snapshot(c, source)
                require(after['attempts'] == item['before']['attempts'], 'attempt_history_changed')
                c.execute('INSERT INTO fb_once_get_reconciliation VALUES(?,?,?,?,?,?,?)', (source, expected, proof['video_id'], item['before_sha256'], sha(after), json.dumps(proof, sort_keys=True), now.isoformat()))
                report['items'].append({'source_key': source, 'status': 'published', 'video_id': proof['video_id'], 'published_at': proof['published_at']})
            c.commit()
        except BaseException:
            c.rollback(); raise
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest='command', required=True)
    plan = sub.add_parser('plan'); plan.add_argument('--db', required=True); plan.add_argument('--evidence', nargs='+', required=True); plan.add_argument('--output', required=True)
    apply = sub.add_parser('apply'); apply.add_argument('--plan', required=True); apply.add_argument('--expected-sha256', required=True); apply.add_argument('--backup', required=True); apply.add_argument('--output', required=True)
    a = p.parse_args(); os.umask(0o077)
    value = make_plan(a.db, a.evidence) if a.command == 'plan' else apply_plan(json.loads(Path(a.plan).read_text()), a.expected_sha256, a.backup)
    with open(a.output, 'x', encoding='utf8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2); f.write('\n')
    print(json.dumps({'command': a.command, 'eligible_or_applied': len(value['items']), 'unresolved': len(value.get('unresolved', [])), 'plan_sha256': value['plan_sha256']}))


if __name__ == '__main__':
    main()
