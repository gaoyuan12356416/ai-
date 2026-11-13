"""Record a GET-proven video for an unknown original task. Never issue a POST."""
import argparse
import contextlib
import datetime as dt
import json
import re
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_fb_unpublished import backup, require, sha
from features.fb_auto_posts.core import FBAutoPostStore
from features.pipeline_dispatch.core import dumps, stamp

def validate(task, item, operation):
    require(item['id'] == task['id'] and item['page_id'] == task['page_id'] and item['short_url'] == task['short_url'], 'evidence_identity_changed')
    matches = item.get('matched_videos', [])
    require(len(matches) == 1 and matches[0].get('exact_short_url') is True, 'unique_short_link_match_required')
    video = matches[0]; status = video.get('status', {})
    require(str(video.get('id', '')).isdigit() and status.get('video_status') == 'ready'
            and status.get('publishing_phase', {}).get('publish_status') == 'published', 'publication_not_confirmed')
    created = dt.datetime.strptime(video['created_time'], '%Y-%m-%dT%H:%M:%S%z')
    began = dt.datetime.fromisoformat(operation['created_at'])
    finished = dt.datetime.fromisoformat(task['completed_at_utc'])
    require(began - dt.timedelta(minutes=2) <= created <= finished + dt.timedelta(minutes=2), 'publication_time_outside_original_attempt')
    require(any(x.get('status') == 200 and x.get('rows', 0) > 0 for x in item['checks']), 'successful_page_video_read_required')
    return video

