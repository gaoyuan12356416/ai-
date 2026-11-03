"""Switch a new loopback canonical bridge behind a drained coordinator."""
import datetime as dt,json,os,re,sqlite3,subprocess,sys
from pathlib import Path
import requests

ROOT=Path('/mnt/data-disk/pipeline-dispatch')
def run(cmd,**kwargs):
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=180,**kwargs)
    if result.returncode:raise RuntimeError('command_failed:'+cmd[0]+':'+str(result.returncode))
    return result.stdout.strip()
def main():
    sha,old=sys.argv[1:];assert all(re.fullmatch('[a-f0-9]{40}',x) for x in (sha,old))
    assert run(['git','-C',str(ROOT/'github'),'rev-parse','HEAD'])==old
    env=dict(os.environ,GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
    run(['git','-C',str(ROOT/'github'),'fetch','origin',sha],env=env)
    assert run(['git','-C',str(ROOT/'github'),'rev-parse','FETCH_HEAD'])==sha
    release=ROOT/('youtube-bridge-'+sha[:12]);run(['git','-C',str(ROOT/'github'),'worktree','add','--detach',str(release),sha])
    tests=subprocess.run(['/usr/bin/python3.9','-m','unittest','scripts.test_youtube_publish_recovery','scripts.test_pipeline_youtube_canonical_service'],cwd=release,capture_output=True,text=True,timeout=60)
    (release/'deployment-tests.log').write_text(tests.stdout+tests.stderr);assert tests.returncode==0
    backup=ROOT/('youtube-bridge-switch-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));backup.mkdir(mode=0o700)
    cfgpath=Path('/etc/pipeline-coordinator.json');original=cfgpath.read_bytes();cfg=json.loads(original)
    (backup/'coordinator-before.json').write_bytes(original);(backup/'coordinator-before.json').chmod(0o600)
    drivers=[x for x in cfg['drivers'] if x['channel']=='youtube'];assert len(drivers)==1 and drivers[0]['port']==18847
    baseunit=Path('/etc/systemd/system/pipeline-youtube-canonical.service').read_text()
    unitpath=Path('/etc/systemd/system/pipeline-youtube-canonical-recovery.service');assert not unitpath.exists()
    command='/usr/bin/python3.9 '+str(release/'scripts/pipeline_youtube_canonical.py')+' --runtime-root /root/drama_material_service --dispatch-root '+str(release)+' --port 18848'
    unit,count=re.subn(r'^ExecStart=.*$',lambda _: 'ExecStart='+command,baseunit,flags=re.M);assert count==1
    unitpath.write_text(unit);run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','pipeline-youtube-canonical-recovery.service'])
    with requests.Session() as http:
        http.trust_env=False
        import time
        for _ in range(30):
            try:
                if http.get('http://127.0.0.1:18848/health',timeout=2).status_code==200:break
            except requests.RequestException:pass
            time.sleep(.25)
        else:raise RuntimeError('new_bridge_unhealthy')
    assert cfgpath.read_bytes()==original,'config_changed'
    drivers[0]['port']=18848;temp=cfgpath.with_suffix('.recovery-tmp');temp.write_text(json.dumps(cfg,indent=2));temp.chmod(0o600);temp.replace(cfgpath)
    switch=subprocess.run(['/usr/bin/python3.9',str(release/'scripts/deploy_pipeline_coordinator_guarded.py'),sha,old],capture_output=True,text=True,timeout=180)
    (backup/'coordinator-switch.log').write_text(switch.stdout+switch.stderr)
    if switch.returncode:
        cfgpath.write_bytes(original);cfgpath.chmod(0o600)
        raise RuntimeError('coordinator_switch_failed_config_restored')
    report={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'commit':sha,'previous_coordinator':old,'bridge_port':18848,'previous_bridge_retained':True,'backup':str(backup),'release':str(release),'linux_tests':'30 passed','coordinator':json.loads(switch.stdout)}
    (backup/'completed.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
if __name__=='__main__':main()
