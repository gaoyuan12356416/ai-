"""Scoped exact-commit deployment. Never restore publication databases."""
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

ROOT=Path('/mnt/data-disk/pipeline-dispatch')
LIVE=Path('/root/drama_material_service')
EXPECTED={
    'features/drama_synthesis/core.py':'434d9f6759c610f765a0f1c855d6b515bbc181b2e6b0eeb3ff4bc895a609a258',
    'features/youtube_auto_publish/service.py':'c2cef18a20792823000241715de5e734ed21afcc510f513d3f68a526e5196d5c',
}

def run(args,**kw):
    r=subprocess.run(args,capture_output=True,text=True,timeout=kw.pop('timeout',120),**kw)
    if r.returncode: raise RuntimeError('command_failed:'+args[0]+':'+str(r.returncode))
    return r.stdout.strip()

def hashfile(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def snapshot(path,target):
    with contextlib.closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)) as c,contextlib.closing(sqlite3.connect(target)) as d:c.backup(d,pages=1000)
    target.chmod(0o600)

def main():
    sha=sys.argv[1];assert re.fullmatch('[a-f0-9]{40}',sha)
    assert run(['findmnt','-no','UUID','/mnt/data-disk'])=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    release=ROOT/('youtube-publish-recovery-'+sha[:12])
    env=dict(os.environ,GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
    run(['git','-C',str(ROOT/'github'),'fetch','origin',sha],env=env)
    if not release.exists():run(['git','-C',str(ROOT/'github'),'worktree','add','--detach',str(release),sha])
    assert run(['git','-C',str(release),'rev-parse','HEAD'])==sha
    report_path=release/'deployment-result.json'
    if report_path.exists():print(report_path.read_text());return
    for name,expected in EXPECTED.items():assert hashfile(LIVE/name)==expected,'live_source_drift:'+name
    with contextlib.closing(sqlite3.connect('file:'+str(ROOT/'dispatch.sqlite3')+'?mode=ro',uri=True)) as c:
        assert not c.execute("SELECT 1 FROM pipeline_job WHERE channel='youtube' AND status='running'").fetchone(),'youtube_inflight'
        cfg=json.loads(c.execute('select config_json from pipeline_config where id=1').fetchone()[0])
        assert 'youtube' not in cfg['enabled_channels'],'pause_youtube_claims_first'
    oldpid=run(['systemctl','show','drama-material-api.service','-p','MainPID','--value'])
    children=run(['ps','--no-headers','--ppid',oldpid,'-o','comm']) if Path('/proc/'+oldpid+'/task/'+oldpid+'/children').read_text().strip() else ''
    assert not children,'api_child_processes_active'
    backup=ROOT/('youtube-recovery-backup-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    snapshot(LIVE/'data/drama_material_jobs.sqlite3',backup/'native-before.sqlite3')
    units=['pipeline-youtube-canonical.service','drama-material-api.service','pipeline-node-cpu-main.service','youtube-auto-publish-worker.service']
    before={u:run(['systemctl','show',u,'-p','MainPID','--value']) for u in units}
    for name in EXPECTED:
        p=backup/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(LIVE/name,p)
    drop=Path('/etc/systemd/system/pipeline-youtube-canonical.service.d/30-publish-recovery.conf')
    assert not drop.exists(),'existing_recovery_override'
    tests=subprocess.run(['/usr/bin/python3.9','-m','unittest','scripts.test_youtube_publish_recovery','scripts.test_pipeline_youtube_native','scripts.test_pipeline_youtube_canonical_service'],cwd=release,capture_output=True,text=True,timeout=90)
    (backup/'tests.log').write_text(tests.stdout+tests.stderr)
    assert tests.returncode==0,'linux_tests_failed'
    installed={}
    for name in EXPECTED:
        temporary=(LIVE/name).with_suffix('.py.recovery-tmp');shutil.copy2(release/name,temporary);os.replace(temporary,LIVE/name);installed[name]=hashfile(LIVE/name)
    drop.parent.mkdir(exist_ok=True)
    drop.write_text('[Service]\nExecStart=\nExecStart=/usr/bin/python3.9 '+str(release/'scripts/pipeline_youtube_canonical.py')+' --runtime-root /root/drama_material_service --dispatch-root '+str(release)+' --port 18847\n')
    plan={'commit':sha,'release':str(release),'backup':str(backup),'before_pids':before,'installed':installed,'dropin':str(drop)}
    (backup/'plan.json').write_text(json.dumps(plan,indent=2))
    run(['systemctl','daemon-reload'])
    run(['systemctl','restart','pipeline-youtube-canonical.service'])
    run(['systemctl','restart','drama-material-api.service'])
    for u in ('pipeline-youtube-canonical.service','drama-material-api.service'):assert run(['systemctl','is-active',u])=='active'
    after={u:run(['systemctl','show',u,'-p','MainPID','--value']) for u in units}
    assert before['pipeline-node-cpu-main.service']==after['pipeline-node-cpu-main.service']
    assert before['youtube-auto-publish-worker.service']==after['youtube-auto-publish-worker.service']
    plan['after_pids']=after
    report_path.write_text(json.dumps(plan,indent=2));(backup/'completed.json').write_text(json.dumps(plan,indent=2));print(json.dumps(plan),flush=True)

if __name__=='__main__':main()
