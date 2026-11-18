"""Bounded, user-authorized pause of new hk-gpu prepare claims via the live API.

Read SQLite only for observations. Existing leases, workers and publishers are
untouched. Restore only this pool, preserving concurrent changes elsewhere.
"""
import argparse
import datetime
import http.client
import json
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

DB='/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3'
ACTOR={'is_admin':True,'user_id':'codex:gpu-render-math-20261118'}


def snapshot():
    with sqlite3.connect('file:'+DB+'?mode=ro',uri=True,timeout=5) as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
        row=c.execute('SELECT version,config_json FROM pipeline_config WHERE id=1').fetchone()
        config=json.loads(row['config_json'])
        jobs=[dict(x) for x in c.execute("SELECT id,generation,channel,started_at FROM pipeline_job WHERE node_id='hk-gpu' AND stage='prepare' AND status='running'")]
    return {'version':row['version'],'config':config,'running':jobs,'at':time.time()}


def change(expected_version, pool):
    pid=subprocess.check_output(['systemctl','show','pipeline-coordinator.service','-p','MainPID','--value'],text=True).strip()
    env=dict(x.split(b'=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)
    token=env[b'PIPELINE_DISPATCH_INTERNAL_TOKEN'].decode()
    body=dict(expected_version=expected_version,host_id='hk-gpu',stage='prepare',
              max_workers=pool['ceiling'],scaling_mode=pool['scaling_mode'],actor=ACTOR)
    con=http.client.HTTPConnection('127.0.0.1',18846,timeout=20)
    try:
        con.request('POST','/internal/pipeline-monitor/capacity',json.dumps(body),
                    {'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        response=con.getresponse();data=json.loads(response.read())
        if response.status!=200:raise RuntimeError(data.get('code','capacity_change_failed'))
    finally:con.close()


def save(path, record):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(record,indent=2),encoding='utf8');temp.replace(path)


def restore(record):
    for attempt in range(5):
        current=snapshot();pool=current['config']['hosts']['hk-gpu']['prepare']
        if pool==record['before_pool']:
            return 'already_restored'
        if pool!=record['paused_pool']:
            return 'pool_changed_externally_preserved'
        try:
            change(current['version'],record['before_pool'])
            after=snapshot()
            if after['config']['hosts']['hk-gpu']['prepare']==record['before_pool']:
                return 'restored'
        except Exception:
            # Unknown HTTP outcome: inspect current state/version before retry.
            pass
        time.sleep(2)
    raise RuntimeError('restore_readback_mismatch')


def main():
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--window-seconds',type=float,default=1200)
    p.add_argument('--drain-seconds',type=float,default=1800)
    args=p.parse_args();args.directory.mkdir(parents=True,exist_ok=False)
    path=args.directory/'window.json';before=snapshot()
    original=before['config']['hosts']['hk-gpu']['prepare']
    if original.get('scaling_mode')!='manual' or original['target']!=original['ceiling']:
        raise RuntimeError('unexpected_gpu_capacity_mode')
    paused=dict(original,target=0,ceiling=0,scaling_mode='manual')
    record=dict(before=before,before_pool=original,paused_pool=paused,status='pause_requested',production_signals=0)
    save(path,record)
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        change(before['version'],paused)
        readback=snapshot()
        if readback['config']['hosts']['hk-gpu']['prepare']!=paused:
            raise RuntimeError('pause_readback_mismatch')
        record.update(status='draining',paused_at=time.time(),after_pause=readback);save(path,record)
        drain_deadline=time.monotonic()+args.drain_seconds
        while not stopping:
            current=snapshot()
            if current['config']['hosts']['hk-gpu']['prepare']!=paused:
                record['reason']='capacity_changed_externally';break
            record['latest_running']=current['running'];record['observed_at']=time.time()
            if not current['running'] and 'ready_at' not in record:
                record.update(status='test_window',ready_at=time.time(),deadline=time.time()+args.window_seconds)
            save(path,record)
            if (args.directory/'finish').exists():
                record['reason']='test_finished';break
            if time.time()>=record.get('deadline',float('inf')):
                record['reason']='test_window_elapsed';break
            if 'ready_at' not in record and time.monotonic()>=drain_deadline:
                record['reason']='drain_timeout';break
            time.sleep(2)
    except BaseException as exc:
        record['error_type']=type(exc).__name__
        raise
    finally:
        record['restore_status']=restore(record)
        record['after']=snapshot();record['status']='finished';record['finished_at']=time.time()
        save(path,record)


if __name__=='__main__':main()
