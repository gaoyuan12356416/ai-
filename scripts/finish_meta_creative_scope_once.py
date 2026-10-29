"""Recheck a frozen brand cleanup and execute its released Creative phase once.

All Meta operations remain in the main API, with its normal ACL and shared
execution budget. Request IDs are stable; unknown HTTP outcomes are resolved
from the ledger and are never blindly submitted again.
"""
import argparse
from contextlib import closing
import json
import hashlib
from pathlib import Path
import sqlite3
import time

import requests


LEDGER = Path('/mnt/data-disk/fb-ad-asset-delete/tasks.sqlite3')
SESSIONS = Path('/root/drama_material_service/data/drama_material_jobs.sqlite3')


def refresh_job(http, url):
    # A restarted API lazily recovers dead owners on its first asset request.
    # Polling SQLite alone would leave the monitor waiting on an orphan forever.
    for attempt in range(3):
        try:
            http.get(url, params={'page': 1, 'page_size': 1}, timeout=30).raise_for_status()
            return
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
            time.sleep(2)


def retryable_recheck(metadata):
    error = metadata.get('error', {})
    return (error.get('code') == 'source_unavailable' and error.get('retryable') is True or
            not error and metadata.get('status') == 'interrupted' and
            metadata.get('step') == '核验进程已中断，进度保留，请人工重新核验；未自动恢复删除')


