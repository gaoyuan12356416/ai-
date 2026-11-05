"""Scoped GitHub-first install. Rollback restores code only, never ledgers."""
import contextlib
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
import urllib.request

MAIN=Path('/root/drama_material_service')
REPO=Path('/mnt/data-disk/pipeline-dispatch/github')
DBS={'x':Path('/var/lib/x-post-automation/accounts.sqlite3'),'dispatch':Path('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')}
FILES=['app.py','features/x_posts/log_query.py','features/x_posts/log_management.py','static/x-post-logs.html','static/x-post-logs.js']

def run(args,**kw):
    return subprocess.run(list(map(str,args)),check=True,capture_output=True,text=True,timeout=60,**kw).stdout.strip()

def sha(p):
    return hashlib.sha256(Path(p).read_bytes().replace(b'\r\n',b'\n')).hexdigest()

def save(p,value):
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');p.chmod(0o600)

def backup(db,dest):
    with contextlib.closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)) as source:
        with contextlib.closing(sqlite3.connect(dest)) as target:
            source.backup(target)
            assert target.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    dest.chmod(0o600)

def prepare(source,commit,directory):
    source,directory=Path(source).resolve(),Path(directory).resolve()
    assert str(directory).startswith('/mnt/data-disk/x-post-log-controls-20261105/')
    assert run(['findmnt','-n','-o','UUID','/mnt/data-disk'])=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert run(['git','-C',REPO,'rev-parse',commit+'^{commit}'])==commit
    assert sha(MAIN/'app.py')=='6c79a8ef4c7a4701e7d4a17bcf80e2e634e7f106c3f8f4168f33bae34ebc1d90','main_api_baseline_changed'
    assert sha(MAIN/'static/x-post-logs.html')=='7bb07e5c73ea63541cdbef339ba22f7e8f5aed61f77b871485ce78a78353618b','page_baseline_changed'
    directory.mkdir(parents=True,exist_ok=False)
    manifest={'source':str(source),'commit':commit,'files':[],'pids':{u:run(['systemctl','show',u,'-p','MainPID','--value']) for u in ['drama-material-api.service','x-post-automation.service','pipeline-coordinator.service']}}
    destinations=[(MAIN/rel,source/rel) for rel in FILES]
    for static in [Path('/usr/share/nginx/html'),Path('/opt/x-post-automation/current/static').resolve()]:
        destinations += [(static/name,source/'static'/name) for name in ['x-post-logs.html','x-post-logs.js']]
    for index,(target,new) in enumerate(destinations):
        assert new.is_file()
        old=directory/'before'/str(index);old.parent.mkdir(exist_ok=True)
        exists=target.exists()
        if exists:shutil.copy2(target,old)
        st=target.stat() if exists else target.parent.stat()
        manifest['files'].append({'target':str(target),'source':str(new),'backup':str(old),'existed':exists,'before':sha(target) if exists else None,'after':sha(new),'uid':st.st_uid,'gid':st.st_gid,'mode':st.st_mode&0o777 if exists else 0o644})
    for rel in FILES:
        if rel.endswith('.py'):compile((source/rel).read_bytes(),rel,'exec')
    for name,db in DBS.items():backup(db,directory/(name+'-before.sqlite3'))
    save(directory/'manifest.json',manifest)
    print(json.dumps({'prepared':str(directory),'commit':commit}),flush=True)

