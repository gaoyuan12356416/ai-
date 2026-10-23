"""Exact-commit, idle-only compatible probe update; never restore business state."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
OLD_GPU='1747c5cd560903032b90ce18d961bc1fe3caddb6'
HASHES={
 'features/video_editing/worker.py':'ae35b53312c51f9aedee16cc2291cbbf69567486f10492b0d73965d9aa7209cd',
 'features/video_editing/service.py':'740f600d3c2aaf71927c6ffa1856350f51034c34eb4a276b8cf4e9b26e0601dd',
 'static/video-editing.js':'c4d0f0c0ce583561b079b063ac6f84244b6695ab5917ae4218e7f89bb2189f00',
 'static/video-editing.html':'ae03f613b25705535b0ad0899ab71e3cfb6524e2f0a0beaeab12ac18ea7485da',
 'static/video-editing.css':'1f5188160a73a388b1056da4a32a825551feecddbc3d5d2f4a8074bfad56827b'}
CLAIMS=['drama-material-job-worker.service','video-editing-worker.service']
PUBLISHERS=['drama-youtube-unified-writer.service','youtube-auto-publish-worker.service']
MARKER=Path('/etc/video-editing/intake.disabled')

def run(*args,timeout=60,**kwargs):
    return subprocess.check_output(args,stderr=subprocess.PIPE,text=True,timeout=timeout,**kwargs).strip()

def prop(unit,key):return run('systemctl','show',unit,'-p',key,'--value')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def save(path,obj):path.write_text(json.dumps(obj,indent=2),encoding='utf-8')
def atomic(path,data,mode=0o644):
    assert not path.is_symlink()
    temp=path.with_name(path.name+'.probe-new');assert not temp.exists()
    with temp.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    temp.chmod(mode);os.replace(temp,path)

def idle_cpu():
    for path,sql in [('/root/drama_material_service/data/drama_material_jobs.sqlite3',"select count(*) from drama_material_job where status not in ('done','failed')"),('/mnt/data-disk/video-editing/state.sqlite3',"select count(*) from ve_task where status not in ('completed','failed','cancelled')")]:
        with sqlite3.connect('file:'+path+'?mode=ro',uri=True) as db:assert db.execute(sql).fetchone()[0]==0,'active_jobs'

def cpu(phase,rev):
    assert run('findmnt','-n','-o','UUID','--target','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free>2*1024**3
    live=Path('/root/drama_material_service');backup=Path('/mnt/data-disk/video-editing/backups')/('probe-concurrency-'+rev[:12])
    targets={live/f:f for f in HASHES}
    targets.update({Path('/usr/share/nginx/html')/Path(f).name:f for f in HASHES if f.startswith('static/')})
    if phase=='prepare':
        assert not backup.exists() and not MARKER.exists()
        for dest,src in targets.items():assert sha(dest)==HASHES[src],str(dest)
        idle_cpu();backup.mkdir(mode=0o700)
        control={'claims':{u:prop(u,'ActiveState') for u in CLAIMS},'publishers':{u:prop(u,'MainPID') for u in PUBLISHERS},'api_pid':prop('drama-material-api.service','MainPID')}
        save(backup/'control.json',control)
        MARKER.parent.mkdir(exist_ok=True);MARKER.parent.chmod(0o755);atomic(MARKER,b'probe concurrency deployment\n')
        idle_cpu()
        for unit in CLAIMS:run('systemctl','stop',unit);assert prop(unit,'MainPID')=='0'
    else:
        control=json.loads((backup/'control.json').read_text())
        assert MARKER.read_bytes()==b'probe concurrency deployment\n'
        for u,pid in control['publishers'].items():assert prop(u,'MainPID')==pid
        if phase=='apply':
            idle_cpu();assert all(prop(u,'MainPID')=='0' for u in CLAIMS)
            rows=[]
            for n,(dest,src) in enumerate(targets.items()):
                assert sha(dest)==HASHES[src]
                old=backup/('file-'+str(n));shutil.copy2(dest,old)
                rows.append({'path':str(dest),'source':src,'backup':str(old),'old_sha':sha(old),'new_sha':sha(ROOT/src)})
            save(backup/'files.json',rows)
            with sqlite3.connect('file:/mnt/data-disk/video-editing/state.sqlite3?mode=ro',uri=True) as db,sqlite3.connect(str(backup/'evidence.sqlite3')) as out:db.backup(out)
            run('systemctl','stop','drama-material-api.service')
            for row in rows:atomic(Path(row['path']),(ROOT/row['source']).read_bytes())
            run('python3','-m','py_compile',str(live/'features/video_editing/service.py'),str(live/'features/video_editing/worker.py'))
            run('systemctl','start','drama-material-api.service')
            assert prop('drama-material-api.service','ActiveState')=='active'
            for row in rows:assert sha(Path(row['path']))==row['new_sha']
        elif phase=='restore':
            for u,state in control['claims'].items():
                if state=='active':run('systemctl','start',u)
                assert prop(u,'ActiveState')==state
            MARKER.unlink()
        else:raise ValueError('phase')
    proof={'ok':True,'host':'cpu','phase':phase,'release':rev,'backup':str(backup)}
    save(backup/(phase+'.json'),proof);return proof

def gpu(rev):
    import pwd
    base=Path('/data/drama-synthesis-gpu');assert ROOT==base/'releases'/rev
    assert (base/'current').resolve()==base/'releases'/OLD_GPU
    changed=run('git','-C',str(ROOT),'diff','--name-only',OLD_GPU,rev).splitlines()
    allowed={'features/drama_synthesis/async_runtime.py','features/video_editing/gpu_integration.py','scripts/drama_synthesis_gpu_worker.py','scripts/deploy_video_editing_probe.py','tests/test_video_editing_runtime.py','tests/test_video_editing_probe_runtime.py','doc/20261023.video-editing-probe.md'}
    assert set(changed)<=allowed
    spec=importlib.util.spec_from_file_location('guard',ROOT/'scripts/deploy_video_editing_gpu.py');d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
    env=d.effective_env();health=d.health();d.check_health(health,OLD_GPU);assert not MARKER.exists()
    before=d.wait_for_idle(env['DRAMA_WORK_ROOT'],15);d.wait_for_media_idle(env,15)
    identity=pwd.getpwnam('drama-synthesis-gpu')
    candidate_env=dict(env,DRAMA_GPU_RELEASE_SHA=rev,PYTHONDONTWRITEBYTECODE='1',PYTHONNOUSERSITE='1')
    preflight=d.run(str(base/'runtime/current/bin/python'),str(ROOT/'scripts/check_drama_synthesis_gpu_runtime.py'),'--check-app-import',env=candidate_env,cwd=ROOT,user=identity.pw_uid,group=identity.pw_gid,timeout=300)
    assert json.loads(preflight)['ok']
    backup=base/'deploy-backups'/('probe-concurrency-'+rev[:12]);d.backup_configuration(backup)
    save(backup/'before.json',{'release':OLD_GPU,'ledger':before,'health':health})
    atomic(MARKER,b'probe concurrency deployment\n')
    d.wait_for_idle(env['DRAMA_WORK_ROOT'],0);d.wait_for_media_idle(env,3)
    d.run('systemctl','stop','--no-block',d.WORKER);d.wait_worker_stopped()
    assert d.ledger_snapshot(env['DRAMA_WORK_ROOT'])['hashes']==before['hashes']
    d.atomic(d.ENV_FILE,d.merge_environment(d.ENV_FILE.read_bytes(),{'DRAMA_GPU_RELEASE_SHA':rev,'VIDEO_EDITING_PROBE_CONCURRENCY':'3'}))
    d.switch_link(ROOT);d.run('systemctl','start',d.WORKER,timeout=360);d.run('systemctl','start',d.TUNNEL)
    new=d.wait_for_health(rev,health);assert new['video_editing_probe_concurrency']==3 and new['render_concurrency']==1
    assert d.ledger_snapshot(env['DRAMA_WORK_ROOT'])['hashes']==before['hashes']
    assert d.property_value(d.TUNNEL,'ActiveState')=='active'
    MARKER.unlink();assert d.health()['video_editing_enabled']
    proof={'ok':True,'host':'gpu','release':rev,'backup':str(backup),'probe_concurrency':3,'render_concurrency':1,'ledger_unchanged':True,'ledger_records':len(before['hashes'])}
    save(backup/'result.json',proof);return proof

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--host',choices=['cpu','gpu'],required=True);parser.add_argument('--phase',choices=['prepare','apply','restore'],default='apply');args=parser.parse_args()
    rev=run('git','-C',str(ROOT),'rev-parse','HEAD')
    assert not run('git','-C',str(ROOT),'status','--porcelain','--untracked-files=all')
    sys.path.insert(0,str(ROOT))
    print(json.dumps(cpu(args.phase,rev) if args.host=='cpu' else gpu(rev)))
