"""Scoped GitHub release: preserve in-flight workers and drain only RPC handlers."""
import argparse, ast, contextlib, datetime as dt, hashlib, json, os, re, shutil, signal, sqlite3, subprocess, sys, time
from pathlib import Path
import requests
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

BASE=Path('/mnt/data-disk/pipeline-dispatch')
NATIVE=Path('/root/drama_material_service')
NODE=Path('/mnt/data-disk/pipeline-dispatch-runtime/fb-lease-f3bd38531f6d/node')
COORD=Path('/mnt/data-disk/pipeline-dispatch-runtime/fb-lease-f3bd38531f6d/coordinator')
EXEC='features/pipeline_dispatch/executors.py'
ENGINE='features/youtube_auto_publish/engine.py'
EXPECTED={'features/pipeline_dispatch/core.py':'e9fbbd04e94a1b5812f7f473742387618ac055871721849e66256b1d3e205ab2',
          'features/pipeline_dispatch/adapters.py':'23d318f0196477a97716d1fde492687bb33501417f61755290701f016baae89f',
          ENGINE:'6d3deecb5364d670430641e02245c3436274978a3f1b33714bde7e75b0709737'}

def run(args,timeout=60,**kwargs):
    p=subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=timeout,**kwargs)
    if p.returncode:raise RuntimeError('command_failed:'+str(args[0])+':'+str(p.returncode))
    return p.stdout.strip()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.youtube-next');tmp.write_bytes(data)
    if path.exists():shutil.copystat(path,tmp);os.chown(tmp,path.stat().st_uid,path.stat().st_gid)
    tmp.replace(path)
def snapshot(source,target):
    with contextlib.closing(sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True)) as c,contextlib.closing(sqlite3.connect(target)) as d:c.backup(d,pages=1000)
    target.chmod(0o600)
def pid(unit):return int(run(['systemctl','show',unit,'-p','MainPID','--value']))
def claims():
    with contextlib.closing(sqlite3.connect('file:'+str(BASE/'dispatch.sqlite3')+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        return [dict(r) for r in c.execute("SELECT id,source_key,channel,node_id,generation,worker_id,status FROM pipeline_job WHERE status='running'")]
def drain_publish(node,unit,pids=None):
    parent=pid(unit)
    if pids is None:
        with contextlib.closing(sqlite3.connect('file:'+str(BASE/'dispatch.sqlite3')+'?mode=ro',uri=True)) as c:
            pids=[r[0] for r in c.execute("SELECT pid FROM pipeline_worker WHERE node_id=? AND stage='publish' AND draining=0",(node,))]
    signalled=[]
    for child in sorted(set(pids)):
        try:
            stat=Path('/proc/'+str(child)+'/stat').read_text().split(') ',1)[1].split()
            if int(stat[1])!=parent:continue
            os.kill(child,signal.SIGTERM);signalled.append(child)
        except FileNotFoundError:pass
    return {'parent_pid':parent,'drained_publish_pids':signalled}
def patched_executor(live,source):
    def span(text):
        tree=ast.parse(text)
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='NodeExecutors')
        f=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='youtube_publish')
        return f.lineno-1,f.end_lineno
    a=live.read_text();b=source.read_text();start,end=span(a);s,e=span(b)
    return ''.join(a.splitlines(True)[:start]+b.splitlines(True)[s:e]+a.splitlines(True)[end:]).encode()
