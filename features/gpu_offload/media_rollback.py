"""Return transferred preparation slots only after the HK helper drains."""
import argparse
from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import urllib.request

from .sql_reconciliation import atomic_json

NODE = 'hk-cpu-assist'
ROOT = Path('/mnt/data-disk/gpu-backend-offload-20261123')
LEDGER = Path('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')


def snapshot(ledger=LEDGER):
    with closing(sqlite3.connect(Path(ledger).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT version,config_json FROM pipeline_config WHERE id=1').fetchone()
        config=json.loads(row['config_json']);config['version']=row['version']
        node=db.execute('SELECT last_seen_at FROM pipeline_node WHERE node_id=?',(NODE,)).fetchone()
        held=[dict(row) for row in db.execute("""SELECT id,status FROM pipeline_job j
            WHERE node_id=? AND stage='prepare' AND
            (status IN ('running','unknown','legacy_running') OR
             EXISTS (SELECT 1 FROM pipeline_resource r WHERE r.job_id=j.id))""",(NODE,))]
        return dict(config=config,held=held,last_seen_at=node['last_seen_at'] if node else None)


def set_capacity(config,host,slots):
    pid=subprocess.check_output(['systemctl','show','pipeline-coordinator.service','-p','MainPID','--value']).decode().strip()
    env=dict(value.split('=',1) for value in Path('/proc/'+pid+'/environ').read_bytes().decode().split('\0') if '=' in value)
    body=dict(actor={'is_admin':True,'user_id':'codex-gpu-offload-rollback','role':'admin'},
              expected_version=config['version'],host_id=host,stage='prepare',max_workers=slots,scaling_mode='manual')
    req=urllib.request.Request('http://127.0.0.1:18846/internal/pipeline-monitor/capacity',
        data=json.dumps(body).encode(),method='PUT',headers={'Authorization':'Bearer '+env['PIPELINE_DISPATCH_INTERNAL_TOKEN'],'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=12) as response:return json.load(response)


def rollback(action,state_path,*,read=snapshot,change=set_capacity):
    state_path=Path(state_path);current=read();config=current['config']
    cpu=config['hosts']['cpu-main']['prepare'];helper=config['hosts'][NODE]['prepare']
    if action=='status':return dict(cpu=cpu,helper=helper,held=current['held'],version=config['version'])
    if config['hosts'][NODE]['publish']['target']!=0:raise RuntimeError('helper_publication_configuration_changed')
    state=json.loads(state_path.read_text()) if state_path.exists() else None
    if action=='drain':
        if state and state['phase']=='restored':
            if helper['target']==0:return state
            archive=state_path.with_suffix('.completed-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
            state_path.replace(archive);state=None
        if not state:
            if helper['target'] not in (1,2) or cpu.get('scaling_mode')!='manual':
                raise RuntimeError('current_transfer_requires_review')
            state=dict(phase='planned',slots=helper['target'],cpu_before=cpu.copy(),
                       version_before=config['version'],started_at=dt.datetime.now(dt.timezone.utc).isoformat())
            atomic_json(state_path,state)
        if helper['target'] not in (0,state['slots']) or cpu!=state['cpu_before']:
            raise RuntimeError('transfer_capacity_changed_since_rollback_started')
        if helper['target']:change(config,NODE,0)
        state['phase']='draining';atomic_json(state_path,state)
        return dict(state,held=current['held'])
    if action!='restore' or not state:raise RuntimeError('drain_must_be_recorded_first')
    if state['phase']=='restored':return state
    if helper['target']!=0:raise RuntimeError('helper_still_accepting_tasks')
    if current['held']:return dict(state,waiting_for=current['held'])
    last=current.get('last_seen_at')
    if not last or (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(last.replace('Z','+00:00'))).total_seconds()>45:
        return dict(state,waiting_for='fresh_helper_heartbeat')
    expected=dict(state['cpu_before'],target=state['cpu_before']['target']+state['slots'],
                  ceiling=state['cpu_before']['target']+state['slots'],scaling_mode='manual')
    if cpu==state['cpu_before']:change(config,'cpu-main',expected['target'])
    elif cpu!=expected:raise RuntimeError('cpu_capacity_changed_since_rollback_started')
    state['phase']='restored';atomic_json(state_path,state)
    return state


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['status','drain','restore']);args=parser.parse_args()
    print(json.dumps(rollback(args.action,ROOT/'media-rollback.json')))


if __name__=='__main__':main()
