"""Finish only unstarted frozen Videos after the Creative continuation and audit."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import time
import requests
import deploy_meta_asset_recovery as rollout
from deploy_meta_delete_throughput import require_api_stopped
from deploy_meta_pending_resume import BASE, FILES, no_writes
from finish_meta_creative_scope_once import read, SESSIONS, refresh_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--deployed-backup', type=Path)
    args = parser.parse_args()
    root, release = args.output.parent, Path(__file__).resolve().parents[1]
    assert Path('/mnt/data-disk').resolve() in root.resolve().parents
    job = read('SELECT * FROM fb_asset_delete_v2_jobs WHERE job_id=?', (args.job,))[0]
    scope = json.loads(job['metadata'])
    assert set(scope['ids']) == {'XEY147', 'XEY50328'}
    assert {str(p['id']) for p in scope['products']} == {'2373', '3360', '2472', '3443'}
    report = dict(job_id=args.job, operation=args.operation, release=str(release), only_pending=True)

    def save(**fields):
        report.update(fields, updated_at=int(time.time()))
        tmp = args.output.with_suffix('.tmp')
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(args.output)
        print(json.dumps(fields, ensure_ascii=False), flush=True)

    save(stage='waiting_for_creatives_and_first_audit')
    for filename, unit in [('continuation-controller.json','meta-scope-continuation-20261029'),
                           ('video-absence-audit.json','meta-scope-video-audit-20261029')]:
        while True:
            path = root/filename
            if path.exists() and json.loads(path.read_text()).get('stage') == 'finished':
                break
            state = subprocess.check_output(['systemctl','show',unit,'-p','ActiveState','--value'], text=True).strip()
            assert state in ('activating','active'), 'Prerequisite did not finish: '+unit
            time.sleep(30)
    save(stage='waiting_for_idle')
    while True:
        try:
            no_writes()
            break
        except AssertionError:
            time.sleep(30)
    pid = subprocess.check_output(['systemctl','show','drama-material-api','-p','MainPID','--value'], text=True).strip()
    env = dict(x.split(b'=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)
    assert env.get(b'DRAMA_JOB_USE_WORKER',b'').lower() in (b'1',b'true',b'yes',b'on')
    if args.deployed_backup:
        backup = args.deployed_backup.resolve()
        assert rollout.ROOT in backup.parents
        plan = json.loads((backup/'plan.json').read_text())
        expected_files = set(FILES) | {'features/fb_ad_asset_delete/graph.py'}
        assert {row['source'] for row in plan['files']} and {row['source'] for row in plan['files']} <= expected_files
        assert all(rollout.digest(release/row['source']) == row['after'] for row in plan['files'])
        assert all(rollout.digest(Path(row['target'])) == row['after'] for row in plan['files'])
        assert all(rollout.digest(release/name) == rollout.digest(rollout.LIVE/name) for name in expected_files)
    else:
        rollout.BASE, rollout.BASELINE_HASHES, rollout.FILES, rollout.idle = BASE, {}, FILES, no_writes
        captured = io.StringIO()
        with redirect_stdout(captured):
            rollout.prepare(release)
        plan = json.loads(captured.getvalue())
        backup = Path(plan['backup'])
        save(stage='prepared', backup=str(backup), commit=plan['commit'], before_pid=pid)
        no_writes()
        subprocess.run(['systemctl','stop','drama-material-api'], check=True)
        try:
            require_api_stopped()
            rollout.switch(backup)
        finally:
            subprocess.run(['bash',str(release/'scripts/safe_restart_drama_api.sh')], check=True)
    for attempt in range(30):
        try:
            if requests.get('http://127.0.0.1:8787/api/ui/topbar', timeout=5).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(1)
    else:
        raise RuntimeError('Main API health failed; no execution submitted')
    deployed = json.loads((backup/'plan.json').read_text())
    assert all(rollout.digest(Path(row['target'])) == row['after'] for row in deployed['files'])
    save(stage='verified', backup=str(backup), commit=plan['commit'], after_pid=subprocess.check_output(['systemctl','show','drama-material-api','-p','MainPID','--value'], text=True).strip(), health_status=200)
    actor = json.loads(job['actor'])
    session = read('SELECT session_token FROM drama_admin_session WHERE tenant_key=? AND user_id=? AND expires_at>? ORDER BY expires_at DESC LIMIT 1',
                   (actor[0],actor[1],int(time.time())), SESSIONS)
    assert session, 'Owner session unavailable'
    http = requests.Session()
    http.cookies.set('drama_admin_session', session[0]['session_token'])
    http.headers.update(Host='ai.yingliangads.com', Origin='https://ai.yingliangads.com')
    url = 'http://127.0.0.1:8787/api/fb-post-ad-delete/jobs/'+args.job
    http.get(url, params={'page':1,'page_size':1}, timeout=30).raise_for_status()

    def reconcile_once():
        count = read("SELECT COUNT(*) AS n FROM fb_asset_delete_v2_video_accounts WHERE job_id=? AND status='unknown'", (args.job,))[0]['n']
        count += read("SELECT COUNT(*) AS n FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind<>'video' AND status='unknown'", (args.job,))[0]['n']
        for number in range(count):
            try:
                response = http.post(url+'/reconcile', json={'preview_id':job['preview_id']}, timeout=45)
                response.raise_for_status()
            except requests.RequestException:
                save(reconciliation='read_not_confirmed', read_index=number)
                break
    reconcile_once()
    request_id = args.operation+'-execute'
    runs = read('SELECT * FROM fb_asset_delete_v2_runs WHERE request_id=?', (request_id,))
    if not runs:
        save(stage='submitting_pending_video', request_id=request_id)
        try:
            response = http.post(url+'/execute', json=dict(preview_id=job['preview_id'],request_id=request_id,phases=['video'],only_pending=True), timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            save(stage='execution_response_unconfirmed')
        runs = read('SELECT * FROM fb_asset_delete_v2_runs WHERE request_id=?', (request_id,))
    assert len(runs) == 1 and runs[0]['job_id'] == args.job and runs[0]['preview_id'] == job['preview_id']
    assert json.loads(runs[0]['phases']) == ['video'] and json.loads(runs[0]['summary']).get('execution_options',{}).get('only_pending') is True
    while runs[0]['status'] == 'running':
        counts = read("SELECT status,count(*) AS count FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='video' GROUP BY status", (args.job,))
        save(stage='executing_pending_video', run_id=runs[0]['run_id'], video_counts=counts)
        time.sleep(30)
        refresh_job(http, url)
        runs = read('SELECT * FROM fb_asset_delete_v2_runs WHERE request_id=?', (request_id,))
    pending = read("SELECT COUNT(*) AS n FROM fb_asset_delete_v2_objects WHERE job_id=? AND kind='video' AND status='pending'", (args.job,))[0]['n']
    save(stage='video_run_finished', run_status=runs[0]['status'], pending=pending)
    assert pending == 0, 'Unstarted Videos remain after another interruption; do not blindly resubmit'
    reconcile_once()
    save(stage='final_read_only_audit')
    subprocess.run(['python3',str(release/'scripts/audit_meta_scope_video_absence.py'), '--job',args.job,
                    '--output',str(root/'video-absence-audit-final.json')], check=True)
    save(stage='finished')


if __name__ == '__main__':
    main()
