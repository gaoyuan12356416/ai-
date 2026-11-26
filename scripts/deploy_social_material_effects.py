"""Install only the report, active tag selector, FB planner, and their browser assets."""
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
import tempfile
import time

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SRC))
BASE = 'c4c075ecd5efb4c95d3ee634a37b968306056165'
LINKS = {'fb':Path('/opt/fb-auto-post/current'),'x':Path('/opt/x-tag-publisher/current')}
REPORT = Path('/opt/social-material-effects/current')
DATA = Path('/mnt/data-disk/social-material-effects')
NGINX = Path('/etc/nginx/default.d/x-material-effects.conf')
UNITS = ('social-material-effects-report.service','social-material-effects-refresh.service','social-material-effects-refresh.timer')
COMMON = ['features/social_material_effects/'+s+'.py' for s in ('__init__','data','selection')]
FILES = {'fb':['features/fb_auto_posts/'+s+'.py' for s in ('core','repositories','validation','catalog_plan','moboreels')] + COMMON,
         'x':['features/x_auto_posts/'+s+'.py' for s in ('publisher','selector','validation')] + ['features/x_material_effects/selection.py'] + COMMON}
PUBLIC = ['quick-nav.js','x-effect-fields.js','x-material-effects.js','fb-auto-publish-template.js',
          'x-tag-auto-publish-templates.js','x-material-effects.html','fb-auto-publish-template.html',
          'x-tag-auto-publish-template.html','x-tag-auto-publish-templates.html']


def run(args, **kwargs):
    return subprocess.run(list(map(str,args)),check=True,**kwargs)


def git(*args):
    return subprocess.check_output(['git','-C',str(SRC),*args])


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ''