def rehearse(source,directory):
    directory=Path(directory)
    sys.path.insert(0,str(source))
    from features.x_posts.log_management import LogManager
    from features.pipeline_dispatch.adapters import LedgerAdapter,default_specs
    for name in DBS:shutil.copy2(directory/(name+'-before.sqlite3'),directory/(name+'-test.sqlite3'))
    manager=LogManager(directory/'x-test.sqlite3',directory/'dispatch-test.sqlite3')
    adapter=LedgerAdapter(default_specs({'x':directory/'x-test.sqlite3'})[0],driver=object())
    # All changes below target disposable database copies only.
    actor={'tenant_key':'offline','user_id':'deployment-rehearsal','name':'offline'}
    read=manager.query({'page_size':100})
    report={'query_count':read['pagination']['total'],'manual_count':manager.query({'task_source':'manual_publish'})['pagination']['total'],'reset':[],'stop_resume':[]}
    with contextlib.closing(manager.connect(readonly=True)) as c:
        immutable={t:hashlib.sha256(repr(c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall()).encode()).hexdigest() for t in ['pipeline_x_operation','pipeline_x_checkpoint','dispatch.pipeline_operation','dispatch.pipeline_receipt','dispatch.pipeline_publication']}
        ids=[r[0] for r in c.execute("SELECT id FROM x_post_queue WHERE status IN ('failed','queued','waiting_relay') ORDER BY id DESC LIMIT 500")]
    for qid in ids:
        with contextlib.closing(manager.connect(readonly=True)) as c:
            s=manager.snapshot(c,qid);version=manager.version(s)
        failed=s['queue']['status']=='failed'
        bucket='reset' if failed else 'stop_resume'
        if len(report[bucket])>=12 or manager.reason(s,'reset' if failed else 'stop'):continue
        before_key={k:s['queue'][k] for k in s['queue'] if k not in {'status','updated_at'}}
        payload={'operation_id':'rehearsal_'+str(qid)+'_first','action':'reset' if failed else 'stop','items':[{'queue_id':qid,'version':version}]}
        answer=manager.apply(payload,actor);assert answer['succeeded']==1,answer
        if not failed:
            with contextlib.closing(manager.connect(readonly=True)) as c:version=manager.version(manager.snapshot(c,qid))
            answer=manager.apply({'operation_id':'rehearsal_'+str(qid)+'_resume','action':'reset','items':[{'queue_id':qid,'version':version}]},actor)
            assert answer['succeeded']==1,answer
        with contextlib.closing(manager.connect(readonly=True)) as c:
            after=manager.snapshot(c,qid)
        assert before_key=={k:after['queue'][k] for k in before_key},'frozen_identity_changed'
        projected=adapter.project(adapter.read(qid))
        assert not projected['reconcile_only'] and not projected['unknown_outcome'],projected['source_key']
        report[bucket].append({'queue_id':qid,'source':s['queue']['source_type'],'manual':bool(s['queue']['manual_run_id']),'stage':projected['stage']})
    with contextlib.closing(manager.connect(readonly=True)) as c:
        for t,h in immutable.items():assert hashlib.sha256(repr(c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall()).encode()).hexdigest()==h,t
    assert report['reset'] and report['stop_resume']
    report['receipt_and_operation_history_unchanged']=True
    save(directory/'rehearsal.json',report)
    print(json.dumps({k:len(v) if isinstance(v,list) else v for k,v in report.items()}),flush=True)

def copy(entry,rollback=False):
    dest=Path(entry['target']);source=Path(entry['backup'] if rollback else entry['source'])
    if rollback and not entry['existed']:
        # New modules are inert after the old app entrypoint is restored. Keep
        # them for audit instead of deleting a module another process may use.
        return
    tmp=dest.with_name(dest.name+'.x-log-next')
    assert not tmp.exists()
    shutil.copy2(source,tmp);os.chown(tmp,entry['uid'],entry['gid']);tmp.chmod(entry['mode']);os.replace(tmp,dest)

