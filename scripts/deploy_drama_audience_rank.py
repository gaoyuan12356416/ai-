"""GitHub-first catalog/UI deployment with narrow publisher overlays and rollback."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from features.drama_pay_rank.integration import patch_app,patch_nav
from scripts.deploy_hit_drama import run,sha,save,atomic,activate as activate_lane
from scripts.deploy_pipeline_runtime import verify_source,preserve_tree_owners

MAIN=Path('/root/drama_material_service')
WEB=Path('/usr/share/nginx/html')
HEALTH='/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
POINTERS={'x':'/opt/x-tag-publisher/current','fb':'/opt/fb-auto-post/current','standard':'/opt/x-post-automation/current'}
ASSETS=['drama-pay-rank.html','drama-pay-rank.js','drama-pay-rank.css','x-tag-revenue.js']
SHARED=['features/x_tag_revenue/taxonomy.py']
AUDIENCE=[str(p.relative_to(SOURCE)) for p in (SOURCE/'features/drama_audience').glob('*.py')]
RANK=[str(p.relative_to(SOURCE)) for p in (SOURCE/'features/drama_pay_rank').glob('*.py')]


def prepare(folder):
    assert run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    commit=verify_source(SOURCE,'HEAD');folder.mkdir(parents=True,exist_ok=False);folder.chmod(0o755)
    manifest=dict(commit=commit,source=str(SOURCE),status='prepared',main={},lanes={})
    for lane,pointer in POINTERS.items():
        previous=Path(pointer).resolve();base=folder/lane;base.mkdir();candidate=base/'candidate'
        shutil.copytree(previous,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
        preserve_tree_owners(previous,candidate)
        files=AUDIENCE+SHARED+(['features/x_tag_publish/policy.py'] if lane=='x' else ['features/x_posts/drama_profile.py'] if lane=='standard' else [])
        info=dict(platform=lane,commit=commit,source=str(SOURCE),previous=str(previous),candidate=str(candidate),files={},static={},status='prepared')
        for name in files:
            target=candidate/name
            assert target.resolve().is_relative_to(candidate.resolve()),'overlay_symlink_escape'
            atomic(target,(SOURCE/name).read_bytes());compile(target.read_bytes(),name,'exec')
            info['files'][name]=dict(before=sha(previous/name),after=sha(target))
        save(base/'manifest.json',info);manifest['lanes'][lane]=info
    targets={MAIN/name:(SOURCE/name).read_bytes() for name in AUDIENCE+RANK+SHARED+['features/x_tag_revenue/refresh.py']}
    targets[MAIN/'app.py']=patch_app((MAIN/'app.py').read_text()).encode()
    compile(targets[MAIN/'app.py'],'app.py','exec')
    for root in (MAIN/'static',WEB):
        for asset in ASSETS:targets[root/asset]=(SOURCE/'static'/asset).read_bytes()
        targets[root/'quick-nav.js']=patch_nav((root/'quick-nav.js').read_text()).encode()
        for filename in ['index.html','x-account-list.html',*['x-tag-'+p.name[len('x-tag-'):] for p in root.glob('x-tag-*.html')]]:
            path=root/filename
            if not path.exists():continue
            text=path.read_text()
            for asset in ('quick-nav.js','x-tag-revenue.js'):
                content=targets.get(root/asset)
                if content:
                    text=re.sub(r'(/'+re.escape(asset)+r')(?:\?[^"\']*)?',r'\1?v='+hashlib.sha256(content).hexdigest()[:12],text)
            if text!=path.read_text():targets[path]=text.encode()
    for target,content in targets.items():
        index=str(len(manifest['main']));old=folder/('before-'+index);new=folder/('after-'+index)
        if target.exists():shutil.copy2(target,old)
        new.write_bytes(content)
        manifest['main'][str(target)]=dict(before=sha(target),after=sha(new),backup=str(old),next=str(new))
    save(folder/'manifest.json',manifest)
    print(json.dumps(dict(status='prepared',commit=commit,folder=str(folder),main_files=len(targets))),flush=True)


def main_files(manifest,rollback=False):
    for name,row in manifest['main'].items():
        assert sha(name) in (row['before'],row['after']),'concurrent_main_change:'+name
    for name,row in manifest['main'].items():
        if rollback and not row['before']:
            # Additive libraries/assets can remain for newer consumers.
            continue
        atomic(name,Path(row['backup' if rollback else 'next']).read_bytes())


def check_no_writes(db_path):
    with closing(sqlite3.connect('file:'+db_path+'?mode=ro',uri=True)) as db:
        return db.execute("SELECT count(*) FROM x_post_publish_log WHERE unknown_outcome=0 AND status IN ('media_uploading','post_creating','repost_creating')").fetchone()[0]==0


def switch_standard(folder,rollback=False):
    info=json.loads((folder/'standard/manifest.json').read_text());current=Path(POINTERS['standard'])
    expected=info['candidate'] if rollback else info['previous'];destination=info['previous'] if rollback else info['candidate']
    assert str(current.resolve())==expected,'standard_release_drift'
    assert check_no_writes('/var/lib/x-post-automation/accounts.sqlite3'),'standard_write_inflight'
    for name,row in info['files'].items():assert sha(current/name)==row['after' if rollback else 'before'],'standard_file_drift'
    temp=current.with_name('current.audience-next');temp.symlink_to(destination);temp.replace(current)
    run('systemctl','restart','x-post-automation.service',timeout=60)


def install_jobs():
    current=Path('/opt/drama-catalog/current');current.parent.mkdir(parents=True,exist_ok=True)
    temp=current.with_name('current.next');temp.symlink_to(SOURCE);temp.replace(current)
    for name in ('drama-audience-sync','drama-pay-rank-refresh'):
        for suffix in ('service','timer'):atomic(Path('/etc/systemd/system')/(name+'.'+suffix),(SOURCE/'deploy'/(name+'.'+suffix)).read_bytes())
    run('systemctl','daemon-reload')
    run('systemctl','enable','--now','drama-audience-sync.timer','drama-pay-rank-refresh.timer')


def apply(folder,rollback=False):
    manifest=json.loads((folder/'manifest.json').read_text())
    from scripts.deploy_x_hierarchy import switch_window
    from scripts.reproject_drama_audiences import preview,publish
    from contextlib import ExitStack
    import fcntl
    for lane in ('x','fb','youtube','coordinator'):
        run(sys.executable,HEALTH,'snapshot','--lane',lane,'--output',folder/('health-'+lane+'-before.json'))
    portrait_timers=['x-channel-tag-revenue-refresh.timer','fb-page-portrait-refresh.timer']
    states={unit:run('systemctl','is-active',unit) for unit in portrait_timers}
    for unit,state in states.items():
        if state=='active':run('systemctl','stop',unit)
    try:
        if rollback:
            # Do not rewind confirmed audience facts or any publication ledger.
            activate_lane('fb',folder/'fb',True)
            with switch_window(folder,55):
                main_files(manifest,True);switch_standard(folder,True)
                info=manifest['lanes']['x'];pointer=Path(POINTERS['x'])
                assert str(pointer.resolve())==info['candidate'],'x_release_drift'
                assert check_no_writes('/mnt/data-disk/x-tag-publisher/posts.sqlite3'),'tag_write_inflight'
                temp=pointer.with_name('current.audience-next');temp.symlink_to(info['previous']);temp.replace(pointer)
                run('systemctl','restart','x-tag-auto.service','x-tag-gateway.service','x-tag-post.service','x-channel-tag-revenue.service','drama-material-api.service',timeout=90)
            run('systemctl','disable','--now','drama-pay-rank-refresh.timer','drama-audience-sync.timer')
        else:
            # Wait for the two read-only refresh jobs, not unrelated publishing.
            deadline=time.monotonic()+600
            while any(subprocess.run(['systemctl','is-active','--quiet',u.replace('.timer','.service')]).returncode==0 for u in portrait_timers):
                if time.monotonic()>deadline:raise RuntimeError('portrait_refresh_running_retry')
                time.sleep(2)
            with switch_window(folder,55):
                with (Path('/mnt/data-disk/fb-page-portraits')/'refresh.lock').open('a') as fblock:
                    fcntl.flock(fblock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    preview(folder/'reprojection')
                    # Catalog backup is online and remains outside publication DBs.
                    with closing(sqlite3.connect('file:/mnt/data-disk/drama-audience/catalog.sqlite3?mode=ro',uri=True)) as src,closing(sqlite3.connect(folder/'audience-before.sqlite3')) as dst:src.backup(dst)
                    assert check_no_writes('/mnt/data-disk/x-tag-publisher/posts.sqlite3'),'tag_write_inflight'
                    main_files(manifest)
                    publish(folder/'reprojection')
                    info=manifest['lanes']['x'];pointer=Path(POINTERS['x'])
                    assert str(pointer.resolve())==info['previous'],'x_release_drift'
                    for name,row in info['files'].items():assert sha(pointer/name)==row['before'],'x_file_drift'
                    temp=pointer.with_name('current.audience-next');temp.symlink_to(info['candidate']);temp.replace(pointer)
                    switch_standard(folder)
                    run('systemctl','restart','x-tag-auto.service','x-tag-gateway.service','x-tag-post.service','x-channel-tag-revenue.service','drama-material-api.service',timeout=120)
            # The existing FB release helper drains only FB and retains its states.
            activate_lane('fb',folder/'fb')
            run('systemctl','restart','fb-page-portrait.service',timeout=30)
            install_jobs()
        manifest['status']='rolled_back' if rollback else 'active';save(folder/'manifest.json',manifest)
    finally:
        for unit,state in states.items():
            if state=='active':run('systemctl','start',unit)
        for lane in ('x','fb','youtube','coordinator'):
            run(sys.executable,HEALTH,'restore','--baseline',folder/('health-'+lane+'-before.json'),'--wait-seconds','25','--output',folder/('health-'+lane+'-after.json'),timeout=60)
    print(json.dumps(dict(status=manifest['status'],commit=manifest['commit'],folder=str(folder))),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','apply','rollback']);p.add_argument('--folder',type=Path,required=True);a=p.parse_args()
    assert a.folder.resolve().is_relative_to('/mnt/data-disk')
    prepare(a.folder) if a.action=='prepare' else apply(a.folder,a.action=='rollback')
