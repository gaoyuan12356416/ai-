"""Retry only Nov 4 zero-queue FIFO rejections, one conflicting batch at a time.

The concurrent planner's atomic rejection proves no queue was created. Existing
admission and all frozen fields stay intact. Current planners, queues and other
channels continue; this incident-only watcher expires after six hours.
"""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_schedule_recovery import identity,fingerprint
from scripts.recover_x_prepost_failures import backup,writable
from scripts.recover_x_unstarted_manual import connect,require

SCOPE=(1054,1057,1063)
TABLE='x_post_catchup_fifo_retry_audit'
SCHEMA='CREATE TABLE IF NOT EXISTS '+TABLE+' (run_id INTEGER NOT NULL,failure_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL,backup_path TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(run_id,failure_sha256))'

def inspect(c):
    runs=[dict(r) for r in c.execute('SELECT * FROM x_post_schedule_run WHERE id IN (1054,1057,1063) ORDER BY id')]
    require(len(runs)==3,'incident_runs_missing')
    for r in runs:
        require(r['run_date']=='2026-11-04' and r['source_type']=='material' and r['material_pool_snapshot_max_id']==4444,'incident_scope_changed')
        # A successfully committed partial plan sets expected_count to its
        # actual queue count. It is outside recovery and must not block peers.
        if r['queued_count']:continue
        grant=c.execute('SELECT * FROM x_post_schedule_zero_write_resume_audit WHERE run_id=?',(r['id'],)).fetchone()
        require(grant and grant['identity_sha256']==fingerprint(identity(r)),'original_admission_changed')
    active=[r['id'] for r in runs if r['status']=='claimed' and not r['queued_count']]
    if active:return dict(waiting_for_same_pool_planners=active)
    for r in runs:
        if r['status']!='failed_preflight' or r['error_code']!='x_post_pool_fifo_conflict':continue
        require(r['error_message']=='素材计划必须使用当前素材池最新的可用记录' and r['plan_attempted_at']
            and r['finished_at']>=r['plan_attempted_at'],'not_known_atomic_fifo_rejection')
        require(not any(r[k] for k in ('queued_count','published_count','failed_count','unknown_count','started_at')),'run_has_work')
        require(not c.execute('SELECT 1 FROM x_post_queue WHERE schedule_run_id=?',(r['id'],)).fetchone(),'queue_exists')
        count=c.execute('SELECT count(*) FROM '+TABLE+' WHERE run_id=?',(r['id'],)).fetchone()[0] if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone() else 0
        if count>=3:continue
        return dict(target=r,failure_sha256=fingerprint(r))
    return dict(done=True,states=[dict(run_id=r['id'],status=r['status'],queued_count=r['queued_count'],error_code=r['error_code']) for r in runs])

def tick(path,folder):
    with contextlib.closing(connect(path)) as c:
        c.execute('BEGIN');state=inspect(c)
    if 'target' not in state:return state
    target=backup(path,Path(folder)/'fifo-race-backups','native')
    with contextlib.closing(writable(path)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            require(inspect(c)==state,'planner_state_changed')
            c.execute(SCHEMA)
            for action in ('UPDATE','DELETE'):
                c.execute('CREATE TRIGGER IF NOT EXISTS '+TABLE+'_no_'+action.lower()+' BEFORE '+action+' ON '+TABLE+" BEGIN SELECT RAISE(ABORT,'fifo_recovery_history_immutable'); END")
            now=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ');r=state['target']
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?)',(r['id'],state['failure_sha256'],json.dumps(r,ensure_ascii=False,sort_keys=True),target,now))
            c.execute("UPDATE x_post_schedule_run SET status='claimed',plan_attempted_at='',error_code='',error_message='',finished_at='',updated_at=?,lease_heartbeat_at=? WHERE id=?",(now,now,r['id']))
            require(identity(c.execute('SELECT * FROM x_post_schedule_run WHERE id=?',(r['id'],)).fetchone())==identity(r),'frozen_scope_changed')
            c.commit()
        except Exception:c.rollback();raise
    return dict(rearmed_run_id=r['id'],original_failure_sha256=state['failure_sha256'],backup=target,queues_created=0,platform_called=False)

def main():
    folder=Path('/mnt/data-disk/x-publish-recovery-20261104-on-20261105');path='/var/lib/x-post-automation/accounts.sqlite3'
    deadline=time.monotonic()+6*3600;last=None
    while time.monotonic()<deadline:
        result=tick(path,folder)
        if result!=last:
            print(json.dumps(result,ensure_ascii=False),flush=True)
            with (folder/'fifo-race-sequence.jsonl').open('a') as f:f.write(json.dumps(dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),**result),ensure_ascii=False)+'\n')
            last=result
        if result.get('done'):return
        time.sleep(45)
    print(json.dumps({'watch_expired':True,'state':last}),flush=True)

if __name__=='__main__':main()
