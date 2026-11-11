"""Exact-preimage upgrade of the isolated read service and two public assets."""
import argparse
from datetime import datetime,timezone
import hashlib,json
from pathlib import Path
import shutil,subprocess,time,urllib.request

BASE='6dec73f5db5520d12ffbf807419bb2f18a101893'
SERVICE='fb-page-portrait.service'
RUNTIME=Path('/mnt/data-disk/fb-page-managed-profile-20261111/deployment/candidate')
PROTECTED=('fb-auto-post-service.service','drama-material-api.service')

def run(*args):return subprocess.check_output(args,text=True).strip()
def digest(data):return hashlib.sha256(data).hexdigest()
def pids():return {s:run('systemctl','show',s,'-p','MainPID','--value') for s in PROTECTED}
def ready():
    for _ in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18852/health',timeout=2) as response:
                if json.load(response)['ok']:return
        except OSError:time.sleep(.25)
    raise RuntimeError('read_service_not_ready')

def main():
    p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--commit');p.add_argument('--deployment',required=True);p.add_argument('--rollback',action='store_true');a=p.parse_args()
    deployment=Path(a.deployment).resolve()
    assert deployment.parent==Path('/mnt/data-disk/fb-page-managed-profile-20261111')
    assert run('findmnt','-no','UUID','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    before=pids()
    if a.rollback:
        m=json.loads((deployment/'manifest.json').read_text())
        for row in m['files']:
            target=Path(row['target'])
            if row['before'] is None:target.unlink(missing_ok=True)
            else:shutil.copy2(deployment/row['before'],target)
        run('systemctl','restart',SERVICE);ready()
        assert pids()==before
        print('Scoped code rollback complete; configuration, income and publication data preserved.');return
    source=Path(a.source).resolve()
    assert run('git','-C',str(source),'rev-parse','HEAD')==a.commit
    assert not run('git','-C',str(source),'status','--porcelain')
    assert Path('/opt/fb-page-portraits/current').resolve()==RUNTIME
    assert not deployment.exists()
    entries=[]
    for name in ('page_list.py','server.py','refresh.py'):
        relative='features/fb_page_portrait/'+name
        entries.append((relative,RUNTIME/relative,name=='page_list.py'))
    for root in (Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html')):
        for name in ('fb-video-publish.js','fb-pages.html'):entries.append(('static/'+name,root/name,False))
    for relative,target,new in entries:
        if new:assert not target.exists()
        else:
            expected=subprocess.check_output(['git','-C',str(source),'show',BASE+':'+relative])
            assert digest(target.read_bytes())==digest(expected),'live_file_drift:'+str(target)
    deployment.mkdir()
    manifest=dict(commit=a.commit,at=datetime.now(timezone.utc).isoformat(),protected_pids=before,files=[])
    for relative,target,new in entries:
        backup=None if new else 'backup/'+str(target).lstrip('/')
        if backup:
            (deployment/backup).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,deployment/backup)
        manifest['files'].append(dict(target=str(target),before=backup,source=relative,after_sha256=digest((source/relative).read_bytes())))
    (deployment/'manifest.json').write_text(json.dumps(manifest,indent=2))
    # Runtime first, public entry point last. No publisher/main API restart.
    for relative,target,new in entries[:3]:shutil.copy2(source/relative,target)
    run('systemctl','restart',SERVICE);ready()
    for relative,target,new in entries[3:]:shutil.copy2(source/relative,target)
    assert pids()==before
    manifest['after_pids']=pids();manifest['service']=run('systemctl','is-active',SERVICE)
    (deployment/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps(dict(commit=a.commit,deployment=str(deployment),service=manifest['service'],protected_pids=before)))

if __name__=='__main__':main()
