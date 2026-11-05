"""Install task-only YouTube isolation while retaining existing claim identities."""
import argparse, datetime as dt, json, os, re, shutil, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy_youtube_isolation import run,sha,write,snapshot,pid,switch_coordinator,BASE,NATIVE
import requests

def reload_planner():
    unit='youtube-auto-publish-worker.service';old=pid(unit)
    # The existing SIGTERM handler finishes its claimed enqueue work. Sending
    # the signal directly avoids systemd's bounded stop timeout and SIGKILL.
    run(['systemctl','kill','--kill-who=main','--signal=SIGTERM',unit])
    deadline=time.monotonic()+55
    while time.monotonic()<deadline:
        new=pid(unit)
        if new and new!=old:return {'old_pid':old,'new_pid':new}
        time.sleep(.5)
    return {'old_pid':old,'draining':True}

def main():
    p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--commit');p.add_argument('--rollback');a=p.parse_args()
    if a.rollback:
        manifest=Path(a.rollback);plan=json.loads(manifest.read_text());folder=manifest.parent
        with sqlite3.connect(BASE/'dispatch.sqlite3') as c:
            assert not c.execute("SELECT 1 FROM pipeline_job WHERE channel='youtube' AND stage='publish' AND status='running'").fetchone(),'wait_for_existing_youtube_claims_to_finish'
        with sqlite3.connect(NATIVE/'data/drama_material_jobs.sqlite3') as c:
            assert not c.execute("SELECT 1 FROM drama_youtube_publish WHERE lease_owner<>''").fetchone(),'wait_for_existing_youtube_native_owners'
        assert sha(NATIVE/'features/drama_synthesis/core.py')==plan['native_after']
        cfg=json.loads(Path('/etc/pipeline-coordinator.json').read_text())
        driver=next(x for x in cfg['drivers'] if x['channel']=='youtube');assert driver['port']==18850
        driver['port']=plan['previous_bridge_port']
        write(NATIVE/'features/drama_synthesis/core.py',(folder/'native-core-before.py').read_bytes())
        switch_coordinator(Path(plan['previous_coordinator']),json.dumps(cfg).encode(),folder)
        print(json.dumps({'rollback':True,'planner':reload_planner(),'ledgers_preserved':True}));return
    source=Path(a.source).resolve();assert re.fullmatch('[a-f0-9]{40}',a.commit)
    assert run(['git','-C',source,'rev-parse','HEAD'])==a.commit
    assert not run(['git','-C',source,'status','--porcelain'])
    assert run(['git','-C',source,'for-each-ref','--contains',a.commit,'refs/remotes/origin'])
    assert run(['findmnt','-no','UUID','/mnt/data-disk'])=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    previous=Path('/opt/pipeline-dispatch/current').resolve()
    expected={'core.py':'2444cef5e890b9e9fcb3eb7b4e182edcdf30a48a84e803f2f9d42916d9952305',
              'adapters.py':'4ad39e2bd9a582a81f4247d4ad00e66b4d42f31718afdc48ee59bb5d49bd1c1c',
              'youtube_isolation.py':'11c5dfc42ff8c5718ead773d5843ef49d8becd958b03e1a8255ddfaec5edf3ed'}
    for name,value in expected.items():assert sha(previous/'features/pipeline_dispatch'/name)==value
    native=NATIVE/'features/drama_synthesis/core.py'
    assert sha(native)=='97186372b546e8d5aa11be790045dc7e5cb9e70342fb172277aa79b72ba8638e'
    # Preserve every other live line, even when this release has another base.
    revised=native.read_text().replace('WHERE workflow=? AND source_material_id=? AND channel_id=? AND (video_id','WHERE workflow=? AND preparation_id=? AND channel_id=? AND (video_id').replace('(_workflow, _reviewed[1], channel_id)).fetchone()','(_workflow, _reviewed[0], channel_id)).fetchone()')
    assert revised==(source/'features/drama_synthesis/core.py').read_text()
    folder=BASE/('youtube-task-isolation-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));folder.mkdir(mode=0o700)
    snapshot(BASE/'dispatch.sqlite3',folder/'dispatch-before.sqlite3');snapshot(NATIVE/'data/drama_material_jobs.sqlite3',folder/'native-before.sqlite3')
    (folder/'native-core-before.py').write_bytes(native.read_bytes())
    candidate=folder/'coordinator';shutil.copytree(previous,candidate,ignore=shutil.ignore_patterns('__pycache__','.git'))
    for name in expected:shutil.copy2(source/'features/pipeline_dispatch'/name,candidate/'features/pipeline_dispatch'/name)
    write(native,revised.encode())
    unit=Path('/etc/systemd/system/pipeline-youtube-task-isolation.service');assert not unit.exists()
    original=Path('/etc/systemd/system/pipeline-youtube-isolation.service').read_text()
    command='/usr/bin/python3.9 '+str(source/'scripts/pipeline_youtube_canonical.py')+' --runtime-root '+str(NATIVE)+' --dispatch-root '+str(source)+' --port 18850'
    unit.write_text(re.sub(r'^ExecStart=.*$',lambda _: 'ExecStart='+command,original,flags=re.M))
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now',unit.name])
    with requests.Session() as http:
        http.trust_env=False
        for _ in range(50):
            try:
                if http.get('http://127.0.0.1:18850/health',timeout=2).status_code==200:break
            except requests.RequestException:pass
            time.sleep(.2)
        else:raise RuntimeError('bridge_not_ready')
    cfg_path=Path('/etc/pipeline-coordinator.json');cfg=json.loads(cfg_path.read_text())
    (folder/'coordinator-before.json').write_bytes(cfg_path.read_bytes())
    driver=next(x for x in cfg['drivers'] if x['channel']=='youtube');assert driver['port']==18849
    plan={'commit':a.commit,'previous_coordinator':str(previous),'previous_bridge_port':driver['port'],'native_after':sha(native),'coordinator':str(candidate)}
    (folder/'manifest.json').write_text(json.dumps(plan,indent=2));driver['port']=18850
    plan['switch']=switch_coordinator(candidate,json.dumps(cfg).encode(),folder)
    plan['planner']=reload_planner()
    (folder/'manifest.json').write_text(json.dumps(plan,indent=2))
    print(json.dumps({'backup':str(folder),**plan}),flush=True)

if __name__=='__main__':main()