def read(sql, values=(), path=LEDGER):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, values)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--creative-approval', type=Path)
    parser.add_argument('--controller-state', type=Path)
    args = parser.parse_args()
    assert Path('/mnt/data-disk').resolve() in args.output.resolve().parents
    job = read('SELECT * FROM fb_asset_delete_v2_jobs WHERE job_id=?', (args.job,))[0]
    scope = json.loads(job['metadata'])
    assert set(scope['ids']) == {'XEY147', 'XEY50328'}
    assert {str(p['id']) for p in scope['products']} == {'2373', '3360', '2472', '3443'}
    assert job['status'] in ('ready', 'completed', 'partial', 'interrupted')
    assert not read("SELECT 1 FROM fb_asset_delete_v2_runs WHERE job_id=? AND status='running'", (args.job,))
    actor = json.loads(job['actor'])
    session = read('SELECT session_token FROM drama_admin_session WHERE tenant_key=? AND user_id=? AND expires_at>? ORDER BY expires_at DESC LIMIT 1',
                   (actor[0], actor[1], int(time.time())), SESSIONS)
    assert session, 'Owner session unavailable'
    http = requests.Session()
    http.cookies.set('drama_admin_session', session[0]['session_token'])
    http.headers.update(Host='ai.yingliangads.com', Origin='https://ai.yingliangads.com')
    url = 'http://127.0.0.1:8787/api/fb-post-ad-delete/jobs/'+args.job
    response = http.get(url, params={'page': 1, 'page_size': 1}, timeout=30)
    response.raise_for_status()
    report = dict(job_id=args.job, operation=args.operation, execute_authorized=args.execute)
    approval_payload = {}
    if args.creative_approval:
        approval = json.loads(args.creative_approval.read_text())
        assert (approval['job_id'], approval['preview_id'], approval['actor']) == (args.job, job['preview_id'], job['actor'])
        keys = approval['creative_keys']
        assert keys and len(keys) == len(set(keys)) == approval['count']
        assert hashlib.sha256('\n'.join(sorted(keys)).encode()).hexdigest() == approval['keys_sha256']
        approval_payload['approved_changed_parent_creative_keys'] = keys
        report['approved_changed_parent_creatives'] = len(keys)

    def save(**fields):
        report.update(fields, updated_at=int(time.time()))
        tmp = args.output.with_suffix('.tmp')
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(args.output)
        print(json.dumps(fields, ensure_ascii=False), flush=True)

    def operation(action, table, suffix, extra):
        request_id = args.operation+'-'+suffix
        rows = read('SELECT * FROM '+table+' WHERE request_id=?', (request_id,))
        if not rows:
            try:
                result = http.post(url+'/'+action, json=dict(preview_id=job['preview_id'], request_id=request_id, **extra), timeout=30)
                result.raise_for_status()
            except requests.RequestException:
                # A committed operation may outlive a dropped response.
                save(stage=action+'_response_unconfirmed', request_id=request_id)
            rows = read('SELECT * FROM '+table+' WHERE request_id=?', (request_id,))
        assert len(rows) == 1 and rows[0]['job_id'] == args.job and rows[0]['preview_id'] == job['preview_id'], 'Operation not confirmed; stop without resubmission'
        while rows[0]['status'] == 'running':
            current = read('SELECT metadata FROM fb_asset_delete_v2_jobs WHERE job_id=?', (args.job,))[0]
            metadata = json.loads(current['metadata'])
            counts = read('SELECT kind,status,count(*) AS count FROM fb_asset_delete_v2_objects WHERE job_id=? GROUP BY kind,status', (args.job,))
            save(stage=action, request_id=request_id, object_counts=counts, recheck=metadata.get('recheck'))
            time.sleep(30)
            refresh_job(http, url)
            rows = read('SELECT * FROM '+table+' WHERE request_id=?', (request_id,))
        save(stage=action+'_finished', request_id=request_id, operation_status=rows[0]['status'])
        return rows[0]['status']

    save(stage='validated')
    if not args.execute:
        return
    if args.controller_state:
        assert args.controller_state.resolve().parent == args.output.resolve().parent
        controller = json.loads(args.controller_state.read_text())
        assert controller['job_id'] == args.job and controller['stage'] in ('verified', 'resuming_recheck')
        controller.update(stage='resuming_recheck', resume_operation=args.operation, updated_at=int(time.time()))
        temp = args.controller_state.with_suffix('.tmp')
        temp.write_text(json.dumps(controller, ensure_ascii=False, indent=2))
        temp.replace(args.controller_state)
    for attempt in range(3):
        suffix = 'recheck'+('-'+str(attempt+1) if attempt else '')
        state = operation('recheck', 'fb_asset_delete_v2_rechecks', suffix, approval_payload if not attempt else {})
        if state == 'completed':
            break
        prior = read('SELECT metadata FROM fb_asset_delete_v2_rechecks WHERE request_id=?', (args.operation+'-'+suffix,))[0]
        if not retryable_recheck(json.loads(prior['metadata'])) or attempt == 2:
            break
        save(stage='retrying_interrupted_reads', next_attempt=attempt+2)
        time.sleep(15)
    assert state == 'completed', 'Read-only recheck interrupted; no execution submitted'
    # No Ad/Video retry: only Creative objects released by this recheck run.
    pending = read("SELECT 1 FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='creative' AND status='pending' LIMIT 1", (args.job,))
    if pending:
        execution = operation('execute', 'fb_asset_delete_v2_runs', 'creative', {'phases': ['creative'], 'only_pending': True})
        assert execution in ('completed', 'partial'), 'Creative execution interrupted; inspect receipts before resuming'
    assert not read("SELECT 1 FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='creative' AND status IN ('pending','in_progress') LIMIT 1", (args.job,)), 'Unstarted Creatives remain'
    counts = read('SELECT kind,status,count(*) AS count FROM fb_asset_delete_v2_objects WHERE job_id=? GROUP BY kind,status', (args.job,))
    status = read('SELECT status FROM fb_asset_delete_v2_jobs WHERE job_id=?', (args.job,))[0]['status']
    save(stage='finished', status=status, object_counts=counts)
    if args.controller_state:
        controller.update(stage='finished', updated_at=int(time.time()))
        temp = args.controller_state.with_suffix('.tmp')
        temp.write_text(json.dumps(controller, ensure_ascii=False, indent=2))
        temp.replace(args.controller_state)


if __name__ == '__main__':
    main()
