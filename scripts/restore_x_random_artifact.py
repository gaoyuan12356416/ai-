"""Restore an exact qualified X random artifact from its original CPU recipe.

No queue/log reset or X request. The immutable COS receipt must match every
regenerated result field before the original content-addressed object uploads.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

def require(value,code):
    if not value:raise RuntimeError(code)

def native_zero_attempt(queue_id):
    with contextlib.closing(sqlite3.connect('file:/var/lib/x-post-automation/accounts.sqlite3?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        q=c.execute('SELECT * FROM x_post_queue WHERE id=?',(queue_id,)).fetchone()
        log=c.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(queue_id,)).fetchone()
        require(q and log and q['random_template_enabled']==1 and q['delivery_mode']=='direct','wrong_queue_kind')
        require(q['status'] in ('queued','failed') and not any(log[k] for k in ('attempt_count','unknown_outcome','x_media_id','x_post_id','published_at')),'publication_evidence_present')
        require(not c.execute('SELECT 1 FROM pipeline_x_operation WHERE source_key=?',('x:queue:'+str(queue_id),)).fetchone(),'operation_evidence_present')
        return dict(q)

def exact_callback(journal,expected):
    def callback(result,key):
        require(result==expected['result'] and key==expected['object_key'],'regenerated_bytes_differ_from_frozen_receipt')
        journal.qualified(result,key)
    return callback

def file_identity(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest(),Path(path).stat().st_size

def preserve_qualified(path,target,expected):
    """Keep qualified bytes across a failed upload and temporary-dir cleanup."""
    require(file_identity(path)==(expected['output_sha256'],expected['output_size']),'cached_bytes_differ')
    target=Path(target);target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if target.exists():
        require(file_identity(target)==file_identity(path),'cache_conflict')
    else:os.link(path,target)
    return target

def exact_restore(processor,request,kind,queue_id,journal):
    expected=journal._read();require(expected is not None,'qualified_receipt_missing')
    try:
        if journal.recover() is not None:return dict(queue_id=queue_id,status='already_available',platform_called=False)
    except Exception as exc:
        require(getattr(exc,'code','')=='artifact_upload_incomplete','unexpected_artifact_state')
    native_zero_attempt(queue_id)
    cache=Path('/mnt/data-disk/x-publish-recovery-20261104-on-20261105/artifact-cache')/(str(queue_id)+'-'+expected['result']['output_sha256']+'.mp4')
    processor.qualified_artifact=exact_callback(journal,expected)
    original=processor.cos.client.upload_file
    def upload(*args,**kwargs):
        try:return original(*args,**kwargs)
        except Exception as exc:
            details={'event':'cos_upload_error','queue_id':queue_id,'error_type':type(exc).__name__}
            for method,name in (('get_status_code','http_status'),('get_error_code','provider_code'),('get_error_msg','provider_message')):
                getter=getattr(exc,method,None)
                if callable(getter):details[name]=str(getter())[:160]
            print(json.dumps(details),flush=True);raise
    processor.cos.client.upload_file=upload
    original_upload=processor.cos.upload
    def durable_upload(key,path,sha,size,**kwargs):
        require(key==expected['object_key'] and (sha,size)==(expected['result']['output_sha256'],expected['result']['output_size']),'upload_identity_changed')
        preserve_qualified(path,cache,expected['result'])
        native_zero_attempt(queue_id)
        return original_upload(key,path,sha,size,**kwargs)
    processor.cos.upload=durable_upload
    if cache.exists():
        require(file_identity(cache)==(expected['result']['output_sha256'],expected['result']['output_size']),'cache_conflict')
        processor.cos.upload(expected['object_key'],cache,expected['result']['output_sha256'],expected['result']['output_size'])
    else:
        print(json.dumps({'event':'restoring_original_bytes','queue_id':queue_id,'kind':kind,
            'expected_sha256':expected['result']['output_sha256']}),flush=True)
        if kind=='x_repair':processor.repair(request)
        else:processor.prepare(request)
    require(journal.recover()==expected['result'],'restoration_verification_failed')
    return dict(queue_id=queue_id,status='restored',sha256=expected['result']['output_sha256'],size=expected['result']['output_size'],platform_called=False)

def restore(queue_id):
    queue=native_zero_attempt(queue_id)
    unit='pipeline-node-cpu-main.service'
    pid=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip()
    proc=Path('/proc')/pid;require(pid.isdigit() and int(pid)>1,'cpu_node_missing')
    runtime=(proc/'cwd').resolve()
    env={k.decode():v.decode() for entry in (proc/'environ').read_bytes().split(b'\0') if b'=' in entry for k,v in [entry.split(b'=',1)]}
    require(env.get('PIPELINE_NODE_ID')=='cpu-main','wrong_node_environment')
    os.environ.update(env);sys.path.insert(0,str(runtime))
    from features.pipeline_dispatch.executors import NodeExecutors,COSArtifactStore
    executor=NodeExecutors({'state_root':env['PIPELINE_NODE_STATE_ROOT'],'backend':'cpu'})
    processor=executor._processor('x_random')
    matches=[]
    for path in processor.root.glob('*.json'):
        value=json.loads(path.read_text())
        if value.get('request',{}).get('queue_id')==queue_id:matches.append((path,value))
    require(len(matches)<=1,'original_recipe_not_unique')
    if matches:
        path,saved=matches[0];request=saved['request']
        journal=COSArtifactStore(processor.cos,'x_random',request,source_key='x:queue:'+str(queue_id))
        expected=journal._read();require(expected is not None,'qualified_receipt_missing')
        require(expected['result']['profile']==processor.config.profile and expected['result']['recipe']==saved['recipe'],'original_recipe_or_backend_changed')
        return exact_restore(processor,request,'x_random',queue_id,journal)
    # A repair failed before its local ready manifest could be written. Rebuild
    # the request through the original preflight; the private receipt binding
    # still verifies its exact source hash, size, trigger, profile and job key.
    with contextlib.closing(sqlite3.connect('file:/var/lib/x-post-automation/accounts.sqlite3?mode=ro',uri=True)) as c:
        row=c.execute("SELECT descriptor_json FROM pipeline_x_claim WHERE source_key=? AND stage='prepare' ORDER BY generation DESC LIMIT 1",('x:queue:'+str(queue_id),)).fetchone()
    require(row is not None,'original_claim_missing');descriptor=json.loads(row[0])
    require(descriptor['kind']=='x_preflight','original_preflight_missing')
    class Restored(Exception):pass
    completed=[]
    def restore_processor(kind,selected,request,context):
        journal=COSArtifactStore(selected.cos,kind,request,source_key='x:queue:'+str(queue_id))
        completed.append(exact_restore(selected,request,kind,queue_id,journal))
        raise Restored()
    class Context:
        def check(self):native_zero_attempt(queue_id)
        def phase(self,*args):pass
        def checkpoint(self,*args):pass
    executor._prepare_processor=restore_processor
    try:executor.x_preflight(descriptor['request'],Context())
    except Restored:return completed[0]
    raise RuntimeError('no_qualified_artifact_to_restore')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--queue-id',type=int,required=True)
    args=parser.parse_args()
    try:print(json.dumps(restore(args.queue_id)),flush=True)
    except Exception as exc:
        print(json.dumps({'queue_id':args.queue_id,'status':'failed','code':getattr(exc,'code',type(exc).__name__),
            'reason':str(exc)[:160] if isinstance(exc,RuntimeError) else type(exc).__name__}),flush=True)
        raise SystemExit(1)
