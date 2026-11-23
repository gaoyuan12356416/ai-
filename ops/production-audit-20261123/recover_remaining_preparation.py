"""Restore audited FB/X preparation only, retaining old claims and failure facts."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,'/opt/pipeline-dispatch/current')
from features.pipeline_dispatch.adapters import canonical_fingerprint

ROOT=Path('/mnt/data-disk/production-audit-20261123')
DBS={'fb':'/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3',
     'x':'/var/lib/x-post-automation/accounts.sqlite3','tag':'/mnt/data-disk/x-tag-publisher/posts.sqlite3'}


def require(value,reason):
    if not value:raise RuntimeError(reason)


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');a=p.parse_args()
    scope=json.loads((ROOT/'historical-publish-holds.json').read_text())['tasks']
    c=sqlite3.connect('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3',timeout=20,isolation_level=None);c.row_factory=sqlite3.Row
    for alias,path in DBS.items():c.execute('ATTACH DATABASE ? AS '+alias,(path,))
    result=[]
    for item in scope:
        key=item['source_key']
        if not key.startswith(('fb:','x:queue:')):continue
        c.execute('BEGIN IMMEDIATE')
        job=dict(c.execute('SELECT * FROM pipeline_job WHERE id=?',(item['prepare_id'],)).fetchone())
        if job['status']!='failed':c.rollback();result.append({'id':job['id'],'already_changed':job['status']});continue
        require(job['stage']=='prepare' and not job['external_intent'],'not_local_preparation')
        hold=c.execute("SELECT status,hold_reason FROM pipeline_job WHERE source_key=? AND stage='publish'",(key,)).fetchone()
        require(hold and tuple(hold)==('backlog_paused','historical_preparation_only'),'publish_hold_missing')
        require(not c.execute('SELECT 1 FROM pipeline_operation WHERE job_id=? LIMIT 1',(job['id'],)).fetchone(),'external_operation_present')
        alias='fb' if key.startswith('fb:') else 'tag' if int(job['source_id'])>9000000000000 else 'x'
        table='fb_auto_task' if alias=='fb' else 'x_post_queue'
        native=dict(c.execute('SELECT * FROM '+alias+'.'+table+' WHERE id=?',(job['source_id'],)).fetchone())
        expected=json.loads(job['payload_json']).get('canonical_fingerprint') or json.loads(job['source_meta_json']).get('canonical_fingerprint')
        require(canonical_fingerprint(native,'id')==expected,'frozen_source_identity_changed')
        evidence={'job':job,'native':native}
        now=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        if alias=='fb':
            require(native['status'] in ('failed','planned') and not any(native.get(k) for k in ('lease_owner','lease_expires_at_utc','unknown_outcome','graph_post_id','prepared_media_url')),'fb_active_or_completed')
            require(not c.execute('SELECT 1 FROM fb.fb_auto_publish_attempt WHERE task_id=? LIMIT 1',(native['id'],)).fetchone(),'fb_publish_attempt_present')
            require(not c.execute('SELECT 1 FROM fb.pipeline_fb_operation WHERE source_key=? LIMIT 1',(key,)).fetchone(),'fb_operation_present')
            binding=c.execute("SELECT * FROM fb.pipeline_fb_claim WHERE source_key=? AND stage='prepare' ORDER BY generation DESC LIMIT 1",(key,)).fetchone()
            if binding:
                evidence['binding']=dict(binding);frozen=json.loads(binding['task_json'])
                require(not binding['receipt_json'] and binding['generation']==job['generation'] and str(frozen['lease_owner']).startswith('pipeline:'),'fb_binding_changed')
                require(all(native[k]==frozen[k] for k in ('gpu_job_id','source_media_url','content_id','material_id','page_id')),'fb_frozen_request_changed')
                # Existing resume logic authenticates this historical owner and
                # freezes the exact prior recipe; no execution is restarted here.
                c.execute("UPDATE fb.fb_auto_task SET status='preparing',lease_owner=?,lease_expires_at_utc='' WHERE id=?",(frozen['lease_owner'],native['id']))
            else:
                require(native['status']=='planned' and native['attempt_count']==0,'fb_unbound_attempt')
        else:
            require(native['status']=='failed','x_native_state_changed')
            require(not c.execute('SELECT 1 FROM '+alias+'.pipeline_x_operation WHERE source_key=? LIMIT 1',(key,)).fetchone(),'x_external_operation_present')
            logs=[dict(r) for r in c.execute('SELECT * FROM '+alias+'.x_post_publish_log WHERE queue_id=?',(native['id'],))]
            require(len(logs)==1 and logs[0]['status']=='failed' and not any(logs[0].get(k) for k in ('attempt_count','unknown_outcome','x_media_id','x_post_id')),'x_publish_evidence')
            evidence['logs']=logs
            if native.get('manual_run_id'):
                parent=dict(c.execute('SELECT * FROM '+alias+'.x_post_manual_run WHERE id=?',(native['manual_run_id'],)).fetchone());evidence['parent']=parent
                if parent['trigger_source']=='auto_template':
                    require(parent['status'] in ('completed_with_errors','running'),'x_auto_parent_changed')
                    require(not c.execute('SELECT 1 FROM '+alias+'.x_post_publish_log l JOIN '+alias+'.x_post_queue q ON q.id=l.queue_id WHERE q.manual_run_id=? AND (l.unknown_outcome=1 OR l.status IN (\'post_creating\',\'repost_creating\')) LIMIT 1',(parent['id'],)).fetchone(),'x_parent_unknown')
                    c.execute('UPDATE '+alias+".x_post_manual_run SET status='running' WHERE id=?",(parent['id'],))
            c.execute('UPDATE '+alias+".x_post_queue SET status='queued',updated_at=? WHERE id=?",(now,native['id']))
            c.execute('UPDATE '+alias+".x_post_publish_log SET status='reserved',updated_at=? WHERE id=?",(now,logs[0]['id']))
        meta=json.loads(job['source_meta_json']);meta.update(legacy_inflight=False,reconcile_only=False,source_attempt_count=0)
        c.execute("UPDATE pipeline_job SET status='queued',source_status=?,hold_reason='',source_meta_json=?,claim_hash='',node_id='',worker_id='',lease_expires_at='',updated_at=? WHERE id=?",
                  ('preparing' if alias=='fb' and binding else 'planned' if alias=='fb' else 'queued',json.dumps(meta),now,job['id']))
        c.execute("UPDATE pipeline_media_gate SET phase='waiting',ticket='',decision='{}',retry_at='',error='' WHERE source_key=?",(key,))
        c.execute('DELETE FROM pipeline_resource WHERE job_id=?',(job['id'],))
        c.execute('DELETE FROM pipeline_legacy_resource WHERE job_id=?',(job['id'],))
        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(job['id'],'audited_preparation_only_recovery',json.dumps({'previous_error':job['error_code'],'generation_preserved':job['generation'],'publish_held':True}),now))
        if a.apply:
            path=ROOT/('remaining-recovery-before-%d.json'%job['id'])
            with path.open('x') as f:os.chmod(str(path),0o600);json.dump(evidence,f);f.flush();os.fsync(f.fileno())
            c.commit()
        else:c.rollback()
        result.append({'id':job['id'],'source_key':key,'recovered':a.apply})
        if a.apply:(ROOT/'remaining-recovery-result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=='__main__':main()
