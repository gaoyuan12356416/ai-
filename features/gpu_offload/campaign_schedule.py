"""Run one migrated refresher on the original interval and failure backoff."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .sql_reconciliation import atomic_json
from .campaign.service import refresh_delay


def tick(root):
    root=Path(root);config=json.loads((root/'schedule.json').read_text())
    if not config.get('enabled'):return {'status':'disabled'}
    with (root/'refresh.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'already_running'}
        state_path=root/'refresh-state.json'
        state=json.loads(state_path.read_text()) if state_path.exists() else dict(next_due=config['first_due'],failures=0)
        if time.time()<float(state['next_due']):return {'status':'not_due'}
        started=time.time();atomic_json(state_path,dict(state,status='running',started_at=started))
        seed=root/'seed.json';index=root/'index'
        # Child exit separates the large refresh and indexing memory peaks.
        result=subprocess.run([sys.executable,'-m','features.gpu_offload.campaign_refresh',str(seed)])
        if result.returncode==0:
            result=subprocess.run([sys.executable,'-c','from features.gpu_offload.campaign_snapshot import index_snapshot;import sys,json;print(json.dumps(index_snapshot(sys.argv[1],sys.argv[2])))',str(seed),str(index)])
        ok=result.returncode==0;elapsed=time.time()-started
        failures=0 if ok else int(state.get('failures',0))+1
        state=dict(status='complete' if ok else 'failed',started_at=started,finished_at=time.time(),
                   next_due=time.time()+refresh_delay(ok,elapsed,failures),failures=failures,exit_code=result.returncode)
        atomic_json(state_path,state);return state


if __name__=='__main__':print(json.dumps(tick(os.environ['CAMPAIGN_STATE_ROOT'])),flush=True)
