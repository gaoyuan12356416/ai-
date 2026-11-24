"""Install only the affected consumers, retaining the exact previous runtime directories."""
import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


SRC=Path(__file__).resolve().parents[2]
ROOT=Path('/mnt/data-disk/fb-portrait-consumers-20261124')
DEP=ROOT/'deployment'
META='meta-page-recovery-worker.service'
WRITER='fb-page-portrait-refresh.service'
LINKS={k:Path('/opt')/folder/'current' for k,folder in
       [('fb','fb-auto-post'),('meta','meta-page-recovery'),('catalog','drama-catalog')]}
DROP=Path('/etc/systemd/system')/(WRITER+'.d')/'zzzz-portrait-consumers.conf'
CONTROLLER=Path('/mnt/data-disk/backend-read-performance-20261123/fb-source/scripts/deploy_fb_page_pagination.py')
HELPERS=Path('/mnt/data-disk/fb-shared-manual-pages-20261120/source')
FILES={
 'writer':{'features/fb_page_current/selection.py':'6caf51bc38c204e025afe40ac68527d03482a6a4',
           'features/fb_page_portrait/refresh.py':'e538c84bf1d425985695efcc14137b3c70470734','features/fb_page_current/prepared.py':None},
 'fb':{'features/fb_auto_posts/profile_selection.py':'8ecb60c1458f3b7f44c17bb8252eaaf700ab27d6','features/fb_portrait_matching.py':None},
 'meta':{'features/meta_page_recovery/__main__.py':'ef323c1d89ba84c8e52212e075f11329fbc7f1ed','features/fb_portrait_matching.py':None},
 'catalog':{'features/drama_audience/sync.py':'147cb3490d8e42fa34f37b0b2fe3a757da3ffd7f','features/drama_audience/reproject.py':'2d66fb0818cad43f02a9c8ce19124f66b4df65fe',
            'scripts/reproject_drama_audiences.py':'503b54913a010fa993e535cdbb6d73562330b49b','features/drama_audience/fb_preview.py':None}}


def run(*args, **kwargs):
    return subprocess.check_output([str(v) for v in args],text=True,**kwargs).strip()


def prop(unit, key):
    return run('systemctl','show',unit,'-p',key,'--value')


