#!/usr/bin/env python3
"""Read-only GPU recovery acceptance; default prints the plan, never restarts.

Run from the exact clean GitHub candidate with GPU_VIDEO_WORKER_TOKEN and COS
credentials supplied only through the environment. For a COMPLETED render:

  python scripts/validate_video_editing_recovery.py --mode snapshot ...
  # Operator performs a separately approved idle worker restart.
  python scripts/validate_video_editing_recovery.py --mode verify ...

Both invocations use the same --job-id, --base-url, --runtime-work-root,
--editing-work-root, --editing-state-root, and --evidence. --worker-pid must
identify the current listener each time. No POST, process signal, service
mutation, media read/download, or ledger mutation is performed by this helper.
"""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


PLAN = {
    'execution': 'not_started',
    'preferred': 'completed_readback_then_optional_isolated_faults',
    'preconditions': [
        'Exact clean GitHub candidate; save old claim intake setting and worker PID.',
        'Pause only the sole CPU legacy claim inlet; do not stop publishing services.',
        'Require live GPU runtime idle with no active children, launches, or YouTube media request.',
        'Keep live editing intake closed; never interrupt a live accepted job.',
    ],
    'completed_readback': [
        'Finish one synthetic render and snapshot completed metadata with this helper.',
        'After a separately approved idle restart, verify with the new listener PID.',
        'Require unchanged generation, completion timestamp, result and ledger hashes, COS ETag/LastModified/SHA, and no local media work directory.',
    ],
    'isolated_faults': [
        'Use loopback 127.0.0.1:18798, a random in-memory token, one renderer, and independent validation runtime/work/state/cache roots.',
        'Use only synthetic COS identities from the metadata fixture and a new validation output prefix.',
        'Download fault: after a positive partial file size while incomplete, terminate only the verified isolated parent; restart same state and same job ID.',
        'Render fault: while an isolated FFmpeg child is registered, record its boot/start identity, terminate only the isolated parent, and verify resume is blocked while the recorded child is still alive.',
        'Terminate only the positively identified isolated child, restart/reconcile, and resume the same payload/job ID with the generation returned by GET.',
        'After success, snapshot/restart/verify completion with this helper and replay the original submit only on the isolated endpoint; no generation/output change is permitted.',
        'If the synthetic file completes before fault injection, mark that case untested; never enlarge scope to a live job.',
    ],
    'finish': [
        'Stop only the independently identified temporary listener after its jobs complete.',
        'Verify live worker and publisher PIDs/health unchanged, then restore the original CPU claim setting.',
        'Retain metadata/receipts; remove only positively scoped temporary media after no child remains. Never delete shared ledgers.',
    ],
}


class ValidationError(Exception):
    pass


def require(value,code):
    if not value:raise ValidationError(code)


def file_hash(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size<=4*1024*1024,'metadata_file_invalid')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listener_identity(pid,port,proc=Path('/proc')):
    process=proc/str(pid)
    stat=(process/'stat').read_text();fields=stat[stat.rfind(')')+2:].split()
    require(len(fields)>19,'worker_identity_unverified')
    sockets=set()
    for fd in (process/'fd').iterdir():
        try:target=os.readlink(fd)
        except FileNotFoundError:continue
        match=re.fullmatch(r'socket:\[(\d+)\]',target)
        if match:sockets.add(match[1])
    found=False
    for line in (process/'net/tcp').read_text().splitlines()[1:]:
        row=line.split()
        if len(row)>9 and row[9] in sockets and row[3]=='0A':
            address,hex_port=row[1].split(':')
            found|=address=='0100007F' and int(hex_port,16)==port
    require(found,'worker_loopback_listener_unverified')
    return {'pid':pid,'start_ticks':fields[19],'boot_id':(proc/'sys/kernel/random/boot_id').read_text().strip()}


def get_json(base,path,token):
    import requests
    with requests.Session() as session:
        session.trust_env=False
        response=session.get(base+path,headers={'Authorization':'Bearer '+token},timeout=(3,10),allow_redirects=False)
    require(response.status_code==200,'readback_http_failed')
    value=response.json();require(isinstance(value,dict),'readback_response_invalid')
    return value.get('item',value)


def cos_receipt(artifact,client):
    require(isinstance(artifact,dict) and re.fullmatch(r'[0-9a-f]{64}',str(artifact.get('sha256',''))),'output_identity_invalid')
    require(str(artifact.get('key','')).startswith('video-editing/'),'output_namespace_invalid')
    headers={str(k).lower():str(v) for k,v in client.head_object(Bucket=artifact['bucket'],Key=artifact['key']).items()}
    require(int(headers.get('content-length','-1'))==artifact['size_bytes'],'output_size_mismatch')
    require(headers.get('x-cos-meta-sha256')==artifact['sha256'],'output_sha_mismatch')
    grants=client.get_object_acl(Bucket=artifact['bucket'],Key=artifact['key']).get('AccessControlList',{}).get('Grant',[])
    if isinstance(grants,dict):grants=[grants]
    require(grants and all(not g.get('Grantee',{}).get('URI') and str(g.get('Grantee',{}).get('Type','')).lower()!='group' for g in grants),'output_acl_unverified')
    require(headers.get('etag') and headers.get('last-modified'),'output_receipt_incomplete')
    return {key:headers.get(key,'') for key in ('etag','last-modified','content-length','x-cos-meta-sha256','x-cos-meta-size')}


