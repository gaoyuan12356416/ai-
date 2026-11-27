"""Scoped consumer switches and additive daily collection; rollback preserves observations."""
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
import urllib.request

SRC=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SRC))
ROOT=Path('/mnt/data-disk')
COMMON=['features/social_material_effects/'+n+'.py' for n in ('data','selection','window')]
FILES={
 'fb':COMMON+['features/fb_auto_posts/'+n+'.py' for n in ('repositories','catalog_plan','moboreels')],
 'collector':COMMON+['features/social_material_effects/'+n+'.py' for n in ('facebook','refresh','graph')]+['features/x_material_effects/collector.py'],
 'tt':COMMON+['features/tt_promotion/'+n+'.py' for n in ('profiles','social_effects','pub_native')],
}
LINKS={k:Path('/opt/'+v+'/current') for k,v in [('fb','fb-auto-post'),('tt','tt-promotion'),('collector','social-material-effects-collector')]}
BASE={'fb':'2d9398dafa985991d924841c172fd894388f9413','collector':'2d9398dafa985991d924841c172fd894388f9413','tt':'c37b99ad6d8c2fae5f5752a8290aada39614e00d'}
ASSETS={'fb':['fb-auto-publish-template.html','x-effect-fields.js'],'tt':['tt-promotion-auto.html','tt-promotion-auto.js']}
HEALTH='/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'


def run(*args,**kw):
    return subprocess.check_output(list(map(str,args)),**kw).decode().strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ''


def save(path,obj):
    path.write_text(json.dumps(obj,indent=2)+'\n')


def atomic(path,content):
    path.parent.mkdir(parents=True,exist_ok=True)
    stat=path.stat() if path.exists() else None
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        f.write(content);tmp=Path(f.name)
    tmp.chmod(stat.st_mode & 0o7777 if stat else 0o644)
    if stat:os.chown(tmp,stat.st_uid,stat.st_gid)
    tmp.replace(path)


def point(link,dest):
    temp=link.with_name('current.window-next')
    if temp.is_symlink():temp.unlink()
    temp.symlink_to(dest);temp.replace(link)


def content(repo,base,name,live):
    expected=subprocess.run(['git','-C',str(repo),'show',base+':'+name],capture_output=True)
    old=expected.stdout if expected.returncode==0 else b''
    actual=live.read_bytes() if live.is_file() else b''
    desired=(repo/name).read_bytes()
    if actual.replace(b'\r\n',b'\n') not in (old.replace(b'\r\n',b'\n'),desired.replace(b'\r\n',b'\n')):
        raise RuntimeError('concurrent_source_change:'+str(live))
    return desired


def backup(db,path):
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(path)) as dst:
        src.backup(dst)
    path.chmod(0o600)