def switch_coordinator(target,config_bytes,folder):
    from scripts.deploy_pipeline_coordinator_guarded import handlers_drained
    unit='pipeline-coordinator.service';oldpid=str(pid(unit));link=Path('/opt/pipeline-dispatch/current')
    previous=link.resolve();cfg=Path('/etc/pipeline-coordinator.json');old_config=cfg.read_bytes()
    tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ');marker='yt-rpc-'+tag
    rule=['-p','tcp','--dport','18846','-m','conntrack','--ctstate','NEW','-m','comment','--comment',marker,'-j','DROP']
    watchdog='yt-rpc-gate-'+tag.lower();gated=False;changed=False
    run(['systemd-run','--quiet','--unit='+watchdog,'--on-active=55s','--','/usr/sbin/iptables','-D','INPUT',*rule])
    began=time.monotonic()
    try:
        run(['iptables','-I','INPUT','1',*rule]);gated=True
        deadline=time.monotonic()+25
        while True:
            observed=handlers_drained(oldpid)
            if observed=={'threads':2,'connected_sockets':0,'accept_queue':0}:break
            if time.monotonic()>deadline:raise RuntimeError('rpc_handlers_still_active')
            time.sleep(.2)
        before=claims();(folder/'claims-before-switch.json').write_text(json.dumps(before))
        run(['systemctl','stop',unit],timeout=10)
        tmp=link.with_name('current.youtube-next');tmp.symlink_to(target);tmp.replace(link)
        write(cfg,config_bytes);changed=True
        run(['systemctl','start',unit],timeout=10)
        with requests.Session() as http:
            http.trust_env=False
            # Existing connections are drained. Probe the listener through /proc
            # while new SYNs are buffered by the temporary gate.
            deadline=time.monotonic()+10
            while not any(len(parts)>3 and parts[1].endswith(':499E') and parts[3]=='0A' for parts in (line.split() for line in Path('/proc/net/tcp').read_text().splitlines())):
                if time.monotonic()>deadline:raise RuntimeError('listener_not_ready')
                time.sleep(.1)
    except BaseException:
        if changed:
            run(['systemctl','stop',unit],timeout=10)
            tmp=link.with_name('current.youtube-next');tmp.symlink_to(previous);tmp.replace(link)
            write(cfg,old_config);run(['systemctl','start',unit],timeout=10)
        raise
    finally:
        if gated:run(['iptables','-D','INPUT',*rule])
        run(['systemctl','stop',watchdog+'.timer'])
    return {'old_pid':int(oldpid),'new_pid':pid(unit),'gate_seconds':round(time.monotonic()-began,3),'previous_root':str(previous)}