def select_observed_operation(operations, native_operations, attempts):
    """Allow one final unknown write only after fully recorded Graph rejections."""
    require(bool(operations), 'platform_attempt_missing')
    if len(operations) == 1:
        return operations[0]
    central = {row['operation_id']: row for row in operations}
    native = sorted(native_operations, key=lambda row: row['sequence'])
    by_sequence = {row['sequence']: row for row in attempts}
    require(len(central) == len(native) == len(attempts)
            and {row['operation_id'] for row in native} == set(central)
            and [row['sequence'] for row in native] == list(range(1, len(native) + 1))
            and set(by_sequence) == set(range(1, len(native) + 1)), 'attempt_chain_incomplete')
    for index, row in enumerate(native):
        operation = central[row['operation_id']]
        attempt = by_sequence[row['sequence']]
        result, checkpoint = json.loads(row['result_json']), json.loads(row['checkpoint_json'])
        require(row['stage'] == 'publish' and operation['operation'] == 'fb_video_publish'
                and row['generation'] == operation['generation']
                and row['request_sha256'] == operation['request_sha256']
                and row['credential_id'] == attempt['credential_id']
                and row['fb_user_id'] == attempt['fb_user_id']
                and result == checkpoint and result.get('operation_id') == row['operation_id']
                and not result.get('post_id')
                and result.get('kind') == attempt['result_kind']
                and result.get('error_code') == attempt['error_code']
                and result.get('trace_id', '') == attempt['trace_id'], 'attempt_proof_mismatch')
        if index < len(native) - 1:
            require(result['kind'] == 'definite_failure'
                    and re.fullmatch(r'fb_graph_[0-9]+', result['error_code'])
                    and bool(result.get('trace_id')), 'prior_attempt_not_explicit_graph_rejection')
        else:
            require(result['kind'] == 'unknown', 'final_attempt_not_unknown')
    return central[native[-1]['operation_id']]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('native','central','evidence','output'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--task-id',type=int,required=True);p.add_argument('--apply',action='store_true');p.add_argument('--plan-sha256');p.add_argument('--backup-dir')
    a=p.parse_args(); evidence=json.loads(Path(a.evidence).read_text())
    require(evidence.get('get_only') is True, 'get_evidence_required')
    items=[i for i in evidence['items'] if i['id']==a.task_id];require(len(items)==1,'task_evidence_missing')
    if a.apply:
        require(a.backup_dir and a.plan_sha256,'apply_arguments_required')
        folder=Path(a.backup_dir);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
        backup(a.native,folder/'fb.sqlite3');backup(a.central,folder/'dispatch.sqlite3')
    mode='rw' if a.apply else 'ro'
    with contextlib.closing(sqlite3.connect(Path(a.native).resolve().as_uri()+'?mode='+mode,uri=True,timeout=30)) as c:
        c.row_factory=sqlite3.Row;c.execute('ATTACH DATABASE ? AS dispatch',(Path(a.central).resolve().as_uri()+'?mode='+mode,))
        c.execute('BEGIN IMMEDIATE' if a.apply else 'BEGIN')
        task=dict(c.execute('SELECT * FROM fb_auto_task WHERE id=?',(a.task_id,)).fetchone())
        key='fb:auto:'+str(a.task_id)
        job=dict(c.execute("SELECT * FROM dispatch.pipeline_job WHERE source_key=? AND stage='publish'",(key,)).fetchone())
        require(task['status']=='unknown' and not task['graph_post_id'] and not task['lease_owner'] and not task['lease_expires_at_utc'], 'original_task_not_idle_unknown')
        require(job['status']=='unknown','central_state_changed')
        operations=[dict(r) for r in c.execute('SELECT * FROM dispatch.pipeline_operation WHERE job_id=?',(job['id'],))]
        native_operations=[dict(r) for r in c.execute('SELECT * FROM pipeline_fb_operation WHERE source_key=? AND stage=?',(key,'publish'))]
        attempts=[dict(r) for r in c.execute('SELECT * FROM fb_auto_publish_attempt WHERE task_id=?',(a.task_id,))]
        observed_operation=select_observed_operation(operations,native_operations,attempts)
        video=validate(task,items[0],observed_operation)
        require(not c.execute('SELECT 1 FROM fb_auto_task WHERE id<>? AND graph_post_id=?',(task['id'],video['id'])).fetchone(),'video_already_bound')
        require(not c.execute('SELECT 1 FROM fb_video_task WHERE graph_post_id=?',(video['id'],)).fetchone(),'video_already_bound_to_video_task')
        require(not c.execute('SELECT 1 FROM dispatch.pipeline_publication WHERE channel=? AND publish_id=?',('fb',video['id'])).fetchone(),'video_already_recorded')
        require(not c.execute('SELECT 1 FROM dispatch.pipeline_publication WHERE source_key=?',(key,)).fetchone(),'publication_already_recorded')
        result={'task_id':task['id'],'source_key':key,'job_id':job['id'],'page_id':task['page_id'],'video_id':video['id'],
                'published_at':stamp(dt.datetime.strptime(video['created_time'], '%Y-%m-%dT%H:%M:%S%z')),'evidence_sha256':sha(evidence),
                'observed_operation_id':observed_operation['operation_id'],
                'state_sha256':sha({'task':task,'job':job,'operations':operations,'native_operations':native_operations,'attempts':attempts})}
        result['plan_sha256']=sha(result)
        if a.apply:
            require(result['plan_sha256']==a.plan_sha256,'plan_changed')
            now=stamp();published=result['published_at']
            proof={'source_key':key,'publish_id':video['id'],'status':'published','published_at':published,'get_verified':True,'evidence_sha256':result['evidence_sha256']}
            c.execute("UPDATE fb_auto_task SET status='published',graph_post_id=?,unknown_outcome=0,error_code='',error_message='',next_reconcile_at_utc='',completed_at_utc=? WHERE id=?",(video['id'],published,task['id']))
            FBAutoPostStore._upsert_ledger(c,task['id'],task['page_id'],task['material_id'],'published',video['id'],0,False,'',now)
            FBAutoPostStore._refresh_run(c,task['run_id'],now)
            c.execute("UPDATE dispatch.pipeline_job SET status='published',source_status='published',error_code='',error_message='',finished_at=?,updated_at=?,lease_expires_at='',canonical_receipt=? WHERE id=?",(published,now,dumps(proof),job['id']))
            c.execute('INSERT INTO dispatch.pipeline_publication VALUES(?,?,?,?,?,?,?,?,?,?)',(key,job['id'],'fb',job['owner_user_id'],job['owner_tenant_key'],job['required_module'],job['node_id'],video['id'],published,dumps(proof)))
            c.execute('DELETE FROM dispatch.pipeline_resource WHERE job_id=?',(job['id'],))
            c.execute('DELETE FROM dispatch.pipeline_legacy_resource WHERE job_id=?',(job['id'],))
            c.execute('INSERT INTO dispatch.pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(job['id'],'fb_unknown_resolved_by_exact_video_get',dumps({**result,'previous_error':task['error_code']}),now))
            c.commit()
        else:c.rollback()
    result['applied']=a.apply;path=Path(a.output);path.write_text(json.dumps(result,indent=2));path.chmod(0o600)
    print(json.dumps(result))

if __name__=='__main__':main()
