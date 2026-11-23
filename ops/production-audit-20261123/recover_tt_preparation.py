"""Recover only audited pre-execution TT tasks, with publication already held."""
import datetime as dt
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0, '/opt/pipeline-dispatch/current')
from features.pipeline_dispatch.adapters import canonical_fingerprint

ROOT = Path('/mnt/data-disk/production-audit-20261123')


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');args=p.parse_args()
    pid = int(subprocess.check_output(['systemctl', 'show', 'tt-auto-post-service.service', '-p', 'MainPID', '--value']))
    if pid in (0, 3659290):
        raise RuntimeError('original_selection_owner_has_not_exited')
    scope = json.loads((ROOT / 'historical-publish-holds.json').read_text())['tasks']
    c = sqlite3.connect('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3', timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE '/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3' AS native")
    evidence, recovered, banned, skipped = [], [], [], []
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    c.execute('BEGIN IMMEDIATE')
    for item in scope:
        key = item['source_key']
        if not key.startswith('tt:auto:'):
            continue
        job = dict(c.execute('SELECT * FROM pipeline_job WHERE id=?', (item['prepare_id'],)).fetchone())
        row = dict(c.execute('SELECT * FROM native.tt_auto_task WHERE id=?', (int(key.split(':')[-1]),)).fetchone())
        hold = c.execute("SELECT status,hold_reason FROM pipeline_job WHERE source_key=? AND stage='publish'", (key,)).fetchone()
        if not hold or tuple(hold) != ('backlog_paused', 'historical_preparation_only'):
            raise RuntimeError('publication_hold_missing:' + key)
        if (any(row.get(k) for k in ('gpu_job_id','prepared_media_url','publish_id','publish_attempt_count','preparation_attempt_count','unknown_outcome'))
                or any(c.execute('SELECT 1 FROM ' + table + ' WHERE job_id=? LIMIT 1', (job['id'],)).fetchone()
                       for table in ('pipeline_operation','pipeline_receipt','pipeline_checkpoint'))
                or c.execute('SELECT 1 FROM native.pipeline_tt_operation WHERE source_key=? LIMIT 1', (key,)).fetchone()
                or c.execute('SELECT 1 FROM native.pipeline_tt_claim WHERE source_key=? LIMIT 1', (key,)).fetchone()):
            skipped.append({'key':key,'reason':'execution_evidence'}); continue
        # Platform bans are terminal selection failures, never a retry/unban.
        platform_banned = row['error_code'] == 'tt_upstream_rejected' and 'code=spam_risk_user_banned_from_posting' in row['error_message']
        if platform_banned:
            if row['status'] == 'retry_wait' and not row['claim_token']:
                evidence.append({'job':job,'native':row})
                c.execute("UPDATE native.tt_auto_task SET status='failed',next_attempt_at_utc='',claim_phase='',updated_at=? WHERE id=?", (now,row['id']))
                c.execute("UPDATE pipeline_media_gate SET phase='done',retry_at='',error='tt_account_posting_banned' WHERE source_key=?", (key,))
                c.execute('INSERT INTO native.tt_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                          (row['run_id'],row['id'],'platform_ban_retry_stopped',row['status'],'failed',row['error_message'],'{}',now))
            banned.append(key); continue
        orphan = (row['id'] in (5764,5829) and row['status']=='selecting' and job['generation']==0
                  and row['claim_worker']=='pipeline:media-selection' and row['claim_phase']=='selection'
                  and row['updated_at']<'2026-11-23T02:01:10' and row['lease_expires_at_utc']<now and not row['material_id'])
        selected = (job['status']=='failed' and job['error_code'] in ('canonical_identity_changed','tt_auto_internal_error')
                    and row['status']=='reserved' and row['material_id'] and row['content_id'] and not row['claim_token'])
        payload = json.loads(job['payload_json'])
        original = canonical_fingerprint(dict(row,material_id='',content_id=''), 'id')
        if not (orphan or selected) or payload.get('kind') or (selected and payload.get('canonical_fingerprint') != original):
            skipped.append({'key':key,'reason':'state_or_identity_changed'}); continue
        evidence.append({'job':job,'native':row})
        if orphan:
            c.execute("UPDATE native.tt_auto_task SET status='retry_wait',claim_token='',claim_worker='',lease_expires_at_utc='',claim_phase='selection',next_attempt_at_utc=?,updated_at=? WHERE id=?", (now,now,row['id']))
            c.execute('INSERT INTO native.tt_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                      (row['run_id'],row['id'],'selection_owner_exit_reconciled','selecting','retry_wait','',json.dumps({'previous_owner_exited':True}),now))
        else:
            payload['canonical_fingerprint'] = canonical_fingerprint(row, 'id')
        meta = json.loads(job['source_meta_json'])
        meta.update(canonical_fingerprint=payload['canonical_fingerprint'],legacy_inflight=False,reconcile_only=False,source_attempt_count=0,execution_available=True)
        c.execute("UPDATE pipeline_job SET status='queued',hold_reason='',payload_json=?,source_meta_json=?,claim_hash='',node_id='',worker_id='',lease_expires_at='',updated_at=? WHERE id=?",
                  (json.dumps(payload),json.dumps(meta),now,job['id']))
        c.execute("UPDATE pipeline_media_gate SET phase='waiting',ticket='',decision='{}',retry_at='',error='' WHERE source_key=?", (key,))
        c.execute('DELETE FROM pipeline_legacy_resource WHERE job_id=?', (job['id'],))
        c.execute('DELETE FROM pipeline_resource WHERE job_id=?', (job['id'],))
        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                  (job['id'],'audited_preparation_only_recovery',json.dumps({'previous_status':job['status'],'previous_error':job['error_code'],'generation_preserved':job['generation'],'publish_held':True}),now))
        recovered.append(key)
    result={'recovered':recovered,'platform_banned':banned,'skipped':skipped}
    if not args.apply:
        c.rollback();print(json.dumps(result));return
    target=ROOT/'tt-recovery-before.json'
    with target.open('x') as f:
        os.chmod(str(target),0o600);json.dump(evidence,f);f.flush();os.fsync(f.fileno())
    c.commit()
    (ROOT/'tt-recovery-result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({k:len(v) for k,v in result.items()}))


if __name__ == '__main__':
    main()
