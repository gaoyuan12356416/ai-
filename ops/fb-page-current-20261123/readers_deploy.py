"""Blue/green HTTP handoff; only the canonical assignment process runs the outbox."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request
from deploy import ROOT,SOURCE,atomic,backup,build,disk,put,stamp

ROLES={
 'api':('fb-page-portrait',18852,21852,'/etc/nginx/default.d/fb-page-portrait.conf'),
 'assignment':('fb-page-assignment',8943,21943,'/etc/nginx/default.d/fb-page-assignment.conf'),
}


def cmd(*args):
    return subprocess.check_output(args,text=True,stderr=subprocess.STDOUT).strip()


def health(role,port):
    uri='http://127.0.0.1:'+str(port)
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if role=='api':
        with client.open(uri+'/health',timeout=4) as r:
            if json.load(r).get('ok') is not True:raise RuntimeError('portrait unhealthy')
        req=urllib.request.Request(uri+'/api/fb-page-portraits?ids=101')
    else:
        req=urllib.request.Request(uri+'/api/integrations/v1/drama-page-assignments',data=b'{}',method='POST')
    try:client.open(req,timeout=4)
    except urllib.error.HTTPError as exc:
        if exc.code==401:return True
        raise
    raise RuntimeError('anonymous probe unexpectedly admitted')


def ready(role,port):
    deadline=time.monotonic()+35
    while True:
        try:return health(role,port)
        except (OSError,RuntimeError):
            if time.monotonic()>deadline:raise
            time.sleep(0.5)


def candidates():
    disk()
    (ROOT/'pins').mkdir(exist_ok=True,mode=0o700)
    for role,(name,normal,green,conf) in ROLES.items():
        target=build(role)
        unit=Path('/etc/systemd/system/'+name+'.service')
        new_name='fb-page-current-'+role+'-green.service'
        path=Path('/etc/systemd/system')/new_name
        owners=set(re.findall(r'pid=(\d+)',cmd('ss','-Hltnp','sport','=',':'+str(green))))
        if owners and owners!={cmd('systemctl','show',new_name,'-p','MainPID','--value')}:
            raise RuntimeError('standby loopback port already in use')
        extra='ReadWritePaths='+str(ROOT/'pins')+'\n' if role=='api' else ''
        args=(f'features.fb_page_portrait.server --port {green}' if role=='api' else
              f'features.fb_page_assignment.server --env /etc/fb-page-assignment.env --port {green} '
              '--data-root /mnt/data-disk/fb-page-assignment --foreground-only')
        put(path,unit.read_text()+f'\n[Service]\nWorkingDirectory={target}\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
            f'ExecStart=\nExecStart=/usr/bin/python3 -m {args}\n'+extra)
        backup(Path(conf),role+'-nginx')
        backup(unit,role+'-canonical-unit')
        backup(Path('/etc/systemd/system/'+name+'.service.d/zz-page-current.conf'),role+'-override')
        cmd('systemctl','daemon-reload')
        active=cmd('systemctl','show',new_name,'-p','ActiveState','--value')
        if active=='active':
            pid=cmd('systemctl','show',new_name,'-p','MainPID','--value')
            if Path('/proc/'+pid+'/cwd').resolve()!=target:
                if f'127.0.0.1:{green};' in Path(conf).read_text():raise RuntimeError('green serves traffic, finish previous handoff')
                cmd('systemctl','restart',new_name)
        else:cmd('systemctl','start',new_name)
        ready(role,green)
        pid=cmd('systemctl','show',new_name,'-p','MainPID','--value')
        if pid not in re.findall(r'pid=(\d+)',cmd('ss','-Hltnp','sport','=',':'+str(green))):
            raise RuntimeError('readiness probe did not reach the standby process')
    atomic(ROOT/'green-ready.json',dict(revision=SOURCE.name,at=stamp()))


def nginx_workers():
    master=int(cmd('systemctl','show','nginx','-p','MainPID','--value'))
    children=Path(f'/proc/{master}/task/{master}/children').read_text().split()
    workers={}
    for pid in children:
        try:
            if b'nginx: worker process' in Path('/proc/'+pid+'/cmdline').read_bytes():
                workers[pid]=Path('/proc/'+pid+'/stat').read_text().split()[21]
        except FileNotFoundError:pass
    return workers


def route(green):
    saved={}
    for role,(name,normal,standby,f) in ROLES.items():
        p=Path(f);src,dst=(normal,standby) if green else (standby,normal)
        loaded=p.read_text();before='127.0.0.1:'+str(src)+';';after='127.0.0.1:'+str(dst)+';'
        if before not in loaded and after not in loaded:raise RuntimeError('unrecognized upstream '+f)
        if before in loaded:
            saved[p]=loaded
            put(p,loaded.replace(before,after))
    old=nginx_workers()
    try:
        cmd('nginx','-t');cmd('systemctl','reload','nginx')
    except Exception:
        for p,content in saved.items():put(p,content)
        cmd('nginx','-t');cmd('systemctl','reload','nginx')
        raise
    # Never terminate an accepted request. Existing workers finish on their own.
    while old:
        for pid,start in list(old.items()):
            p=Path('/proc/'+pid+'/stat')
            try:alive=p.read_text().split()[21]==start
            except FileNotFoundError:alive=False
            if not alive:del old[pid]
        if old:time.sleep(1)


def override(role,rollback=False):
    name=ROLES[role][0]
    p=Path('/etc/systemd/system/'+name+'.service.d/zz-page-current.conf')
    if rollback:
        record=json.loads((ROOT/'backup'/(role+'-override.json')).read_text())
        if record['existed']:put(p,(ROOT/'backup'/(role+'-override')).read_text())
        else:p.unlink(missing_ok=True)
    else:
        target=build(role)
        put(p,'[Service]\nWorkingDirectory='+str(target)+'\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'+
            ('ReadWritePaths='+str(ROOT/'pins')+'\n' if role=='api' else ''))


def handoff(rollback=False):
    r=json.loads((ROOT/'green-ready.json').read_text())
    if not rollback and r['revision']!=SOURCE.name:raise RuntimeError('unverified green release')
    for role,(_,_,green,_) in ROLES.items():ready(role,green)
    route(True)
    # A new HTTP owner can already accept requests. The standby cannot start an outbox worker.
    for role,(name,normal,_,_) in ROLES.items():
        override(role,rollback)
        cmd('systemctl','daemon-reload')
        cmd('systemctl','restart',name+'.service')
        ready(role,normal)
    route(False)
    for role in ROLES:cmd('systemctl','stop','fb-page-current-'+role+'-green.service')
    atomic(ROOT/('readers-rollback.json' if rollback else 'readers-live.json'),dict(revision=SOURCE.name,at=stamp()))


if __name__=='__main__':
    ps=argparse.ArgumentParser();ps.add_argument('action',choices=['candidates','handoff','rollback'])
    action=ps.parse_args().action
    if action=='candidates':candidates()
    elif action=='handoff':handoff()
    else:candidates();handoff(True)
    print(action,'complete',flush=True)
