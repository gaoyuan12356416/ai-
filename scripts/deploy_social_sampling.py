"""GitHub-first scoped collector/report switch; never restart publishing workers."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

SRC=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SRC))
from scripts.deploy_social_effect_window import atomic,content,point,run,save,sha
from scripts.deploy_social_material_daily import health

BASE='fe2e6877ca0c4be8d05949daffda676d279cd3bd'
LINKS=dict(report=Path('/opt/social-material-effects/current'),collector=Path('/opt/social-material-effects-collector/current'),
           x=Path('/opt/x-material-effects/current'))
COMMON=['features/social_material_effects/'+n+'.py' for n in ('sampling','window')]
FILES=dict(report=COMMON+['features/social_material_effects/'+n+'.py' for n in ('data','report')],
           collector=COMMON+['features/social_material_effects/'+n+'.py' for n in ('data','graph','backfill')],
           x=COMMON+['features/social_material_effects/__init__.py','features/x_material_effects/collector.py','features/x_material_effects/close.py'])
TIMERS=['social-material-effects-refresh.timer','social-material-effects-backfill.timer','x-material-effects-refresh.timer']
ASSETS=['x-material-effects.html','x-material-effects.js']
CLOSE_SERVICE='''[Unit]
Description=Read X counters near the Beijing day end
After=network-online.target
RequiresMountsFor=/mnt/data-disk

[Service]
Type=oneshot
WorkingDirectory=/opt/x-material-effects/current
EnvironmentFile=/etc/x-tag-publisher.env
ExecStart=/usr/bin/python3 -B -m features.x_material_effects.close
TimeoutStartSec=25min
UMask=0022
'''
CLOSE_TIMER='''[Unit]
Description=Beijing 23:30 closing collection with two retry opportunities

[Timer]
OnCalendar=*-*-* 23:30,40,50:00 Asia/Shanghai
AccuracySec=5s
Unit=x-material-effects-close.service

[Install]
WantedBy=timers.target
'''


def stage(folder):
    if run('findmnt','-n','-o','UUID','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('verified_data_disk_required')
    commit=run('git','-C',SRC,'rev-parse','HEAD')
    if run('git','-C',SRC,'status','--porcelain','--untracked-files=no') or not run('git','-C',SRC,'branch','-r','--contains',commit):
        raise RuntimeError('fetched_clean_source_required')
    folder.mkdir(parents=True,exist_ok=False)
    record=dict(commit=commit,source=str(SRC),lanes={},assets=[],timers=TIMERS)
    for lane,names in FILES.items():
        previous=LINKS[lane].resolve(strict=True);candidate=folder/lane;candidate.mkdir()
        run('cp','-a','--reflink=auto',str(previous)+'/.',candidate)
        changes={}
        for name in names:
            target=candidate/name
            if target.parent.is_symlink():
                original=target.parent.resolve();target.parent.unlink();shutil.copytree(original,target.parent,symlinks=True)
            if target.is_symlink():target.unlink()
            if not target.resolve().is_relative_to(candidate):raise RuntimeError('source_symlink_escape')
            base='a8e298a0' if lane=='x' else BASE
            raw=content(SRC,base,name,previous/name)
            compile(raw,name,'exec');atomic(target,raw)
            changes[name]=dict(before=sha(previous/name),after=sha(target))
        record['lanes'][lane]=dict(previous=str(previous),candidate=str(candidate),files=changes)
    assets=folder/'assets';assets.mkdir()
    for root in (Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html')):
        for name in ASSETS:
            target=(root/name).resolve(strict=True)
            if any(a['target']==str(target) for a in record['assets']):continue
            ident=str(len(record['assets']));before=assets/(ident+'.before');after=assets/(ident+'.after')
            shutil.copy2(target,before);after.write_bytes(content(SRC,BASE,'static/'+name,target))
            record['assets'].append(dict(target=str(target),id=ident,before=sha(before),after=sha(after)))
    for name,raw in [('x-material-effects-close.service',CLOSE_SERVICE),('x-material-effects-close.timer',CLOSE_TIMER)]:
        target=Path('/etc/systemd/system')/name
        if target.exists():raise RuntimeError('existing_unowned_unit:'+name)
        (folder/name).write_text(raw)
    run('systemd-analyze','verify',folder/'x-material-effects-close.service',folder/'x-material-effects-close.timer')
    save(folder/'manifest.json',record)


def active(service):
    return run('systemctl','show',service,'--property=ActiveState','--value') in ('active','activating','deactivating')


def switch(folder,reverse=False):
    record=json.loads((folder/'manifest.json').read_text())
    timers=list(TIMERS)+(['x-material-effects-close.timer'] if reverse else [])
    for lane,info in record['lanes'].items():
        expected=Path(info['candidate'] if reverse else info['previous'])
        if LINKS[lane].resolve()!=expected:raise RuntimeError('concurrent_pointer_change:'+lane)
    for asset in record['assets']:
        if sha(Path(asset['target']))!=asset['after' if reverse else 'before']:
            raise RuntimeError('concurrent_asset_change:'+asset['target'])
    run('systemctl','stop',*timers)
    try:
        services=[t.replace('.timer','.service') for t in timers]
        # Only these read collectors drain; publishing continues throughout.
        while any(active(service) for service in services):time.sleep(3)
        if not reverse:
            run('/usr/bin/python3','-B',SRC/'scripts/recover_social_daily_samples.py',folder.parent)
        else:
            run('systemctl','disable','x-material-effects-close.timer')
        for lane,info in record['lanes'].items():point(LINKS[lane],Path(info['previous' if reverse else 'candidate']))
        for asset in record['assets']:
            atomic(Path(asset['target']),(folder/'assets'/(asset['id']+('.before' if reverse else '.after'))).read_bytes())
        for name in ('x-material-effects-close.service','x-material-effects-close.timer'):
            dest=Path('/etc/systemd/system')/name
            if reverse:
                if dest.read_text()!=(folder/name).read_text():raise RuntimeError('concurrent_unit_change:'+name)
                dest.unlink()
            else:atomic(dest,(folder/name).read_bytes())
        run('systemctl','daemon-reload')
        run('systemctl','restart','social-material-effects-report.service')
        health()
        if not reverse:run('systemctl','enable','--now','x-material-effects-close.timer')
        record['status']='rolled_back' if reverse else 'active';save(folder/'manifest.json',record)
    finally:
        run('systemctl','start',*TIMERS)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['stage','activate','rollback']);p.add_argument('folder',type=Path)
    args=p.parse_args();folder=args.folder.resolve()
    if not folder.is_relative_to('/mnt/data-disk/social-daily-sampling-20261127'):raise ValueError('unexpected_deployment_root')
    stage(folder) if args.action=='stage' else switch(folder,args.action=='rollback')
