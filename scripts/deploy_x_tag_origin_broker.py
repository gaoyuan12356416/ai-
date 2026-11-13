"""Overlay four reviewed Tag files; safely drain only Tag before switching.

Deploy from a verified pushed GitHub checkout. Never restore business DBs.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.pipeline_service_drain import write_hold,pending,release

ROOT=Path(__file__).resolve().parents[1]
POINTER=Path('/opt/x-tag-publisher/current')
FILES=['features/x_tag_publish/'+f+'.py' for f in ('gateway','hit_timing','runtime','dispatch_native')]
TRIGGERS=('x-tag-auto-runner.timer','x-tag-auto-runner.path')
UNIT='x-tag-post.service'


def run(*args):return subprocess.run(args,check=True,capture_output=True,text=True).stdout.strip()
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def save(folder,r):
    p=folder/'record.json';tmp=folder/'record.tmp';tmp.write_text(json.dumps(r,indent=2)+'\n');tmp.chmod(0o600);tmp.replace(p)
def emit(**r):print(json.dumps(r),flush=True)
def state(unit):return dict(x.split('=',1) for x in run('systemctl','show',unit,'-p','ActiveState','-p','SubState','-p','UnitFileState','-p','MainPID').splitlines())
def ready(url):
    import urllib.request
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(120):
        try:
            with opener.open(url,timeout=2) as response:
                if response.status==200:return
        except (OSError,ValueError):
            pass
        time.sleep(.5)
    raise RuntimeError('service_not_ready')


def prepare(folder):
    folder.mkdir(mode=0o755,parents=True,exist_ok=False)
    folder.chmod(0o755)  # Service UID traversal must not depend on operator umask.
    previous=POINTER.resolve(strict=True);candidate=folder/'candidate'
    run('cp','-a','--reflink=auto',str(previous),str(candidate))
    r={'state':'prepared','source':str(ROOT),'revision':run('git','-C',str(ROOT),'rev-parse','HEAD'),
       'previous':str(previous),'candidate':str(candidate),'files':{},'utc':dt.datetime.now(dt.timezone.utc).isoformat()}
    for name in FILES:
        r['files'][name]={'before':sha(previous/name),'after':sha(ROOT/name)}
        shutil.copy2(ROOT/name,candidate/name);(candidate/name).chmod(0o644)
    run('/usr/bin/python3','-m','py_compile',*(str(candidate/name) for name in FILES))
    save(folder,r);emit(state='prepared',revision=r['revision'])


def switch(folder,rollback=False):
    r=json.loads((folder/'record.json').read_text())
    expected=r['candidate'] if rollback else r['previous'];target=r['previous'] if rollback else r['candidate']
    if str(POINTER.resolve())!=expected:raise RuntimeError('concurrent_tag_release_change')
    for name,hashes in r['files'].items():
        if sha(POINTER/name)!=hashes['after' if rollback else 'before']:raise RuntimeError('concurrent_tag_file_change')
    if 'triggers_before' not in r or (rollback and r['state']=='active'):
        r['triggers_before']={u:state(u) for u in TRIGGERS}
    save(folder,r)
    for unit in TRIGGERS:run('systemctl','stop',unit)
    write_hold(UNIT)
    r['state']='draining';save(folder,r)
    previous=None
    while True:
        jobs=pending(UNIT);signature=[(j['id'],j['generation']) for j in jobs]
        if signature!=previous:emit(draining=jobs);previous=signature
        if not jobs:break
        time.sleep(2)
    if str(POINTER.resolve())!=expected:
        release(UNIT)
        for unit,old in r['triggers_before'].items():
            if old['ActiveState']=='active':run('systemctl','start',unit)
        r['state']='concurrent_change_refused';save(folder,r);raise RuntimeError('concurrent_tag_release_change')
    r['services_before']={u:state(u) for u in (UNIT,'x-tag-gateway.service')};save(folder,r)
    run('systemctl','stop',UNIT)
    run('systemctl','stop','x-tag-gateway.service')
    temporary=POINTER.with_name('current.origin-new');temporary.symlink_to(target);temporary.replace(POINTER)
    run('systemctl','start','x-tag-gateway.service')
    ready('http://127.0.0.1:8898/healthz')
    run('systemctl','start',UNIT)
    ready('http://127.0.0.1:8821/health')
    for unit,old in r['triggers_before'].items():
        if old['ActiveState']=='active':run('systemctl','start',unit)
    r.update(state='rolled_back' if rollback else 'active',completed_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
             triggers_after={u:state(u) for u in TRIGGERS},services_after={u:state(u) for u in (UNIT,'x-tag-gateway.service')})
    save(folder,r);emit(state=r['state'],revision=r['revision'],target=target)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','activate','rollback'));p.add_argument('--directory',required=True,type=Path);a=p.parse_args()
    if a.action=='prepare':prepare(a.directory)
    else:switch(a.directory,a.action=='rollback')
if __name__=='__main__':main()
