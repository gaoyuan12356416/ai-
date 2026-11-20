"""Reviewed, GitHub-first scoped overlay. No publication is used for acceptance."""
import argparse
from contextlib import closing
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from scripts.deploy_drama_launch_range import remember
from scripts.deploy_hit_drama_multi_id import atomic
BASE='44736848bc567e93a408c03d9fe3041fec3333cd'
ROOTS={'x':Path('/opt/x-post-automation/current'),'tag':Path('/opt/x-tag-publisher/current'),'main':Path('/root/drama_material_service')}
UNITS=['x-post-automation.service','x-tag-post.service','x-auto-post-service.service','x-tag-auto.service','x-tag-gateway.service','drama-material-api.service']
HEALTH='/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
DBS=['/var/lib/x-post-automation/accounts.sqlite3','/mnt/data-disk/x-tag-publisher/posts.sqlite3',
     '/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3','/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3']

def run(*args):return subprocess.check_output(list(map(str,args)),text=True).strip()
def save(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2))
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else ''

def overlay(folder,name,live):
    after=(SOURCE/name).read_bytes().replace(b'\r\n',b'\n')
    result=subprocess.run(['git','-C',str(SOURCE),'show',BASE+':'+name],capture_output=True)
    if result.returncode:
        assert not live or live.replace(b'\r\n',b'\n')==after,'new_file_collision:'+name
        return after
    before=result.stdout.replace(b'\r\n',b'\n');live=live.replace(b'\r\n',b'\n')
    if live==before or live==after:return after
    if name=='features/x_auto_posts/publisher.py':
        # Ordinary lane has an additional frozen scheduling block immediately
        # after this payload. Preserve it and insert at the shared left anchor.
        addition=b"        if (task.selection.get('x_effect') or {}).get('policy'):\n            from features.x_material_effects.reuse import lane\n            payload['reuse_task'] = dict(lane=lane(), task_id=task.id)\n"
        anchor=b'            "actor": "x_auto_post_service",\n        }\n'
        assert live.count(anchor)==1 and after.count(addition)==1
        live=live.replace(anchor,anchor+addition)
        before=before.replace(anchor,anchor+addition)
    if name=='features/x_auto_posts/validation.py' and b'def profile_model(' not in live:
        after=after.replace(b'    return "revenue"\n\n\ndef normalize_template_payload',b'    return value\n\n\ndef normalize_template_payload')
    scratch=folder/'overlay-scratch';scratch.write_bytes(live)
    delta=''.join(difflib.unified_diff(before.decode().splitlines(True),after.decode().splitlines(True),fromfile='a/overlay-scratch',tofile='b/overlay-scratch',n=1))
    result=subprocess.run(['git','apply','--whitespace=nowarn','-'],cwd=folder,input=delta.encode(),capture_output=True)
    assert not result.returncode,'live_variant_requires_review:'+name+':'+result.stderr.decode()
    return scratch.read_bytes()

