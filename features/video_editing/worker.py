"""A single durable CPU dispatcher; all media executes on the GPU worker."""
import copy
import logging
import json
import re
import requests

from . import VideoEditingError, require
from .store import identifier, now

ACTIVE={'submitting','queued_gpu','running','unknown','recovery_required'}


class Dispatcher:
    def __init__(self,service,transport=None):
        self.service=service;self.store=service.store;self.holder=identifier('worker_')
        self.transport=transport

    def request(self,method,path,payload=None):
        if self.transport:
            return self.transport(method,path,payload)
        require(self.service.gpu_url and self.service.gpu_token,'gpu_not_configured','GPU 剪辑服务尚未配置',503)
        with requests.Session() as session:
            session.trust_env=False
            response=session.request(method,self.service.gpu_url+path,headers={'Authorization':'Bearer '+self.service.gpu_token},
                                     json=payload,timeout=(5,35),allow_redirects=False)
        if response.status_code==404: return None
        if response.status_code in {409,429,503}:
            try:rejection=response.json()
            except ValueError:rejection={}
            code=rejection.get('code') if isinstance(rejection,dict) else None
            if code in {'video_editing_disabled','gpu_queue_full'}:
                raise VideoEditingError(code,'GPU 暂停接收或队列已满，稍后继续',response.status_code)
        require(200<=response.status_code<300,'gpu_request_failed','GPU 暂时不可用',503)
        try: result=response.json()
        except Exception: raise VideoEditingError('gpu_response_invalid','GPU 返回无效结果',503) from None
        require(isinstance(result,dict),'gpu_response_invalid','GPU 返回无效结果',503)
        return result.get('item',result)

    def tick(self):
        if not self.store.lease(self.holder,ttl=120): return False
        # An already accepted upload may finish while new intake is paused.
        # Once reopened, schedule its verification without re-uploading bytes.
        if self.service.intake_enabled:
            sources,_=self.store.list('source',"kind='asset' AND status='uploaded'",(),1)
            if sources:
                source=sources[0]; tenant,user=json.loads(source['owner'])
                self.service.enqueue_probe(source['id'],{'tenant_key':tenant,'user_id':user,'is_admin':False})
        with self.store.transaction() as db:
            rows=db.execute("SELECT * FROM ve_task WHERE status NOT IN ('completed','failed','cancelled') ORDER BY created_at,rowid").fetchall()
            rows=[self.store.row(r) for r in rows]
            active=[r for r in rows if r['status'] in ACTIVE]
            # Do not submit a second job while an accepted/unknown one remains.
            row=active[0] if active else next((r for r in rows if r['status'] in {'queued','resume_queued'}),None)
            if not row: return False
            if row['status'] in {'queued','resume_queued'} and row['data'].get('next_attempt_at',0)>now():return False
            fresh=row['status']=='queued'
            resume=row['status']=='resume_queued'
            if fresh or resume:
                row['data']['dispatch_started_at']=now()
                self.store.update('task',row['id'],{'status':'submitting','data':row['data']},db)
        path='/api/gpu-video/video-editing/jobs/'+row['id']
        try:
            if fresh:
                response=self.request('POST','/api/gpu-video/video-editing/jobs',row['data']['payload'])
            elif resume:
                existing=self.request('GET',path)
                if existing is None:
                    response=self.request('POST','/api/gpu-video/video-editing/jobs',row['data']['payload'])
                elif existing.get('status') in {'failed','recovery_required'}:
                    generation=existing.get('generation')
                    require(type(generation) is int and generation>=1,'gpu_generation_missing','GPU 恢复版本尚未确认',503)
                    response=self.request('POST',path+'/resume',{**row['data']['payload'],'expected_generation':generation})
                else:
                    response=existing
            else:
                response=self.request('GET',path)
            if response is None:
                self.fail_or_hold(row,'unknown','gpu_submission_unknown','GPU 接收状态待核查，请恢复同一任务')
            else:
                self.accept(row,response)
        except VideoEditingError as exc:
            if (fresh or resume) and exc.code in {'video_editing_disabled','gpu_queue_full'}:
                with self.store.transaction() as db:
                    current=self.store.get('task',row['id'],db);d=current['data']
                    d.update(next_attempt_at=now()+30,stage='waiting_gpu_capacity',error_code=exc.code,error_message='GPU 暂停接收或队列已满，稍后自动继续')
                    self.store.update('task',row['id'],{'status':'resume_queued' if resume else 'queued','data':d},db)
            else:
                self.fail_or_hold(row,'unknown',exc.code,'制作状态待核查，请稍后刷新')
        except Exception:
            logging.warning('video editing GPU status check failed for %s',row['id'])
            self.fail_or_hold(row,'unknown','gpu_transport_unknown','GPU 连接中断，系统将继续核查同一任务')
        return True

    def fail_or_hold(self,row,status,code,message):
        with self.store.transaction() as db:
            current=self.store.get('task',row['id'],db)
            if current['status']=='completed': return
            if current['status']=='resume_queued' and row['status']!='resume_queued':return
            d=current['data'];d['error_code']=str(code)[:100];d['error_message']=message
            self.store.update('task',row['id'],{'status':status,'data':d},db)
            if status=='failed' and row['kind']=='probe':
                source=self.store.get('source',d['source_id'],db)
                if source and source['status']!='deleted' and source['data'].get('probe_task_id')==row['id']:
                    source['data'].update(error_code=str(code)[:100],error_message=message)
                    self.store.update('source',source['id'],{'status':'failed','data':source['data']},db)

    def verify_artifact(self,artifact,*,source_expected=None):
        if not artifact: return
        u=self.service.uploads
        require(artifact.get('bucket')==u.bucket and str(artifact.get('key','')).startswith(u.prefix+'/') and
                type(artifact.get('size_bytes')) is int and artifact['size_bytes']>0 and re.fullmatch(r'[0-9a-f]{64}',str(artifact.get('sha256',''))),
                'gpu_artifact_invalid','GPU 成片身份无效',409)
        head=u.client.head_object(Bucket=u.bucket,Key=artifact['key'])
        head={str(k).lower():v for k,v in head.items()}
        require(int(head.get('content-length',-1))==artifact['size_bytes'],'gpu_artifact_invalid','成片大小校验失败',409)
        # A source asset's pre-probe upload has the upload nonce, not yet a SHA
        # metadata header; the GPU response binds the actual source digest.
        if source_expected is not None:
            require(all(artifact.get(k)==source_expected.get(k) for k in ('bucket','key','size_bytes')) and
                    str(head.get('etag','')).strip('"')==str(source_expected.get('etag','')).strip('"'),
                    'gpu_source_changed','原始素材对象身份已改变',409)
        else:
            require(head.get('x-cos-meta-sha256')==artifact['sha256'],'gpu_artifact_invalid','成片摘要校验失败',409)
        u.verify_private(artifact['key'])

    def accept(self,row,response):
        require(response.get('job_id',row['id'])==row['id'],'gpu_identity_conflict','GPU 任务身份不匹配',409)
        status=response.get('status')
        if status in {'failed','recovery_required'}:
            error=response.get('error') if isinstance(response.get('error'),dict) else {}
            code=str(error.get('code') or response.get('error_code') or 'gpu_render_failed')
            if not re.fullmatch(r'[A-Za-z0-9_]{1,100}',code): code='gpu_render_failed'
            messages={'video_editing_disk_budget':'GPU 临时空间不足，请稍后恢复同一任务',
                      'video_editing_source_changed':'原始视频内容已改变，请重新选择素材或剧集',
                      'source_changed':'原始视频内容已改变，请重新选择素材或剧集',
                      'video_editing_transition_too_long':'转场片段过短，请检查淡入淡出时长',
                      'video_editing_last_transition':'最后一个片段不能设置过渡，请检查时间线',
                      'video_editing_clip_too_short':'保留片段必须至少包含一帧，请调整时间线',
                      'video_editing_probe_failed':'无法读取视频信息，请检查源视频是否有效',
                      'video_editing_output_invalid':'成片规格或音画时长校验失败，请恢复同一任务',
                      'video_editing_storage_unavailable':'剪辑 COS 存储尚未配置，请联系管理员',
                      'recovery_required':'GPU 服务已恢复，请继续同一任务'}
            self.fail_or_hold(row,status,code,messages.get(code,'制作失败，可查看配置后恢复同一任务'))
            return
        if status!='completed':
            require(status in {'queued','running','pending'},'gpu_status_invalid','GPU 状态无效',503)
            with self.store.transaction() as db:
                current=self.store.get('task',row['id'],db)
                if current['status']=='completed':return
                if current['status']=='resume_queued' and row['status']!='resume_queued':return
                d=current['data'];d['stage']=str(response.get('stage') or status)[:80]
                progress=response.get('progress',0)
                d['progress']=progress if isinstance(progress,(int,float,dict)) else 0
                d.pop('error_code',None);d.pop('error_message',None)
                self.store.update('task',row['id'],{'status':'queued_gpu' if status=='queued' else 'running','data':d},db)
            return
        result=response.get('result') or response
        from .recipe import recipe_fingerprint
        expected=recipe_fingerprint(row['data']['payload']['recipe'],operation=row['kind'])
        require(result.get('job_id')==row['id'] and result.get('operation')==row['kind'] and
                result.get('task_type')=='video_editing_v1' and result.get('recipe_sha256')==expected,
                'gpu_result_identity_conflict','GPU 结果与冻结任务不一致',409)
        for field in ('artifact','thumbnail','preview'):
            source_expected=row['data']['payload']['recipe']['sources'][0]['identity'] if row['kind']=='probe' and field=='artifact' else None
            self.verify_artifact(result.get(field),source_expected=source_expected)
        if row['kind']=='render':
            require(result.get('artifact'),'gpu_artifact_missing','GPU 未返回成片',409)
        else:
            metadata=result.get('metadata') or {};identity=result.get('source_identity') or {}
            require(type(metadata.get('duration_ms')) is int and metadata['duration_ms']>0 and
                    type(metadata.get('width')) is int and metadata['width']>0 and type(metadata.get('height')) is int and metadata['height']>0 and
                    re.fullmatch(r'[0-9a-f]{64}',str(identity.get('sha256',''))),'gpu_probe_invalid','视频探测结果无效',409)
            frozen=row['data']['payload']['recipe']['sources'][0]['identity']
            require(identity.get('size_bytes')==frozen['size_bytes'] and str(identity.get('etag','')).strip('"')==str(frozen.get('etag','')).strip('"'),
                    'gpu_source_changed','视频原始内容已变化',409)
        with self.store.transaction() as db:
            current=self.store.get('task',row['id'],db)
            if current['status']=='completed':return
            d=current['data'];d.update(result=result,progress=100,stage='completed');d.pop('error_code',None);d.pop('error_message',None)
            self.store.update('task',row['id'],{'status':'completed','data':d},db)
            if row['kind']=='render' and d.get('output_id'):
                output=self.store.get('output',d['output_id'],db)
                require(output and output['task_id']==row['id'],'output_identity_conflict','成片账本身份不一致',409)
                self.store.update('output',output['id'],{'status':'ready','data':result},db)
            if row['kind']=='probe':
                source=self.store.get('source',d['source_id'],db)
                if source and source['status']!='deleted' and source['data'].get('probe_task_id')==row['id']:
                    sd=source['data'];sd['identity']={**sd['identity'],**identity};sd['metadata']=metadata
                    for field in ('thumbnail','preview'):
                        if result.get(field):sd[field]=result[field]
                    self.store.update('source',source['id'],{'status':'ready','data':sd},db)
