"""Exact-file guarded coordinator rollout; never restart execution nodes.

Run beside deploy_pipeline_queue_states.py from the same GitHub commit.
Rollback requires new preparation claims stopped and namespaced locks drained.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

import deploy_pipeline_queue_states as guard

MODULES=('features/pipeline_dispatch/core.py','features/pipeline_dispatch/service.py')
guard.MODULES=MODULES
ROOT=guard.ROOT
DB=guard.DB


def snapshot(target):
    with contextlib.closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)) as source:
        with contextlib.closing(sqlite3.connect(str(target))) as dest: source.backup(dest)
    os.chmod(target,0o600)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--sha');parser.add_argument('--expected-sha')
    parser.add_argument('--rollback');parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    assert ROOT.parent.is_mount() and guard.run(['findmnt','-n','-o','UUID','--target',str(ROOT.parent)])==guard.UUID
    runtime=Path('/opt/pipeline-dispatch/current').resolve()
    assert str(runtime).startswith('/mnt/data-disk/pipeline-dispatch-runtime/')
    replacements={};expected={};report={'apply':args.apply,'runtime':str(runtime)}
    if args.rollback:
        backup=Path(args.rollback).resolve()
        assert backup.parent==ROOT and backup.name.startswith('x-prepare-overlap-')
        manifest=json.loads((backup/'manifest.json').read_text())
        assert manifest['runtime']==str(runtime)
        with contextlib.closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)) as c:
            assert c.execute("SELECT count(*) FROM pipeline_resource WHERE resource_key LIKE 'prepare:%'").fetchone()[0]==0, 'drain_preparation_lanes_before_rollback'
        for item in manifest['files']:
            target=Path(item['path']);expected[target]=target.read_bytes()
            assert guard.digest(expected[target])==item['deployed_sha256']
            replacements[target]=(backup/item['backup']).read_bytes()
        report['rollback']=str(backup)
    else:
        assert all(re.fullmatch('[a-f0-9]{40}',sha or '') for sha in (args.sha,args.expected_sha))
        env=dict(os.environ,GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
        for sha in (args.sha,args.expected_sha): guard.run(['git','-C',str(guard.REPO),'fetch','origin',sha],timeout=120,env=env)
        for name in MODULES:
            target=runtime/name
            expected[target]=subprocess.check_output(['git','-C',str(guard.REPO),'show',args.expected_sha+':'+name])
            replacements[target]=subprocess.check_output(['git','-C',str(guard.REPO),'show',args.sha+':'+name])
            assert target.read_bytes()==expected[target], 'unexpected_live_file:'+name
            compile(replacements[target],name,'exec')
        report.update(sha=args.sha,expected_sha=args.expected_sha)
    report['file_hashes']={str(p):guard.digest(v) for p,v in replacements.items()}
    if not args.apply:
        print(json.dumps(report));return
    tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=ROOT/('x-prepare-overlap-'+tag);backup.mkdir(mode=0o700)
    shutil.copy2(__file__,backup/'deploy_x_prepare_overlap.py')
    shutil.copy2(Path(guard.__file__),backup/'deploy_pipeline_queue_states.py')
    files=[]
    for i,(target,old) in enumerate(expected.items()):
        name='original-'+str(i);(backup/name).write_bytes(old)
        files.append({'path':str(target),'backup':name,'deployed_sha256':guard.digest(replacements[target])})
    (backup/'manifest.json').write_text(json.dumps({'runtime':str(runtime),'files':files},indent=2))
    snapshot(backup/'dispatch.before.sqlite3')
    services=('pipeline-node-cpu-main.service','x-post-automation.service','drama-material-api.service')
    before_pids={s:guard.pid(s) for s in services};before_config=guard.config_fingerprint()
    protected=[runtime/'features/pipeline_dispatch/adapters.py',runtime/'features/pipeline_dispatch/queue_states.py',
               Path('/usr/share/nginx/html/pipeline-monitor.js'),Path('/usr/share/nginx/html/pipeline-monitor.html')]
    before_files={str(p):guard.digest(p.read_bytes()) for p in protected}
    report.update(guard.restart_with_files(replacements,expected,runtime,tag))
    report.update(backup=str(backup),configuration_before=before_config,configuration_after=guard.config_fingerprint(),
        unrelated_pids_unchanged={s:guard.pid(s)==v for s,v in before_pids.items()},
        unrelated_files_unchanged=all(guard.digest(Path(p).read_bytes())==v for p,v in before_files.items()))
    assert all(report['unrelated_pids_unchanged'].values()) and report['unrelated_files_unchanged']
    assert all(p.read_bytes()==v for p,v in replacements.items())
    (backup/'result.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__=='__main__':main()
