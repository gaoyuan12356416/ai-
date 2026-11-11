"""GitHub-verified, scoped release overlay; preserve all ledgers and other lanes."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
HEALTH='/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
WEB=Path('/usr/share/nginx/html')
MAIN=Path('/root/drama_material_service/static')

def run(*args,timeout=60):
    result=subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('command_failed:'+str(args[0])+':'+result.stderr[-2000:])
    return result.stdout.strip()

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else ''
def save(path,value):Path(path).write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.hit-next');temp.write_bytes(data);temp.chmod(0o644);temp.replace(path)
def replace(text,old,new):
    if text.count(old)!=1:raise RuntimeError('anchor_changed:'+old[:90])
    return text.replace(old,new,1)

def static_js(text,platform):
    if platform=='x':
        text=replace(text,'      actions.appendChild(actionButton("手动执行", "run", id, "primary"));','      actions.appendChild(actionButton("手动执行", "run", id, "primary"));\n      actions.appendChild(actionButton("爆款剧发布", "drama", id, "primary"));')
        return replace(text,'    if (action === "edit") {','    if (action === "drama") { await window.HitDramaPublish.open("x", id); return; }\n    if (action === "edit") {')
    line=next(line for line in text.splitlines() if 'data-action="material"' in line)
    text=replace(text,line,line+'\n'+line.replace('data-action="material"','data-action="drama"').replace('爆款素材发布','爆款剧发布'))
    text=replace(text,'    if (["run", "material"].includes(action)','    if (["run", "material", "drama"].includes(action)')
    return replace(text,'    if (action === "material") {','    if (action === "drama") { await window.HitDramaPublish.open("fb", id); return; }\n    if (action === "material") {')

def settings(platform):
    if platform=='x':return Path('/opt/x-tag-publisher/current'),['features/x_tag_publish/selection.py','features/x_tag_publish/hit_drama.py'],Path('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')
    return Path('/opt/fb-auto-post/current'),['features/fb_auto_posts/'+n+'.py' for n in ('core','repositories','service','hit_drama')],Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')

def prepare(platform,folder):
    assert run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    commit=run('git','-C',SOURCE,'rev-parse','HEAD')
    base={'x':'6ba8280a7d6884d2dcc9f2546caf71b8a96dc99c','fb':'e34277d1ccbc3f7f611d40d786ba01ebaf82d796'}[platform]
    assert not run('git','-C',SOURCE,'status','--porcelain') and run('git','-C',SOURCE,'branch','-r','--contains',commit),'pushed_clean_source_required'
    current,files,db=settings(platform);previous=current.resolve();candidate=folder/'candidate'
    folder.mkdir(parents=True,exist_ok=False);folder.chmod(0o755)
    shutil.copytree(previous,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
    for target in [candidate,*candidate.rglob('*')]:
        origin=previous/target.relative_to(candidate)
        if origin.exists() and not target.is_symlink():
            stat=origin.stat();os.chown(target,stat.st_uid,stat.st_gid)
    info=dict(platform=platform,commit=commit,source=str(SOURCE),previous=str(previous),candidate=str(candidate),files={},static={},status='prepared')
    for name in files:
        p=previous/name;baseline=subprocess.run(['git','-C',str(SOURCE),'show',base+':'+name],capture_output=True)
        if baseline.returncode==0:assert p.read_bytes().replace(b'\r\n',b'\n')==baseline.stdout,'backend_baseline_changed:'+name
        else:assert not p.exists(),'new_file_collision:'+name
        atomic(candidate/name,(SOURCE/name).read_bytes())
        compile((candidate/name).read_bytes(),name,'exec')
        info['files'][name]=dict(before=sha(p),after=sha(candidate/name))
    if platform=='x':
        name='features/x_auto_posts/service.py';p=candidate/name
        text=replace(p.read_text(),'    return XAutoPostService(\n','    from features.x_tag_publish.hit_drama import TagAutoPostService\n    return TagAutoPostService(\n')
        atomic(p,text.encode());compile(text,name,'exec');info['files'][name]=dict(before=sha(previous/name),after=sha(p))
    prefix='x-tag' if platform=='x' else 'fb'
    for root in (candidate/'static',MAIN,WEB):
        js=root/(prefix+'-auto-publish-templates.js');html=root/(prefix+'-auto-publish-templates.html')
        data=static_js(js.read_text(),platform).encode();page=html.read_text()
        import re
        page=re.sub(r'(/'+re.escape(js.name)+r')(?:\?[^"\']*)?',r'\1?v='+hashlib.sha256(data).hexdigest()[:12],page)
        page=replace(page,'</body>','  <script src="/hit-drama-publish.js?v='+sha(SOURCE/'static/hit-drama-publish.js')[:12]+'"></script>\n</body>')
        for path,content in [(js,data),(html,page.encode()),(root/'hit-drama-publish.js',(SOURCE/'static/hit-drama-publish.js').read_bytes())]:
            if root==candidate/'static':atomic(path,content);continue
            index=str(len(info['static']));old=folder/('static-'+index+'.old');new=folder/('static-'+index+'.new')
            if path.exists():shutil.copy2(path,old)
            new.write_bytes(content)
            info['static'][str(path)]=dict(before=sha(path),after=hashlib.sha256(content).hexdigest(),backup=str(old),next=str(new))
    test='test_x_hit_drama.py' if platform=='x' else 'test_fb_hit_drama.py'
    shutil.copy2(SOURCE/'scripts'/test,candidate/'scripts'/test)
    proof=subprocess.run([sys.executable,'-B','-m','unittest','scripts.'+test[:-3],'-q'],cwd=candidate,capture_output=True,text=True,timeout=120)
    (folder/'tests.txt').write_text(proof.stdout+proof.stderr);assert proof.returncode==0,'candidate_tests_failed'
    # Preserve a consistent online snapshot. Rollback never restores this database.
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(folder/'ledger-before.sqlite3')) as dst:src.backup(dst)
    (folder/'ledger-before.sqlite3').chmod(0o600)
    save(folder/'manifest.json',info);print(json.dumps(dict(status='prepared',platform=platform,folder=str(folder),commit=commit)),flush=True)

def publish_static(info,rollback=False):
    for name,row in info['static'].items():
        assert sha(name)==row['after' if rollback else 'before'],'static_changed:'+name
    for name,row in info['static'].items():
        if rollback and not row['before']:
            # The other platform may now use this additive shared asset.
            if Path(name).name!='hit-drama-publish.js':Path(name).unlink(missing_ok=True)
        else:atomic(name,Path(row['backup' if rollback else 'next']).read_bytes())

def verify_current(info,rollback):
    current,_,_=settings(info['platform']);expected=info['candidate'] if rollback else info['previous']
    assert str(current.resolve())==expected,'later_release_present'
    for name,row in info['files'].items():assert sha(current/name)==row['after' if rollback else 'before'],'live_file_changed:'+name
    return current

def activate(platform,folder,rollback=False):
    info=json.loads((folder/'manifest.json').read_text());assert info['platform']==platform
    current=verify_current(info,rollback);destination=info['previous'] if rollback else info['candidate']
    if rollback and platform=='x':
        _,_,db=settings(platform)
        with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as conn:
            rows=conn.execute("SELECT r.blacklist_snapshot_json FROM x_auto_run r WHERE EXISTS(SELECT 1 FROM x_auto_task t WHERE t.run_id=r.id AND t.status NOT IN ('published','failed','canceled','skipped'))").fetchall()
            assert not any('hit_drama_publish' in json.loads(row[0]) for row in rows),'unfinished_hit_drama_tasks_keep_new_selector'
    run(sys.executable,HEALTH,'snapshot','--lane',platform,'--output',folder/'schedulers-before.json')
    if platform=='fb':
        from scripts import deploy_fb_skipped_recheck as fb
        states={unit:fb.show(unit) for unit in fb.TIMERS};before=fb.configuration();audit={}
        try:
            fb.fb_enabled(False)
            for unit,state in states.items():
                if state=='active':fb.run(['systemctl','stop',unit])
            deadline=time.monotonic()+55
            while fb.native_active():
                assert time.monotonic()<deadline,'active_fb_work_preserved_retry_later';time.sleep(.5)
            manifest=dict(lanes=dict(fb=dict(previous_root=str(current.resolve()),candidate=destination,files={k:dict(before=v['after' if rollback else 'before'],after=v['before' if rollback else 'after']) for k,v in info['files'].items() if (Path(destination)/k).is_file()})))
            # The helper saves its own manifest; retain this deployment manifest.
            switch_folder=folder/('rollback-switch' if rollback else 'switch');switch_folder.mkdir(exist_ok=True)
            fb.switch('fb',switch_folder,manifest,audit,defer_plan_read=True)
            publish_static(info,rollback)
        finally:
            fb.fb_enabled('fb' in before['enabled_channels'],'fb' in before.get('preparation_only_channels',[]))
            for unit,state in states.items():
                if state=='active':fb.run(['systemctl','start',unit])
            save(folder/'switch-audit.json',audit)
            run(sys.executable,HEALTH,'restore','--baseline',folder/'schedulers-before.json','--wait-seconds','25','--output',folder/'schedulers-after.json')
    else:
        from scripts.deploy_x_hierarchy import active, lock_file
        from contextlib import ExitStack
        triggers=['x-tag-auto-scheduler.timer','x-tag-auto-runner.timer','x-tag-auto-runner.path'];workers=['x-tag-auto-scheduler.service','x-tag-auto-runner.service']
        states={unit:active(unit) for unit in triggers}
        try:
            for unit,enabled in states.items():
                if enabled:run('systemctl','stop',unit)
            deadline=time.monotonic()+55
            while any(active(unit) for unit in workers) or run('ss','-Hnt','state','established','( sport = :18843 )'):
                assert time.monotonic()<deadline,'active_tag_work_preserved_retry_later';time.sleep(1)
            with ExitStack() as stack:
                for lock in ('/run/x-tag-auto/scheduler.lock','/run/x-tag-auto/runner.lock'):stack.enter_context(lock_file(lock))
                verify_current(info,rollback)
                temp=current.with_name('current.hit-next');temp.symlink_to(destination);temp.replace(current)
                run('systemctl','restart','x-tag-auto.service',timeout=60)
                run('curl','-fsS','http://127.0.0.1:18843/health')
                publish_static(info,rollback)
        finally:
            for unit,enabled in states.items():
                if enabled:run('systemctl','start',unit)
            after={unit:active(unit) for unit in triggers};save(folder/'tag-schedulers.json',dict(before=states,after=after))
            assert states==after,'tag_trigger_restore_failed'
            run(sys.executable,HEALTH,'snapshot','--lane','x','--output',folder/'schedulers-after.json')
    info['status']='rolled_back' if rollback else 'active';save(folder/'manifest.json',info)
    print(json.dumps(dict(status=info['status'],platform=platform,commit=info['commit'],folder=str(folder))),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','apply','rollback']);p.add_argument('--platform',choices=['x','fb'],required=True);p.add_argument('--folder',required=True);a=p.parse_args()
    folder=Path(a.folder).resolve();assert folder.is_relative_to('/mnt/data-disk')
    if a.action=='prepare':prepare(a.platform,folder)
    else:activate(a.platform,folder,a.action=='rollback')
