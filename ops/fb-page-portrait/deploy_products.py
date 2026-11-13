"""Upgrade only the inactive collector; preserve serving/publishing processes."""
import argparse
from datetime import datetime,timezone
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

EXPECTED={'collect.py':'28b74438aae233281e9e6583408a9783f8e5e29cf88a1d76dfd79314b6498661',
          'build.py':'6c266690021af8474066c159e4916746360b9e9ccb3a04ac8435eebded73a5b3',
          'classification.py':None}
PREFIX='features/fb_page_portrait/'
ROOT=Path('/mnt/data-disk/fb-page-portraits')
SERVICES=('fb-page-portrait.service','fb-auto-post-service.service','drama-material-api.service')

def run(*args):return subprocess.check_output(args,text=True).strip()
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None
def pids():return {s:run('systemctl','show',s,'-p','MainPID','--value') for s in SERVICES}

def main():
    p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--commit');p.add_argument('--deployment',required=True)
    p.add_argument('--rollback',action='store_true');a=p.parse_args()
    folder=Path(a.deployment).resolve()
    assert folder.is_relative_to(Path('/mnt/data-disk/fb-page-portrait-products-20261113'))
    assert run('findmnt','-n','-o','UUID','--target','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    runtime=Path('/opt/fb-page-portraits/current').resolve()
    before=pids()
    with (ROOT/'refresh.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.rollback:
            manifest=json.loads((folder/'manifest.json').read_text())
            assert str(runtime)==manifest['runtime']
            for name,item in manifest['files'].items():
                assert sha(runtime/PREFIX/name)==item['after'],'live_drift:'+name
            for name,item in manifest['files'].items():
                target=runtime/PREFIX/name
                if item['before'] is None:target.unlink()
                else:shutil.copy2(folder/'before'/name,target)
            assert pids()==before
            print(json.dumps({'rollback':'complete','preserved_pids':before,'profiles':'preserved'}));return
        source=Path(a.source).resolve()
        assert run('git','-C',str(source),'rev-parse','HEAD')==a.commit
        assert not run('git','-C',str(source),'status','--porcelain')
        for name,expected in EXPECTED.items():assert sha(runtime/PREFIX/name)==expected,'live_drift:'+name
        assert not folder.exists();(folder/'before').mkdir(parents=True)
        manifest={'commit':a.commit,'runtime':str(runtime),'at':datetime.now(timezone.utc).isoformat(),'preserved_pids':before,'files':{}}
        for name,expected in EXPECTED.items():
            target=runtime/PREFIX/name
            if expected is not None:shutil.copy2(target,folder/'before'/name)
            manifest['files'][name]={'before':expected,'after':sha(source/PREFIX/name)}
        (folder/'manifest.json').write_text(json.dumps(manifest,indent=2))
        for name in ('classification.py','build.py','collect.py'):
            target=runtime/PREFIX/name;temporary=target.with_suffix('.next')
            shutil.copy2(source/PREFIX/name,temporary);temporary.replace(target)
        assert all(sha(runtime/PREFIX/name)==item['after'] for name,item in manifest['files'].items())
        assert pids()==before
        print(json.dumps(manifest))

if __name__=='__main__':main()
