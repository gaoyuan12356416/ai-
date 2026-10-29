#!/usr/bin/env python3
"""Metadata-only isolated COS/catalog canary. Never reads or downloads media.

Supply one JSON object on stdin, capture stdout in a subprocess pipe, and never
log the sign response. Examples (all operations require a stable run_id):
  {"op":"create","run_id":"canary_20261022","operation_id":"asset_001",
   "name":"asset.mp4","size_bytes":18000000,"content_type":"video/mp4",
   "library_kind":"material","series_code":"21481"}
  {"op":"sign","run_id":"canary_20261022","upload_id":"vu_...",
   "part_number":1,"content_md5":"..."}
  {"op":"reconcile|complete|abort","run_id":"canary_20261022","upload_id":"vu_..."}
  {"op":"catalog_products","run_id":"canary_20261022"}
  {"op":"catalog_dramas","run_id":"canary_20261022","app_id":"...","q":"..."}
  {"op":"catalog_episodes","run_id":"canary_20261022","app_id":"...","content_id":"..."}

The live app supplies existing configuration and catalog readers. Only the
video_editing package comes from this candidate checkout. State/COS keys are
isolated under validation/<run_id>; no production editing ledger is opened.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sys


def load_environment(paths):
    """Read literal environment files, never source/execute shell content."""
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line=line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):line=line[7:].lstrip()
            key,separator,value=line.partition('=')
            key=key.strip();value=value.strip()
            if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key):
                raise ValueError('invalid_environment_file')
            if value.startswith(('"',"'")):
                parsed=shlex.split(value,comments=True,posix=True)
                if len(parsed)!=1:raise ValueError('invalid_environment_value')
                value=parsed[0]
            os.environ[key]=value


def load_live_app(live_root,candidate):
    live_root=live_root.resolve(strict=True)
    sys.path.insert(0,str(live_root))
    spec=importlib.util.spec_from_file_location('_video_editing_validation_live_app',live_root/'app.py')
    app=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=app
    spec.loader.exec_module(app)
    # Keep other feature packages on the live runtime; replace only the owned
    # candidate package, including when the live app already imported its copy.
    import features
    for name in list(sys.modules):
        if name=='features.video_editing' or name.startswith('features.video_editing.'):
            del sys.modules[name]
    package=candidate/'features/video_editing'
    spec=importlib.util.spec_from_file_location('features.video_editing',package/'__init__.py',
                                               submodule_search_locations=[str(package)])
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    features.video_editing=module
    spec.loader.exec_module(module)
    return app


def dispatch(payload,app,root):
    from features.video_editing import require
    from features.video_editing.store import Store
    from features.video_editing.uploads import Uploads
    from features.video_editing.catalog import Catalog
    require(isinstance(payload,dict),'invalid_json','请提供 JSON 对象')
    run_id=str(payload.get('run_id',''))
    require(re.fullmatch(r'[A-Za-z0-9_-]{8,80}',run_id),'invalid_run_id','验证标识无效')
    op=payload.get('op')
    require(op in {'create','sign','reconcile','complete','abort','catalog_products','catalog_dramas','catalog_episodes'},
            'invalid_operation','验证操作无效')
    root=root.resolve()
    location=root/run_id
    require(not location.is_symlink() and location.resolve().parent==root,'invalid_validation_path','验证路径无效')
    db_path=location/'state.sqlite3'
    require(not db_path.is_symlink(),'invalid_validation_path','验证路径无效')
    store=Store(db_path)
    actor={'tenant_key':'video-editing-validation','user_id':run_id,'name':'COS validation','is_admin':False}
    if op.startswith('catalog_'):
        catalog=Catalog(store,app)
        if op=='catalog_products':
            items=[{k:v for k,v in row.items() if k in {'app_id','name','product_name','app_name','label','app'}} for row in catalog.products()]
        elif op=='catalog_dramas':
            items=[{k:v for k,v in row.items() if k!='cover_url'} for row in catalog.dramas(payload.get('app_id',''),payload.get('q',''))]
        else:
            items=[{'source_id':row['id'],'name':row['name'],'status':row['status'],
                    **{k:row['data'][k] for k in ('app_id','content_id','episode_number')}}
                   for row in catalog.episodes(payload.get('app_id',''),payload.get('content_id',''),actor)]
        return {'ok':True,'op':op,'items':items,'total':len(items)}
    from qcloud_cos import CosConfig,CosS3Client
    bucket=os.environ.get('VIDEO_EDITING_COS_BUCKET',getattr(app,'COS_BUCKET',''))
    region=os.environ.get('VIDEO_EDITING_COS_REGION',getattr(app,'COS_REGION',''))
    secret_id=getattr(app,'COS_SECRET_ID','');secret_key=getattr(app,'COS_SECRET_KEY','')
    require(bucket and region and secret_id and secret_key,'cos_not_configured','COS 配置不完整',503)
    conf=CosConfig(Region=region,SecretId=secret_id,SecretKey=secret_key,Scheme='https',Timeout=30,KeepAlive=False)
    uploads=Uploads(store,CosS3Client(conf,retry=0),bucket,region,'video-editing/validation/'+run_id)
    uploads.resource_resolver=Catalog(store,app).resource
    if op=='create':row=uploads.create(payload,actor)
    elif op=='sign':
        from features.video_editing import uploads as uploads_module
        expires=payload.get('validation_expiry_seconds',600)
        require(type(expires) is int and 1<=expires<=600,'invalid_validation_expiry','验证签名时长须为1至600秒')
        previous=uploads_module.SIGN_SECONDS
        try:
            uploads_module.SIGN_SECONDS=expires
            return {'ok':True,'op':op,'item':uploads.sign(str(payload.get('upload_id','')),payload.get('part_number'),payload,actor)}
        finally:
            uploads_module.SIGN_SECONDS=previous
    else:row=getattr(uploads,op)(str(payload.get('upload_id','')),actor)
    item=uploads.dto(row)
    if row['status']=='completed':
        # Object location is safe evidence; it contains no signature or key.
        source=store.get('source',row['asset_id'])
        item['object_identity']=source['data']['identity']
        item['private_acl_verified']=True
    return {'ok':True,'op':op,'item':item}


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-root',type=Path,default=Path('/root/drama_material_service'))
    parser.add_argument('--validation-root',type=Path,default=Path('/mnt/data-disk/video-editing/validation'))
    parser.add_argument('--env-file',type=Path,action='append',default=[])
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        raw=sys.stdin.read(65537)
        if len(raw)>65536:raise ValueError('request_too_large')
        payload=json.loads(raw)
        # A signature is usable authority. Refuse interactive terminal output;
        # the local test orchestrator must consume it in memory via a pipe.
        if isinstance(payload,dict) and payload.get('op')=='sign' and sys.stdout.isatty():
            raise ValueError('signature_requires_pipe')
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            load_environment([args.live_root/'.env',Path('/etc/drama-synthesis/cpu.env'),
                              Path('/etc/video-editing/video-editing.env'),*args.env_file])
            app=load_live_app(args.live_root,Path(__file__).resolve().parents[1])
            result=dispatch(payload,app,args.validation_root)
    except Exception as exc:
        # Never echo external exception text: transport errors can embed signed
        # URLs, SQL credentials, or environment values. Codes are allowlisted.
        code=getattr(exc,'code','validation_failed')
        if not isinstance(code,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,80}',code):code='validation_failed'
        result={'ok':False,'code':code,'error':'验证未完成；请根据错误码检查隔离会话或配置'}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')))
    return 0 if result.get('ok') else 1


if __name__=='__main__':
    raise SystemExit(main())