def prepare(folder):
    assert run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git','-C',SOURCE,'status','--porcelain')
    commit=run('git','-C',SOURCE,'rev-parse','HEAD');assert run('git','-C',SOURCE,'branch','-r','--contains',commit)
    folder.mkdir(parents=True,exist_ok=False,mode=0o700)
    info=dict(commit=commit,source=str(SOURCE),files={},roots={k:str(v.resolve()) for k,v in ROOTS.items()},units={u:run('systemctl','is-active',u) for u in UNITS})
    save(folder/'manifest.json',info)
    changed=run('git','-C',SOURCE,'diff','--name-only',BASE,commit).splitlines()
    names=[n for n in changed if n.startswith('features/') or n=='app.py']
    for lane,root in ROOTS.items():
        for name in names:
            if name=='app.py' or name=='features/retired_modules.py':
                if lane!='main':continue
            elif name.startswith('features/x_auto_posts/') or name.startswith('features/x_accounts/'):
                if lane=='main':continue
            elif name.startswith(('features/x_tag_publish/','features/x_growth_profile/')):
                if lane!='tag':continue
            path=root/name
            if not path.exists() and not name.startswith('features/x_material_effects/'):continue
            data=overlay(folder,name,path.read_bytes() if path.exists() else b'');compile(data,name,'exec')
            path.parent.mkdir(parents=True,exist_ok=True)
            remember(folder,info,path,data)
        save(folder/'manifest.json',info)
    for name in (n for n in changed if n.startswith('static/')):
        for root in [ROOTS['main'],Path('/usr/share/nginx/html')]:
            path=root/(Path(name).name if root.name=='html' else name)
            data=overlay(folder,name,path.read_bytes() if path.exists() else b'')
            remember(folder,info,path,data)
    for name in ('x-material-effects-api.service','x-material-effects-refresh.service','x-material-effects-refresh.timer'):
        remember(folder,info,Path('/etc/systemd/system')/name,(SOURCE/'deploy'/name).read_bytes())
    remember(folder,info,Path('/etc/nginx/default.d/x-material-effects.conf'),(SOURCE/'deploy/x-material-effects.nginx.conf').read_bytes())
    for index,path in enumerate(DBS):
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(folder/('ledger-'+str(index)+'.sqlite3'))) as dst:src.backup(dst)
    info['status']='prepared';save(folder/'manifest.json',info)
    print(json.dumps(dict(status='prepared',files=len(info['files']),commit=commit)),flush=True)

def activate(folder,rollback=False):
    info=json.loads((folder/'manifest.json').read_text());action='rollback' if rollback else 'apply'
    for lane,path in ROOTS.items():assert str(path.resolve())==info['roots'][lane],'runtime_moved:'+lane
    for path,row in info['files'].items():assert digest(path) in (row['before'],row['after']),'concurrent_change:'+path
    for lane in ('x','fb','tt','youtube','coordinator'):
        run(sys.executable,HEALTH,'snapshot','--lane',lane,'--output',folder/(action+'-before-'+lane+'.json'))
    # Owners execute their established stop barrier; no timeout kills or lease resets.
    for unit in UNITS:
        print('draining '+unit,flush=True);run('systemctl','stop',unit)
    try:
        for path,row in info['files'].items():
            assert digest(path) in (row['before'],row['after']),'concurrent_change:'+path
            if rollback and not row['existed']:continue
            atomic(Path(path),Path(row['backup' if rollback else 'next']).read_bytes(),row)
        if not rollback:
            from scripts.migrate_x_growth_retirement import migrate
            save(folder/'growth-migration.json',{p:migrate(p) for p in DBS[2:]})
            current=Path('/opt/x-material-effects/current');current.parent.mkdir(parents=True,exist_ok=True)
            temporary=current.with_name('current.next');temporary.unlink(missing_ok=True);temporary.symlink_to(SOURCE);temporary.replace(current)
        run('systemctl','daemon-reload');run('nginx','-t');run('systemctl','reload','nginx')
        if not rollback:
            run('systemctl','enable','--now','x-material-effects-api.service')
            run('systemctl','disable','--now','x-growth-profile.timer')
        for unit in UNITS:run('systemctl','start',unit)
        for lane in ('x','fb','tt','youtube','coordinator'):
            run(sys.executable,HEALTH,'restore','--baseline',folder/(action+'-before-'+lane+'.json'),'--wait-seconds','25','--output',folder/(action+'-after-'+lane+'.json'))
        info['status']='rolled_back' if rollback else 'active';save(folder/'manifest.json',info)
        print(json.dumps(dict(status=info['status'],commit=info['commit'])),flush=True)
    except Exception:
        print('Activation incomplete; retaining evidence and current ledgers. Inspect before resuming.',flush=True)
        raise

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','activate','rollback']);p.add_argument('--folder',type=Path,required=True);a=p.parse_args()
    assert a.folder.resolve().is_relative_to(Path('/mnt/data-disk'))
    if a.action=='prepare':prepare(a.folder)
    else:activate(a.folder,a.action=='rollback')
