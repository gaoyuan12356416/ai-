"""Scoped X tag material-window deployment; ledger/receipt data never rolls back."""
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

SOURCE=Path(__file__).resolve().parents[1]
ROOT=Path('/mnt/data-disk/x-material-window-20261123')
LIVE=Path('/opt/x-tag-publisher/current')
EFFECTS=Path('/opt/x-material-effects/current')
BASE='319626a9'
FEATURES=['features/x_auto_posts/'+n+'.py' for n in ('core','publisher','selector','validation')]+[
    'features/x_material_effects/reuse.py','features/x_material_effects/window.py','features/x_posts/service.py']
STATIC=['static/x-tag-auto-publish-template.html','static/x-tag-auto-publish-template.js']
TRIGGERS=('x-tag-auto-runner.timer','x-tag-auto-runner.path')
UNITS=('x-tag-post.service','x-tag-auto.service','x-material-effects-api.service',*TRIGGERS,'x-tag-auto-scheduler.timer')
AUTO='/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3'
POSTS='/mnt/data-disk/x-tag-publisher/posts.sqlite3'
HEALTH='/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'


def run(*args):return subprocess.check_output(list(map(str,args)),text=True).strip()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).exists() else None
def save(name,value):(ROOT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2))
def manifest():return json.loads((ROOT/'deployment.json').read_text())
def states():
    return {u:dict(line.split('=',1) for line in run('systemctl','show',u,'-p','ActiveState','-p','SubState','-p','MainPID','-p','NextElapseUSecRealtime').splitlines()) for u in UNITS}
def readonly(path):return sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=15)


def prepare():
    assert not run('git','-C',SOURCE,'status','--porcelain')
    commit=run('git','-C',SOURCE,'rev-parse','HEAD')
    assert run('git','-C',SOURCE,'branch','-r','--contains',commit)
    ROOT.mkdir(mode=0o700,exist_ok=True)
    assert not (ROOT/'deployment.json').exists()
    pairs=[(name,LIVE/name) for name in FEATURES]
    pairs += [(name,EFFECTS/name) for name in FEATURES if name.startswith('features/x_material_effects/')]
    for name in STATIC:
        pairs += [(name,LIVE/name),(name,Path('/root/drama_material_service')/name),
                  (name,Path('/usr/share/nginx/html')/Path(name).name)]
    m=dict(commit=commit,source=str(SOURCE),units=states(),files=[],stage='prepared')
    for name,target in pairs:
        target=target.resolve();new=SOURCE/name
        old=subprocess.run(['git','-C',str(SOURCE),'show',BASE+':'+name],capture_output=True)
        assert target.exists()==(old.returncode==0),'unexpected_live_file:'+str(target)
        if target.exists():assert target.read_bytes().replace(b'\r\n',b'\n')==old.stdout.replace(b'\r\n',b'\n'),'concurrent_change:'+str(target)
        if name.endswith('.py'):compile(new.read_bytes(),name,'exec')
        index=len(m['files']);backup=ROOT/'before'/str(index);backup.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():shutil.copy2(target,backup)
        st=(target if target.exists() else target.parent/'reuse.py').stat()
        m['files'].append(dict(name=name,target=str(target),backup=str(backup),before=sha(target),after=sha(new),uid=st.st_uid,gid=st.st_gid,mode=st.st_mode&0o777))
    with closing(readonly(POSTS)) as db:
        m['queue_guard_before']=db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_x_effect_account_dedupe'").fetchone()[0]
    for label,path in [('auto',AUTO),('posts',POSTS),('reuse','/mnt/data-disk/x-material-effects/reuse.sqlite3')]:
        with closing(readonly(path)) as src,closing(sqlite3.connect(ROOT/(label+'-before.sqlite3'))) as dst:
            src.execute('BEGIN');src.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
            src.backup(dst,pages=2048,sleep=.01)
            assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        print('backed_up',label,flush=True)
    run(sys.executable,HEALTH,'snapshot','--lane','x','--output',ROOT/'schedulers-before.json')
    save('deployment.json',m);print(json.dumps(dict(stage='prepared',commit=commit,files=len(m['files']))))


