"""Exact-GitHub release operations. No social publication or ledger mutation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import time
import urllib.request

P = argparse.ArgumentParser()
P.add_argument('action', choices=['stage-hk','stage-cpu','gate','switch-hk','switch-cpu','resume','audit'])
P.add_argument('releases', help='JSON mapping drama/tt/fb to exact pushed 40-character SHA')
A = P.parse_args()
RELEASES = json.loads(Path(A.releases).read_text())
assert set(RELEASES) == {'drama','tt','fb'}
assert all(re.fullmatch('[0-9a-f]{40}', s) for s in RELEASES.values())
ORIGIN = 'https://github.com/gaoyuan12356416/ai-.git'
OP = '20260920-source-overlay'
CPU = Path('/mnt/data-disk/random-overlay-gpu/backups') / OP
HK = Path('/data/random-overlay-gpu/backups') / OP
STAGE = Path('/mnt/data-disk/random-overlay-gpu/releases') / RELEASES['drama']
CONFIG = {
    'drama': ('drama-synthesis-gpu-worker.service','/data/drama-synthesis-gpu/current','/data/drama-synthesis-gpu/releases'),
    'tt': ('tt-gpu-publisher.service','/data/tt-post-gpu/random-current','/data/tt-post-gpu/releases'),
    'fb': ('fb-page-random-overlay-gpu.service','/opt/fb-page-random-overlay/current','/data/random-overlay-gpu/releases'),
}
CATALOG = 'b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
CPU_FILES = ['features/drama_synthesis/core.py','static/index.html']
BASE_SHA = {
    'features/drama_synthesis/core.py':'e0a95e00f1cf38ddd70209737d7d0ad44e520a7bbb35a312f7444f15032f156f',
    'static/index.html':'bcd901b3fe41929607d5d6ed018be6b4fa0b2e496d7b93e586e8a4b6e0b4cf9b',
}

def run(args, timeout=120):
    return subprocess.check_output(args, timeout=timeout).decode().strip()

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def state(unit):
    return run(['systemctl','show',unit,'-p','ActiveState,SubState,MainPID,NRestarts'])

def env(unit):
    pid=run(['systemctl','show',unit,'-p','MainPID','--value'])
    assert int(pid)>1
    return dict(x.decode().split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)

def write(path, value, mode=0o600):
    tmp=path.with_name(path.name+'.source-overlay-new')
    assert not tmp.exists()
    with tmp.open('w') as f:
        f.write(value);f.flush();os.fsync(f.fileno())
    tmp.chmod(mode);os.replace(str(tmp),str(path))

def save(path, value):
    write(path,json.dumps(value,indent=2)+'\n')

def cpu_disk():
    assert run(['findmnt','-n','-o','UUID','--target','/mnt/data-disk'])=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert run(['findmnt','-n','-o','TARGET','--target','/mnt/data-disk'])=='/mnt/data-disk'

def checkout(path, commit):
    old_umask=os.umask(0o022)
    if not path.exists():
        path.mkdir(parents=True)
        run(['git','init',str(path)])
        run(['git','-C',str(path),'remote','add','origin',ORIGIN])
    assert run(['git','-C',str(path),'remote','get-url','origin'])==ORIGIN
    assert not run(['git','-C',str(path),'status','--porcelain'])
    run(['git','-C',str(path),'fetch','--depth','1','origin',commit],600)
    run(['git','-C',str(path),'checkout','--detach',commit])
    assert run(['git','-C',str(path),'rev-parse','HEAD'])==commit
    path.chmod(0o755)
    os.umask(old_umask)

def health(cpu=False):
    out={}
    for lane,port,path in [('drama',18788 if cpu else 8787,'/healthz'),('tt',18830 if cpu else 8830,'/health'),('fb',18836 if cpu else 8836,'/health')]:
        deadline=time.monotonic()+60
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d%s'%(port,path),timeout=10) as r:out[lane]=json.load(r)
                break
            except Exception:
                if time.monotonic()>=deadline:raise
                time.sleep(1)
        assert out[lane]['source_overlay_version']==1, lane
    assert out['drama']['release_sha']==RELEASES['drama']
    return out

if A.action=='stage-hk':
    assert Path('/data').is_dir() and shutil.disk_usage('/data').free>20*1024**3
    for lane,(_,_,base) in CONFIG.items():checkout(Path(base)/RELEASES[lane],RELEASES[lane])
    print(json.dumps({'staged':RELEASES}))
elif A.action=='stage-cpu':
    cpu_disk();checkout(STAGE,RELEASES['drama'])
    api_pid=run(['systemctl','show','drama-material-api.service','-p','MainPID','--value'])
    python=os.readlink('/proc/'+api_pid+'/exe')
    run([python,'-c',"import ast;from pathlib import Path;ast.parse(Path("+repr(str(STAGE/'features/drama_synthesis/core.py'))+").read_text())"])

    assert not CPU.exists();CPU.mkdir(mode=0o700,parents=True);CPU.chmod(0o700)
    entries={}
    for name in CPU_FILES:
        src=Path('/root/drama_material_service')/name
        assert sha(src)==BASE_SHA[name],name+' baseline drift'
        dest=CPU/'files'/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dest)
        entries[str(src)]=sha(src)
    public=Path('/usr/share/nginx/html/index.html')
    assert sha(public)==BASE_SHA['static/index.html'],'public baseline drift'
    shutil.copy2(public,CPU/'public-index.html');entries[str(public)]=sha(public)
    save(CPU/'state-before.json',{'files':entries,'services':{u:state(u) for u in ['drama-material-api.service','drama-material-job-worker.service']},'releases':RELEASES})
    print(json.dumps({'cpu_backup':str(CPU),'staged':str(STAGE)}))
elif A.action=='gate':
    cpu_disk();assert (CPU/'state-before.json').exists()
    maintenance='/mnt/data-disk/random-overlay-gpu/backups/20260908-cache/maintenance.py'
    for action in ['gate-on','pause']:run(['python3',maintenance,action,'tt','--apply'])
    timer='fb-auto-post-prepare.timer';before=run(['systemctl','show',timer,'-p','ActiveState','--value'])
    assert not (CPU/'fb-prepare-timer.json').exists();save(CPU/'fb-prepare-timer.json',{'active_state':before})
    if before=='active':run(['systemctl','stop',timer])
    run(['python3',str(STAGE/'doc/122.random-source-overlay/operations/pause_fb_claims.py')])
    # Existing worker handles SIGTERM by finishing its current job before exit.
    run(['systemctl','stop','--no-block','drama-material-job-worker.service'])
    print(json.dumps({'gated':True,'drama_worker':state('drama-material-job-worker.service')}))
elif A.action=='switch-hk':
    qa=Path('/data/drama-synthesis-gpu/work/source-overlay-qa-20260920/report.json')
    report=json.loads(qa.read_text());assert report['ok'] and report['releases']==RELEASES
    pointers={}
    for lane,(unit,pointer,base) in CONFIG.items():
        pid=run(['systemctl','show',unit,'-p','MainPID','--value']);cg=run(['systemctl','show',unit,'-p','ControlGroup','--value'])
        assert set((Path('/sys/fs/cgroup/systemd')/cg.lstrip('/')/'cgroup.procs').read_text().split())=={pid},unit+' busy'
        assert run(['git','-C',base+'/'+RELEASES[lane],'rev-parse','HEAD'])==RELEASES[lane]
        pointers[pointer]=str(Path(pointer).resolve())
    for port in [8787,8830,8836]:assert not run(['ss','-Htn','state','established','( sport = :%d )'%port]),'HTTP busy'
    for p in Path('/data/drama-synthesis-gpu/work/jobs/.runtime/jobs').glob('*.json'):
        assert json.loads(p.read_text())['status'] not in ['queued','running'],'Drama queue busy'
    assert not HK.exists();HK.mkdir(mode=0o700,parents=True);HK.chmod(0o700)
    save(HK/'state-before.json',{'pointers':pointers,'releases':RELEASES,'services':{u:state(u) for u,_,_ in CONFIG.values()}})
    for lane,(unit,_,_) in CONFIG.items():
        src=Path('/etc/systemd/system')/unit
        if src.exists():shutil.copy2(src,HK/unit)
        if Path(str(src)+'.d').exists():shutil.copytree(str(src)+'.d',str(HK/(unit+'.d')))
    dropin=Path('/etc/systemd/system/drama-synthesis-gpu-worker.service.d/99-zzz-source-overlay.conf')
    assert not dropin.exists()
    for unit,_,_ in CONFIG.values():run(['systemctl','stop',unit],120)
    for lane,(_,pointer,base) in CONFIG.items():
        tmp=Path(pointer+'.source-overlay-new');assert not tmp.exists();tmp.symlink_to(base+'/'+RELEASES[lane]);os.replace(str(tmp),pointer)
    # EnvironmentFile overrides Environment= regardless of drop-in ordering.
    # Append a final file so the previous catalog file cannot restore the old SHA.
    release_env=Path('/etc/random-overlay-subtemplates/drama-source-overlay-20260920.env')
    assert not release_env.exists()
    write(release_env,'DRAMA_GPU_RELEASE_SHA='+RELEASES['drama']+'\n')
    write(dropin,'[Service]\nEnvironmentFile='+str(release_env)+'\n',0o644)
    run(['systemctl','daemon-reload'])
    for unit,_,_ in CONFIG.values():run(['systemctl','start',unit],600)
    for unit in ['drama-synthesis-gpu-tunnel.service','tt-gpu-reverse-tunnel.service','fb-page-random-overlay-tunnel.service']:run(['systemctl','restart',unit])
    result=health()
    identities={}
    for lane,(unit,pointer,base) in CONFIG.items():
        pid=run(['systemctl','show',unit,'-p','MainPID','--value'])
        expected=str(Path(base)/RELEASES[lane])
        assert str(Path(pointer).resolve())==expected
        assert os.readlink('/proc/'+pid+'/cwd')==expected,lane+' running wrong cwd'
        assert run(['git','-C',expected,'rev-parse','HEAD'])==RELEASES[lane]
        prefix={'drama':'DRAMA','tt':'TT_POST_GPU','fb':'FB_PAGE_GPU'}[lane]
        assert env(unit)[prefix+'_RANDOM_OVERLAY_MANIFEST_SHA256']==CATALOG
        identities[lane]={'pid':pid,'cwd':expected,'sha':RELEASES[lane],'catalog':CATALOG}
    result['process_identities']=identities
    save(HK/'switch-after.json',result);print(json.dumps(result))
elif A.action=='switch-cpu':
    cpu_disk();health(True)
    assert run(['systemctl','show','drama-material-job-worker.service','-p','MainPID','--value'])=='0'
    before=json.loads((CPU/'state-before.json').read_text())
    for path,digest in before['files'].items():assert sha(path)==digest,'CPU drift '+path
    for name in CPU_FILES:
        src=STAGE/name;target=Path('/root/drama_material_service')/name
        write(target,src.read_text(),target.stat().st_mode&0o777)
    target=Path('/usr/share/nginx/html/index.html');write(target,(STAGE/'static/index.html').read_text(),target.stat().st_mode&0o777)
    run(['/usr/bin/python3','-m','py_compile','/root/drama_material_service/features/drama_synthesis/core.py'])
    for unit in ['drama-material-api.service','drama-material-job-worker.service']:run(['systemctl','restart',unit],120)
    save(CPU/'switch-after.json',{'files':{p:sha(p) for p in before['files']},'services':{u:state(u) for u in ['drama-material-api.service','drama-material-job-worker.service']}})
    print(json.dumps({'cpu_switched':True,'release':RELEASES['drama']}))
elif A.action=='resume':
    cpu_disk();health(True)
    pause=json.loads((CPU/'fb-claim-pause.json').read_text());pid=pause['pid']
    if pause.get('stop_requested'):
        proc=Path('/proc/%d/stat'%pid)
        if proc.exists():
            assert proc.read_text().rsplit(')',1)[1].split()[19]==pause['start_ticks']
            assert int(run(['systemctl','show',pause['unit'],'-p','MainPID','--value']))==pid
            os.kill(pid,signal.SIGCONT);pause['resumed']=True
        else:pause['resumed']='process_already_exited'
    save(CPU/'fb-claim-resume.json',pause)
    maintenance='/mnt/data-disk/random-overlay-gpu/backups/20260908-cache/maintenance.py'
    for action in ['resume','gate-off']:run(['python3',maintenance,action,'tt','--apply'])
    if json.loads((CPU/'fb-prepare-timer.json').read_text())['active_state']=='active':run(['systemctl','start','fb-auto-post-prepare.timer'])
    save(CPU/'resumed.json',{'ok':True,'fb_timer':state('fb-auto-post-prepare.timer')});print('resume complete')
elif A.action=='audit':
    print(json.dumps(health(Path('/mnt/data-disk').is_dir()),indent=2))