def activate(directory,rollback=False):
    directory=Path(directory);m=json.loads((directory/'manifest.json').read_text())
    if not rollback:assert (directory/'rehearsal.json').is_file()
    for entry in m['files']:
        expected=entry['after'] if rollback else entry['before']
        if expected is not None:assert sha(entry['target'])==expected,'concurrent_edit:'+entry['target']
    unit='drama-material-api.service'
    pid=run(['systemctl','show',unit,'-p','MainPID','--value'])
    marker='x-log-api-switch-'+str(int(time.time()))
    rule=['-p','tcp','--dport','8787','-m','conntrack','--ctstate','NEW','-m','comment','--comment',marker,'-j','DROP']
    observations=[];gated=False;changed=False
    def release():
        nonlocal gated
        if gated:
            run(['iptables','-D','INPUT']+rule);gated=False
        subprocess.run(['systemctl','stop',marker+'.timer'],capture_output=True,timeout=10)
    try:
        run(['systemd-run','--quiet','--unit='+marker,'--on-active=90s','--','/usr/sbin/iptables','-D','INPUT']+rule)
        run(['iptables','-I','INPUT','1']+rule);gated=True
        deadline=time.monotonic()+40
        while True:
            connections=[r for r in run(['ss','-Hntp']).splitlines() if ('pid='+pid+',') in r]
            threads=len(list(Path('/proc',pid,'task').iterdir()))
            listeners=[r for r in run(['ss','-Hlntp']).splitlines() if ('pid='+pid+',') in r]
            backlog=sum(int(r.split()[1]) for r in listeners)
            state={'threads':threads,'connections':len(connections),'backlog':backlog};observations.append(state)
            # Six known persistent threads are idle in the verified stack dump;
            # additional HTTP/job threads or external connections must drain.
            if threads<=6 and not connections and not backlog:break
            if time.monotonic()>=deadline:raise RuntimeError('main_api_still_busy')
            time.sleep(.25)
        # The dump records no locals/credentials and must show only idle workers.
        spy=directory.parent/'tools/bin/py-spy'
        assert spy.is_file(),'verified_stack_reader_required'
        stacks=run([spy,'dump','--pid',pid])
        stack_file=directory/('rollback-stacks.txt' if rollback else 'stacks.txt')
        stack_file.write_text(stacks);stack_file.chmod(0o600)
        thread_stacks=re.findall(r'Thread [^\n]+\n((?:    [^\n]+\n?)+)',stacks)
        assert thread_stacks and len(thread_stacks)<=6,'unexpected_background_threads'
        assert all(s.lstrip().startswith(('wait (','select (','_worker (')) for s in thread_stacks),'active_background_writer'
        # Source comparison is repeated inside the brief entrance hold.
        for entry in m['files']:
            expected=entry['after'] if rollback else entry['before']
            if expected is not None:assert sha(entry['target'])==expected,'concurrent_edit:'+entry['target']
        run(['systemctl','--job-mode=ignore-dependencies','stop',unit])
        changed=True
        for entry in m['files']:copy(entry,rollback)
        release()
        run(['systemctl','start',unit])
        deadline=time.monotonic()+30
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8787/api/ui/topbar',timeout=3) as response:assert response.status==200
                break
            except Exception:
                if time.monotonic()>deadline:raise
                time.sleep(.5)
    except BaseException:
        if changed and not rollback:
            subprocess.run(['systemctl','--job-mode=ignore-dependencies','stop',unit],capture_output=True,timeout=30)
            for entry in m['files']:copy(entry,True)
        release();subprocess.run(['systemctl','start',unit],capture_output=True,timeout=30)
        raise
    finally:
        release();save(directory/('rollback-switch.json' if rollback else 'switch.json'),{'observations':observations,'gate_removed':True})
    m['state']='rolled_back' if rollback else 'active'
    m['after_pids']={u:run(['systemctl','show',u,'-p','MainPID','--value']) for u in m['pids']}
    for u in ['x-post-automation.service','pipeline-coordinator.service']:assert m['after_pids'][u]==m['pids'][u],u
    save(directory/'manifest.json',m)
    print(json.dumps({'state':m['state'],'commit':m['commit'],'pids':m['after_pids']}),flush=True)

if __name__=='__main__':
    action=sys.argv[1]
    if action=='prepare':prepare(*sys.argv[2:])
    elif action=='rehearse':rehearse(*sys.argv[2:])
    else:activate(sys.argv[2],rollback=action=='rollback')
