"""Complete a reviewed Nov 4 reconstruction before any committed X result.

Only the four currently known lost random artifacts are in scope. The original
qualification is archived; all source, recipe and measured media properties
must match. Existing final native results, attempts and unknown writes exclude
this recovery. This helper never calls X or resets a queue.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.restore_x_random_artifact import native_zero_attempt,file_identity,require

SCOPE=frozenset((5923,5925,6009,6041))
BYTE_FIELDS=frozenset(('output_sha256','output_size','output_url','probe'))

def validate_candidate(record,qid,reviewed_sha):
    require(qid in SCOPE and record['queue_id']==qid,'outside_incident_scope')
    old=record['old_receipt'];new=record['candidate_receipt']
    require(new['binding']==old['binding'],'source_binding_changed')
    require({k:v for k,v in old['result'].items() if k not in BYTE_FIELDS}==
        {k:v for k,v in new['result'].items() if k not in BYTE_FIELDS},'source_recipe_or_profile_changed')
    require({k:v for k,v in old['result']['probe'].items() if k!='size'}==
        {k:v for k,v in new['result']['probe'].items() if k!='size'},'media_contract_changed')
    require(new['result']['output_sha256']==reviewed_sha and len(reviewed_sha)==64,'reviewed_bytes_changed')
    return old,new

def native_uncommitted(qid,request):
    native_zero_attempt(qid)
    with contextlib.closing(sqlite3.connect('file:/var/lib/x-post-automation/accounts.sqlite3?mode=ro',uri=True)) as n:
        row=n.execute('SELECT status,request_json,result_json FROM x_post_random_template_preparation WHERE queue_id=?',(qid,)).fetchone()
        require(row and row[0]=='frozen' and json.loads(row[1])==request and json.loads(row[2])=={},'completed_native_result_must_not_change')
        require(n.execute('SELECT status FROM x_post_queue WHERE id=?',(qid,)).fetchone()[0]=='failed','queue_not_settled')
        row=n.execute('SELECT artifact_json FROM pipeline_x_artifact WHERE queue_id=?',(qid,)).fetchone()
        if row:validate_native_input(json.loads(row[0]),request)

def validate_native_input(artifact,request):
    # pipeline_x_artifact records the preflight INPUT. It is intentionally
    # retained; the final random output lives in result_json checked above.
    require(artifact.get('sha256')==request['source_sha256'] and artifact.get('size')==request['source_size']
        and artifact.get('url')==request['source_url'],'native_artifact_is_not_frozen_input')

def promote(plan,evidence,record,reviewed_sha,folder):
    qid=record['queue_id'];old,new=validate_candidate(record,qid,reviewed_sha)
    require(len(plan['items'])==1 and plan['items'][0]['queue_id']==qid and plan['days']==['2026-11-04'],'plan_scope_changed')
    item=plan['items'][0]
    require(item['stage']=='prepare' and item['prior_log']['error_code']=='artifact_upload_incomplete','wrong_failure')
    pid=subprocess.check_output(['systemctl','show','pipeline-node-cpu-main.service','-p','MainPID','--value'],text=True).strip()
    proc=Path('/proc')/pid
    os.environ.update({k.decode():v.decode() for e in (proc/'environ').read_bytes().split(b'\0') if b'=' in e for k,v in [e.split(b'=',1)]})
    sys.path.insert(0,str((proc/'cwd').resolve()))
    from features.pipeline_dispatch.executors import NodeExecutors,COSArtifactStore
    from features.x_posts import media_repair as repair
    from qcloud_cos import CosConfig,CosS3Client
    from scripts.recover_x_prepost_failures import backup,writable,validate_nodes,utcnow
    validate_nodes(plan,evidence,utcnow())
    processor=NodeExecutors({'state_root':os.environ['PIPELINE_NODE_STATE_ROOT'],'backend':'cpu'})._processor('x_random')
    cfg=processor.config
    processor.cos.client=CosS3Client(CosConfig(Region=cfg.cos_region,SecretId=cfg.cos_secret_id,SecretKey=cfg.cos_secret_key,Timeout=120,KeepAlive=False))
    saved=json.loads((processor.root/(old['result']['job_key']+'.json')).read_text());request=saved['request']
    require(not saved.get('result') and saved['recipe']==old['result']['recipe'],'original_manifest_changed')
    native_uncommitted(qid,request)
    journal=COSArtifactStore(processor.cos,'x_random',request,source_key='x:queue:'+str(qid))
    require(journal.binding==old['binding'] and journal.digest(old['result'])==old['result_sha256']
        and journal.digest(new['result'])==new['result_sha256'],'qualification_identity_changed')
    folder=Path(folder);cache=(folder/'artifact-cache').resolve();path=Path(record['local_path']).resolve()
    require(path.parent==cache and file_identity(path)==(reviewed_sha,new['result']['output_size']),'candidate_bytes_changed')
    probe=repair.validate_output(repair._probe_payload(cfg,path,processor.repair.runner),path,(720,1280),cfg.max_output_bytes,
        expected_duration=request['source_duration'],max_duration_seconds=request['max_duration_seconds'])
    require(probe==new['result']['probe'],'candidate_probe_changed')
    require(new['object_key']==cfg.cos_prefix+'/'+request['job_key']+'/'+reviewed_sha+'.mp4'
        and new['result']['output_url']==processor.cos.url(new['object_key']),'destination_changed')
    current=journal._read();require(current in (old,new),'qualification_changed')
    if current==new and journal.recover()==new['result']:
        return dict(queue_id=qid,status='already_restored',platform_called=False)
    try:journal.recover()
    except Exception as exc:require(getattr(exc,'code','')=='artifact_upload_incomplete','wrong_artifact_state')
    else:raise RuntimeError('original_artifact_available_use_it')
    audit_path=folder/(str(qid)+'-qualification-recovery.json')
    archive_key=journal.key+'.superseded-'+journal.digest(old)+'.json'
    if audit_path.exists():
        audit=json.loads(audit_path.read_text());require(audit['old_receipt']==old and audit['new_receipt']==new,'recovery_record_changed')
    else:
        audit=dict(queue_id=qid,old_receipt=old,new_receipt=new,archive_key=archive_key,plan_sha256=plan['plan_sha256'],
            actor='codex-operator:user-Nov4-catchup',platform_called=False,
            backups=[backup(plan['native'],folder/'qualification-backups','native'),backup(plan['central'],folder/'qualification-backups','dispatch')])
        with audit_path.open('x') as f:json.dump(audit,f,indent=2);f.flush();os.fsync(f.fileno())
    hold='operator_catchup_artifact_'+str(qid)
    with contextlib.closing(writable(plan['central'])) as c:
        c.execute('BEGIN IMMEDIATE')
        changed=c.execute("UPDATE pipeline_job SET hold_reason=? WHERE id=? AND status='failed' AND generation=? AND hold_reason IN ('',?) AND external_intent=0",
            (hold,item['job_id'],item['from_generation'],hold)).rowcount
        require(changed==1,'prepare_state_changed');c.commit()
    try:
        native_uncommitted(qid,request);require(journal._read()==old,'qualification_changed')
        processor.cos.upload(new['object_key'],path,reviewed_sha,new['result']['output_size'])
        if journal._document(archive_key) is None:
            try:journal.client.put_object(Bucket=cfg.cos_bucket,Key=archive_key,Body=journal.encoded(old),ACL='private',ContentType='application/json',EnableMD5=True,Metadata={'x-cos-forbid-overwrite':'true'})
            except Exception:pass
        require(journal._document(archive_key)==old,'archive_not_verified')
        native_uncommitted(qid,request);require(journal._read()==old,'qualification_changed')
        try:journal.client.put_object(Bucket=cfg.cos_bucket,Key=journal.key,Body=journal.encoded(new),ACL='private',ContentType='application/json',EnableMD5=True)
        except Exception:pass
        require(journal._read()==new and journal.recover()==new['result'],'qualification_requires_readback')
        audit.update(status='restored',original_receipt_archived=True)
    finally:
        if journal._read() in (old,new):
            with contextlib.closing(writable(plan['central'])) as c:
                c.execute("UPDATE pipeline_job SET hold_reason='' WHERE id=? AND status='failed' AND generation=? AND hold_reason=?",(item['job_id'],item['from_generation'],hold))
        with audit_path.open('w') as f:json.dump(audit,f,indent=2);f.flush();os.fsync(f.fileno())
    return dict(queue_id=qid,status=audit['status'],new_sha256=reviewed_sha,size=new['result']['output_size'],platform_called=False)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--plan',required=True);p.add_argument('--evidence',required=True);p.add_argument('--candidate',required=True);p.add_argument('--reviewed-sha',required=True);p.add_argument('--folder',required=True);args=p.parse_args()
    print(json.dumps(promote(json.loads(Path(args.plan).read_text()),json.loads(Path(args.evidence).read_text()),json.loads(Path(args.candidate).read_text()),args.reviewed_sha,args.folder)),flush=True)
