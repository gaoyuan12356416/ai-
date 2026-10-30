"""Video editing application service and immutable render submissions."""
import copy
import hashlib
import os
import re
import threading
import math
from pathlib import Path

from . import VideoEditingError, require
from .catalog import Catalog, probe_identity
from .store import Store, can_manage, encode, identifier, now, owner
from .uploads import Uploads, MAX_BYTES, PART_BYTES
from .recipe import LEGACY_PROFILE, PROFILE

MAX_DRAMAS=20
TERMINAL={'completed','failed','cancelled'}


def operation_id(value):
    value=str(value or '')
    require(re.fullmatch(r'[A-Za-z0-9_-]{8,128}',value),'operation_id_required','请提供操作标识')
    return value


class Service:
    def __init__(self,store,uploads,app=None,gpu_url='',gpu_token='',intake_enabled=True):
        self.store,self.uploads,self.app=store,uploads,app
        self.catalog=Catalog(store,app) if app is not None else None
        self.uploads.resource_resolver=self.catalog.resource if self.catalog else None
        from .material_source import MaterialSource
        self.material_source=MaterialSource(self)
        from .material_replication import MaterialReplication
        self.material_replication=MaterialReplication(self)
        self.gpu_url,self.gpu_token=gpu_url.rstrip('/'),gpu_token
        self._intake_enabled=bool(intake_enabled)
        self.export_profile=os.environ.get('VIDEO_EDITING_EXPORT_PROFILE',PROFILE).strip()
        require(self.export_profile in (LEGACY_PROFILE,PROFILE),'video_editing_export_profile_invalid','剪辑导出配置无效',503)

    @property
    def intake_enabled(self):
        return self._intake_enabled and not Path(os.environ.get('VIDEO_EDITING_DISABLED_FILE','/etc/video-editing/intake.disabled')).exists()

    def require_intake(self):
        require(self.intake_enabled,'video_editing_intake_disabled','新剪辑任务已暂停，已有任务仍可查询和恢复',503)

    def config(self):
        return {'enabled':self.intake_enabled,'intake_enabled':self.intake_enabled,'max_file_bytes':MAX_BYTES,'part_size':PART_BYTES,'max_dramas':MAX_DRAMAS,'profile':self.export_profile,
                'worker_configured':bool(self.gpu_url and self.gpu_token),'cos_configured':bool(self.uploads.client and self.uploads.bucket),
                'draft_schema_version':2,'resource_library':True,'mirror_subtitle_band':True}

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

    def source(self,sid,actor,db=None,ready=False,check_product=True):
        row=self.store.get('source',sid,db)
        require(row and row['status']!='deleted' and (row['kind']=='episode' or row['status']=='ready' or can_manage(row,actor)),
                'source_not_found','素材不存在或尚不可使用',404)
        if row['kind']=='episode' and self.catalog is not None and check_product:
            self.catalog.require_product(row['data'].get('app_id',''))
        if ready:
            require(row['status']=='ready','source_not_ready','请等待所有素材校验完成',409)
        return row

    def source_dto(self,row,actor):
        from .recipe import preview_geometry
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
        result.update(library_kind=d.get('library_kind','material'),series_code=d.get('series_code',''),
                      english_title=d.get('english_title',''),english_title_status=d.get('english_title_status','missing'),
                      preview_geometry=preview_geometry(d.get('metadata',{}),proxy=bool(d.get('preview'))))
        for k in ('app_id','content_id','episode_number','title'):
            if k in d: result[k]=d[k]
        # Shared sources expose media progress, never another owner's task payload.
        task=self.store.get('task',d['probe_task_id']) if d.get('probe_task_id') and row['status']!='ready' else None
        if task and task['kind']=='probe' and task['data'].get('source_id')==row['id']:
            td=task['data'];metrics=td.get('progress',{})
            metrics=metrics if isinstance(metrics,dict) else {}
            result['probe_progress']={'status':task['status'],
                'stage':td.get('stage','queued') if td.get('stage') in {'queued','starting','downloading','verifying','waiting_media','normalizing','uploading','completed','waiting_gpu_capacity'} else task['status'],
                'updated_at':task['updated_at'],
                **{key:metrics[key] for key in ('download_bytes','total_bytes','download_bps')
                   if type(metrics.get(key)) in (int,float) and math.isfinite(metrics[key]) and 0<=metrics[key]<=1e18}}
        if row['kind']=='episode' and not result['preview_url'] and row['status']=='ready': result['preview_url']=d.get('url','')
        return result

    def assets(self,actor,query='',status='',limit=50,offset=0,library_kind='',series_code=''):
        conditions=["kind='asset'","status<>'deleted'"]; args=[]
        if library_kind:
            require(library_kind in {'material','free_episode'},'library_kind_invalid','素材栏目无效')
            conditions.append('library_kind=?');args.append(library_kind)
        if series_code:
            conditions.append('series_code=?');args.append(str(series_code))
        if status=='all':
            if not actor.get('is_admin'):
                conditions.append("(status='ready' OR owner=?)");args.append(owner(actor))
        else:
            conditions.append("status='ready'")
        if query:
            conditions.append('name LIKE ?');args.append('%'+str(query)[:100]+'%')
        rows,total=self.store.list('source',' AND '.join(conditions),tuple(args),min(100,max(1,int(limit))),max(0,int(offset)))
        return {'items':[self.source_dto(r,actor) for r in rows],'total':total}

    def mutate_asset(self,sid,actor,name=None,delete=False,series_code=None,library_kind=None):
        original=self.source(sid,actor)
        require(original['kind']=='asset' and can_manage(original,actor),'permission_denied','仅上传者或管理员可以管理素材',403)
        resource=None
        if series_code is not None and not delete:
            require(self.catalog is not None,'catalog_unavailable','资源目录暂不可用',503)
            resource=self.catalog.resource(series_code)
        if library_kind is not None:require(library_kind in {'material','free_episode'},'library_kind_invalid','素材栏目无效')
        with self.store.transaction() as db:
            row=self.source(sid,actor,db)
            require(row['kind']=='asset' and can_manage(row,actor),'permission_denied','仅上传者或管理员可以管理素材',403)
            if delete:
                self.store.update('source',sid,{'status':'deleted'},db)
            else:
                changes={}
                if name is not None:
                    name=str(name).strip()
                    require(0<len(name)<=240 and not any(ord(c)<32 for c in name),'name_invalid','素材名称无效')
                    changes['name']=name
                if resource is not None or library_kind is not None:
                    data=copy.deepcopy(row['data'])
                    if resource is not None:data.update(resource)
                    if library_kind is not None:data['library_kind']=library_kind
                    changes['data']=data
                require(changes,'asset_update_required','请选择需要修改的字段')
                self.store.update('source',sid,changes,db)
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
            recipe={'version':1,'profile':LEGACY_PROFILE,'sources':[{'source_id':sid,'kind':source['kind'],'url':d['url'],
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

    def project_dto(self,row,actor=None,upgrade=False):
        result={**row['data'],'id':row['id'],'name':row['name'],'version':row['version'],
                'created_at':row['created_at'],'updated_at':row['updated_at']}
        if actor is not None:
            from .projects_v2 import draft_issues,upgrade_units
            if row['data'].get('schema_version')==2:result['issues']=draft_issues(self,row['data'],actor)
            elif upgrade:result['upgrade_units']=upgrade_units(self,row)
        return result

    def validate_draft(self,payload,actor):
        require(isinstance(payload,dict),'draft_invalid','剪辑内容无效')
        if payload.get('schema_version')==2:
            from .projects_v2 import normalize_draft
            return normalize_draft(payload)
        require(not(set(payload)-{'name','asset_id','dramas','expected_version','operation_id'}),'draft_fields_invalid','剪辑内容包含未知字段')
        name=str(payload.get('name','')).strip()
        require(0<len(name)<=120,'name_invalid','请输入项目名称（最多120字）')
        asset_id=str(payload.get('asset_id',''))
        if asset_id:
            source=self.source(asset_id,actor)
            require(source['kind']=='asset','asset_invalid','请选择素材库视频')
        dramas=payload.get('dramas',[])
        require(isinstance(dramas,list) and len(dramas)<=MAX_DRAMAS,'drama_count_invalid',f'最多选择{MAX_DRAMAS}部短剧')
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
                        return self.project_dto(self.project(previous['project_id'],actor,db),actor)
                pid=identifier('vp_')
                self.store.insert('project',{'id':pid,'owner':owner(actor),'name':draft['name'],'version':1,'data':draft},db)
                if op:db.execute('INSERT INTO ve_project_operation(owner,operation_id,project_id,draft_hash) VALUES(?,?,?,?)',(owner(actor),op,pid,draft_hash))
            row=self.project(pid,actor,db)
        return self.project_dto(row,actor)

    def _render_batch_result(self,batch,actor,db):
        require(batch is not None,'batch_not_found','制作批次不存在，请联系管理员核查',409)
        tasks=[self.store.get('task',tid,db) for tid in batch['data']['task_ids']]
        # Old administrator-created tasks retain their original ownership.
        # Reusing a version must never become a way to bypass task() access.
        require(all(task and can_manage(task,actor) for task in tasks),
                'batch_task_permission_conflict','此版本已有制作批次，请管理员查看原批次',409)
        return {'batch_id':batch['id'],'project_version_id':batch['data']['project_version_id'],
                'items':[self.task_dto(task) for task in tasks]}

    def _remember_render_operation(self,db,actor_owner,op,pid,version,batch):
        db.execute('INSERT INTO ve_render_operation(owner,operation_id,project_id,project_version,batch_id,created_at) VALUES(?,?,?,?,?,?)',
                   (actor_owner,op,pid,version,batch['id'],now()))

    def render(self,pid,payload,actor):
        project=self.project(pid,actor)
        expected=payload.get('expected_version')
        saved=self.store.get('project_version','vv_'+hashlib.sha256((pid+':'+str(expected)).encode()).hexdigest()[:32])
        draft=saved['data'] if saved else project['data']
        if draft.get('schema_version')==2:
            from .projects_v2 import render_units
            return render_units(self,pid,payload,actor)
        op=operation_id(payload.get('operation_id'))
        actor_owner=owner(actor);expected=payload.get('expected_version')
        from .recipe import validate_recipe
        with self.store.transaction() as db:
            project=self.project(pid,actor,db); draft=project['data']
            mapped=db.execute('SELECT * FROM ve_render_operation WHERE owner=? AND operation_id=?',(actor_owner,op)).fetchone()
            if mapped:
                require(mapped['project_id']==pid and type(expected) is int and mapped['project_version']==expected,
                        'operation_conflict','同一制作操作内容已变化',409)
                return self._render_batch_result(self.store.get('batch',mapped['batch_id'],db),actor,db)
            # Existing releases keyed a batch directly by caller + request op.
            # Preserve those exact historical identities before canonical reuse.
            batch=self.store.row(db.execute('SELECT * FROM ve_batch WHERE owner=? AND operation_id=?',(actor_owner,op)).fetchone())
            if batch and 'request_operation_id' in batch['data']:
                batch=None
            if batch:
                require(batch['project_id']==pid and type(expected) is int and batch['data']['project_version']==expected,
                        'operation_conflict','同一制作操作内容已变化',409)
                result=self._render_batch_result(batch,actor,db)
                self._remember_render_operation(db,actor_owner,op,pid,expected,batch)
                return result
            require(type(expected) is int and expected>=1,'project_version_conflict','项目版本无效，请刷新后制作',409)
            # A browser reload, another tab or an administrator uses a new op.
            # The first accepted batch for this saved version remains canonical.
            rows=db.execute('SELECT * FROM ve_batch WHERE project_id=? ORDER BY created_at,rowid',(pid,)).fetchall()
            batch=next((row for row in (self.store.row(r) for r in rows) if row['data']['project_version']==expected),None)
            if batch:
                result=self._render_batch_result(batch,actor,db)
                self._remember_render_operation(db,actor_owner,op,pid,expected,batch)
                return result
            require(expected==project['version'],'project_version_conflict','项目已被更新，请刷新后制作',409)
            require(draft['asset_id'] and draft['dramas'],'project_incomplete','请选择素材和短剧')
            self.source(draft['asset_id'],actor,db,ready=True)
            result=[];batch_id=identifier('vb_')
            # New tasks belong to the project owner even when submitted by an
            # administrator. Request operations remain scoped to their caller.
            record_owner=project['owner']
            internal_op='submit_'+hashlib.sha256((actor_owner+'\0'+op).encode()).hexdigest()
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
                recipe=validate_recipe({'version':1,'profile':self.export_profile,'sources':sources,'clips':clips},operation='render')
                task_operation=internal_op+'_'+str(index)
                prior=self.store.row(db.execute('SELECT * FROM ve_task WHERE owner=? AND operation_id=?',(record_owner,task_operation)).fetchone())
                if prior:
                    require(prior['project_id']==pid and prior['data'].get('project_version')==project['version'] and encode(prior['data']['payload']['recipe'])==encode(recipe),
                            'operation_conflict','同一制作操作内容已变化',409)
                    prior['data'].update(batch_id=batch_id,project_version_id=version_id,output_id='vo_'+prior['id'][3:])
                    self.store.update('task',prior['id'],{'data':prior['data']},db)
                    result.append(prior);continue
                self.require_intake()
                tid=identifier('ve_')
                data={'project_version':project['version'],'project_version_id':version_id,'batch_id':batch_id,'output_id':'vo_'+tid[3:],
                      'submitted_by':actor_owner,'request_operation_id':op,
                      'drama_title':drama['title'],'app_id':drama['app_id'],'content_id':drama['content_id'],
                      'stage':'queued','progress':0,'payload':{'job_id':tid,'task_type':'video_editing_v1','operation':'render','recipe':recipe}}
                self.store.insert('task',{'id':tid,'owner':record_owner,'project_id':pid,'operation_id':task_operation,'kind':'render','status':'queued','data':data},db)
                result.append(self.store.get('task',tid,db))
            self.store.insert('batch',{'id':batch_id,'owner':record_owner,'project_id':pid,'operation_id':internal_op,
                'data':{'project_version':project['version'],'project_version_id':version_id,'task_ids':[r['id'] for r in result],
                        'submitted_by':actor_owner,'request_operation_id':op}},db)
            for row in result:
                output_id=row['data']['output_id']
                if self.store.get('output',output_id,db) is None:
                    self.store.insert('output',{'id':output_id,'owner':row['owner'],'task_id':row['id'],'batch_id':batch_id,
                        'status':'ready' if row['status']=='completed' else 'pending','data':row['data'].get('result') or {}},db)
            self._remember_render_operation(db,actor_owner,op,pid,expected,self.store.get('batch',batch_id,db))
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
                'unit_id':d.get('unit_id',''),'series_code':d.get('series_code',''),'english_title':d.get('english_title',''),
                'batch_id':d.get('batch_id',''),'project_version_id':d.get('project_version_id',''),'output_id':d.get('output_id',''),
                'stage':d.get('stage',row['status']),'progress':d.get('progress',0),'error_code':d.get('error_code',''),
                'error_message':d.get('error_message',''),'output':output,
                'material_upload':self.material_source.for_task(row),
                'material_replication':self.material_replication.for_task(row),
                'created_at':row['created_at'],'updated_at':row['updated_at']}

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
