"""Restore one original unqueued Nov 4 Auto selection after account isolation.

Task 2796 / run 1817 originally selected material 8009155. Its provisional
reservation was released on another task's unknown-account hold. Restore the
same pairing once; the later task 2895 selected that same material and must not
also be resumed. Normal preparation rehydrates and validates the exact source.
"""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.recover_x_prepost_failures import backup,writable
from scripts.recover_x_unstarted_manual import connect,require,rows
from features.x_auto_posts.core import XPostAutoStore

TABLE='x_auto_original_selection_recovery_2796'
SCHEMA='CREATE TABLE IF NOT EXISTS '+TABLE+' (task_id INTEGER PRIMARY KEY,state TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL)'

def task_proof(a):
    t=dict(a.execute('SELECT * FROM x_auto_task WHERE id=2796').fetchone())
    require(t['status']=='failed' and t['error_code']=='x_post_account_needs_review' and t['execution_run_id']==1817
        and t['account_id']=='19' and t['template_id']==2 and t['template_version']==10,'original_task_changed')
    require(not any(t[k] for k in ('material_id','content_id','series_code','execution_queue_id','execution_log_id','publish_id','publish_url',
        'unknown_outcome','preparation_attempt_count','publish_attempt_count','claim_worker','claim_token','lease_expires_at_utc')),'task_has_work_or_result')
    r=dict(a.execute('SELECT * FROM x_auto_run WHERE id=?',(t['run_id'],)).fetchone());require(r['shanghai_date']=='2026-11-04','wrong_day')
    events=rows(a,"SELECT * FROM x_auto_event WHERE task_id=2796 AND event_type='material_reserved'")
    require(len(events)==1 and json.loads(events[0]['details_json'])=={'material_id':'8009155','content_id':'2lcPGlz7Bd'},'selection_not_proven')
    require(not a.execute("SELECT 1 FROM x_auto_material_ledger WHERE task_id=2796 OR material_id='8009155'").fetchone(),'material_reserved_elsewhere')
    require(not a.execute("SELECT 1 FROM pipeline_canonical_hold WHERE source_key='x:auto:2796'").fetchone(),'task_held')
    return dict(task=t,original_event=events[0])

def native_proof(n):
    r=dict(n.execute('SELECT * FROM x_post_manual_run WHERE id=1817').fetchone())
    require(r['status']=='failed_preflight' and r['error_code']=='x_post_account_needs_review' and r['run_date']=='2026-11-04'
        and r['trigger_source']=='auto_template' and r['external_task_key']=='x-auto-task-2796'
        and json.loads(r['account_ids_json'])==[19] and json.loads(r['material_ids_json'])==['8009155'],'original_run_changed')
    require(not any(r[k] for k in ('queued_count','published_count','failed_count','unknown_count','started_at')),'native_work_exists')
    require(not n.execute("SELECT 1 FROM x_post_queue WHERE manual_run_id=1817 OR material_id='8009155'").fetchone(),'material_queued_elsewhere')
    reservations=rows(n,'SELECT * FROM x_post_manual_material_reservation WHERE manual_run_id=1817')
    require(len(reservations)==1 and reservations[0]['id']==1854 and reservations[0]['state']=='released'
        and reservations[0]['material_key']=='8009155' and reservations[0]['release_reason']=='x_post_account_needs_review','reservation_changed')
    require(not n.execute("SELECT 1 FROM x_post_manual_material_reservation WHERE material_key='8009155' AND state='active'").fetchone(),'material_held_elsewhere')
    return dict(run=r,reservation=reservations[0])

def restore(folder):
    auto='/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3';native='/var/lib/x-post-automation/accounts.sqlite3';folder=Path(folder)
    with contextlib.closing(connect(auto)) as a,contextlib.closing(connect(native)) as n:
        if n.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():
            prior=n.execute('SELECT * FROM '+TABLE+' WHERE task_id=2796').fetchone()
            if prior and prior['state']=='complete':return dict(already_applied=True,task_id=2796,platform_called=False)
        proof=dict(auto=task_proof(a),native=native_proof(n))
    backups=[backup(auto,folder/'original2796-backups','auto'),backup(native,folder/'original2796-backups','native')]
    now=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds');proof.update(backups=backups,actor='codex-operator:user-Nov4-catchup')
    (folder/'original2796-proof.json').write_text(json.dumps(proof,indent=2,ensure_ascii=False))
    with contextlib.closing(writable(auto)) as a,contextlib.closing(writable(native)) as n:
        a.execute('BEGIN IMMEDIATE');n.execute('BEGIN IMMEDIATE')
        try:
            require(task_proof(a)==proof['auto'] and native_proof(n)==proof['native'],'state_changed')
            n.execute(SCHEMA)
            n.execute('INSERT INTO '+TABLE+' VALUES(2796,?,?,?)',('native_armed',json.dumps(proof,ensure_ascii=False),now))
            n.execute("UPDATE x_post_manual_material_reservation SET state='active',release_reason='',updated_at=? WHERE id=1854",(now,))
            n.execute("UPDATE x_post_manual_run SET status='queued',finished_at='',error_code='',error_message='',updated_at=? WHERE id=1817",(now,))
            n.commit()
            # Only the Auto worker claims auto_template requests. Until this
            # transaction commits, the rearmed native run has no executor.
            t=proof['auto']['task'];reserved=proof['auto']['original_event']['created_at']
            selection={'material':{'material_id':'8009155'},'operator_restored_original_event':proof['auto']['original_event']['id']}
            a.execute("INSERT INTO x_auto_material_ledger(material_id,task_id,run_id,template_id,content_id,reserved_at_utc,last_task_status,updated_at) VALUES('8009155',2796,?,2,'2lcPGlz7Bd',?,'preparing',?)",(t['run_id'],reserved,now))
            a.execute("UPDATE x_auto_task SET status='preparing',material_id='8009155',content_id='2lcPGlz7Bd',selection_json=?,reserved_at_utc=?,error_code='',error_message='',finished_at_utc='',next_attempt_at_utc='',claim_phase='',updated_at=? WHERE id=2796",(json.dumps(selection),reserved,now))
            a.execute("UPDATE x_auto_run SET status='running',finished_at_utc='',updated_at=? WHERE id=?",(now,t['run_id']))
            XPostAutoStore._event(a,event_type='operator_original_selection_restored',created_at=now,task_id=2796,run_id=t['run_id'],from_status='failed',to_status='preparing',details={'material_id':'8009155','native_run_id':1817,'original_event_id':proof['auto']['original_event']['id']})
            a.commit()
            n.execute("UPDATE "+TABLE+" SET state='complete' WHERE task_id=2796")
        except Exception:a.rollback();n.rollback();raise
    result=dict(task_id=2796,manual_run_id=1817,material_id='8009155',status='preparing',new_task_created=False,platform_called=False)
    (folder/'original2796-applied.json').write_text(json.dumps(result,indent=2));return result

if __name__=='__main__':print(json.dumps(restore(sys.argv[1])))
