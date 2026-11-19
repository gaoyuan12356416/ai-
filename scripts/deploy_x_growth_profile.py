"""GitHub-first, scoped growth-profile rollout; preserve all publishing history."""
import argparse
from contextlib import closing
import difflib
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import subprocess
import sys
import time

SOURCE=Path(__file__).resolve().parents[1]
ROOT=Path('/opt/x-tag-publisher/current')
BASE='6ea43e27'
HEALTH='/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
CHANGED=['features/x_tag_publish/'+n+'.py' for n in ('policy','selection','pool','gateway','hit_drama')]
NEW=['features/x_growth_profile/'+n+'.py' for n in ('__init__','store','model','metrics','selection','api','collector')]
STATIC=['static/'+n for n in ('x-growth-profile.js','x-tag-publish-ui.js','x-tag-auto-publish-template.js','x-tag-auto-publish-template.html')]
UNITS=['x-growth-profile.service','x-growth-profile.timer']

def run(*args,timeout=60):
    return subprocess.check_output(list(map(str,args)),text=True,timeout=timeout).strip()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def atomic(path,data,mode=0o644,uid=0,gid=0):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.'+path.name+'.growth-next')
    temporary.write_bytes(data);temporary.chmod(mode);os.chown(temporary,uid,gid);os.replace(temporary,path)

def remember(folder,info,path,data):
    path=Path(path);key=str(len(info['files']));exists=path.exists()
    previous=path.read_bytes() if exists else b'';stat=path.stat() if exists else None
    backup=folder/'files'/(key+'.before');backup.parent.mkdir(exist_ok=True)
    backup.write_bytes(previous)
    next_path=folder/'files'/(key+'.after');next_path.write_bytes(data)
    info['files'][str(path)]=dict(existed=exists,before=digest(previous),after=digest(data),backup=str(backup),next=str(next_path),
                                mode=stat.st_mode&0o777 if stat else 0o644,uid=stat.st_uid if stat else 0,gid=stat.st_gid if stat else 0)

def overlay(folder,name,live):
    old=subprocess.check_output(['git','-C',str(SOURCE),'show',BASE+':'+name]).replace(b'\r\n',b'\n')
    new=(SOURCE/name).read_bytes().replace(b'\r\n',b'\n');live=live.replace(b'\r\n',b'\n')
    if live==old:return new
    scratch=folder/'merge-file';scratch.write_bytes(live)
    patch=''.join(difflib.unified_diff(old.decode().splitlines(True),new.decode().splitlines(True),fromfile='a/merge-file',tofile='b/merge-file',n=3))
    result=subprocess.run(['git','apply','--whitespace=nowarn','-'],cwd=folder,input=patch,text=True,capture_output=True)
    if result.returncode:raise RuntimeError('concurrent_live_change_requires_merge:'+name+':'+result.stderr)
    return scratch.read_bytes()

def prepare(folder):
    assert run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk')) and shutil.disk_usage('/mnt/data-disk').free>2*1024**3
    assert not run('git','-C',SOURCE,'status','--porcelain')
    commit=run('git','-C',SOURCE,'rev-parse','HEAD')
    assert run('git','-C',SOURCE,'branch','-r','--contains',commit)
    folder.mkdir(parents=True,mode=0o700,exist_ok=False)
    info=dict(commit=commit,root=str(ROOT.resolve()),files={},status='preparing',before_pids={})
    candidate=folder/'candidate'
    shutil.copytree(ROOT.resolve(),candidate,symlinks=True,ignore=shutil.ignore_patterns('.git','__pycache__','*.pyc','.env*','*.sqlite3*'))
    for name in CHANGED+NEW:
        path=ROOT/name
        data=overlay(folder,name,path.read_bytes()) if name in CHANGED else (SOURCE/name).read_bytes()
        if name in NEW and path.exists():assert path.read_bytes()==data,'new_module_collision:'+name
        dest=candidate/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data);compile(data,name,'exec')
        remember(folder,info,path,data)
    for name in STATIC:
        public=Path('/usr/share/nginx/html')/Path(name).name
        data=(SOURCE/name).read_bytes() if name.endswith('x-growth-profile.js') else overlay(folder,name,public.read_bytes())
        for base in [ROOT,Path('/root/drama_material_service'),Path('/usr/share/nginx/html')]:
            path=base/(Path(name).name if base.name=='html' else name)
            remember(folder,info,path,data)
    for name in UNITS:remember(folder,info,Path('/etc/systemd/system')/name,(SOURCE/'deploy'/name).read_bytes())
    for unit in ['x-tag-auto.service','x-tag-gateway.service','x-tag-post.service','x-post-automation.service']:
        info['before_pids'][unit]=run('systemctl','show',unit,'-p','MainPID','--value')
    for name,path in [('auto','/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3'),('posts','/mnt/data-disk/x-tag-publisher/posts.sqlite3')]:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(folder/(name+'-before.sqlite3'))) as dst:src.backup(dst)
    tests=['scripts/test_x_growth_profile.py','scripts/test_hit_drama_launch_window.py','scripts/test_auto_material_products.py']
    for name in tests:shutil.copy2(SOURCE/name,candidate/name)
    for i,cmd in enumerate([[sys.executable,'-B','scripts/test_x_growth_profile.py'],[sys.executable,'-B','scripts/test_hit_drama_launch_window.py','--platform','x'],[sys.executable,'-B','scripts/test_auto_material_products.py','--platform','x']]):
        result=subprocess.run(cmd,cwd=candidate,env=dict(os.environ,X_GROWTH_DB_PATH=str(folder/'absent-offline-db.sqlite3')),capture_output=True,text=True,timeout=120)
        (folder/('test-'+str(i)+'.log')).write_text(result.stdout+result.stderr)
        if result.returncode:print(result.stdout+result.stderr);raise RuntimeError('candidate_test_failed:'+str(i))
        print('candidate test passed',i,flush=True)
    info['status']='prepared';save(folder/'manifest.json',info)
    print(json.dumps(dict(status='prepared',commit=commit,files=len(info['files']))))

