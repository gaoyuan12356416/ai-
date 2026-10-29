"""One-time, durable continuation of the explicitly authorized Mobo cleanup.

Wait for the current DELETE run, roll out only the verified reference fix while
all writes are idle, then recheck and execute released Creatives through the API.
No timer, automatic retry, or independent Meta DELETE pool is created.
"""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import time

import requests
import deploy_meta_asset_recovery as rollout
from deploy_meta_delete_throughput import require_api_stopped
from deploy_meta_reference_scale import BASE, FILES, no_writes
from finish_meta_creative_scope_once import read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--creative-approval', type=Path)
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[1]
    assert Path('/mnt/data-disk').resolve() in args.output.resolve().parents
    assert args.output.is_dir()
    job = read('SELECT metadata,preview_id,actor FROM fb_asset_delete_v2_jobs WHERE job_id=?', (args.job,))[0]
    scope = json.loads(job['metadata'])
    assert set(scope['ids']) == {'XEY147', 'XEY50328'}
    assert {str(p['id']) for p in scope['products']} == {'2373', '3360', '2472', '3443'}
    report = dict(job_id=args.job, waited_run_id=args.run, release=str(release))
    if args.creative_approval:
        approval = json.loads(args.creative_approval.read_text())
        assert (approval['job_id'], approval['preview_id'], approval['actor']) == (args.job, job['preview_id'], job['actor'])
        keys = approval['creative_keys']
        assert len(keys) == len(set(keys)) == approval['count']
        assert hashlib.sha256('\n'.join(sorted(keys)).encode()).hexdigest() == approval['keys_sha256']
        report.update(approval_manifest=str(args.creative_approval), approved_changed_parent_creatives=len(keys),
                      approval_sha256=approval['keys_sha256'])

    def save(**fields):
        report.update(fields, updated_at=int(time.time()))
        path = args.output/'continuation-controller.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
        print(json.dumps(fields, ensure_ascii=False), flush=True)

    save(stage='waiting_for_current_run')
    while True:
        row = read('SELECT job_id,status FROM fb_asset_delete_v2_runs WHERE run_id=?', (args.run,))[0]
        assert row['job_id'] == args.job
        if row['status'] != 'running':
            break
        time.sleep(30)
    save(stage='waiting_for_idle', prior_run_status=row['status'])
    while True:
        try:
            no_writes()
            break
        except AssertionError:
            time.sleep(30)
    pid = subprocess.check_output(['systemctl','show','drama-material-api.service','-p','MainPID','--value'], text=True).strip()
    env = dict(item.split(b'=', 1) for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in item)
    assert env.get(b'DRAMA_JOB_USE_WORKER', b'').lower() in (b'1', b'true', b'yes', b'on'), 'Separate drama job worker is required for this narrow restart'
    rollout.BASE, rollout.BASELINE_HASHES, rollout.FILES, rollout.idle = BASE, {}, FILES, no_writes
    captured = io.StringIO()
    with redirect_stdout(captured):
        rollout.prepare(release)
    prepared = json.loads(captured.getvalue())
    backup = Path(prepared['backup'])
    save(stage='prepared', backup=str(backup), commit=prepared['commit'], before_pid=pid)
    no_writes()
    subprocess.run(['systemctl','stop','drama-material-api.service'], check=True)
    try:
        require_api_stopped()
        rollout.switch(backup)
        save(stage='applied')
    finally:
        subprocess.run(['bash',str(release/'scripts/safe_restart_drama_api.sh')], check=True)
    for attempt in range(30):
        try:
            response = requests.get('http://127.0.0.1:8787/api/ui/topbar', timeout=5)
            if response.status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(1)
    else:
        raise RuntimeError('Main API health did not recover; no continuation submitted')
    plan = json.loads((backup/'plan.json').read_text())
    assert all(rollout.digest(Path(item['target'])) == item['after'] for item in plan['files'])
    new_pid = subprocess.check_output(['systemctl','show','drama-material-api.service','-p','MainPID','--value'], text=True).strip()
    save(stage='verified', after_pid=new_pid, health_status=200)
    command = ['python3', str(release/'scripts/finish_meta_creative_scope_once.py'),
        '--job', args.job, '--operation', 'scope-recovery-20261029-reference-v2' if args.creative_approval else 'scope-recovery-20261029-reference-v1',
        '--output', str(args.output/'creative-continuation.json'), '--execute']
    if args.creative_approval:
        command += ['--creative-approval', str(args.creative_approval)]
    subprocess.run(command, check=True)
    save(stage='finished')


if __name__ == '__main__':
    main()
