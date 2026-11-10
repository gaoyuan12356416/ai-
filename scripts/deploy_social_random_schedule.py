"""Stage exact GitHub changes over each owning runtime, then switch by lane.

Reuses the already deployed HTTP-drain procedure. Does not clear publication
leases or restore business databases. Static and main source copies stay live.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from scripts import deploy_publish_queue_isolation as switcher
from scripts.deploy_pipeline_runtime import verify_source, atomic_copy

BASE='7b45af9f'
HEALTH='/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
LANES={k:v for k,v in switcher.LANES.items() if k in {'x','tt_auto','tt','fb','coordinator','app'}}
EXTRA_BASELINE={
 'static/fb-post-material-pool.html':'a7df82fb50beff499414b0a39fa7641d874200cbeb45d85fcdb02f6b6c41e3f3',
 'static/fb-post-material-pool.js':'5834197adc9327e17e44ac02216265688c4f4b15126db910dd587761f366f10c',
}


def run(args,**kwargs):return switcher.run(args,**kwargs)
def sha(path):return switcher.sha(path) if Path(path).is_file() else None
def save(path,obj):switcher.save(path,obj)


def applies(lane,rel):
    if rel=='features/social_random_schedule.py':return True
    if lane=='app':return rel=='features/x_posts/service.py' or rel.startswith('static/')
    if rel.startswith('features/pipeline_dispatch/'):
        return lane=='coordinator' or rel.endswith(('adapters.py','x_native.py')) and lane=='x'
    if lane=='coordinator':return False
    prefixes={'x':('features/x_posts/','features/x_auto_posts/','features/x_accounts/','scripts/x_post_schedule_runner.py'),
              'fb':('features/fb_auto_posts/','features/fb_video_publish/'),
              'tt':('features/tt_posts/',),'tt_auto':('features/tt_auto_posts/',)}
    return rel.startswith(prefixes[lane])


def overlay(source,commit,candidate,live,rel,directory,lane):
    old=live/rel; new=candidate/rel
    if not new.resolve().is_relative_to(candidate.resolve()):raise RuntimeError('candidate_symlink_escape:'+lane+'/'+rel)
    before=sha(old)
    base=subprocess.run(['git','-C',str(source),'cat-file','-e',BASE+':'+rel],capture_output=True).returncode==0
    new.parent.mkdir(parents=True,exist_ok=True)
    if base and old.exists():
        patch=subprocess.run(['git','-C',str(source),'diff','--binary',BASE,commit,'--',rel],capture_output=True,check=True).stdout
        proc=subprocess.run(['git','apply','--check','-'],input=patch,cwd=candidate,capture_output=True)
        if proc.returncode:raise RuntimeError('patch_context_drift:'+lane+'/'+rel)
        subprocess.run(['git','apply','-'],input=patch,cwd=candidate,capture_output=True,check=True)
    else:
        if old.exists() and rel not in EXTRA_BASELINE:raise RuntimeError('unexpected_existing_new_file:'+lane+'/'+rel)
        if old.exists() and before!=EXTRA_BASELINE[rel]:raise RuntimeError('extra_static_drift:'+rel)
        shutil.copy2(source/rel,new)
    if old.exists():
        backup=directory/'before'/lane/rel;backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(old,backup)
        stat=old.stat();os.chown(new,stat.st_uid,stat.st_gid)
    else:
        stat=new.parent.stat();os.chown(new,stat.st_uid,stat.st_gid);new.chmod(0o644)
    if rel.endswith('.py'):compile(new.read_bytes(),rel,'exec')
    return {'before':before,'after':sha(new)}


def prepare(source,commit,directory,effective_date):
    source,directory=Path(source).resolve(),Path(directory).resolve()
    commit=verify_source(source,commit)
    if run(['findmnt','-n','-o','UUID','/mnt/data-disk'])!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':raise RuntimeError('data_mount_invalid')
    if not directory.is_relative_to(Path('/mnt/data-disk')):raise RuntimeError('deployment_path_invalid')
    if shutil.disk_usage('/mnt/data-disk').free<8*1024**3:raise RuntimeError('backup_space_low')
    directory.mkdir(parents=True,mode=0o711,exist_ok=False)
    changes=run(['git','-C',source,'diff','--name-only',BASE,commit]).splitlines()
    manifest={'commit':commit,'source':str(source),'effective_date':effective_date,'lanes':{},'database_backups':{}}
    for lane,(root,units) in LANES.items():
        live=Path(root).resolve();candidate=directory/'candidates'/lane
        selected=[rel for rel in changes if applies(lane,rel)]
        if lane=='app':
            candidate.mkdir(parents=True)
            for rel in selected:
                if (live/rel).exists():
                    (candidate/rel).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(live/rel,candidate/rel)
        else:
            shutil.copytree(live,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
            switcher.preserve_owners(live,candidate)
        files={rel:overlay(source,commit,candidate,live,rel,directory,lane) for rel in selected}
        manifest['lanes'][lane]={'before_root':str(live),'candidate':str(candidate),'units':units,'files':files,'state':'prepared','before_pids':{u:switcher.pid(u) for u,_,_ in units}}
    # Nginx can serve a different static tree from the main runtime.
    static={}
    for rel in changes:
        if not rel.startswith('static/'):continue
        live=Path('/usr/share/nginx/html')/Path(rel).name
        before=sha(live);baseline=directory/'before'/'nginx'/Path(rel).name
        baseline.parent.mkdir(parents=True,exist_ok=True)
        if live.exists():shutil.copy2(live,baseline)
        static[rel]={'before':before,'after':sha(source/rel)}
    manifest['static']=static
    for name,db in switcher.DBS.items():
        path=directory/(name+'-before.sqlite3');switcher.backup_db(Path(db),path)
        manifest['database_backups'][name]=str(path)
    save(directory/'manifest.json',manifest)
    print(json.dumps({'prepared':str(directory),'commit':commit,'effective_date':effective_date,'lanes':list(manifest['lanes'])}),flush=True)


INITIALIZERS={
 'x':('x','from features.x_posts.service import XPostStore; XPostStore(path)'),
 'x_auto':('x','from features.x_auto_posts.core import XAutoPostStore; XAutoPostStore(path)'),
 'tt':('tt','from features.tt_posts.core import TTPostStore; TTPostStore(path)'),
 'tt_auto':('tt_auto','from features.tt_auto_posts.core import TTAutoPostStore; TTAutoPostStore(path)'),
 'fb':('fb','from features.fb_auto_posts.core import FBAutoPostStore; from features.fb_video_publish.core import Store; FBAutoPostStore(path); Store(path)'),
}


def initialize(directory,*,rehearsal):
    directory=Path(directory).resolve();manifest=json.loads((directory/'manifest.json').read_text())
    if not rehearsal and not manifest.get('rehearsed'):raise RuntimeError('schema_rehearsal_required')
    for key,(lane,initializer) in INITIALIZERS.items():
        if rehearsal:
            database=directory/(key+'-rehearsal.sqlite3')
            shutil.copy2(manifest['database_backups'][key],database)
        else:database=Path(switcher.DBS[key])
        code="""import contextlib,hashlib,json,sqlite3,sys