def save(path, value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def file_install(path, content):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        f.write(content)
        temp = Path(f.name)
    temp.chmod(0o644)
    if path.exists():
        stat = path.stat()
        temp.chmod(stat.st_mode & 0o7777)
        os.chown(temp,stat.st_uid,stat.st_gid)
    os.replace(temp,path)


def content(name, live):
    desired = (SRC/name).read_bytes()
    before = subprocess.run(['git','-C',str(SRC),'show',BASE+':'+name],capture_output=True)
    old = before.stdout if before.returncode==0 else b''
    actual = live.read_bytes() if live.is_file() else b''
    if actual in (old,desired) or (not actual and not old):
        return desired
    if not actual or not old:
        raise RuntimeError('unexpected_live_file:'+str(live))
    with tempfile.TemporaryDirectory() as tmp:
        files=[Path(tmp)/s for s in ('current','base','desired')]
        for f,raw in zip(files,(actual,old,desired)):
            f.write_bytes(raw)
        merged=subprocess.run(['git','merge-file','-p',*map(str,files)],capture_output=True)
        if merged.returncode:
            raise RuntimeError('concurrent_change_conflict:'+str(live))
        return merged.stdout


def copy_tree(old, new):
    new.mkdir()
    run(['cp','-a','--reflink=auto',str(old)+'/.',new])


def stage(folder):
    commit=git('rev-parse','HEAD').decode().strip()
    if git('status','--porcelain','--untracked-files=no').strip() or not git('branch','-r','--contains',commit).strip():
        raise RuntimeError('clean_fetched_commit_required')
    folder.mkdir(parents=True,exist_ok=False)
    assets=folder/'assets'
    assets.mkdir()
    record=dict(commit=commit,assets=[],units={},old_report=str(REPORT.resolve()) if REPORT.exists() else None)
    for lane,names in FILES.items():
        part=folder/lane
        part.mkdir()
        old=LINKS[lane].resolve(strict=True)
        new=part/'candidate'
        copy_tree(old,new)
        changes={}
        for name in names:
            target=new/name
            if not target.resolve().is_relative_to(new):
                raise RuntimeError('external_source_symlink:'+name)
            file_install(target,content(name,old/name))
            changes[name]=dict(before=sha(old/name),after=sha(target))
        # Keep the activated template editors in release trees used by later deployments.
        runtime_assets=[n for n in PUBLIC if n.startswith('fb-auto') or n=='x-effect-fields.js'] if lane=='fb' else [
            n for n in PUBLIC if n.startswith('x-tag') or n=='x-effect-fields.js']
        for name in runtime_assets:
            target=new/'static'/name
            if target.is_symlink():
                target.unlink()
            file_install(target,(SRC/'static'/name).read_bytes())
        save(part/'manifest.json',dict(platform=lane,commit=commit,previous=str(old),candidate=str(new),
                                      status='prepared',files=changes,static={},source=str(SRC)))
    new=folder/'report'
    copy_tree(Path('/opt/x-material-effects/current').resolve(strict=True),new)
    for p in sorted((SRC/'features/social_material_effects').glob('*.py')):
        file_install(new/'features/social_material_effects'/p.name,p.read_bytes())
    file_install(new/'features/fb_auto_posts/__init__.py',(SRC/'features/fb_auto_posts/__init__.py').read_bytes())
    file_install(new/'features/x_material_effects/api.py',content('features/x_material_effects/api.py',new/'features/x_material_effects/api.py'))
    for root in (Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html')):
        for name in PUBLIC:
            original=root/name
            path=original.resolve(strict=True)
            if any(a['target']==str(path) for a in record['assets']):
                continue
            ident=str(len(record['assets']))
            backup, after=assets/(ident+'.before'),assets/(ident+'.after')
            shutil.copy2(path,backup)
            after.write_bytes(content('static/'+name,path))
            record['assets'].append(dict(path=str(original),target=str(path),before=sha(backup),after=sha(after),id=ident))
    config=NGINX.read_text()
    old_line='proxy_pass http://127.0.0.1:8899;'
    if config.count(old_line)!=1 or 'location = /api/x-material-effects {' not in config:
        raise RuntimeError('report_route_changed')
    (folder/'nginx.before').write_text(config)
    (folder/'nginx.after').write_text(config.replace(old_line,'proxy_pass http://127.0.0.1:18989;'))
    for unit in UNITS:
        path=Path('/etc/systemd/system')/unit
        record['units'][unit]=sha(path)
        if path.exists():
            shutil.copy2(path,folder/(unit+'.before'))
    save(folder/'manifest.json',record)


def switch_lane(folder,lane,reverse):
    from scripts import deploy_hit_drama as controller
    from scripts import deploy_fb_skipped_recheck as fb
    from scripts.deploy_fb_profile_priority import owner_active
    part=folder/lane
    info=json.loads((part/'manifest.json').read_text())
    destination=Path(info['previous' if reverse else 'candidate'])
    if LINKS[lane].resolve()==destination and info['status']==('rolled_back' if reverse else 'active'):
        return
    if lane=='fb':
        def settled():
            deadline=time.monotonic()+900
            while owner_active():
                if time.monotonic()>=deadline:
                    raise RuntimeError('fb_owner_still_busy')
                time.sleep(1)
            return 0
        fb.native_active=settled
    controller.activate(lane,part,reverse,defer_plan_read=False)
    if lane=='x':
        run([sys.executable,controller.HEALTH,'restore','--baseline',part/'schedulers-before.json',
             '--wait-seconds','25','--output',part/'schedulers-restored.json'])


def pointer(link, dest):
    link.parent.mkdir(parents=True,exist_ok=True)
    temp=link.with_name('current.channels-next')
    if temp.is_symlink():temp.unlink()
    temp.symlink_to(dest)
    temp.replace(link)


def web(folder, reverse):
    info=json.loads((folder/'manifest.json').read_text())
    for a in info['assets']:
        if Path(a['path']).resolve()!=Path(a['target']) or sha(Path(a['target'])) not in (a['before'],a['after']):
            raise RuntimeError('concurrent_static_change:'+a['path'])
    before,after=folder/'nginx.before',folder/'nginx.after'
    if sha(NGINX) not in (sha(before),sha(after)):
        raise RuntimeError('concurrent_nginx_change')
    for unit in UNITS:
        if sha(Path('/etc/systemd/system')/unit) not in (info['units'][unit],sha(SRC/'deploy'/unit)):
            raise RuntimeError('concurrent_unit_change:'+unit)
    if not reverse:
        if REPORT.exists() and REPORT.resolve() not in (folder/'report',Path(info['old_report'] or '/')):
            raise RuntimeError('later_report_release')
        from features.social_material_effects.report import query
        for ch in ('x','facebook'):
            query(dict(channel=[ch],page_size=['1']),dict(role='admin'))
        pointer(REPORT,folder/'report')
        for unit in UNITS:
            file_install(Path('/etc/systemd/system')/unit,(SRC/'deploy'/unit).read_bytes())
        run(['systemctl','daemon-reload'])
        run(['systemctl','enable','--now',UNITS[0],UNITS[2]])
        run(['curl','--retry','12','--retry-connrefused','--retry-delay','1','-fsS','http://127.0.0.1:18989/healthz'])
    elif REPORT.exists() and REPORT.resolve()!=folder/'report':
        raise RuntimeError('later_report_release')
    file_install(NGINX,(before if reverse else after).read_bytes())
    try:
        run(['nginx','-t'])
        run(['systemctl','reload','nginx'])
    except Exception:
        file_install(NGINX,(after if reverse else before).read_bytes())
        raise
    side='before' if reverse else 'after'
    for a in info['assets']:
        file_install(Path(a['target']),(folder/'assets'/(a['id']+'.'+side)).read_bytes())
    if reverse:
        run(['systemctl','disable','--now',UNITS[2],UNITS[0]])
        for unit in UNITS:
            path=Path('/etc/systemd/system')/unit
            if info['units'][unit]:
                file_install(path,(folder/(unit+'.before')).read_bytes())
            else:
                path.unlink(missing_ok=True)
        run(['systemctl','daemon-reload'])
        if info['old_report']:
            pointer(REPORT,info['old_report'])
        elif REPORT.is_symlink():
            REPORT.unlink()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('stage','fb','x','web','rollback'))
    parser.add_argument('folder',type=Path)
    args=parser.parse_args()
    folder=args.folder.resolve()
    if not folder.is_relative_to('/mnt/data-disk') or not os.path.ismount('/mnt/data-disk'):
        raise ValueError('mounted_data_disk_required')
    if args.action=='stage':
        stage(folder)
    elif args.action=='web':
        web(folder,False)
    elif args.action=='rollback':
        web(folder,True)
        for lane in ('x','fb'):
            switch_lane(folder,lane,True)
    else:
        switch_lane(folder,args.action,False)