def save(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        f.write((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name,path)


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ''


def load():
    return json.loads((DEP/'manifest.json').read_text())


def candidate(lane):
    return DEP/lane/'candidate'


def check_code(base,lane):
    for rel,blob in FILES[lane].items():
        path=base/rel
        if blob is None:
            if path.exists():
                raise RuntimeError('unexpected existing module '+str(path))
        else:
            if not path.is_file() or run('git','hash-object',path)!=blob:
                raise RuntimeError('live source changed '+str(path))


def prepare():
    if run('findmnt','-nro','UUID','--mountpoint','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data disk missing')
    if (DEP/'manifest.json').exists():
        raise RuntimeError('deployment already prepared')
    git=run('git','-C',SRC,'rev-parse','HEAD')
    if run('git','-C',SRC,'status','--porcelain') or not run('git','-C',SRC,'branch','-r','--contains',git):
        raise RuntimeError('clean pushed checkout required')
    previous={k:str(v.resolve()) for k,v in LINKS.items()}
    previous['writer']=prop(WRITER,'WorkingDirectory')
    for lane,root in previous.items():
        check_code(Path(root),lane)
    if DROP.exists():
        raise RuntimeError('writer override already present')
    DEP.mkdir(parents=True,exist_ok=True)
    DEP.chmod(0o755)
    sys.path.insert(0,str(HELPERS))
    from scripts.deploy_mobo_upcoming import runtime_image
    from scripts.deploy_pipeline_runtime import preserve_tree_owners
    for lane in FILES:
        dest=candidate(lane)
        prev=Path(previous[lane])
        dest.parent.mkdir(exist_ok=True)
        dest.parent.chmod(0o755)
        shutil.copytree(prev,dest,symlinks=True,ignore=shutil.ignore_patterns('.git','__pycache__','*.pyc'))
        preserve_tree_owners(prev,dest)
        for rel in FILES[lane]:
            compile((SRC/rel).read_bytes(),rel,'exec')
            shutil.copyfile(SRC/rel,dest/rel)
            (dest/rel).chmod((prev/rel).stat().st_mode & 0o777 if (prev/rel).exists() else 0o644)
    folder=DEP/'fb'
    prev,dest=Path(previous['fb']),candidate('fb')
    earlier=json.loads((CONTROLLER.parents[2]/'fb-release/manifest.json').read_text())
    helpers=earlier['helpers']
    if any(digest(Path(k))!=v for k,v in helpers.items()):
        raise RuntimeError('FB switch helpers changed')
    save(folder/'manifest.json',dict(platform='fb',commit=git,source=str(SRC),previous=str(prev),candidate=str(dest),
         previous_image=runtime_image(prev),candidate_image=runtime_image(dest),files={rel:dict(before=digest(prev/rel),after=digest(dest/rel)) for rel in FILES['fb']},
         static={},status='prepared',helpers=helpers,main_api_pid=prop('drama-material-api.service','MainPID')))
    save(DEP/'manifest.json',dict(commit=git,previous=previous,controller_sha256=digest(CONTROLLER)))
    print('prepared',git,flush=True)


def swap(link, old, new):
    if link.resolve()==new:
        return
    if not link.is_symlink() or link.resolve()!=old:
        raise RuntimeError('concurrent release at '+str(link))
    next_link=link.with_name(link.name+'.portrait-consumers-next')
    next_link.symlink_to(new)
    os.replace(next_link,link)


def change(lane,rollback=False):
    info=load()
    old=Path(info['previous'][lane]);new=candidate(lane)
    for rel in FILES[lane]:
        if digest(new/rel)!=digest(SRC/rel):
            raise RuntimeError('candidate changed: '+rel)
    previous,destination=(new,old) if rollback else (old,new)
    if lane=='writer':
        content=('[Service]\nWorkingDirectory='+str(new)+'\n').encode()
        current=prop(WRITER,'WorkingDirectory')
        if current not in (str(previous),str(destination)):
            raise RuntimeError('writer moved')
        if rollback:
            if DROP.exists() and DROP.read_bytes()!=content:
                raise RuntimeError('writer override changed')
            DROP.unlink(missing_ok=True)
        else:
            share=Path('/mnt/data-disk/fb-page-portraits/matching')
            share.mkdir(exist_ok=True,mode=0o750)
            os.chown(share,0,grp.getgrnam('fb-auto-post').gr_gid)
            share.chmod(0o750)
            if DROP.exists() and DROP.read_bytes()!=content:
                raise RuntimeError('writer override changed')
            DROP.write_bytes(content)
        run('systemctl','daemon-reload')
        if prop(WRITER,'WorkingDirectory')!=str(destination):
            raise RuntimeError('writer unit not loaded')
    elif lane=='fb':
        if digest(CONTROLLER)!=info['controller_sha256']:
            raise RuntimeError('FB controller changed')
        if LINKS['fb'].resolve()!=destination:
            subprocess.run([sys.executable,'-B',str(CONTROLLER),'rollback' if rollback else 'apply','--folder',str(DEP/'fb')],check=True)
        if not (DEP/'fb/schedulers-after.json').is_file() or not json.loads((DEP/'fb/schedulers-after.json').read_text())['ok']:
            raise RuntimeError('FB scheduler handoff incomplete')
    else:
        if LINKS[lane].resolve()==previous and not rollback:
            check_code(previous,lane)
        swap(LINKS[lane],previous,destination)
        if lane=='meta':
            def working():
                return Path('/proc',prop(META,'MainPID'),'cwd').resolve()
            if working()!=destination:
                # SIGTERM is handled by the worker, which finishes its current job.
                run('systemctl','restart','--no-block',META)
                while working()!=destination or prop(META,'ActiveState')!='active':
                    if prop(META,'ActiveState')=='failed':
                        raise RuntimeError('Meta recovery worker failed to start')
                    time.sleep(1)
    print(lane,'rolled back' if rollback else 'active',destination,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['prepare','apply','rollback'])
    p.add_argument('lane',choices=tuple(FILES)+('all',),nargs='?',default='all')
    args=p.parse_args()
    if args.action=='prepare':
        prepare()
    else:
        rollback=args.action=='rollback'
        order=['writer','catalog','meta','fb']
        lanes=(list(reversed(order)) if rollback else order) if args.lane=='all' else [args.lane]
        for lane in lanes:
            change(lane,rollback)