def main():
    p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--commit');p.add_argument('--hk-node',action='store_true');p.add_argument('--worker-pids',default='');p.add_argument('--rollback');a=p.parse_args()
    if a.rollback:
        manifest=Path(a.rollback);plan=json.loads(manifest.read_text());folder=manifest.parent
        for record in plan['installed']:
            assert sha(record['path'])==record['after'],'later_source_drift'
        for record in plan['installed']:write(record['path'],Path(record['backup']).read_bytes())
        if not plan.get('hk_node'):
            cfg=json.loads(Path('/etc/pipeline-coordinator.json').read_text())
            driver=next(x for x in cfg['drivers'] if x['channel']=='youtube');assert driver['port']==18849
            driver['port']=plan['old_bridge_port']
            switch_coordinator(Path(plan['previous_coordinator']),json.dumps(cfg).encode(),folder)
            print(json.dumps(drain_publish('cpu-main','pipeline-node-cpu-main.service')))
        else:print(json.dumps(drain_publish('hk-gpu','pipeline-node-hk-gpu.service',[int(x) for x in a.worker_pids.split(',') if x])))
        print(json.dumps({'rolled_back':True,'ledgers_preserved':True}));return
    source=Path(a.source).resolve();assert re.fullmatch('[a-f0-9]{40}',a.commit)
    assert run(['git','-C',source,'rev-parse','HEAD'])==a.commit
    assert not run(['git','-C',source,'status','--porcelain'])
    assert run(['git','-C',source,'for-each-ref','--contains',a.commit,'refs/remotes/origin'])
    tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if a.hk_node:
        node=Path(run(['systemctl','show','pipeline-node-hk-gpu.service','-p','WorkingDirectory','--value'])).resolve()
        assert str(node).startswith(('/data/pipeline-dispatch-runtime/','/opt/pipeline-dispatch/releases/'))
        folder=Path('/data/pipeline-dispatch-runtime')/('youtube-fix-'+tag)
    else:
        assert run(['findmnt','-no','UUID','/mnt/data-disk'])=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
        node=NODE;folder=BASE/('youtube-isolation-'+tag)
    folder.mkdir(mode=0o700);installed=[]
    def install(target,data):
        backup=folder/'backups'/str(len(installed));backup.parent.mkdir(exist_ok=True)
        backup.write_bytes(target.read_bytes());before=sha(target);write(target,data)
        installed.append({'path':str(target),'backup':str(backup),'before':before,'after':sha(target)})
    assert sha(node/ENGINE)==EXPECTED[ENGINE]
    assert sha(node/EXEC) in {'11b1bd6f4fa4ebdf16dbfdbfbb66ffc90bcc17da16797dd85c8a0f793b75dcad','36ef4b600521ef2f4c19c04100bd40723f1b800c1f2c7901c183bca9bb428f90','f5c2ac68be92dbe9eccefa33de9778b4d09ac78cebe6305c9b11dc03971e7cb8'}
    if a.hk_node:
        install(node/EXEC,patched_executor(node/EXEC,source/EXEC));install(node/ENGINE,(source/ENGINE).read_bytes())
        plan={'commit':a.commit,'hk_node':True,'installed':installed,'reload':drain_publish('hk-gpu','pipeline-node-hk-gpu.service',[int(x) for x in a.worker_pids.split(',') if x])}
    else:
        assert Path('/opt/pipeline-dispatch/current').resolve()==COORD
        for rel in ('features/pipeline_dispatch/core.py','features/pipeline_dispatch/adapters.py'):assert sha(COORD/rel)==EXPECTED[rel]
        assert sha(NATIVE/ENGINE)==EXPECTED[ENGINE]
        snapshot(BASE/'dispatch.sqlite3',folder/'dispatch-before.sqlite3');snapshot(NATIVE/'data/drama_material_jobs.sqlite3',folder/'native-before.sqlite3')
        candidate=folder/'coordinator';shutil.copytree(COORD,candidate,ignore=shutil.ignore_patterns('__pycache__','.git'))
        for rel in ('features/pipeline_dispatch/core.py','features/pipeline_dispatch/adapters.py','features/pipeline_dispatch/youtube_isolation.py','features/pipeline_dispatch/youtube_readback.py'):
            shutil.copy2(source/rel,candidate/rel)
        install(NATIVE/ENGINE,(source/ENGINE).read_bytes())
        unit=Path('/etc/systemd/system/pipeline-youtube-isolation.service');assert not unit.exists()
        original=Path('/etc/systemd/system/pipeline-youtube-canonical.service').read_text()
        command='/usr/bin/python3.9 '+str(source/'scripts/pipeline_youtube_canonical.py')+' --runtime-root '+str(NATIVE)+' --dispatch-root '+str(source)+' --port 18849'
        unit.write_text(re.sub(r'^ExecStart=.*$',lambda _: 'ExecStart='+command,original,flags=re.M))
        run(['systemctl','daemon-reload']);run(['systemctl','enable','--now',unit.name])
        with requests.Session() as http:
            http.trust_env=False
            for _ in range(40):
                try:
                    if http.get('http://127.0.0.1:18849/health',timeout=2).status_code==200:break
                except requests.RequestException:pass
                time.sleep(.25)
            else:raise RuntimeError('new_bridge_unhealthy')
        config_path=Path('/etc/pipeline-coordinator.json');cfg=json.loads(config_path.read_text())
        (folder/'coordinator-before.json').write_bytes(config_path.read_bytes())
        driver=next(x for x in cfg['drivers'] if x['channel']=='youtube');old_port=driver['port'];assert old_port==18848;driver['port']=18849
        # Publish children load updated modules only after their present task
        # returns. Preparation children and the node supervisor stay alive.
        install(node/EXEC,patched_executor(node/EXEC,source/EXEC));install(node/ENGINE,(source/ENGINE).read_bytes())
        plan={'commit':a.commit,'installed':installed,'previous_coordinator':str(COORD),'old_bridge_port':old_port,'coordinator':str(candidate)}
        (folder/'manifest.json').write_text(json.dumps(plan,indent=2))
        plan['switch']=switch_coordinator(candidate,json.dumps(cfg,indent=2).encode(),folder)
        plan['reload']=drain_publish('cpu-main','pipeline-node-cpu-main.service')
        plan['claims_after']=claims()
    (folder/'manifest.json').write_text(json.dumps(plan,indent=2))
    print(json.dumps({'backup':str(folder),'manifest':str(folder/'manifest.json'),**plan}),flush=True)

if __name__=='__main__':main()