path=sys.argv[1];date=sys.argv[2]
def snapshot():
 with contextlib.closing(sqlite3.connect(path)) as c:
  tables=[r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%_task' OR name LIKE '%_queue' OR name LIKE '%_publish_log' OR name LIKE '%_schedule_run')\")]
  return {t:([r[1] for r in c.execute('PRAGMA table_info('+t+')')],c.execute('SELECT count(*) FROM '+t).fetchone()[0],hashlib.sha256(repr(c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall()).encode()).hexdigest()) for t in tables}
before=snapshot() if sys.argv[3]=='rehearsal' else {}
INITIALIZER
from features import social_random_schedule as daily
with contextlib.closing(sqlite3.connect(path)) as c,c:
 daily.ensure_schema(c,effective_date=date)
 existing=c.execute('SELECT effective_date FROM social_random_policy WHERE policy=?',(daily.POLICY,)).fetchone()[0]
 if existing!=date:
  if c.execute('SELECT 1 FROM social_random_day LIMIT 1').fetchone():raise RuntimeError('existing_target_day_activation_conflict')
  c.execute('UPDATE social_random_policy SET effective_date=? WHERE policy=?',(date,daily.POLICY))
 for table,(columns,count,digest) in before.items():
  rows=c.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY rowid').fetchall()
  if len(rows)!=count or hashlib.sha256(repr(rows).encode()).hexdigest()!=digest:raise RuntimeError('initialization_changed_business_facts:'+table)
print(json.dumps({'policy_date':date,'preserved_tables':len(before)}))
""".replace('INITIALIZER',initializer)
        command=[sys.executable,'-c',code,database,manifest['effective_date'],'rehearsal' if rehearsal else 'live']
        if not rehearsal:
            unit=LANES[lane][1][1 if key=='x_auto' else 0][0]
            user=run(['systemctl','show',unit,'-p','User','--value']) or 'root'
            command=['runuser','-u',user,'--',*command]
        result=run(command,cwd=manifest['lanes'][lane]['candidate'],timeout=90)
        print(json.dumps({'database':key,'rehearsal':rehearsal,'result':json.loads(result)}),flush=True)
    if rehearsal:
        shutil.copy2(manifest['database_backups']['dispatch'],directory/'dispatch-rehearsal.sqlite3')
        source=Path(manifest['source'])
        for action in ['plan','apply','apply']:
            command=[sys.executable,'-m','scripts.migrate_social_random_schedule',action,'--fb',directory/'fb-rehearsal.sqlite3','--dispatch',directory/'dispatch-rehearsal.sqlite3','--manifest',directory/'retime-rehearsal.json']
            if action=='plan':command.extend(['--effective-date',manifest['effective_date']])
            print(run(command,cwd=source,timeout=90),flush=True)
        manifest['rehearsed']=True;save(directory/'manifest.json',manifest)
    else:
        manifest['schema_initialized']=True;save(directory/'manifest.json',manifest)


def switch(directory,lane):
    directory=Path(directory).resolve()
    manifest=json.loads((directory/'manifest.json').read_text())
    if not manifest.get('rehearsed') or not manifest.get('schema_initialized'):raise RuntimeError('schema_rehearsal_required')
    health_lane={'tt_auto':'tt','tt':'tt'}.get(lane,lane)
    baseline=directory/('schedulers-before-'+lane+'.json')
    if lane!='app':run([sys.executable,HEALTH,'snapshot','--lane',health_lane,'--output',baseline])
    old_sha=switcher.sha;old_copy=switcher.copy_file
    switcher.sha=lambda path:old_sha(path) if Path(path).is_file() else None
    switcher.copy_file=atomic_copy
    try:switcher.switch(directory,lane,False)
    finally:
        switcher.sha=old_sha;switcher.copy_file=old_copy
        if lane!='app':run([sys.executable,HEALTH,'restore','--baseline',baseline,'--wait-seconds','25','--output',directory/('schedulers-after-'+lane+'.json')],timeout=45)
    if lane=='app':
        for rel,hashes in manifest['static'].items():
            target=Path('/usr/share/nginx/html')/Path(rel).name
            if sha(target) not in {hashes['before'],hashes['after']}:raise RuntimeError('static_drift:'+rel)
            atomic_copy(Path(manifest['source'])/rel,target)
        print(json.dumps({'static_files':len(manifest['static']),'restarted':False}))


def rollback_before_effective(directory):
    from features import social_random_schedule as daily
    from scripts.migrate_social_random_schedule import reverse_before_effective
    directory=Path(directory).resolve();path=directory/'manifest.json';manifest=json.loads(path.read_text())
    if daily.local_day()>=manifest['effective_date']:raise RuntimeError('after_activation_keep_compatible_consumers_and_forward_fix')
    for key,table in [('x','x_post_schedule_run'),('x','x_post_manual_run'),('x_auto','x_auto_task'),('tt','tt_post_queue'),('tt_auto','tt_auto_task')]:
        with contextlib.closing(sqlite3.connect('file:'+switcher.DBS[key]+'?mode=ro',uri=True)) as c:
            if c.execute('SELECT 1 FROM '+table+' WHERE schedule_policy=? LIMIT 1',(daily.POLICY,)).fetchone():raise RuntimeError('target_tasks_already_exist:'+key)
    retime=directory/'retime-live.json'
    if retime.exists():print(json.dumps(reverse_before_effective(switcher.DBS['fb'],switcher.DBS['dispatch'],json.loads(retime.read_text()))),flush=True)
    original=json.loads(json.dumps(manifest))
    for item in manifest['lanes'].values():item['files']={rel:h for rel,h in item['files'].items() if h['before'] is not None}
    save(path,manifest)
    original_rollback=switcher.rollback_queue_scopes
    switcher.rollback_queue_scopes=lambda _:None  # This release does not revert queue isolation.
    try:
        for lane in ['fb','tt','tt_auto','x','coordinator','app']:
            if original['lanes'][lane]['state'] not in {'active','installed'}:continue
            health_lane={'tt_auto':'tt','tt':'tt'}.get(lane,lane)
            before=directory/('rollback-schedulers-before-'+lane+'.json')
            if lane!='app':run([sys.executable,HEALTH,'snapshot','--lane',health_lane,'--output',before])
            switcher.switch(directory,lane,True)
            if lane!='app':run([sys.executable,HEALTH,'restore','--baseline',before,'--wait-seconds','25','--output',directory/('rollback-schedulers-after-'+lane+'.json')],timeout=45)
        for rel,hashes in original['static'].items():
            if hashes['before'] is not None:atomic_copy(directory/'before/nginx'/Path(rel).name,Path('/usr/share/nginx/html')/Path(rel).name)
    finally:switcher.rollback_queue_scopes=original_rollback


def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    prepare_parser=sub.add_parser('prepare');prepare_parser.add_argument('source');prepare_parser.add_argument('commit');prepare_parser.add_argument('directory');prepare_parser.add_argument('--effective-date',required=True)
    activate=sub.add_parser('activate');activate.add_argument('directory');activate.add_argument('lane',choices=LANES)
    rehearsal=sub.add_parser('rehearse');rehearsal.add_argument('directory')
    init=sub.add_parser('initialize');init.add_argument('directory')
    rollback=sub.add_parser('rollback-before-effective');rollback.add_argument('directory')
    args=p.parse_args()
    if args.action=='prepare':prepare(args.source,args.commit,args.directory,args.effective_date)
    elif args.action in {'rehearse','initialize'}:initialize(args.directory,rehearsal=args.action=='rehearse')
    elif args.action=='rollback-before-effective':rollback_before_effective(args.directory)
    else:switch(args.directory,args.lane)


if __name__=='__main__':main()
