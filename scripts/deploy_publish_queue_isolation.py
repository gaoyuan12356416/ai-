"""GitHub-source overlays; drain only affected HTTP services, preserve all ledgers.

Usage: prepare SOURCE SHA DIRECTORY; activate DIRECTORY [LANE]; rollback DIRECTORY LANE.
Never restores a database/token backup. Code rollback keeps current task facts.
"""
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
import time
import urllib.request

LANES={
    'x':('/opt/x-post-automation/current',[('x-post-automation.service',8810,1),('x-auto-post-service.service',18833,1)]),
    'tt_auto':('/opt/tt-auto-post/current',[('tt-auto-post-service.service',18831,1)]),
    'tt':('/opt/tt-post/current',[('tt-post-service.service',18829,1)]),
    'fb':('/opt/fb-auto-post/current',[('fb-auto-post-service.service',18835,1)]),
    'coordinator':('/opt/pipeline-dispatch/current',[('pipeline-coordinator.service',18846,2)]),
    'report':('/opt/post-daily-report/current',[]),
    'app':('/root/drama_material_service',[]),
}
DBS={
 'dispatch':'/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3',
 'x':'/var/lib/x-post-automation/accounts.sqlite3',
 'x_auto':'/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3',
 'tt':'/mnt/data-disk/tt-post-publisher/tt-post.sqlite3',
 'tt_auto':'/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3',
 'fb':'/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3',
}

def run(args,timeout=60,**kwargs):
    r=subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=timeout,**kwargs)
    if r.returncode:raise RuntimeError('command_failed:'+str(args[0])+':'+str(r.returncode)+':'+r.stderr[-500:])
    return r.stdout.strip()

def sha(p):return hashlib.sha256(Path(p).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
def save(path,obj):
    Path(path).write_text(json.dumps(obj,indent=2)+'\n');Path(path).chmod(0o600)
def pid(unit):return run(['systemctl','show',unit,'-p','MainPID','--value'])
def swap(link,target):
    temporary=link.with_name(link.name+'.queue-isolation-next')
    if temporary.exists() or temporary.is_symlink():raise RuntimeError('temporary_link_conflict')
    temporary.symlink_to(target);os.replace(temporary,link)

def copy_file(source,target):
    old=target.stat() if target.exists() else source.stat()
    temporary=target.with_name(target.name+'.queue-isolation-next')
    shutil.copy2(source,temporary);os.chown(temporary,old.st_uid,old.st_gid);os.replace(temporary,target)

def backup_db(source,target):
    with contextlib.closing(sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True)) as src:
        with contextlib.closing(sqlite3.connect(str(target))) as dst:
            src.backup(dst);assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    target.chmod(0o600)

def preserve_owners(source,target):
    for current,dirs,files in os.walk(source,followlinks=False):
        for item in [Path(current)]+[Path(current)/name for name in dirs+files]:
            other=target/item.relative_to(source)
            if other.exists() or other.is_symlink():
                st=item.lstat();os.chown(other,st.st_uid,st.st_gid,follow_symlinks=False)

