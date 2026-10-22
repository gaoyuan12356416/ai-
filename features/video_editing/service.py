"""Video editing application service and immutable render submissions."""
import copy
import hashlib
import os
import re
import threading
from pathlib import Path

from . import VideoEditingError, require
from .catalog import Catalog, probe_identity
from .store import Store, can_manage, encode, identifier, now, owner
from .uploads import Uploads, MAX_BYTES, PART_BYTES

PROFILE='video-editing-720-v1'
TERMINAL={'completed','failed','cancelled'}


def operation_id(value):
    value=str(value or '')
    require(re.fullmatch(r'[A-Za-z0-9_-]{8,128}',value),'operation_id_required','请提供操作标识')
    return value


class Service:
    def __init__(self,store,uploads,app=None,gpu_url='',gpu_token='',intake_enabled=True):
        self.store,self.uploads,self.app=store,uploads,app
        self.catalog=Catalog(store,app) if app is not None else None
        self.gpu_url,self.gpu_token=gpu_url.rstrip('/'),gpu_token
        self._intake_enabled=bool(intake_enabled)

    @property
    def intake_enabled(self):
        return self._intake_enabled and not Path(os.environ.get('VIDEO_EDITING_DISABLED_FILE','/etc/video-editing/intake.disabled')).exists()

    def require_intake(self):
        require(self.intake_enabled,'video_editing_intake_disabled','新剪辑任务已暂停，已有任务仍可查询和恢复',503)

    def config(self):
        return {'enabled':self.intake_enabled,'intake_enabled':self.intake_enabled,'max_file_bytes':MAX_BYTES,'part_size':PART_BYTES,'max_dramas':20,'profile':PROFILE,
                'worker_configured':bool(self.gpu_url and self.gpu_token),'cos_configured':bool(self.uploads.client and self.uploads.bucket)}

    def signed_url(self,artifact,download=False):
        if not artifact or not artifact.get('key') or not self.uploads.client:
            return ''
        # All exposed private URLs point to our own dedicated COS namespace.
        if artifact.get('bucket')!=self.uploads.bucket or not artifact['key'].startswith(self.uploads.prefix+'/'):
            return ''
        params={'response-content-disposition':'attachment'} if download else {}
        try:
            return self.uploads.client.get_presigned_url(Bucket=self.uploads.bucket,Key=artifact['key'],Method='GET',Expired=900,Params=params)
        except Exception:
            return ''

    def source(self,sid,actor,db=None,ready=False):
        row=self.store.get('source',sid,db)
        require(row and row['status']!='deleted' and (row['kind']=='episode' or row['status']=='ready' or can_manage(row,actor)),
                'source_not_found','素材不存在或尚不可使用',404)
        if row['kind']=='episode' and self.catalog is not None:
            self.catalog.require_product(row['data'].get('app_id',''))
        if ready:
            require(row['status']=='ready','source_not_ready','请等待所有素材校验完成',409)
        return row

    def source_dto(self,row,actor):
        d=row['data']; identity=d.get('identity',{})
        preview=d.get('preview') or (identity if row['kind']=='asset' else {})
        result={'id':row['id'],'source_id':row['id'],'kind':row['kind'],'name':row['name'],'status':row['status'],
                'owner_user_id':d.get('owner_user_id',''),'owner_name':d.get('owner_name',''),
                'can_manage':row['kind']=='asset' and can_manage(row,actor),'size_bytes':identity.get('size_bytes',d.get('size_bytes',0)),
                'metadata':d.get('metadata',{}),'preview_url':self.signed_url(preview),
                'thumbnail_url':self.signed_url(d.get('thumbnail',{})),'created_at':row['created_at'],'updated_at':row['updated_at'],
                'expires_at':now()+900,
                'probe_task_id':d.get('probe_task_id',''),
                'error_code':d.get('error_code',''),'error_message':d.get('error_message','')}
        for k in ('app_id','content_id','episode_number','title'):
            if k in d: result[k]=d[k]
        if row['kind']=='episode' and not result['preview_url'] and row['status']=='ready': result['preview_url']=d.get('url','')
        return result

    def assets(self,actor,query='',status='',limit=50,offset=0):
        conditions=["kind='asset'","status<>'deleted'"]; args=[]
        if status=='all':
            if not actor.get('is_admin'):
                conditions.append("(status='ready' OR owner=?)");args.append(owner(actor))
        else:
            conditions.append("status='ready'")
        if query:
            conditions.append('name LIKE ?');args.append('%'+str(query)[:100]+'%')
        rows,total=self.store.list('source',' AND '.join(conditions),tuple(args),min(100,max(1,int(limit))),max(0,int(offset)))
        return {'items':[self.source_dto(r,actor) for r in rows],'total':total}

    def mutate_asset(self,sid,actor,name=None,delete=False):
        with self.store.transaction() as db:
            row=self.source(sid,actor,db)
            require(row['kind']=='asset' and can_manage(row,actor),'permission_denied','仅上传者或管理员可以管理素材',403)
            if delete:
                self.store.update('source',sid,{'status':'deleted'},db)
            else:
                name=str(name or '').strip()
                require(0<len(name)<=240 and not any(ord(c)<32 for c in name),'name_invalid','素材名称无效')
                self.store.update('source',sid,{'name':name},db)
            row=self.store.get('source',sid,db)
        return self.source_dto(row,actor)

    def enqueue_probe(self,sid,actor):
        source=self.source(sid,actor)
        if source['status'] in {'ready','probing'}:
            return source
        self.require_intake()
        require(source['status'] in {'uploaded','unprobed','failed'},'source_not_uploaded','素材尚未上传完成',409)
        d=copy.deepcopy(source['data'])
        if source['kind']=='episode' and not d.get('identity'):
            d['identity']=probe_identity(d['url'])
        with self.store.transaction() as db:
            source=self.source(sid,actor,db)
            if source['status'] in {'ready','probing'}: return source
            # One source has at most one active probe, even across operators.
            task_id=identifier('ve_')
            recipe={'version':1,'profile':PROFILE,'sources':[{'source_id':sid,'kind':source['kind'],'url':d['url'],
                    'identity':d['identity']}],'clips':[]}
            from .recipe import validate_recipe
            recipe=validate_recipe(recipe,operation='probe')
            payload={'job_id':task_id,'task_type':'video_editing_v1','operation':'probe','recipe':recipe}
            self.store.insert('task',{'id':task_id,'owner':owner(actor),'project_id':'','operation_id':'probe_'+sid+'_'+task_id,
                'kind':'probe','status':'queued','data':{'source_id':sid,'payload':payload,'stage':'queued','progress':0}},db)
            d['probe_task_id']=task_id; d.pop('error_code',None);d.pop('error_message',None)
            self.store.update('source',sid,{'status':'probing','data':d},db)
        return self.source(sid,actor)

    def complete_upload(self,upload_id,actor,reconcile=False):
        row=self.uploads.reconcile(upload_id,actor) if reconcile else self.uploads.complete(upload_id,actor)
        source=self.store.get('source',row['asset_id'])
        if row['status']=='completed' and source and source['status']!='deleted' and self.intake_enabled:
            self.enqueue_probe(row['asset_id'],actor)
        result=self.uploads.dto(row)
        result['probe_pending']=bool(source and source['status']=='uploaded')
        return result

    def project(self,pid,actor,db=None):
        row=self.store.get('project',pid,db)
        require(row and can_manage(row,actor),'project_not_found','剪辑项目不存在',404)
        return row

    @staticmethod
    def project_dto(row):
        return {**row['data'],'id':row['id'],'name':row['name'],'version':row['version'],
                'created_at':row['created_at'],'updated_at':row['updated_at']}

    def validate_draft(self,payload,actor):
        require(isinstance(payload,dict),'draft_invalid','剪辑内容无效')
        require(not(set(payload)-{'name','asset_id','dramas','expected_version','operation_id'}),'draft_fields_invalid','剪辑内容包含未知字段')
        name=str(payload.get('name','')).strip()
        require(0<len(name)<=120,'name_invalid','请输入项目名称（最多120字）')
        asset_id=str(payload.get('asset_id',''))
        if asset_id:
            source=self.source(asset_id,actor)
            require(source['kind']=='asset','asset_invalid','请选择素材库视频')
        dramas=payload.get('dramas',[])
        require(isinstance(dramas,list) and len(dramas)<=20,'drama_count_invalid','最多选择20部短剧')
        normalized=[];seen=set()
        for drama in dramas:
            require(isinstance(drama,dict) and not(set(drama)-{'app_id','content_id','title','clips'}),'drama_invalid','剧目配置无效')
            key=(str(drama.get('app_id','')),str(drama.get('content_id','')))
            require(all(re.fullmatch(r'[A-Za-z0-9._-]{1,128}',x) for x in key) and key not in seen,'drama_invalid','剧目编号无效或重复')
            seen.add(key); clips=drama.get('clips',[])
            require(isinstance(clips,list) and len(clips)<=300,'clip_count_invalid','单剧最多300个片段')
            clean=[]
            for clip in clips:
                require(isinstance(clip,dict) and not(set(clip)-{'id','source_id','start_ms','end_ms','fit','mute','transition'}),'clip_invalid','剪辑片段字段无效')
                sid=str(clip.get('source_id','')); source=self.source(sid,actor)
                require((source['kind']=='asset' and sid==asset_id) or (source['kind']=='episode' and (source['data'].get('app_id'),source['data'].get('content_id'))==key),
                        'clip_source_mismatch','片段必须来自所选素材或当前剧目',409)
                start,end=clip.get('start_ms'),clip.get('end_ms')
                require(type(start) is int and type(end) is int and 0<=start<end,'clip_range_invalid','片段起止时间无效')
                require(clip.get('fit','contain') in {'contain','cover'} and type(clip.get('mute',False)) is bool and clip.get('transition','cut') in {'cut','fade'},'clip_style_invalid','拼接形式无效')
                duration=source['data'].get('metadata',{}).get('duration_ms')
                if duration: require(end<=duration,'clip_range_invalid','片段超出视频时长')
                cid=str(clip.get('id') or identifier('clip_'))
                require(re.fullmatch(r'[A-Za-z0-9_-]{1,128}',cid),'clip_id_invalid','片段编号无效')
                clean.append({'id':cid,'source_id':sid,'start_ms':start,'end_ms':end,'fit':clip.get('fit','contain'),
                              'mute':clip.get('mute',False),'transition':clip.get('transition','cut')})
            require(len({x['id'] for x in clean})==len(clean),'clip_id_duplicate','片段编号重复')
            normalized.append({'app_id':key[0],'content_id':key[1],'title':str(drama.get('title',''))[:200],'clips':clean})
        return {'name':name,'asset_id':asset_id,'dramas':normalized}

    def save_project(self,payload,actor,pid=None):
        draft=self.validate_draft(payload,actor)
        with self.store.transaction() as db:
            if pid:
                row=self.project(pid,actor,db)
                require(type(payload.get('expected_version')) is int and payload['expected_version']==row['version'],'project_version_conflict','项目已被更新，请刷新后再保存',409)
                self.store.update('project',pid,{'name':draft['name'],'version':row['version']+1,'data':draft},db)
            else:
                op=operation_id(payload.get('operation_id')) if payload.get('operation_id') else None
                draft_hash=hashlib.sha256(encode(draft).encode()).hexdigest()
                if op:
                    previous=db.execute('SELECT * FROM ve_project_operation WHERE owner=? AND operation_id=?',(owner(actor),op)).fetchone()
                    if previous:
                        require(previous['draft_hash']==draft_hash,'operation_conflict','同一创建操作的剪辑内容已变化',409)
                        return self.project_dto(self.project(previous['project_id'],actor,db))
                pid=identifier('vp_')
                self.store.insert('project',{'id':pid,'owner':owner(actor),'name':draft['name'],'version':1,'data':draft},db)
                if op:db.execute('INSERT INTO ve_project_operation(owner,operation_id,project_id,draft_hash) VALUES(?,?,?,?)',(owner(actor),op,pid,draft_hash))
            row=self.project(pid,actor,db)
        return self.project_dto(row)

    def render(self,pid,payload,actor):
        op=operation_id(payload.get('operation_id'))
        from .recipe import validate_recipe
        with self.store.transaction() as db:
            project=self.project(pid,actor,db); draft=project['data']
            batch=self.store.row(db.execute('SELECT * FROM ve_batch WHERE owner=? AND operation_id=?',(owner(actor),op)).fetchone())
            if batch:
                require(batch['project_id']==pid and type(payload.get('expected_version')) is int and
                        batch['data']['project_version']==payload['expected_version'],
                        'operation_conflict','同一制作操作内容已变化',409)
                return {'batch_id':batch['id'],'project_version_id':batch['data']['project_version_id'],
                        'items':[self.task_dto(self.store.get('task',tid,db)) for tid in batch['data']['task_ids']]}
            require(type(payload.get('expected_version')) is int and payload['expected_version']==project['version'],'project_version_conflict','项目已被更新，请刷新后制作',409)
            require(draft['asset_id'] and draft['dramas'],'project_incomplete','请选择素材和短剧')
            self.source(draft['asset_id'],actor,db,ready=True)
            result=[];batch_id=identifier('vb_')
            version_id='vv_'+hashlib.sha256((pid+':'+str(project['version'])).encode()).hexdigest()[:32]
            saved=self.store.get('project_version',version_id,db)
            if saved:
                require(encode(saved['data'])==encode(draft),'project_snapshot_conflict','项目版本快照不一致',409)
            else:
                self.store.insert('project_version',{'id':version_id,'owner':project['owner'],'project_id':pid,
                                  'version':project['version'],'data':draft},db)
            for index,drama in enumerate(draft['dramas']):
                clips=drama['clips']
                require(clips and any(c['source_id']==draft['asset_id'] for c in clips) and any(c['source_id']!=draft['asset_id'] for c in clips),
                        'timeline_incomplete','每部剧时间线必须包含素材片段和至少一个剧集片段')
                sources=[]
                for sid in dict.fromkeys(c['source_id'] for c in clips):
                    source=self.source(sid,actor,db,ready=True); d=source['data']; meta=d['metadata']
                    sources.append({'source_id':sid,'kind':source['kind'],'url':d['url'],'identity':d['identity'],**meta})
                recipe=validate_recipe({'version':1,'profile':PROFILE,'sources':sources,'clips':clips},operation='render')
                task_operation=op+'_'+str(index)
                prior=self.store.row(db.execute('SELECT * FROM ve_task WHERE owner=? AND operation_id=?',(owner(actor),task_operation)).fetchone())
                if prior:
                    require(prior['project_id']==pid and prior['data'].get('project_version')==project['version'] and encode(prior['data']['payload']['recipe'])==encode(recipe),
                            'operation_conflict','同一制作操作内容已变化',409)
                    prior['data'].update(batch_id=batch_id,project_version_id=version_id,output_id='vo_'+prior['id'][3:])
                    self.store.update('task',prior['id'],{'data':prior['data']},db)
                    result.append(prior);continue
                self.require_intake()
                tid=identifier('ve_')
                data={'project_version':project['version'],'project_version_id':version_id,'batch_id':batch_id,'output_id':'vo_'+tid[3:],
                      'drama_title':drama['title'],'app_id':drama['app_id'],'content_id':drama['content_id'],
                      'stage':'queued','progress':0,'payload':{'job_id':tid,'task_type':'video_editing_v1','operation':'render','recipe':recipe}}
                self.store.insert('task',{'id':tid,'owner':owner(actor),'project_id':pid,'operation_id':task_operation,'kind':'render','status':'queued','data':data},db)
                result.append(self.store.get('task',tid,db))
            self.store.insert('batch',{'id':batch_id,'owner':owner(actor),'project_id':pid,'operation_id':op,
                'data':{'project_version':project['version'],'project_version_id':version_id,'task_ids':[r['id'] for r in result]}},db)
            for row in result:
                output_id=row['data']['output_id']
                if self.store.get('output',output_id,db) is None:
                    self.store.insert('output',{'id':output_id,'owner':row['owner'],'task_id':row['id'],'batch_id':batch_id,
                        'status':'ready' if row['status']=='completed' else 'pending','data':row['data'].get('result') or {}},db)
        return {'batch_id':batch_id,'project_version_id':version_id,'items':[self.task_dto(r) for r in result]}

    def task(self,tid,actor):
        row=self.store.get('task',tid)
        require(row and can_manage(row,actor),'task_not_found','制作任务不存在',404)
        return row

    def task_dto(self,row):
        d=row['data']; result=d.get('result') or {}; artifact=result.get('artifact') or {}
        output=None
        if row['status']=='completed' and row['kind']=='render':
            output={'id':d.get('output_id','vo_'+row['id'][3:]),**{k:v for k,v in artifact.items() if k not in {'url','key','bucket'}},
                    'preview_url':self.signed_url(artifact),'download_url':self.signed_url(artifact,download=True),
                    'thumbnail_url':self.signed_url(result.get('thumbnail') or {}),'metadata':{k:artifact[k] for k in ('duration_ms','width','height','fps','has_audio') if k in artifact}}
            output['expires_at']=now()+900
        return {'id':row['id'],'project_id':row['project_id'],'kind':row['kind'],'status':row['status'],'drama_title':d.get('drama_title',''),
                'batch_id':d.get('batch_id',''),'project_version_id':d.get('project_version_id',''),'output_id':d.get('output_id',''),
                'stage':d.get('stage',row['status']),'progress':d.get('progress',0),'error_code':d.get('error_code',''),
                'error_message':d.get('error_message',''),'output':output,'created_at':row['created_at'],'updated_at':row['updated_at']}

    def resume(self,tid,actor):
        with self.store.transaction() as db:
            row=self.store.get('task',tid,db)
            require(row and can_manage(row,actor),'task_not_found','制作任务不存在',404)
            require(row['status'] in {'failed','recovery_required','unknown'},'task_not_resumable','该任务不可恢复',409)
            if row['kind']=='probe':
                source=self.store.get('source',row['data']['source_id'],db)
                require(source and source['status']!='deleted' and source['data'].get('probe_task_id')==tid,
                        'probe_superseded','素材探测已被更新或删除',409)
            row['data']['resume_requested']=True
            row['data'].pop('error_code',None);row['data'].pop('error_message',None)
            self.store.update('task',tid,{'status':'resume_queued','data':row['data']},db)
        return self.task_dto(self.task(tid,actor))