def copy_file(f,rollback=False):
    p=Path(f['target'])
    if rollback and f['before'] is None:return  # unused additive helper is safe
    data=Path(f['backup']).read_bytes() if rollback else (SOURCE/f['name']).read_bytes()
    tmp=p.with_name(p.name+'.window-next');tmp.write_bytes(data)
    os.chown(tmp,f['uid'],f['gid']);os.chmod(tmp,f['mode']);os.replace(tmp,p)


def quiet_auto():
    assert run('systemctl','show','x-tag-auto-runner.service','-p','MainPID','--value')=='0','runner_still_finishing'
    with closing(readonly(AUTO)) as db:
        assert not db.execute("SELECT 1 FROM x_auto_task WHERE claim_token<>'' AND status IN ('selecting','preparing','publishing') LIMIT 1").fetchone(),'auto_request_inflight'


def install(rollback=False):
    m=manifest()
    for f in m['files']:
        assert sha(f['target'])==(f['after'] if rollback else f['before']),'concurrent_change:'+f['target']
    for u in TRIGGERS:run('systemctl','stop',u)
    quiet_auto()  # Let an existing runner finish naturally before switching.
    for f in m['files']:
        if not f['name'].startswith('static/'):copy_file(f,rollback)
    run('systemctl','restart','x-material-effects-api.service')
    m['old_pid']=run('systemctl','show','x-tag-post.service','-p','MainPID','--value')
    m['stage']='rollback_pending' if rollback else 'restart_pending';save('deployment.json',m)
    run('systemctl','restart','--no-block','x-tag-post.service')
    print(json.dumps(dict(stage=m['stage'],prior_pid=m['old_pid'])))


def finish():
    m=manifest();s=states();rollback=m['stage']=='rollback_pending'
    assert m['stage'] in ('restart_pending','rollback_pending')
    assert s['x-tag-post.service']['ActiveState']=='active' and s['x-tag-post.service']['MainPID'] not in ('0',m['old_pid']),'publisher_draining'
    quiet_auto()
    if rollback:
        # Restore only the constraint definition; current queues/receipts remain.
        code='import sqlite3\nc=sqlite3.connect('+repr(POSTS)+')\nc.execute("BEGIN IMMEDIATE")\nc.execute("DROP TRIGGER trg_x_effect_account_dedupe")\nc.execute('+repr(m['queue_guard_before'])+')\nc.commit()\nc.close()'
    else:
        code='import sys,sqlite3\nsys.path.insert(0,'+repr(str(LIVE))+')\nfrom features.x_material_effects.window import install_queue_guard\nc=sqlite3.connect('+repr(POSTS)+')\ninstall_queue_guard(c)\nc.close()'
    run('runuser','-u','x-post-automation','--',sys.executable,'-B','-c',code)
    run('systemctl','restart','x-tag-auto.service')
    for f in m['files']:
        if f['name'].startswith('static/'):
            assert sha(f['target'])==(f['after'] if rollback else f['before']),'concurrent_static_change'
            copy_file(f,rollback)
    for u in TRIGGERS:
        if m['units'][u]['ActiveState']=='active':run('systemctl','start',u)
    run(sys.executable,HEALTH,'restore','--baseline',ROOT/'schedulers-before.json','--wait-seconds','25','--output',ROOT/'schedulers-after.json')
    s=states()
    assert all(s[u]['ActiveState']=='active' for u in UNITS)
    m['stage']='rolled_back' if rollback else 'active';m['after']=s;save('deployment.json',m)
    print(json.dumps(dict(stage=m['stage'],services=s)))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','install','finish','rollback']);args=parser.parse_args()
    if args.action=='prepare':prepare()
    elif args.action=='finish':finish()
    else:install(args.action=='rollback')