def prepare(source,commit,directory):
    source,directory=Path(source).resolve(),Path(directory).resolve()
    assert run(['findmnt','-n','-o','UUID','/mnt/data-disk'])=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert directory.is_relative_to(Path('/mnt/data-disk'))
    assert run(['git','-C','/mnt/data-disk/pipeline-dispatch/github','rev-parse',commit+'^{commit}'])==commit
    # Service users must traverse this parent to reach their candidate tree.
    # Database backups and the manifest are independently private (0600).
    directory.mkdir(parents=True,mode=0o711,exist_ok=False)
    expected=json.loads((source/'ops/publish-queue-isolation/files.json').read_text())
    manifest={'commit':commit,'source':str(source),'lanes':{},'database_backups':{}}
    for lane,(link,units) in LANES.items():
        live=Path(link).resolve();candidate=directory/'candidates'/lane
        candidate.parent.mkdir(exist_ok=True)
        if lane=='app':
            candidate.mkdir()
            for rel in expected[lane]:
                (candidate/rel).parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(live/rel,candidate/rel)
        else:
            shutil.copytree(live,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
            preserve_owners(live,candidate)
        item={'before_root':str(live),'candidate':str(candidate),'units':units,'files':expected[lane],
              'before_pids':{u:pid(u) for u,_,_ in units},'state':'prepared'}
        manifest['lanes'][lane]=item
        for rel,hashes in item['files'].items():
            assert sha(live/rel)==hashes['before'],'affected_source_drift:'+lane+'/'+rel
            assert (candidate/rel).resolve().is_relative_to(candidate),'overlay_symlink_escape'
            dst=directory/'before'/lane/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(live/rel,dst)
        run(['git','apply','--check',source/'ops/publish-queue-isolation'/(lane+'.patch')],cwd=candidate)
        run(['git','apply',source/'ops/publish-queue-isolation'/(lane+'.patch')],cwd=candidate)
        for rel,hashes in item['files'].items():
            assert sha(candidate/rel)==hashes['after'],'candidate_hash_mismatch:'+rel
            compile((candidate/rel).read_bytes(),rel,'exec')
            st=(live/rel).stat();os.chown(candidate/rel,st.st_uid,st.st_gid)
    for name,db in DBS.items():
        target=directory/(name+'-before.sqlite3');backup_db(Path(db),target)
        manifest['database_backups'][name]=str(target)
    save(directory/'manifest.json',manifest)
    # Rehearse lock-scope conversion on the backup. Prove task/receipt facts stay byte-identical.
    rehearsal=directory/'dispatch-rehearsal.sqlite3';shutil.copy2(directory/'dispatch-before.sqlite3',rehearsal)
    code='''import sqlite3,hashlib,json,sys
from features.pipeline_dispatch.core import DispatchStore
p=sys.argv[1]
def facts():
 c=sqlite3.connect(p)
 try:return {t:hashlib.sha256(repr(c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall()).encode()).hexdigest() for t in ['pipeline_job','pipeline_claim_history','pipeline_operation','pipeline_publication']}
 finally:c.close()
before=facts();DispatchStore(p);DispatchStore(p);assert before==facts();print('task_claim_operation_publication_facts_preserved')
'''
    proof=run([sys.executable,'-c',code,rehearsal],cwd=directory/'candidates/coordinator')
    (directory/'rehearsal.log').write_text(proof+'\n')
    print(json.dumps({'prepared':str(directory),'rehearsal':proof}),flush=True)

def handlers(process):
    rows=run(['ss','-Hntp']).splitlines()
    listeners=run(['ss','-Hlntp']).splitlines()
    return [len(list(Path('/proc',process,'task').iterdir())),
            sum(('pid='+process+',') in r for r in rows),
            sum(int(r.split()[1]) for r in listeners if ('pid='+process+',') in r)]

def refine_fb(source,commit,directory):
    source,directory=Path(source).resolve(),Path(directory).resolve()
    assert run(['git','-C','/mnt/data-disk/pipeline-dispatch/github','rev-parse',commit+'^{commit}'])==commit
    path=directory/'manifest.json';m=json.loads(path.read_text());item=m['lanes']['fb']
    assert item['state']=='prepared' and str(Path(LANES['fb'][0]).resolve())==item['before_root']
    rel='features/fb_video_publish/guards.py';target=Path(item['candidate'])/rel
    assert sha(target)==item['files'][rel]['after']
    compile((source/rel).read_bytes(),rel,'exec');copy_file(source/rel,target)
    item['files'][rel]['after']=sha(target);item['followup_commit']=commit;save(path,m)
    print(json.dumps({'refined':'fb','commit':commit}),flush=True)

def health(port,maintenance_triggers=(),wait_seconds=15):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    until=time.monotonic()+wait_seconds
    while True:
        try:
            with opener.open('http://127.0.0.1:%s/health'%port,timeout=2) as r:
                if r.status==200:return
        except urllib.error.HTTPError as exc:
            # TT reports 503 while its scheduler wakeups are deliberately
            # stopped for this switch. Only those exact expected failures may
            # pass the process probe; full readiness is checked after restore.
            if exc.code==503 and maintenance_triggers:
                try:
                    body=json.loads(exc.read())
                    problems=body.get('automation',{}).get('problems',[])
                    expected={name+':inactive' for name in maintenance_triggers}
                    if problems and set(problems)<=expected:return
                except (ValueError,AttributeError,TypeError):pass
        except OSError:pass
        if time.monotonic()>until:raise RuntimeError('health_failed:'+str(port))
        time.sleep(.25)

def idle_for_switch(lane,units,processes,state):
    if lane!='fb':return all(state[u]==[threads,0,0] for u,_,threads in units)
    for u,_,_ in units:
        if state[u]==[1,0,0]:continue
        if state[u]!=[3,0,0]:return False
        # Native FB has two permanent readback workers. Preserve any current
        # lookup; switch only once both workers are waiting on their queue.
        spy='/mnt/data-disk/publish-queue-isolation-20261105/diagnostic-tools/bin/py-spy'
        stacks=run([spy,'dump','--pid',processes[u],'--nonblocking'])
        workers=[part for part in stacks.split('\nThread ') if '"fb-readback-' in part]
        if len(workers)!=2 or not all('get (queue.py:' in part for part in workers):return False
    return True

def rollback_queue_scopes(database):
    """Project only lock views for old coordinator code, preserving all facts."""
    with contextlib.closing(sqlite3.connect(str(database),timeout=10)) as c, c:
        c.row_factory=sqlite3.Row;c.execute('BEGIN IMMEDIATE')
        assert not c.execute("SELECT 1 FROM pipeline_job WHERE channel IN ('x','tt') AND status IN ('running','legacy_running') LIMIT 1").fetchone(),'rollback_requires_x_tt_claims_to_finish'
        rows=c.execute("SELECT * FROM pipeline_job WHERE channel IN ('x','tt') AND id IN (SELECT job_id FROM pipeline_resource UNION SELECT job_id FROM pipeline_legacy_resource)").fetchall()
        for row in rows:
            held=c.execute('SELECT * FROM pipeline_resource WHERE job_id=?',(row['id'],)).fetchall()
            legacy=c.execute('SELECT 1 FROM pipeline_legacy_resource WHERE job_id=?',(row['id'],)).fetchone()
            for table in ('pipeline_resource','pipeline_legacy_resource'):c.execute('DELETE FROM '+table+' WHERE job_id=?',(row['id'],))
            for resource in json.loads(row['resource_keys_json']):
                if held:c.execute('INSERT OR IGNORE INTO pipeline_resource VALUES(?,?,?,?,?)',(resource,row['id'],row['generation'],int(row['status']=='unknown'),held[0]['legacy']))
                if legacy or row['status']=='unknown':c.execute('INSERT OR IGNORE INTO pipeline_legacy_resource VALUES(?,?)',(resource,row['id']))

def switch(directory,lane,rollback=False,*,preserve_queue_scopes=False):
    path=directory/'manifest.json';m=json.loads(path.read_text());item=m['lanes'][lane]
    link=Path(LANES[lane][0]);old=Path(item['candidate'] if rollback else item['before_root'])
    target=Path(item['before_root'] if rollback else item['candidate'])
    if lane=='app':
        # These copies are not the active X writer. Main API proxies to 8810;
        # updating its source copies requires no main API or worker restart.
        for rel,h in item['files'].items():
            destination=link/rel;expected=h['after'] if rollback else h['before']
            assert sha(destination)==expected,'affected_source_drift:'+rel
            src=directory/'before'/lane/rel if rollback else target/rel
            copy_file(src,destination)
        item['state']='rolled_back' if rollback else 'installed';save(path,m)
        print(json.dumps({'lane':lane,'state':item['state'],'restart':False}),flush=True);return
    if link.resolve()==target:
        for rel,h in item['files'].items():assert sha(link/rel)==h['before' if rollback else 'after']
        for _,port,_ in LANES[lane][1]:health(port)
        item['state']='rolled_back' if rollback else 'active'
        item['active_pids']={u:pid(u) for u,_,_ in LANES[lane][1]};save(path,m)
        print(json.dumps({'lane':lane,'state':'already_active_verified'}),flush=True);return
    assert link.resolve()==old,'later_runtime_switch_preserved:'+lane
    if rollback and lane=='coordinator' and not preserve_queue_scopes:
        with contextlib.closing(sqlite3.connect('file:'+DBS['dispatch']+'?mode=ro',uri=True)) as c:
            assert not c.execute("SELECT 1 FROM pipeline_job WHERE channel IN ('x','tt') AND status IN ('running','legacy_running') LIMIT 1").fetchone(),'rollback_requires_x_tt_claims_to_finish'
    for rel,h in item['files'].items():assert sha(link/rel)==h['after' if rollback else 'before'],'affected_source_drift:'+rel
    units=LANES[lane][1];processes={u:pid(u) for u,_,_ in units}
    paths=set();triggers=set()
    for u,_,_ in units:
        user=run(['systemctl','show',u,'-p','User','--value']) or 'root'
        for rel in item['files']:run(['runuser','-u',user,'--','test','-r',target/rel])
        for dependent in run(['systemctl','show',u,'-p','RequiredBy','--value']).split():
            if dependent.endswith('.path') and run(['systemctl','show',dependent,'-p','ActiveState','--value'])=='active':paths.add(dependent)
            for trigger in run(['systemctl','show',dependent,'-p','TriggeredBy','--value']).split():
                if trigger.endswith(('.path','.timer')) and run(['systemctl','show',trigger,'-p','ActiveState','--value'])=='active':triggers.add(trigger)
    triggers.update(paths)
    item['active_dependent_paths']=sorted(paths);save(path,m)
    marker='queue-isolation-'+lane+'-'+str(int(time.time()))
    rules=[];stopped=[];swapped=False;observations=[];began=time.monotonic()
    drain_seconds=int(os.environ.get('QUEUE_ISOLATION_DRAIN_SECONDS','20'))
    assert 20<=drain_seconds<=300
    def release_network_gate():
        while rules:
            rule,watchdog=rules.pop()
            run(['iptables','-D','INPUT']+rule)
            subprocess.run(['systemctl','stop',watchdog+'.timer'],capture_output=True,timeout=10)
    try:
        # Stop only wakeups, not their already-running execution services.
        if triggers:run(['systemctl','stop',*sorted(triggers)],timeout=20)
        for u,port,threads in units:
            rule=['-p','tcp','--dport',str(port),'-m','conntrack','--ctstate','NEW','-m','comment','--comment',marker,'-j','DROP']
            watchdog=marker+'-'+str(port)
            run(['systemd-run','--quiet','--unit='+watchdog,'--on-active='+str(drain_seconds+45)+'s','--','/usr/sbin/iptables','-D','INPUT']+rule)
            run(['iptables','-I','INPUT','1']+rule);rules.append((rule,watchdog))
        deadline=time.monotonic()+drain_seconds
        while True:
            state={u:handlers(processes[u]) for u,_,_ in units};observations.append(state)
            if idle_for_switch(lane,units,processes,state):break
            if time.monotonic()>deadline:
                raise RuntimeError('affected_http_handlers_still_active:'+lane)
            time.sleep(.25)
        stopped=[u for u,_,_ in units]
        # Avoid Requires= propagation stopping callers with in-flight work.
        if stopped:run(['systemctl','--job-mode=ignore-dependencies','stop',*stopped],timeout=20)
        for u in stopped:assert pid(u)=='0'
        assert link.resolve()==old,'concurrent_runtime_switch'
        if rollback and lane=='coordinator' and not preserve_queue_scopes:rollback_queue_scopes(DBS['dispatch'])
        swap(link,target);swapped=True
        # Startup readiness probes use these same listeners.
        release_network_gate()
        for u,port,_ in units:run(['systemctl','start',u],timeout=15)
        for u,_,_ in units:assert run(['systemctl','is-active',u])=='active'
        for dependent in paths:run(['systemctl','start',dependent],timeout=15)
        for _,port,_ in units:health(port,maintenance_triggers=triggers)
    except BaseException:
        if swapped:
            run(['systemctl','--job-mode=ignore-dependencies','stop',*[u for u,_,_ in units]],timeout=20)
            swap(link,old)
        release_network_gate()
        for u in stopped:run(['systemctl','start',u],timeout=20)
        for dependent in paths:run(['systemctl','start',dependent],timeout=15)
        raise
    finally:
        release_network_gate()
        for trigger in triggers:run(['systemctl','start',trigger],timeout=15)
        save(directory/(lane+('-rollback' if rollback else '')+'-drain.json'),
             {'seconds':time.monotonic()-began,'observations':observations,'temporary_rules_removed':marker not in run(['iptables','-S','INPUT'])})
    item['state']='rolled_back' if rollback else 'active';item['active_pids']={u:pid(u) for u,_,_ in units}
    save(path,m);print(json.dumps({'lane':lane,'state':item['state'],'pids':item['active_pids']}),flush=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','activate','rollback','refine-fb']);p.add_argument('args',nargs='+');a=p.parse_args()
    if a.action=='prepare':prepare(*a.args);return
    if a.action=='refine-fb':refine_fb(*a.args);return
    directory=Path(a.args[0]).resolve();lanes=a.args[1:] or list(LANES)
    for lane in lanes:switch(directory,lane,a.action=='rollback')

if __name__=='__main__':main()
