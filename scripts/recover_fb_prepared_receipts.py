"""Restore verified, never-published FB auto artifacts without rendering or POST.

Default is a read-only plan. Apply requires paused FB admission and a matching
plan digest. Original claims, failures, source selection and publishing facts
are retained. A repeated completed operation is a no-op.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.core import dumps,digest
from features.pipeline_dispatch.executors import _artifact
from features.fb_auto_posts.core import FBAutoPostStore


def require(value,code):
    if not value:raise ValueError(code)


def snapshot(c,evidence):
    now=dt.datetime.now(dt.timezone.utc)
    require(evidence.get('version')==1 and evidence.get('node_id')=='hk-gpu','evidence_invalid')
    items=evidence.get('items',[])
    require(0<len(items)<=20 and len({i['job_id'] for i in items})==len(items),'scope_invalid')
    operation=digest(dumps(evidence));plan=[]
    config=json.loads(c.execute('SELECT config_json FROM pipeline_config WHERE id=1').fetchone()[0])
    require('fb' not in config['enabled_channels'],'fb_admission_must_be_paused')
    for item in items:
        p=item['payload'];a=_artifact(p['result']['preparation']);v=item['verified'];manifest=item['manifest']
        require(p['job_id']==item['job_id'] and p['generation']==1 and p['node_id']==evidence['node_id'],'receipt_scope_changed')
        require(v['sha256']==a['sha256'] and v['size']==a['size'] and v['codec']=='h264' and v['profile']=='High'
                and (v['width'],v['height'],v['frame_rate'])==(720,1280,'30/1')
                and abs(v['source_duration']-v['output_duration'])<=1.5
                and abs(v['output_duration']-a['duration_seconds'])<=.05,'artifact_not_verified')
        require(all(p['result']['preparation'][k]==manifest['result'][k] for k in
                ('job_id','content_id','profile','output_url','output_sha256','output_size','random_overlay_recipe')),'manifest_conflict')
        j=dict(c.execute('SELECT * FROM pipeline_job WHERE id=?',(item['job_id'],)).fetchone())
        require((j['channel'],j['family'],j['stage'])==('fb','auto','prepare') and j['generation']>p['generation']
                and not j['external_intent'] and not j['hold_reason'],'dispatch_identity_changed')
        h=c.execute('SELECT * FROM pipeline_claim_history WHERE job_id=? AND generation=?',(j['id'],p['generation'])).fetchone()
        require(h and h['claim_hash']==digest(p['claim_token']) and h['node_id']==p['node_id'] and h['worker_id']==p['worker_id'],'receipt_authentication_failed')
        t=dict(c.execute('SELECT * FROM native.fb_auto_task WHERE id=?',(int(j['source_id']),)).fetchone())
        audit=json.loads(t['selection_json'] or '{}').get('preparation_receipt_recovery',{})
        if audit.get('operation_sha256')==operation and j['status']=='completed':
            require(t['prepared_sha256']==a['sha256'] and t['prepared_size_bytes']==a['size'],'restored_artifact_changed')
            plan.append({'job_id':j['id'],'task_id':t['id'],'already_applied':True});continue
        require(j['status']=='failed' and j['error_code']=='fb_gpu_transcode_failed'
                and t['status']=='failed' and t['error_code']=='fb_gpu_transcode_failed','failure_not_exact')
        require(not any(t[k] for k in ('graph_post_id','unknown_outcome','lease_owner','lease_expires_at_utc','prepared_sha256','prepared_media_url')),'native_execution_or_artifact_present')
        require(t['recovery_until_utc']>now.isoformat(),'original_recovery_window_expired')
        template=c.execute('SELECT status,current_version FROM native.fb_auto_template WHERE id=?',(t['template_id'],)).fetchone()
        require(template and template['status']=='enabled' and template['current_version']==t['template_version'],'template_changed')
        key=j['source_key']
        require(c.execute('SELECT COUNT(*) FROM pipeline_job WHERE source_key=?',(key,)).fetchone()[0]==1,'publication_stage_present')
        for table in ('pipeline_operation','pipeline_resource','pipeline_legacy_resource','pipeline_checkpoint','pipeline_receipt'):
            require(not c.execute('SELECT 1 FROM '+table+' WHERE job_id=? LIMIT 1',(j['id'],)).fetchone(),'dispatch_write_or_receipt_present')
        require(not c.execute('SELECT 1 FROM pipeline_publication WHERE source_key=?',(key,)).fetchone(),'publication_present')
        for table,column,value in [('fb_auto_publish_attempt','task_id',t['id']),('fb_auto_publish_ledger','task_id',t['id']),
                                  ('pipeline_fb_operation','source_key',key),('pipeline_canonical_hold','source_key',key)]:
            require(not c.execute('SELECT 1 FROM native.'+table+' WHERE '+column+'=? LIMIT 1',(value,)).fetchone(),'native_publication_or_hold_present')
        require(not c.execute("SELECT 1 FROM native.fb_auto_publish_ledger WHERE page_id=? AND material_id=? AND (status IN ('published','submitted','unknown','running') OR graph_post_id<>'' OR unknown_outcome=1) LIMIT 1",(t['page_id'],t['material_id'])).fetchone(),'same_material_publication_present')
        bindings=list(c.execute('SELECT * FROM native.pipeline_fb_claim WHERE source_key=? ORDER BY generation',(key,)))
        require(len(bindings)==j['generation'] and all(b['stage']=='prepare' for b in bindings),'claim_history_changed')
        for b in bindings:
            frozen=json.loads(b['task_json']);descriptor=json.loads(b['descriptor_json'])
            require(all(frozen[k]==t[k] for k in ('id','run_id','template_id','template_version','page_id','material_id','content_id','gpu_job_id','source_media_url','planned_publish_at_utc')),'frozen_identity_changed')
            expected=descriptor['request'];actual=manifest['request']
            require(all(expected[k]==actual[k] for k in ('job_id','content_id','source_url'))
                    and expected['expected_profile']==a['profile'] and expected['video_template']=='random_overlay'
                    and expected['source_trim_tail_seconds']==0,'frozen_request_changed')
        require(bindings[0]['claim_hash']==h['claim_hash'] and not bindings[0]['receipt_json'],'native_receipt_changed')
        plan.append({'job_id':j['id'],'task_id':t['id'],'source_key':key,'generation':j['generation'],
                     'artifact_sha256':a['sha256'],'size':a['size'],'duration':a['duration_seconds'],
                     'state_sha256':digest(dumps({'job':j,'task':t,'bindings':[dict(b) for b in bindings]}))})
    return {'operation_sha256':operation,'items':plan,'plan_sha256':digest(dumps(plan))}


def restore(c,evidence,plan):
    now=dt.datetime.now(dt.timezone.utc).isoformat();operation=plan['operation_sha256']
    scope={i['job_id']:i for i in evidence['items']}
    for row in plan['items']:
        if row.get('already_applied'):continue
        item=scope[row['job_id']];p=item['payload'];a=_artifact(p['result']['preparation']);key=row['source_key'];finished=item['receipt_created_at']
        t=dict(c.execute('SELECT * FROM native.fb_auto_task WHERE id=?',(row['task_id'],)).fetchone())
        selection=json.loads(t['selection_json'] or '{}')
        audit={'operation_sha256':operation,'actor':'authorized_fb_artifact_recovery','previous_status':t['status'],
               'previous_error_code':t['error_code'],'previous_completed_at':t['completed_at_utc'],
               'original_generation':p['generation'],'superseded_generation':row['generation'],
               'artifact_sha256':a['sha256'],'verified_at':evidence['captured_at'],'recovered_at':now}
        selection['preparation_receipt_recovery']=audit
        c.execute("UPDATE native.fb_auto_task SET status='ready',media_url=?,prepared_media_url=?,prepared_sha256=?,prepared_size_bytes=?,prepared_duration_seconds=?,prepared_profile=?,prepared_at_utc=?,error_code='',error_message='',completed_at_utc='',next_prepare_at_utc='',selection_json=? WHERE id=? AND status='failed'",
                  (a['url'],a['url'],a['sha256'],a['size'],str(a['duration_seconds']),a['profile'],finished,dumps(selection),t['id']))
        c.execute("UPDATE native.pipeline_fb_claim SET receipt_json=? WHERE source_key=? AND stage='prepare' AND generation=? AND receipt_json=''",(dumps(p['result']),key,p['generation']))
        FBAutoPostStore._refresh_run(c,t['run_id'],now)
        proof=dumps({'source_key':key,'status':'ready','artifact_sha256':a['sha256'],'operation_sha256':operation})
        c.execute("UPDATE pipeline_job SET status='completed',source_status='ready',artifact_json=?,canonical_receipt=?,finished_at=?,updated_at=?,lease_expires_at='',error_code='',error_message='' WHERE id=? AND status='failed'",
                  (dumps(a),proof,finished,now,row['job_id']))
        c.execute('INSERT INTO pipeline_receipt VALUES(?,?,?,?,?)',(p['receipt_id'],row['job_id'],p['generation'],digest(dumps(p)),now))
        c.execute('INSERT INTO pipeline_delivery VALUES(?,?,?,?,?,?)',(p['receipt_id'],row['job_id'],p['generation'],digest(p['claim_token']),digest(dumps(p)),dumps({'accepted':True,'historical':True,'reconciled':True})))
        for phase,value in p.get('metrics',{}).items():
            if phase.endswith('_seconds'):c.execute('INSERT OR IGNORE INTO pipeline_phase_metric VALUES(?,?,?,?,?,?)',(row['job_id'],p['generation'],phase[:-8],value,None,finished))
        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(row['job_id'],'verified_fb_preparation_restored',dumps(audit),now))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--central',required=True);p.add_argument('--native',required=True);p.add_argument('--evidence',required=True);p.add_argument('--output',required=True);p.add_argument('--apply',action='store_true');p.add_argument('--plan-sha256');p.add_argument('--backup-dir');a=p.parse_args()
    evidence=json.loads(Path(a.evidence).read_text());mode='rw' if a.apply else 'ro'
    with contextlib.closing(sqlite3.connect(Path(a.central).resolve().as_uri()+'?mode='+mode,uri=True,timeout=30)) as c:
        c.row_factory=sqlite3.Row;c.execute('ATTACH DATABASE ? AS native',(Path(a.native).resolve().as_uri()+'?mode='+mode,))
        if a.apply:
            require(a.backup_dir and a.plan_sha256,'apply_arguments_required')
            prior=snapshot(c,evidence)
            if all(i.get('already_applied') for i in prior['items']):
                print(json.dumps({'applied':False,'already_applied':True,'count':len(prior['items'])}));return
            folder=Path(a.backup_dir);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            for src,label in [(a.central,'dispatch'),(a.native,'fb')]:
                dst=folder/(label+'-'+a.plan_sha256[:16]+'.sqlite3')
                require(not dst.exists(),'backup_already_exists')
                with sqlite3.connect(Path(src).resolve().as_uri()+'?mode=ro',uri=True) as s,sqlite3.connect(dst) as d:s.backup(d)
                dst.chmod(0o600)
            c.execute('BEGIN IMMEDIATE')
        else:c.execute('BEGIN')
        plan=snapshot(c,evidence)
        if a.apply:
            require(plan['plan_sha256']==a.plan_sha256,'plan_state_drift');restore(c,evidence,plan);c.commit()
        else:c.rollback()
    plan['applied']=a.apply
    path=Path(a.output);path.write_text(json.dumps(plan,indent=2));path.chmod(0o600)
    print(json.dumps({'applied':a.apply,'count':len(plan['items']),'plan_sha256':plan['plan_sha256']}))


if __name__=='__main__':main()
