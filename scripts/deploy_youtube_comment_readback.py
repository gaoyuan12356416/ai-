"""Switch only the YouTube bridge, retaining coordinator/node code and claims."""
import argparse,datetime as dt,json,re,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy_youtube_isolation import run,switch_coordinator,BASE,NATIVE
import requests
def main():
    p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--commit');p.add_argument('--rollback');a=p.parse_args()
    root=Path('/opt/pipeline-dispatch/current').resolve();cfg_path=Path('/etc/pipeline-coordinator.json');cfg=json.loads(cfg_path.read_text());driver=next(x for x in cfg['drivers'] if x['channel']=='youtube')
    if a.rollback:
        folder=Path(a.rollback).parent;plan=json.loads(Path(a.rollback).read_text());assert driver['port']==18851
        driver['port']=plan['old_port'];print(json.dumps(switch_coordinator(root,json.dumps(cfg).encode(),folder)));return
    source=Path(a.source).resolve();assert re.fullmatch('[a-f0-9]{40}',a.commit)
    assert run(['git','-C',source,'rev-parse','HEAD'])==a.commit and not run(['git','-C',source,'status','--porcelain'])
    assert run(['git','-C',source,'for-each-ref','--contains',a.commit,'refs/remotes/origin'])
    assert driver['port']==18850
    folder=BASE/('youtube-comment-readback-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));folder.mkdir(mode=0o700)
    (folder/'config-before.json').write_bytes(cfg_path.read_bytes())
    unit=Path('/etc/systemd/system/pipeline-youtube-comment-readback.service');assert not unit.exists()
    command='/usr/bin/python3.9 '+str(source/'scripts/pipeline_youtube_canonical.py')+' --runtime-root '+str(NATIVE)+' --dispatch-root '+str(source)+' --port 18851'
    unit.write_text(re.sub(r'^ExecStart=.*$',lambda _:'ExecStart='+command,Path('/etc/systemd/system/pipeline-youtube-task-isolation.service').read_text(),flags=re.M))
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now',unit.name])
    with requests.Session() as http:
        http.trust_env=False
        for _ in range(50):
            try:
                if http.get('http://127.0.0.1:18851/health',timeout=2).status_code==200:break
            except requests.RequestException:pass
            time.sleep(.2)
        else:raise RuntimeError('bridge_not_ready')
    plan={'commit':a.commit,'old_port':driver['port'],'coordinator':str(root)};(folder/'manifest.json').write_text(json.dumps(plan));driver['port']=18851
    plan['switch']=switch_coordinator(root,json.dumps(cfg).encode(),folder)
    (folder/'manifest.json').write_text(json.dumps(plan));print(json.dumps({'backup':str(folder),**plan}))
if __name__=='__main__':main()