_instances={}
_lock=threading.Lock()


def get_service(app):
    path=os.environ.get('VIDEO_EDITING_DB','/mnt/data-disk/video-editing/state.sqlite3')
    with _lock:
        if path not in _instances:
            bucket=os.environ.get('VIDEO_EDITING_COS_BUCKET',getattr(app,'COS_BUCKET',''))
            region=os.environ.get('VIDEO_EDITING_COS_REGION',getattr(app,'COS_REGION',''))
            client=None
            if bucket and region and getattr(app,'COS_SECRET_ID','') and getattr(app,'COS_SECRET_KEY',''):
                try:
                    from qcloud_cos import CosConfig,CosS3Client
                    conf=CosConfig(Region=region,SecretId=app.COS_SECRET_ID,SecretKey=app.COS_SECRET_KEY,Scheme='https',Timeout=30,KeepAlive=False)
                    client=CosS3Client(conf,retry=0)
                except ImportError:
                    pass
            store=Store(path)
            uploads=Uploads(store,client,bucket,region,os.environ.get('VIDEO_EDITING_COS_PREFIX','video-editing/v1'))
            _instances[path]=Service(store,uploads,app,os.environ.get('VIDEO_EDITING_GPU_URL',getattr(app,'GPU_VIDEO_WORKER_URL','')),
                                    os.environ.get('VIDEO_EDITING_GPU_TOKEN',getattr(app,'GPU_VIDEO_WORKER_TOKEN','')),
                                    intake_enabled=os.environ.get('VIDEO_EDITING_ENABLED','0').lower() in {'1','true','yes'})
        return _instances[path]