def apply(folder,rollback=False):
    info=json.loads((folder/'manifest.json').read_text());assert str(ROOT.resolve())==info['root'],'runtime_changed'
    action='rollback' if rollback else 'apply'
    if rollback:
        state=Path('/mnt/data-disk/x-tag-publisher/growth/state.sqlite3')
        if state.exists():
            with closing(sqlite3.connect(state.as_uri()+'?mode=ro',uri=True)) as db:
                assert db.execute("SELECT COUNT(*) FROM channel_mode WHERE mode='growth'").fetchone()[0]==0,'operator_growth_choices_require_forward_fix_or_manual_switch'
        run('systemctl','disable','--now','x-growth-profile.timer')
        run('systemctl','stop','x-growth-profile.service',timeout=60)
    for name,row in info['files'].items():
        actual=digest(Path(name).read_bytes()) if Path(name).exists() else digest(b'')
        assert actual in (row['before'],row['after']),'preserve_later_change:'+name
    run(sys.executable,HEALTH,'snapshot','--lane','x','--output',folder/(action+'-schedulers-before.json'))
    for name,row in info['files'].items():
        if '/static/' in name or name.startswith('/usr/share/nginx/html/'):continue
        if rollback and not row['existed']:continue
        atomic(name,Path(row['backup' if rollback else 'next']).read_bytes(),row['mode'],row['uid'],row['gid'])
    if not rollback:
        sys.path.insert(0,str(ROOT));from features.x_growth_profile.store import initialize
        initialize();user=pwd.getpwnam('x-post-automation');state=Path('/mnt/data-disk/x-tag-publisher/growth')
        os.chown(state,user.pw_uid,user.pw_gid);state.chmod(0o755)
        os.chown(state/'state.sqlite3',user.pw_uid,user.pw_gid);(state/'state.sqlite3').chmod(0o644)
    run('systemctl','daemon-reload')
    # The existing Auto owner drains accepted requests on SIGTERM. The publisher
    # itself stays running; no queues, template enable flags or tokens are reset.
    run('systemctl','restart','--no-block','x-tag-auto.service')
    deadline=time.monotonic()+40
    while run('ss','-Htn','state','established','( sport = :8898 )'):
        if time.monotonic()>deadline:raise RuntimeError('gateway_busy_retry_activation')
        time.sleep(1)
    run('systemctl','restart','--no-block','x-tag-gateway.service')
    info.update(status='switch_requested',action=action);save(folder/'manifest.json',info)
    print(json.dumps(dict(status=info['status'],action=action)))

def verify(folder):
    info=json.loads((folder/'manifest.json').read_text());rollback=info['action']=='rollback'
    for unit,port,path in [('x-tag-auto.service',18843,'/health'),('x-tag-gateway.service',8898,'/healthz')]:
        assert run('systemctl','show',unit,'-p','ActiveState','--value')=='active',unit+':pending'
        assert run('systemctl','show',unit,'-p','MainPID','--value')!=info['before_pids'][unit],unit+':old_process'
        run('curl','--max-time','5','-fsS','http://127.0.0.1:'+str(port)+path)
    run(sys.executable,HEALTH,'restore','--baseline',folder/(info['action']+'-schedulers-before.json'),'--wait-seconds','25','--output',folder/(info['action']+'-schedulers-after.json'))
    for name,row in info['files'].items():
        if not ('/static/' in name or name.startswith('/usr/share/nginx/html/')):continue
        if rollback and not row['existed']:continue
        actual=digest(Path(name).read_bytes()) if Path(name).exists() else digest(b'')
        assert actual in (row['before'],row['after']),'preserve_later_static_change:'+name
        atomic(name,Path(row['backup' if rollback else 'next']).read_bytes(),row['mode'],row['uid'],row['gid'])
    for name,row in info['files'].items():
        if rollback and not row['existed']:continue
        assert digest(Path(name).read_bytes())==row['before' if rollback else 'after'],'hash_mismatch:'+name
    for unit in ['x-tag-post.service','x-post-automation.service']:
        assert run('systemctl','show',unit,'-p','ActiveState','--value')=='active'
        assert run('systemctl','show',unit,'-p','MainPID','--value')==info['before_pids'][unit],unit+':publisher_changed'
    if not rollback:
        run('systemctl','enable','--now','x-growth-profile.timer')
        run('systemctl','start','--no-block','x-growth-profile.service')
    info['status']='rolled_back' if rollback else 'active';save(folder/'manifest.json',info)
    print(json.dumps(dict(status=info['status'],commit=info['commit'],publisher_processes_unchanged=True)))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','apply','verify','rollback']);p.add_argument('--folder',type=Path,required=True);a=p.parse_args()
    if a.action=='prepare':prepare(a.folder.resolve())
    elif a.action=='verify':verify(a.folder.resolve())
    else:apply(a.folder.resolve(),a.action=='rollback')