def stage(folder,tt_source):
    if not ROOT.is_mount() or run('findmnt','-n','-o','UUID',ROOT)!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('verified_data_disk_required')
    folder.mkdir(parents=True,exist_ok=False)
    record={'sources':{},'assets':[]}
    for lane in FILES:
        repo=tt_source if lane=='tt' else SRC
        commit=run('git','-C',repo,'rev-parse','HEAD')
        if run('git','-C',repo,'status','--porcelain','--untracked-files=no') or not run('git','-C',repo,'branch','-r','--contains',commit):
            raise RuntimeError('clean_fetched_source_required')
        record['sources'][lane]=dict(source=str(repo),commit=commit)
        previous=LINKS[lane].resolve(strict=True)
        part=folder/lane;part.mkdir()
        candidate=part/'candidate';candidate.mkdir()
        run('cp','-a','--reflink=auto',str(previous)+'/.',candidate)
        changes={}
        for name in FILES[lane]:
            target=candidate/name
            package=target.parent
            if package.is_symlink():
                original=package.resolve();package.unlink()
                shutil.copytree(original,package,symlinks=True)
            if target.is_symlink():target.unlink()
            if not target.resolve().is_relative_to(candidate):raise RuntimeError('source_symlink_escape')
            raw=content(repo,BASE[lane],name,previous/name)
            atomic(target,raw);compile(raw,name,'exec')
            changes[name]=dict(before=sha(previous/name),after=sha(target))
        for name in ASSETS.get(lane,[]):
            if (candidate/'static').is_symlink():
                original=(candidate/'static').resolve();(candidate/'static').unlink()
                shutil.copytree(original,candidate/'static',symlinks=True)
            target=candidate/'static'/name
            if target.is_symlink():target.unlink()
            atomic(target,(repo/'static'/name).read_bytes())
            for root in [Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html')]:
                original=root/name;target=original.resolve()
                if any(x['target']==str(target) for x in record['assets']):continue
                raw=content(repo,BASE[lane],'static/'+name,target)
                ident=str(len(record['assets']))
                (folder/('asset-'+ident+'.before')).write_bytes(target.read_bytes())
                (folder/('asset-'+ident+'.after')).write_bytes(raw)
                record['assets'].append(dict(lane=lane,path=str(original),target=str(target),id=ident,before=sha(target),after=hashlib.sha256(raw).hexdigest()))
        save(part/'manifest.json',dict(platform=lane,commit=commit,source=str(repo),previous=str(previous),candidate=str(candidate),files=changes,static={},status='prepared'))
    backup(ROOT/'fb-auto-post-publisher/fb-auto-post.sqlite3',folder/'fb-ledger-before.sqlite3')
    backup(ROOT/'tt-promotion/promotion.sqlite3',folder/'tt-ledger-before.sqlite3')
    save(folder/'manifest.json',record)
    print(json.dumps(dict(status='staged',folder=str(folder),sources=record['sources'])),flush=True)


def web(folder,lane,reverse=False):
    manifest=json.loads((folder/'manifest.json').read_text())
    for rec in manifest['assets']:
        if rec['lane']!=lane:continue
        path=Path(rec['path'])
        if str(path.resolve())!=rec['target'] or sha(path) not in (rec['before'],rec['after']):
            raise RuntimeError('concurrent_asset_change:'+str(path))
        atomic(Path(rec['target']),(folder/('asset-'+rec['id']+('.before' if reverse else '.after'))).read_bytes())


def verify(info,lane):
    root=LINKS[lane]
    if str(root.resolve()) not in (info['previous'],info['candidate']):raise RuntimeError('later_release:'+lane)
    side='before' if str(root.resolve())==info['previous'] else 'after'
    for name,hashes in info['files'].items():
        if sha(root/name)!=hashes[side]:raise RuntimeError('concurrent_runtime_change:'+lane+'/'+name)


def tt_switch(folder,reverse):
    info=json.loads((folder/'tt/manifest.json').read_text());verify(info,'tt')
    dest=info['previous' if reverse else 'candidate']
    config=Path('/etc/nginx/default.d/tt-promotion.conf')
    before=config.read_bytes()
    marker=b'proxy_pass http://127.0.0.1:18920;'
    if before.count(marker)!=1:raise RuntimeError('tt_route_changed')
    (folder/'tt/nginx-before.conf').write_bytes(before)
    run(sys.executable,HEALTH,'snapshot','--lane','tt','--output',folder/'tt/schedulers-before.json')
    try:
        # Only new API requests are paused. Existing handlers finish naturally;
        # the independent publishing/copy workers keep their accepted work.
        atomic(config,before.replace(marker,b'return 503;'))
        run('nginx','-t');run('systemctl','reload','nginx')
        deadline=time.monotonic()+120
        while True:
            pid=run('systemctl','show','tt-promotion.service','-p','MainPID','--value')
            threads=len(list(Path('/proc',pid,'task').iterdir())) if pid!='0' else 0
            sockets=run('ss','-Hnt','state','established','( sport = :18920 )')
            if threads<=1 and not sockets:break
            if time.monotonic()>deadline:raise RuntimeError('accepted_tt_handlers_still_running')
            time.sleep(1)
        verify(info,'tt');point(LINKS['tt'],dest)
        run('systemctl','restart','tt-promotion.service')
        for attempt in range(30):
            try:
                with urllib.request.urlopen('http://127.0.0.1:18920/health',timeout=2) as res:
                    if json.load(res).get('ok'):break
            except OSError:
                time.sleep(.5)
        else:raise RuntimeError('tt_health_failed')
        web(folder,'tt',reverse)
        info['status']='rolled_back' if reverse else 'active';save(folder/'tt/manifest.json',info)
    finally:
        atomic(config,before);run('nginx','-t');run('systemctl','reload','nginx')
        run(sys.executable,HEALTH,'restore','--baseline',folder/'tt/schedulers-before.json','--wait-seconds','25','--output',folder/'tt/schedulers-after.json')


def activate(folder,lane,reverse=False):
    if lane=='collector':
        info=json.loads((folder/lane/'manifest.json').read_text());verify(info,lane)
        point(LINKS[lane],info['candidate'])
        info['status']='active';save(folder/lane/'manifest.json',info)
    elif lane=='fb':
        from scripts.deploy_social_material_effects import switch_lane
        switch_lane(folder,'fb',reverse);web(folder,'fb',reverse)
    else:
        tt_switch(folder,reverse)
    print(json.dumps(dict(lane=lane,status='rolled_back' if reverse else 'active')),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('stage','collector','fb','tt','rollback'))
    parser.add_argument('folder',type=Path)
    parser.add_argument('--tt-source',type=Path)
    args=parser.parse_args();folder=args.folder.resolve()
    if not folder.is_relative_to(ROOT):raise ValueError('data_disk_folder_required')
    if args.action=='stage':stage(folder,args.tt_source.resolve())
    elif args.action=='rollback':
        for lane in ('tt','fb'):activate(folder,lane,True)
        print('Daily collection retained; no ledgers, observations or revenue facts restored.')
    else:activate(folder,args.action)
