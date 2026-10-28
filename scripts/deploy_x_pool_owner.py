#!/usr/bin/env python3
"""Narrow GitHub-sourced deployment with owner-only migration and scoped rollback."""
import argparse, contextlib, hashlib, json, os, shutil, sqlite3, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from features.x_posts import pool_owner_migration as migration

BASE=Path('/mnt/data-disk/social-analytics')
DB=Path('/var/lib/x-post-automation/accounts.sqlite3')
USERS=Path('/root/drama_material_service/data/drama_material_jobs.sqlite3')
MAIN=Path('/root/drama_material_service')
X=Path('/opt/x-post-automation/current').resolve()
DAILY=Path('/opt/youtube-publisher-daily-report/current').resolve()
OLD='5d40f5d79b08e5984f49206f481a5446fdb1dcee2cb4f6c57558d2f469587be5'
TRIGGERS=['x-post-schedule.timer','x-post-schedule-claim.timer','x-post-manual.timer',
          'x-post-daily.timer','x-auto-post-runner.timer','x-auto-post-scheduler.timer',
          'x-auto-post-metric.timer','x-auto-post-runner.path','youtube-publisher-daily-report.timer']
WORKERS=[u.replace('.timer','.service') for u in TRIGGERS if u.endswith('.timer')]+['x-post-catchup.service']

def run(*args):return subprocess.check_output(args,text=True).strip()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest() if Path(p).exists() else None
def active(u):return run('systemctl','show',u,'-p','ActiveState','--value')
def save(folder,name,value):(folder/name).write_text(json.dumps(value,ensure_ascii=False,indent=2))
def read(path):return json.loads(Path(path).read_text())