def snapshot(args,client):
    parsed=urlsplit(args.base_url)
    require(parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port and not parsed.path and
            not parsed.query and not parsed.fragment and not parsed.username and not parsed.password,'loopback_url_required')
    require(re.fullmatch(r've_[0-9a-f]{32}',args.job_id),'job_id_invalid')
    token=os.environ.get('GPU_VIDEO_WORKER_TOKEN','');require(token,'worker_token_missing')
    worker=listener_identity(args.worker_pid,parsed.port)
    health=get_json(args.base_url,'/healthz',token)
    require(health.get('ok') and health.get('render_concurrency')==1,'single_slot_health_required')
    job=get_json(args.base_url,'/api/gpu-video/video-editing/jobs/'+args.job_id,token)
    result=job.get('result') or {}
    require(job.get('job_id')==args.job_id and job.get('status')=='completed' and result.get('operation')=='render' and result.get('job_id')==args.job_id,'completed_render_required')
    state=args.editing_state_root/args.job_id
    artifact=result.get('artifact') or {}
    require(not (args.editing_work_root/args.job_id).exists(),'completed_media_not_cleaned')
    record=args.runtime_work_root/'.runtime/jobs'/(args.job_id+'.json')
    raw=json.loads(record.read_text(encoding='utf-8'))
    require(raw.get('status')=='completed' and not raw.get('_children') and not raw.get('_launches') and not raw.get('_resource_blocked'),'completed_process_state_unverified')
    # The HTTP DTO is untrusted here: persist only fixed fields, never URLs,
    # bearer values, raw commands, payloads or unfiltered exception messages.
    identity={k:artifact[k] for k in ('bucket','key','sha256','size_bytes','binding') if k in artifact}
    result_identity={k:result[k] for k in ('job_id','task_type','operation','recipe_sha256')}
    result_identity['artifact']=identity
    return {'version':1,'worker':worker,'release_sha':health.get('release_sha'),
            'job':{k:job[k] for k in ('job_id','fingerprint','generation','completed_at','status')},
            'result':result_identity,'cos':cos_receipt(artifact,client),
            'runtime_record_sha256':file_hash(record),'result_manifest_sha256':file_hash(state/'result.json'),
            'media_work_directory_absent':True}


def verify(before,after):
    require(before.get('version')==1 and after.get('version')==1,'snapshot_version_invalid')
    require(before['worker']!=after['worker'],'worker_was_not_restarted')
    for field in ('release_sha','job','result','cos','runtime_record_sha256','result_manifest_sha256','media_work_directory_absent'):
        require(before.get(field)==after.get(field),'completed_result_changed_'+field)
    return {'ok':True,'job_id':after['job']['job_id'],'generation':after['job']['generation'],
            'restarted':True,'output_recreated':False,'media_work_directory_absent':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('plan','snapshot','verify'),default='plan')
    parser.add_argument('--base-url',default='http://127.0.0.1:18798')
    parser.add_argument('--job-id')
    parser.add_argument('--worker-pid',type=int)
    parser.add_argument('--runtime-work-root',type=Path)
    parser.add_argument('--editing-work-root',type=Path)
    parser.add_argument('--editing-state-root',type=Path)
    parser.add_argument('--evidence',type=Path)
    args=parser.parse_args()
    if args.mode=='plan':print(json.dumps(PLAN,indent=2));return 0
    os.umask(0o077);logging.disable(logging.CRITICAL)
    try:
        require(all((args.job_id,args.worker_pid,args.runtime_work_root,args.editing_work_root,args.editing_state_root,args.evidence)),'required_argument_missing')
        require(all(path.is_absolute() for path in (args.runtime_work_root,args.editing_work_root,args.editing_state_root,args.evidence)),'absolute_paths_required')
        require(not args.evidence.is_symlink() and args.evidence.parent.is_dir(),'evidence_path_invalid')
        from qcloud_cos import CosConfig,CosS3Client
        secret_id=os.environ.get('COS_SECRET_ID','');secret_key=os.environ.get('COS_SECRET_KEY','')
        require(secret_id and secret_key,'cos_credentials_missing')
        region=os.environ.get('VIDEO_EDITING_COS_REGION') or os.environ.get('COS_REGION','ap-hongkong')
        client=CosS3Client(CosConfig(Region=region,SecretId=secret_id,SecretKey=secret_key,Scheme='https',Timeout=10),retry=0)
        current=snapshot(args,client)
        if args.mode=='snapshot':
            with args.evidence.open('x',encoding='utf-8') as stream:json.dump(current,stream,sort_keys=True,indent=2)
            answer={'ok':True,'mode':'snapshot','job_id':args.job_id,'evidence':str(args.evidence)}
        else:
            before=json.loads(args.evidence.read_text(encoding='utf-8'));answer=verify(before,current)
            with args.evidence.with_name(args.evidence.name+'.verified.json').open('x',encoding='utf-8') as stream:
                json.dump({'verification':answer,'after':current},stream,sort_keys=True,indent=2)
    except ValidationError as exc:answer={'ok':False,'code':str(exc)}
    except Exception:answer={'ok':False,'code':'recovery_verification_failed'}
    print(json.dumps(answer,sort_keys=True));return 0 if answer.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
