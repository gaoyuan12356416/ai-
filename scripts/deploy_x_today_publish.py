"""Scoped code activation for today's X publication repair; never restores ledgers."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
LIVE = Path('/opt/x-tag-publisher/current')
ROOT = Path('/mnt/data-disk/x-today-publish-20261123')
BASE = '4006c6d2'
FILES = ['features/x_tag_publish/account_snapshot.py', 'features/x_tag_publish/runtime.py',
         'features/x_tag_publish/gateway.py', 'features/x_tag_publish/dispatch_native.py',
         'features/x_material_effects/selection.py', 'features/x_auto_posts/selector.py']
UNITS = ['x-tag-post.service','x-tag-auto.service','x-tag-gateway.service',
         'x-tag-auto-runner.timer','x-tag-auto-runner.path','x-tag-auto-scheduler.timer']
AUTO = '/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3'
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'

def run(*args):return subprocess.check_output(list(map(str,args)),text=True).strip()
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(name,value):(ROOT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2))
def states():
    return {u:dict(line.split('=',1) for line in run('systemctl','show',u,'-p','ActiveState','-p','SubState','-p','MainPID','-p','NextElapseUSecRealtime').splitlines()) for u in UNITS}
def readonly(path):
    c=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=10);c.row_factory=sqlite3.Row;return c

def prepare():
    assert run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git','-C',SOURCE,'status','--porcelain')
    commit=run('git','-C',SOURCE,'rev-parse','HEAD')
    assert run('git','-C',SOURCE,'branch','-r','--contains',commit)
    ROOT.mkdir(mode=0o700,exist_ok=True)
    assert not (ROOT/'deployment.json').exists()
    m=dict(commit=commit,source=str(SOURCE),runtime=str(LIVE.resolve()),units=states(),files=[],stage='prepared')
    for name in FILES:
        target=LIVE/name;new=SOURCE/name;compile(new.read_bytes(),name,'exec')
        old=subprocess.run(['git','-C',str(SOURCE),'show',BASE+':'+name],capture_output=True)
        assert target.exists()==(old.returncode==0),'unexpected_live_file:'+name
        if target.exists():assert target.read_bytes().replace(b'\r\n',b'\n')==old.stdout.replace(b'\r\n',b'\n'),'concurrent_change:'+name
        backup=ROOT/'before'/name;backup.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():shutil.copy2(target,backup)
        st=(target if target.exists() else LIVE/'features/x_tag_publish/runtime.py').stat()
        m['files'].append(dict(name=name,before=sha(target) if target.exists() else None,after=sha(new),uid=st.st_uid,gid=st.st_gid,mode=st.st_mode&0o777))
    with closing(readonly(AUTO)) as c:
        tasks=[dict(r) for r in c.execute("SELECT * FROM x_auto_task WHERE template_id=9000000000007 AND created_at>='2026-11-22T16:00:00' AND created_at<'2026-11-23T16:00:00' ORDER BY id")]
        save('today-scope.json',dict(day='2026-11-23',template_id=9000000000007,tasks=tasks))
    for label,path in [('auto',AUTO),('posts','/mnt/data-disk/x-tag-publisher/posts.sqlite3'),('dispatch','/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')]:
        # Pin the WAL snapshot: otherwise frequent live writes restart an
        # incremental backup indefinitely. This never pauses the writers.
        with closing(readonly(path)) as src,closing(sqlite3.connect(ROOT/(label+'-before.sqlite3'))) as dst:
            src.execute('BEGIN');src.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
            src.backup(dst,pages=2048,sleep=.01)
            assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        print('backed_up',label,flush=True)
    run(sys.executable,HEALTH,'snapshot','--lane','x','--output',ROOT/'schedulers-before.json')
    save('deployment.json',m);print(json.dumps(dict(stage='prepared',commit=commit,tasks=len(tasks))))

def install(rollback=False,update=False):
    m=json.loads((ROOT/'deployment.json').read_text());assert str(LIVE.resolve())==m['runtime']
    gateway_changed=not update or sha(SOURCE/'features/x_tag_publish/gateway.py')!=sha(LIVE/'features/x_tag_publish/gateway.py')
    for f in m['files']:
        p=LIVE/f['name'];expected=f['after'] if rollback or update else f['before']
        assert (sha(p) if p.exists() else None)==expected,'concurrent_change:'+f['name']
    # Pause only this planner's triggers while its execution service switches.
    for u in ('x-tag-auto-runner.timer','x-tag-auto-runner.path'):run('systemctl','stop',u)
    for f in m['files']:
        if rollback and f['before'] is None:continue  # unused additive helper is harmless
        p=LIVE/f['name'];src=(ROOT/'before'/f['name']) if rollback else SOURCE/f['name']
        tmp=p.with_name(p.name+'.today-next');tmp.write_bytes(src.read_bytes());os.chown(tmp,f['uid'],f['gid']);os.chmod(tmp,f['mode']);os.replace(tmp,p)
        if update:f['after']=sha(src)
    if update:m['commit']=run('git','-C',SOURCE,'rev-parse','HEAD')
    if gateway_changed:run('systemctl','restart','x-tag-gateway.service')
    m['old_pid']=run('systemctl','show','x-tag-post.service','-p','MainPID','--value')
    m['stage']='rollback_pending' if rollback else 'restart_pending';save('deployment.json',m)
    run('systemctl','restart','--no-block','x-tag-post.service')
    print(json.dumps(dict(stage=m['stage'],prior_pid=m['old_pid'])))

def finish():
    m=json.loads((ROOT/'deployment.json').read_text());s=states()
    assert s['x-tag-post.service']['ActiveState']=='active' and s['x-tag-post.service']['MainPID'] not in ('0',m['old_pid']),'publisher_draining'
    with closing(readonly(AUTO)) as c:
        assert not c.execute("SELECT 1 FROM x_auto_task WHERE claim_token<>'' AND status IN ('selecting','preparing','publishing') LIMIT 1").fetchone(),'auto_request_inflight'
    assert run('systemctl','show','x-tag-auto-runner.service','-p','MainPID','--value')=='0','runner_still_finishing'
    run('systemctl','restart','x-tag-auto.service')
    for u,before in m['units'].items():
        if u.endswith(('.timer','.path')) and before['ActiveState']=='active':run('systemctl','start',u)
    run(sys.executable,HEALTH,'restore','--baseline',ROOT/'schedulers-before.json',
        '--wait-seconds','25','--output',ROOT/'schedulers-after.json')
    s=states();assert all(s[u]['ActiveState']=='active' for u in UNITS)
    m['stage']='rolled_back' if m['stage']=='rollback_pending' else 'active';m['after']=s;save('deployment.json',m)
    print(json.dumps(dict(stage=m['stage'],services=s)))

def gateway_only():
    m=json.loads((ROOT/'deployment.json').read_text());assert m['stage']=='active'
    commit=run('git','-C',SOURCE,'rev-parse','HEAD');assert run('git','-C',SOURCE,'branch','-r','--contains',commit)
    files=[f for f in m['files'] if f['name'] in ('features/x_tag_publish/gateway.py','features/x_tag_publish/account_snapshot.py')]
    for f in files:
        p=LIVE/f['name'];assert sha(p)==f['after']
    for f in files:
        p=LIVE/f['name'];src=SOURCE/f['name'];compile(src.read_bytes(),f['name'],'exec')
        tmp=p.with_name(p.name+'.today-next');tmp.write_bytes(src.read_bytes());os.chown(tmp,f['uid'],f['gid']);os.chmod(tmp,f['mode']);os.replace(tmp,p)
        f['after']=sha(p)
    run('systemctl','restart','x-tag-gateway.service')
    m['commit']=commit;m['after']=states();save('deployment.json',m)
    print(json.dumps({'stage':'active','gateway_commit':commit}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','install','finish','rollback','update','gateway']);a=p.parse_args()
    if a.action=='prepare':prepare()
    elif a.action=='finish':finish()
    elif a.action=='gateway':gateway_only()
    else:install(a.action=='rollback',a.action=='update')
