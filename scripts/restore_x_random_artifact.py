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
    require(len(matches)==1,'original_recipe_not_unique')
    path,saved=matches[0];request=saved['request']
    journal=COSArtifactStore(processor.cos,'x_random',request,source_key='x:queue:'+str(queue_id))
    expected=journal._read();require(expected is not None,'qualified_receipt_missing')
    require(expected['result']['profile']==processor.config.profile and expected['result']['recipe']==saved['recipe'],'original_recipe_or_backend_changed')
    try:
        recovered=journal.recover()
        if recovered is not None:return dict(queue_id=queue_id,status='already_available',platform_called=False)
    except Exception as exc:
        require(getattr(exc,'code','')=='artifact_upload_incomplete','unexpected_artifact_state')
    native_zero_attempt(queue_id)
    processor.qualified_artifact=exact_callback(journal,expected)
    original=processor.cos.client.upload_file
    def upload(*args,**kwargs):
        try:return original(*args,**kwargs)
        except Exception as exc:
            details={'event':'cos_upload_error','queue_id':queue_id,'error_type':type(exc).__name__}
            for method,name in (('get_status_code','http_status'),('get_error_code','provider_code')):
                getter=getattr(exc,method,None)
                if callable(getter):details[name]=str(getter())[:80]
            print(json.dumps(details),flush=True);raise
    processor.cos.client.upload_file=upload
    print(json.dumps({'event':'restoring_original_bytes','queue_id':queue_id,'duration':request['source_duration'],
        'expected_sha256':expected['result']['output_sha256'],'recipe_manifest_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}),flush=True)
    result=processor.prepare(request)
    require(result==expected['result'] and journal.recover()==result,'restoration_verification_failed')
    return dict(queue_id=queue_id,status='restored',sha256=result['output_sha256'],size=result['output_size'],platform_called=False)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--queue-id',type=int,required=True)
    args=parser.parse_args()
    try:print(json.dumps(restore(args.queue_id)),flush=True)
    except Exception as exc:
        print(json.dumps({'queue_id':args.queue_id,'status':'failed','code':getattr(exc,'code',type(exc).__name__),
            'reason':str(exc)[:160] if isinstance(exc,RuntimeError) else type(exc).__name__}),flush=True)
        raise SystemExit(1)