def facts():
    with contextlib.closing(sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        out={}
        for name in ('x_post_queue','x_post_publish_log','x_post_repost_ledger'):
            rows=c.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall()
            out[name]=dict(count=len(rows),sha256=hashlib.sha256(json.dumps(rows,ensure_ascii=False,default=str).encode()).hexdigest())
        return out

def mount():
    assert run('findmnt','-no','UUID','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free>2*1024**3

def atomic_copy(src,dst):
    dst=Path(dst);tmp=dst.with_name(dst.name+'.pool-owner-new')
    shutil.copyfile(src,tmp);tmp.chmod(0o644);os.replace(tmp,dst)

def prepare():
    mount();assert not run('git','-C',str(ROOT),'status','--porcelain')
    commit=run('git','-C',str(ROOT),'rev-parse','HEAD')
    folder=BASE/'backups'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-pool-owner-'+commit[:12])
    folder.mkdir(mode=0o700,parents=True)
    entries=[]
    for base in (MAIN,X):
        for rel in ('features/x_posts/attribution.py','features/x_posts/pool_ownership.py'):
            target=base/rel;before=sha(target)
            assert before==(OLD if rel.endswith('/attribution.py') else None),str(target)+' baseline changed'
            backup=folder/('file-'+str(len(entries)))
            if before:shutil.copy2(target,backup)
            entries.append(dict(target=str(target),source=rel,before=before,after=sha(ROOT/rel),backup=str(backup)))
    rel='scripts/social_daily_correction.py';target=DAILY/rel
    assert not target.exists()
    entries.append(dict(target=str(target),source=rel,before=None,after=sha(ROOT/rel),backup=''))
    manifest=migration.plan(DB,USERS);save(folder,'migration.json',manifest)
    summary={}
    for row in manifest['rows']:
        key=json.loads(row['after'])['owner_name'];summary[key]=summary.get(key,0)+1
    state=dict(commit=commit,source=str(ROOT),folder=str(folder),entries=entries,status='prepared',
               migration_sha256=manifest['sha256'],counts=summary)
    save(folder,'deployment.json',state)
    print(json.dumps(dict(folder=str(folder),status='prepared',rows=len(manifest['rows']),counts=summary),ensure_ascii=False))

def execute(folder,rollback=False,wait_seconds=50):
    import fcntl
    mount();folder=Path(folder).resolve();assert BASE/'backups' in folder.parents
    state=read(folder/'deployment.json');assert state['commit']==run('git','-C',str(ROOT),'rev-parse','HEAD')
    assert not run('git','-C',str(ROOT),'status','--porcelain')
    for e in state['entries']:
        assert sha(e['target'])==e['after' if rollback else 'before'],e['target']+' changed'
        assert sha(ROOT/e['source'])==e['after']
    triggers={u:active(u) for u in TRIGGERS}
    state['triggers_before_operation']=triggers;save(folder,'deployment.json',state)
    locks=[];mutated=False
    try:
        enabled=[u for u,s in triggers.items() if s=='active']
        if enabled:run('systemctl','stop',*enabled)
        deadline=time.monotonic()+wait_seconds
        last_notice=0
        while any(active(u) in ('active','activating','deactivating') for u in WORKERS):
            if time.monotonic()>deadline:raise RuntimeError('publishers_busy_no_process_was_interrupted')
            if time.monotonic()-last_notice>=30:
                print(json.dumps(dict(status='waiting_for_existing_publisher',remaining_seconds=round(deadline-time.monotonic()))),flush=True)
                last_notice=time.monotonic()
            time.sleep(1)
        for name in ['/run/x-post-daily/runner.lock','/run/x-auto-post/scheduler.lock','/mnt/data-disk/youtube-publisher-daily-report/run.lock']:
            h=open(name,'a');locks.append(h);fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with contextlib.closing(sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            assert c.execute("SELECT count(*) FROM x_post_queue WHERE status='publishing'").fetchone()[0]==0
        before_facts=facts()
        if not rollback:
            # Refresh after draining; the prepared plan cannot omit newly frozen pool links.
            plan=migration.plan(DB,USERS);save(folder,'migration.json',plan)
            with contextlib.closing(sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True)) as source,contextlib.closing(sqlite3.connect(folder/'accounts.sqlite3')) as backup:
                source.backup(backup)
                assert backup.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            # Rehearse apply/idempotency/rollback on the online backup before touching live data.
            rehearsal=folder/'rehearsal.sqlite3';shutil.copyfile(folder/'accounts.sqlite3',rehearsal)
            migration.apply(rehearsal,plan)
            assert migration.apply(rehearsal,plan)['changed']==0
            migration.apply(rehearsal,plan,rollback=True)
        plan=read(folder/'migration.json')
        mutated=True
        if not rollback:
            for e in state['entries']:atomic_copy(ROOT/e['source'],e['target'])
        else:
            for e in state['entries']:
                if e['before']:atomic_copy(e['backup'],e['target'])
                else:Path(e['target']).unlink()
        result=migration.apply(DB,plan,rollback=rollback)
        assert facts()==before_facts,'publication_facts_changed'
        state.update(status='rolled_back' if rollback else 'applied',migration_result=result,
                     publication_facts=before_facts,migration_sha256=plan['sha256'])
        save(folder,'deployment.json',state)
        run('systemctl','restart','drama-material-api.service')
        deadline=time.monotonic()+40
        import urllib.request
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8787/api/ui/topbar',timeout=3) as response:
                    assert response.status==200
                break
            except Exception:
                if time.monotonic()>deadline:raise
                time.sleep(1)
        assert active('x-post-automation.service')=='active'
        assert active('x-auto-post-service.service')=='active'
        state['status']='rolled_back_verified' if rollback else 'verified'
        save(folder,'deployment.json',state)
    except BaseException:
        if mutated:
            # Never restore old databases. Undo only this manifest's owner metadata and files.
            with contextlib.suppress(Exception):migration.apply(DB,read(folder/'migration.json'),rollback=not rollback)
            for e in state['entries']:
                if rollback:atomic_copy(ROOT/e['source'],e['target'])
                elif e['before']:atomic_copy(e['backup'],e['target'])
                elif Path(e['target']).exists():Path(e['target']).unlink()
            run('systemctl','restart','drama-material-api.service')
        state['status']='operation_failed_restored';save(folder,'deployment.json',state)
        raise
    finally:
        for h in reversed(locks):h.close()
        restore=[u for u,s in triggers.items() if s=='active']
        if restore:run('systemctl','start',*restore)
        state['triggers_after_operation']={u:active(u) for u in TRIGGERS}
        save(folder,'deployment.json',state)
    assert state['triggers_after_operation']==triggers
    print(json.dumps(dict(status=state['status'],folder=str(folder),migration=state['migration_result'],publication_facts_unchanged=True),ensure_ascii=False))

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','apply','rollback']);p.add_argument('--backup')
    p.add_argument('--wait-seconds',type=int,default=50)
    args=p.parse_args()
    if not 1<=args.wait_seconds<=900:raise ValueError('invalid_drain_wait')
    if args.action=='prepare':prepare()
    else:execute(args.backup,rollback=args.action=='rollback',wait_seconds=args.wait_seconds)
if __name__=='__main__':main()
