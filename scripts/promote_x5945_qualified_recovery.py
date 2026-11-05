"""Promote the reviewed Nov 4 queue 5945 reconstruction before any X attempt.

The native ledger has only a frozen SOURCE request, not a completed template
result. The old COS qualification points to an incomplete upload. Preserve that
qualification in an immutable archive; accept the same source/profile/recipe's
validated reconstruction, without changing a queue, attempt, account or body.
"""
import contextlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.restore_x_random_artifact import native_zero_attempt,file_identity,require

QID=5945
NEW_SHA='3229eb6e9dd65c59755f07040ecfff9a2f7f216b0e1d259271ef91694291998a'
OLD_SHA='6e305e651bfee6860e95fec75f18ea033cb582d31b70ba6fdbeef60e3e99df07'
HOLD='operator_x5945_artifact_recovery'

def replacement(previous,probe,sha,size,url):
    require(previous['result']['output_sha256']==OLD_SHA and sha==NEW_SHA,'reviewed_bytes_changed')
    require({k:v for k,v in probe.items() if k!='size'}=={k:v for k,v in previous['result']['probe'].items() if k!='size'},'media_contract_changed')
    result=copy.deepcopy(previous['result'])
    result.update(output_sha256=sha,output_size=size,output_url=url,probe=probe)
    return result

def promote(plan,evidence,folder):
    require(len(plan['items'])==1 and plan['items'][0]['queue_id']==QID and plan['days']==['2026-11-04'],'incident_scope_changed')
    item=plan['items'][0]
    require(item['prior_log']['error_code']=='artifact_upload_incomplete' and item['stage']=='prepare','wrong_failure')
    native_zero_attempt(QID)
    with contextlib.closing(sqlite3.connect('file:'+plan['native']+'?mode=ro',uri=True)) as n:
        row=n.execute('SELECT status,request_json,result_json FROM x_post_random_template_preparation WHERE queue_id=?',(QID,)).fetchone()
        require(row and row[0]=='frozen' and json.loads(row[2])=={},'completed_result_must_not_change')
        require(n.execute('SELECT status FROM x_post_queue WHERE id=?',(QID,)).fetchone()[0]=='failed','queue_must_be_settled')
        request=json.loads(row[1])
    pid=subprocess.check_output(['systemctl','show','pipeline-node-cpu-main.service','-p','MainPID','--value'],text=True).strip()
    proc=Path('/proc')/pid;runtime=(proc/'cwd').resolve()
    os.environ.update({k.decode():v.decode() for entry in (proc/'environ').read_bytes().split(b'\0') if b'=' in entry for k,v in [entry.split(b'=',1)]})
    sys.path.insert(0,str(runtime))
    from features.pipeline_dispatch.executors import NodeExecutors,COSArtifactStore
    from features.x_posts import media_repair as repair
    from scripts.recover_x_prepost_failures import backup,writable,validate_nodes,utcnow
    validate_nodes(plan,evidence,utcnow())
    processor=NodeExecutors({'state_root':os.environ['PIPELINE_NODE_STATE_ROOT'],'backend':'cpu'})._processor('x_random')
    manifest=processor.root/(request['job_key']+'.json');saved=json.loads(manifest.read_text())
    require(saved['request']==request and not saved.get('result'),'original_recipe_changed')
    journal=COSArtifactStore(processor.cos,'x_random',request,source_key='x:queue:'+str(QID));previous=journal._read()
    require(previous and previous['result']['recipe']==saved['recipe'],'receipt_recipe_changed')
    try:journal.recover()
    except Exception as exc:require(getattr(exc,'code','')=='artifact_upload_incomplete','wrong_artifact_failure')
    else:raise RuntimeError('original_artifact_available_use_it')
    folder=Path(folder);path=folder/'artifact-cache/5945-render-in-progress.mp4'
    sha,size=file_identity(path);require((sha,size)==(NEW_SHA,126484611),'candidate_bytes_changed')
    probe=repair.validate_output(repair._probe_payload(processor.config,path,processor.repair.runner),path,(720,1280),processor.config.max_output_bytes,
        expected_duration=request['source_duration'],max_duration_seconds=request['max_duration_seconds'])
    key=processor.config.cos_prefix+'/'+request['job_key']+'/'+sha+'.mp4'
    result=replacement(previous,probe,sha,size,processor.cos.url(key))
    replacement_receipt=dict(binding=journal.binding,object_key=key,result=result,result_sha256=journal.digest(result))
    archive_key=journal.key+'.superseded-'+journal.digest(previous)+'.json'
    backups=[backup(plan['central'],folder/'qualification-backups','dispatch'),backup(plan['native'],folder/'qualification-backups','native')]
    audit=dict(queue_id=QID,plan_sha256=plan['plan_sha256'],old_receipt=previous,new_receipt=replacement_receipt,archive_key=archive_key,
        actor='codex-operator:user-Nov4-catchup',backups=backups,platform_called=False)
    audit_path=folder/'5945-qualification-recovery.json'
    require(not audit_path.exists(),'recovery_record_exists_read_before_resume')
    with audit_path.open('x') as f:json.dump(audit,f,indent=2);f.flush();os.fsync(f.fileno())
    with contextlib.closing(writable(plan['central'])) as c:
        c.execute('BEGIN IMMEDIATE')
        changed=c.execute("UPDATE pipeline_job SET hold_reason=? WHERE id=? AND status='failed' AND generation=? AND hold_reason='' AND external_intent=0",
            (HOLD,item['job_id'],item['from_generation'])).rowcount
        require(changed==1,'prepare_state_changed');c.commit()
    # The task-scoped hold prevents recovery/claiming while the uncommitted
    # qualification is superseded. Runtime writers never overwrite receipts.
    try:
        native_zero_attempt(QID)
        require(journal._read()==previous,'qualification_changed')
        processor.cos.upload(key,path,sha,size)
        existing=journal._document(archive_key)
        if existing is None:
            try:journal.client.put_object(Bucket=journal.config.cos_bucket,Key=archive_key,Body=journal.encoded(previous),ACL='private',ContentType='application/json',EnableMD5=True,Metadata={'x-cos-forbid-overwrite':'true'})
            except Exception:pass
        require(journal._document(archive_key)==previous,'original_archive_not_verified')
        require(journal._read()==previous,'qualification_changed')
        native_zero_attempt(QID)
        try:journal.client.put_object(Bucket=journal.config.cos_bucket,Key=journal.key,Body=journal.encoded(replacement_receipt),ACL='private',ContentType='application/json',EnableMD5=True)
        except Exception:pass
        require(journal._read()==replacement_receipt and journal.recover()==result,'qualification_write_requires_readback')
        audit.update(status='restored',new_sha256=sha,size=size)
    finally:
        current=journal._read()
        if current in (previous,replacement_receipt):
            with contextlib.closing(writable(plan['central'])) as c:
                c.execute("UPDATE pipeline_job SET hold_reason='' WHERE id=? AND status='failed' AND generation=? AND hold_reason=?",(item['job_id'],item['from_generation'],HOLD))
        with audit_path.open('w') as f:json.dump(audit,f,indent=2);f.flush();os.fsync(f.fileno())
    return {k:audit[k] for k in ('queue_id','status','new_sha256','size','platform_called')}
