#!/usr/bin/env python3
"""Activate a verified GPU release with audited stop-after-current-job draining.

Default is read-only. Replaces only the GPU node unit and private installation
manifest; never edits capacities, ledgers, credentials, receipts or job state.
Also supports rollback to a staged verified release under the same drain rule.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.pipeline_node_install import private_json, plan, command

DRAIN_WORKER_SHAS = {
    '7abc1300e57bc515a504d9295708b3d449e40c1ad9ba1dff6e6eb76a8e160b06',
    # Same signal/claim/drain implementation; bounded heartbeat reconfirmation
    # before the next operation, never a retry of a platform request.
    '421978ad0419c514f8d48db737b3110975fb5ba01ec005a7e5b2baad326351e3',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--current', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--replace-pending', action='store_true', help='update an existing draining restart target without signaling jobs again')
    args = parser.parse_args()
    old, new = private_json(args.current), private_json(args.candidate)
    before, after = plan(old), plan(new)
    if old['backend'] != 'gpu' or new['backend'] != 'gpu': raise ValueError('gpu_only')
    for key in ('node_id', 'state', 'unit', 'root'):
        if before[key] != after[key]: raise ValueError('node_identity_changed')
    for info in (before, after):
        root = Path(info['release'])
        if hashlib.sha256((root/'features/pipeline_dispatch/worker.py').read_bytes()).hexdigest() not in DRAIN_WORKER_SHAS:
            raise ValueError('unreviewed_drain_worker')
        if command(['git', '-C', root, 'rev-parse', 'HEAD']) != info['sha'] or command(['git', '-C', root, 'status', '--porcelain']):
            raise ValueError('release_not_clean')
        if (Path(info['staging'])/'.verified').read_text() != info['sha']: raise ValueError('release_not_verified')
    unit = Path('/etc/systemd/system')/before['unit']
    candidate = Path(after['staging'])/after['unit']
    for path in (unit, candidate):
        value = path.read_text()
        if 'KillMode=mixed' not in value or 'TimeoutStopSec=infinity' not in value:
            raise ValueError('unsafe_unit_drain')
    props = dict(line.split('=',1) for line in command(['systemctl','show',before['unit'],
        '-p','ActiveState','-p','MainPID','-p','KillMode','-p','TimeoutStopUSec']).splitlines())
    draining = props['ActiveState'] == 'deactivating' and args.replace_pending
    if (props['ActiveState'] != 'active' and not draining) or props['KillMode'] != 'mixed' or props['TimeoutStopUSec'] != 'infinity':
        raise ValueError('runtime_not_ready')
    if draining:
        jobs = [row.split() for row in command(['systemctl','list-jobs','--no-legend','--plain']).splitlines()]
        if not any(len(row) >= 4 and row[1:4] == [before['unit'],'restart','running'] for row in jobs):
            raise ValueError('no_pending_restart')
    pid = int(props['MainPID']); proc = Path('/proc')/str(pid)
    if before['release']+'/scripts/pipeline_worker.py' not in proc.joinpath('cmdline').read_bytes().decode().split('\0'):
        raise ValueError('runtime_manifest_mismatch')
    report = {'apply':args.apply, 'node_id':before['node_id'], 'previous_sha':before['sha'],
              'candidate_sha':after['sha'], 'old_main_pid':pid,
              'children_before':proc.joinpath('task',str(pid),'children').read_text().split()}
    if not args.apply: print(json.dumps(report)); return
    backup = Path(before['state'])/'maintenance'/('gpu-offload-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    for source, name in ((Path(args.current),'install-manifest.before.json'),
                         (unit,'node.service.before'), (Path(before['staging'])/'node.env','node.env.before')):
        shutil.copyfile(source, backup/name); (backup/name).chmod(0o600)
    report['backup'] = str(backup)
    (backup/'audit.json').write_text(json.dumps(report,indent=2))
    # The running parent still uses its old imported code and environment.
    # systemd starts the staged command only after all old children have joined.
    command(['install','-m','0644',str(candidate),str(unit)])
    temporary = Path(args.current+'.offload-new')
    with temporary.open('x') as handle:
        os.chmod(temporary,0o600); handle.write(json.dumps(new,indent=2)+'\n'); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary,args.current)
    command(['systemctl','daemon-reload'])
    if not draining: command(['systemctl','restart','--no-block',before['unit']])
    report['status'] = 'draining_current_jobs_then_start_verified_release'
    (backup/'audit.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))

if __name__ == '__main__': main()
