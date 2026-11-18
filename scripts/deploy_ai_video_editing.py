"""Install or roll back only the independent AI editor on the CPU host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

SOURCE=Path(__file__).resolve().parents[1]
APP=Path('/opt/ai-video-editing')
OPS=Path('/mnt/data-disk/ai-video-editing-deploy')
STATIC=[Path('/usr/share/nginx/html'),Path('/root/drama_material_service/static')]
UNIT=Path('/etc/systemd/system/ai-video-editing.service')
NGINX=Path('/etc/nginx/default.d/ai-video-editing.conf')


def run(*args):
    return subprocess.check_output(args,cwd=SOURCE,stderr=subprocess.STDOUT)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def install_file(path,data,mode=0o644):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        tmp=Path(f.name)
        try:
            f.write(data);f.flush()
            os.fchmod(f.fileno(),mode)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp,path)


def link_current(target):
    tmp=APP/'next'
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target,target_is_directory=True)
    os.replace(tmp,APP/'current')


def nav_json(path):
    import sys
    sys.path.insert(0,str(SOURCE))
    from features.video_editing.navigation import ensure_navigation
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    updated=ensure_navigation(data)
    if updated==data:return path.read_bytes()
    return (json.dumps(updated,ensure_ascii=False,indent=2)+'\n').encode()


def nav_script(path,commit):
    patch=run('git','diff','--unified=1','d7dc3c6052d9490c2102956fb75e3343808f8211',commit,'--','static/quick-nav.js')
    if b'key: "videoEditingAi"' in path.read_bytes():return path.read_bytes()
    with tempfile.TemporaryDirectory() as temp:
        target=Path(temp)/'static/quick-nav.js'
        target.parent.mkdir()
        target.write_bytes(path.read_bytes())
        subprocess.run(['git','apply','--unidiff-zero','--whitespace=nowarn','-'],
                       cwd=temp,input=patch,check=True)
        run('node','--check',str(target))
        return target.read_bytes()


def activate():
    run('nginx','-t')
    run('systemctl','daemon-reload')
    run('systemctl','enable','ai-video-editing.service')
    run('systemctl','restart','ai-video-editing.service')
    run('systemctl','reload','nginx')


def install(commit):
    if run('git','rev-parse','HEAD').decode().strip()!=commit:raise ValueError('checkout differs')
    files={UNIT:(SOURCE/'deploy'/UNIT.name).read_bytes(),NGINX:(SOURCE/'deploy/ai-video-editing-nginx.conf').read_bytes()}
    for base in STATIC:
        for src in sorted((SOURCE/'static').glob('video-editing-ai*')):
            files[base/src.name]=src.read_bytes()
        files[base/'quick-nav.js']=nav_script(base/'quick-nav.js',commit)
        files[base/'navigation.json']=nav_json(base/'navigation.json')
    release=APP/'releases'/commit
    release.mkdir(parents=True,exist_ok=True)
    shutil.copytree(SOURCE/'features/ai_video_editing',release/'ai_video_editing',dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    run('/usr/bin/python3','-m','compileall','-q',str(release))
    previous=APP/'current'
    backup=OPS/'backups'/commit
    backup.mkdir(parents=True,exist_ok=True)
    mf=backup/'manifest.json'
    if mf.exists():raise ValueError('backup already exists; inspect installation before trying again')
    manifest={'previous':str(previous.resolve()) if previous.exists() else None,'files':[]}
    for i,(path,data) in enumerate(files.items()):
        prior=path.read_bytes() if path.is_file() else None
        if prior==data:continue
        old=str(i) if prior is not None else None
        if old:(backup/old).write_bytes(prior)
        manifest['files'].append({'path':str(path),'prior':old,'before':sha(prior) if prior is not None else None,'after':sha(data)})
    mf.write_text(json.dumps(manifest,indent=2))
    for row in manifest['files']:
        p=Path(row['path'])
        if (sha(p.read_bytes()) if p.exists() else None)!=row['before']:raise ValueError('concurrent update: '+str(p))
        install_file(p,files[p])
    link_current(release)
    activate()
    print('activated',commit,'backup',mf)


def rollback(commit):
    backup=OPS/'backups'/commit
    manifest=json.loads((backup/'manifest.json').read_text())
    current=APP/'current'
    if current.exists() and current.resolve()!=APP/'releases'/commit:raise ValueError('different release active')
    for r in manifest['files']:
        p=Path(r['path'])
        if (sha(p.read_bytes()) if p.exists() else None) not in (r['after'],r['before']):raise ValueError('concurrent change: '+str(p))
    if not manifest['previous']:
        subprocess.run(['systemctl','disable','--now','ai-video-editing.service'],check=False)
    for r in manifest['files']:
        p=Path(r['path'])
        if r['prior'] is None:p.unlink(missing_ok=True)
        else:install_file(p,(backup/r['prior']).read_bytes())
    if manifest['previous']:
        link_current(manifest['previous']);activate()
    else:
        current.unlink(missing_ok=True)
        run('systemctl','daemon-reload');run('nginx','-t');run('systemctl','reload','nginx')
    print('rolled back',commit)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('install','rollback'))
    parser.add_argument('commit')
    args=parser.parse_args()
    (install if args.action=='install' else rollback)(args.commit)
