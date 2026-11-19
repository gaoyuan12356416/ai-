"""Verified GitHub overlays; preserve live queues, receipts and scheduler state."""
import argparse
from contextlib import ExitStack
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
from scripts.deploy_hit_drama import run,sha,save,atomic,health,HEALTH
from scripts.deploy_pipeline_runtime import preserve_tree_owners

ROOTS={'x':Path('/opt/x-tag-publisher/current'),'fb':Path('/opt/fb-auto-post/current'),'mobo':Path('/opt/moboreels-library/current')}
TESTS={'x':['scripts.test_hit_drama_target','scripts.test_x_hit_drama','scripts.test_moboreels_hit.IdentityTests','scripts.test_moboreels_hit.XTests'],
       'fb':['scripts.test_hit_drama_target','scripts.test_fb_hit_drama','scripts.test_moboreels_hit.IdentityTests','scripts.test_moboreels_hit.FacebookTests'],
       'mobo':['tests.test_moboreels_content_tools']}
DROPIN=Path('/etc/systemd/system/x-tag-auto.service.d/80-selection-speed.conf')


def prepare(folder):
    assert run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git','-C',SOURCE,'status','--porcelain')
    commit=run('git','-C',SOURCE,'rev-parse','HEAD')
    assert run('git','-C',SOURCE,'branch','-r','--contains',commit)
    baselines=json.loads((SOURCE/'deploy/moboreels-resource-baselines.json').read_text())
    folder.mkdir(parents=True,exist_ok=False);folder.chmod(0o755)
    for lane,files in baselines.items():
        root=ROOTS[lane];previous=root.resolve();stage=folder/lane;stage.mkdir()
        candidate=stage/'candidate'
        for name,expected in files.items():assert sha(root/name)==expected,'live_drift:'+lane+'/'+name
        shutil.copytree(previous,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
        preserve_tree_owners(previous,candidate)
        info=dict(platform=lane,commit=commit,previous=str(previous),candidate=str(candidate),files={},static={},status='prepared')
        for name,expected in files.items():
            atomic(candidate/name,(SOURCE/name).read_bytes());compile((candidate/name).read_bytes(),name,'exec')
            info['files'][name]=dict(before=expected,after=sha(candidate/name))
        for test in TESTS[lane]:
            name='/'.join(test.split('.')[:2])+'.py'
            atomic(candidate/name,(SOURCE/name).read_bytes())
        proof=subprocess.run([sys.executable,'-B','-m','unittest',*TESTS[lane],'-q'],cwd=candidate,capture_output=True,text=True,timeout=90)
        (stage/'tests.txt').write_text(proof.stdout+proof.stderr)
        assert proof.returncode==0,'candidate_tests_failed:'+lane
        save(stage/'manifest.json',info)
    info=dict(commit=commit,static={},status='prepared')
    for root in [Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html'),*(folder/lane/'candidate/static' for lane in ROOTS)]:
        names=['hit-drama-publish.js','x-tag-auto-publish-templates.html','fb-auto-publish-templates.html']
        if root in [Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html'),folder/'mobo/candidate/static']:
            names+=['moboreels-tools.js','moboreels-recommendations.html']
        for name in names:
            path=root/name
            if not path.exists() and name.endswith('.html'):continue
            if name.endswith('.js'):
                if path.exists():
                    expected='3f31f9dc460cdd8aa737449627c2b1e735b3d2ba21caec33f87463015e909277' if name=='hit-drama-publish.js' else '7dd7177c193dd8d40f4efef4ccb828c67632d3ac604e35e0a365778bb7990589'
                    assert sha(path)==expected,'static_baseline_changed:'+str(path)
                data=(SOURCE/'static'/name).read_bytes()
            else:
                asset='moboreels-tools.js' if name=='moboreels-recommendations.html' else 'hit-drama-publish.js'
                data=re.sub(r'(/'+re.escape(asset)+r')(?:\?[^"\s]*)?',r'\1?v='+sha(SOURCE/'static'/asset)[:12],path.read_text()).encode()
            if path.is_relative_to(folder):atomic(path,data);continue
            key=str(len(info['static']));old=folder/(key+'.old');new=folder/(key+'.new')
            if path.exists():shutil.copy2(path,old)
            new.write_bytes(data)
            info['static'][str(path)]=dict(before=sha(path),after=hashlib.sha256(data).hexdigest(),backup=str(old),next=str(new))
    original=DROPIN.read_text();assert original.count('Environment=X_TAG_SELECTION_BASE=')==1
    assert str(ROOTS['x'].resolve()) in original
    (folder/'x-dropin.old').write_text(original)
    (folder/'x-dropin.new').write_text(re.sub(r'Environment=X_TAG_SELECTION_BASE=[^\n]+','Environment=X_TAG_SELECTION_BASE=/opt/x-tag-publisher/current',original))
    info['dropin_before']=sha(DROPIN);info['dropin_after']=sha(folder/'x-dropin.new')
    save(folder/'manifest.json',info)
    print(json.dumps(dict(status='prepared',commit=commit,folder=str(folder))),flush=True)


def verify(lane,info,rollback=False):
    root=ROOTS[lane];assert str(root.resolve())==info['candidate' if rollback else 'previous'],'later_release_present'
    for name,row in info['files'].items():assert sha(root/name)==row['after' if rollback else 'before'],'live_drift:'+name


def switch(lane,folder,rollback=False):
    stage=folder/lane;info=json.loads((stage/'manifest.json').read_text());verify(lane,info,rollback)
    if rollback and lane in ('x','fb'):
        db=Path('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3' if lane=='x' else '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
        with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as conn:
            column='blacklist_snapshot_json' if lane=='x' else 'config_json'
            prefix='x_auto' if lane=='x' else 'fb_auto'
            rows=conn.execute(f"SELECT r.{column} FROM {prefix}_run r WHERE EXISTS(SELECT 1 FROM {prefix}_task t WHERE t.run_id=r.id AND t.status NOT IN ('published','failed','canceled','cancelled','skipped'))").fetchall()
            for row in rows:
                hit=json.loads(row[0]).get('hit_drama_publish',{})
                assert (hit.get('target',hit)).get('catalog')!='moboreels','unfinished_mobo_run_keep_compatible_code'
    destination=info['previous' if rollback else 'candidate'];root=ROOTS[lane]
    if lane=='fb':
        from scripts.deploy_hit_drama import activate
        activate('fb',stage,rollback)
        return
    if lane=='x':
        from scripts.deploy_x_hierarchy import active,lock_file
        run(sys.executable,HEALTH,'snapshot','--lane','x','--output',stage/'schedulers-before.json')
        triggers=['x-tag-auto-scheduler.timer','x-tag-auto-runner.timer','x-tag-auto-runner.path']
        states={unit:active(unit) for unit in triggers}
        manifest=json.loads((folder/'manifest.json').read_text())
        assert sha(DROPIN)==manifest['dropin_after' if rollback else 'dropin_before']
        try:
            for unit,on in states.items():
                if on:run('systemctl','stop',unit)
            deadline=time.monotonic()+55
            while any(active(unit) for unit in ['x-tag-auto-scheduler.service','x-tag-auto-runner.service']) or run('ss','-Hnt','state','established','( sport = :18843 )'):
                assert time.monotonic()<deadline,'active_tag_work_preserved_retry_later';time.sleep(.5)
            with ExitStack() as stack:
                for lock in ['/run/x-tag-auto/scheduler.lock','/run/x-tag-auto/runner.lock']:stack.enter_context(lock_file(lock))
                verify(lane,info,rollback)
                temp=root.with_name('current.mobo-next');temp.symlink_to(destination);temp.replace(root)
                atomic(DROPIN,(folder/('x-dropin.old' if rollback else 'x-dropin.new')).read_bytes())
                run('systemctl','daemon-reload');run('systemctl','restart','x-tag-auto.service');health(18843)
        finally:
            for unit,on in states.items():
                if on:run('systemctl','start',unit)
            run(sys.executable,HEALTH,'restore','--baseline',stage/'schedulers-before.json','--wait-seconds','25','--output',stage/'schedulers-after.json')
    else:
        # Refuse new requests briefly, let accepted provider writes finish.
        marker='mobo-resource-'+str(os.getpid());rule=['-p','tcp','--dport','8864','-m','conntrack','--ctstate','NEW','-m','comment','--comment',marker,'-j','REJECT','--reject-with','tcp-reset']
        run('systemd-run','--quiet','--unit='+marker,'--on-active=75s','--','/usr/sbin/iptables','-D','INPUT',*rule)
        run('iptables','-I','INPUT','1',*rule)
        try:
            deadline=time.monotonic()+55
            pid=run('systemctl','show','moboreels-library.service','-p','MainPID','--value')
            while run('ss','-Hnt','state','established','( sport = :8864 )') or len(list(Path('/proc',pid,'task').iterdir()))>2:
                assert time.monotonic()<deadline,'active_mobo_request_preserved';time.sleep(.5)
            time.sleep(.5);verify(lane,info,rollback)
            temp=root.with_name('current.resource-next');temp.symlink_to(destination);temp.replace(root)
            run('systemctl','restart','moboreels-library.service')
        finally:
            run('iptables','-D','INPUT',*rule);run('systemctl','stop',marker+'.timer')
        health(8864)
    info['status']='rolled_back' if rollback else 'active';save(stage/'manifest.json',info)
    print(json.dumps(dict(status=info['status'],lane=lane)),flush=True)


def static(folder,rollback=False):
    info=json.loads((folder/'manifest.json').read_text())
    for name,row in info['static'].items():assert sha(name)==row['after' if rollback else 'before'],'static_drift:'+name
    for name,row in info['static'].items():
        if rollback and not row['before']:Path(name).unlink()
        else:atomic(name,Path(row['backup' if rollback else 'next']).read_bytes())
    info['status']='rolled_back' if rollback else 'active';save(folder/'manifest.json',info)
    print(json.dumps(dict(static=info['status'])),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','apply','rollback','static','static-rollback']);parser.add_argument('--folder',required=True);parser.add_argument('--lane',choices=list(ROOTS));args=parser.parse_args()
    folder=Path(args.folder).resolve();assert folder.is_relative_to('/mnt/data-disk')
    if args.action=='prepare':prepare(folder)
    elif args.action.startswith('static'):static(folder,args.action=='static-rollback')
    else:switch(args.lane,folder,args.action=='rollback')
