"""Persistent preparation, human approval and notification state for YouTube."""
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageOps
from .templates import DEFAULT_DESCRIPTION, WorkflowError, long_url, render

MAX_IMAGE = 2 * 1024 * 1024


def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def encode(value): return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True)
def uid(): return uuid.uuid4().hex


class YouTubeWorkflow:
    def __init__(self, db_path, asset_root, source, channels, short_link, engine_store, *, generate=None, notify=None, public_base='https://ai.yingliangads.com', enabled=True):
        self.db_path=str(db_path); self.root=Path(asset_root).resolve()
        self.source,self.channels,self.short_link,self.engine_store=source,channels,short_link,engine_store
        self.generate,self.notify,self.public_base,self.enabled=generate,notify,public_base.rstrip('/'),bool(enabled)
        self.root.mkdir(parents=True,exist_ok=True)
        with self.db() as c:
            c.executescript('''
CREATE TABLE IF NOT EXISTS youtube_auto_preparation(
 id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,operation_id TEXT NOT NULL,
 request_sha TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0,
 body TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 lease_token TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0,
 UNIQUE(tenant,owner,operation_id));
CREATE INDEX IF NOT EXISTS youtube_auto_owner ON youtube_auto_preparation(tenant,owner,created_at);
CREATE INDEX IF NOT EXISTS youtube_auto_work ON youtube_auto_preparation(state,lease_until);
CREATE TABLE IF NOT EXISTS youtube_auto_asset(
 id TEXT PRIMARY KEY,tenant TEXT NOT NULL,owner TEXT NOT NULL,path TEXT NOT NULL,
 sha256 TEXT NOT NULL,mime TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS youtube_auto_setting(tenant TEXT PRIMARY KEY,description TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS youtube_auto_notification(
 task_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,
 lease_until REAL NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(task_id,version));
''')

    @contextmanager
    def db(self, write=False):
        c=sqlite3.connect(self.db_path,timeout=20)
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA busy_timeout=20000')
        try:
            if write:c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback();raise
        finally:c.close()

    def _actor(self, actor):
        tenant=str(actor.get('tenant_key') or '').strip(); owner=str(actor.get('user_id') or '').strip()
        if not tenant or not owner:raise WorkflowError('cookie_auth_required','请重新登录后台',401)
        return tenant,owner

    def _allowed(self, actor, row):
        tenant,owner=self._actor(actor)
        if row is None or row['tenant']!=tenant or (row['owner']!=owner and actor.get('role')!='admin'):
            raise WorkflowError('not_found','任务或封面不存在',404)

    def _row(self,c,task_id,actor=None):
        row=c.execute('SELECT * FROM youtube_auto_preparation WHERE id=?',(str(task_id),)).fetchone()
        if actor is not None:self._allowed(actor,row)
        if row is None:raise WorkflowError('not_found','任务不存在',404)
        return row,json.loads(row['body'])

    def _save(self,c,body,state=None):
        state=state or body['status'];body['status']=state;body['updated_at']=now()
        c.execute('UPDATE youtube_auto_preparation SET state=?,version=?,body=?,updated_at=? WHERE id=?',
                  (state,body['current_version'],encode(body),body['updated_at'],body['id']))

    def _asset(self,c,asset_id,actor):
        row=c.execute('SELECT * FROM youtube_auto_asset WHERE id=?',(str(asset_id),)).fetchone()
        self._allowed(actor,row)
        original=Path(row['path']);path=original.resolve()
        if self.root not in path.parents or not path.is_file() or original.is_symlink():
            raise WorkflowError('cover_unavailable','封面文件不可用',409)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise WorkflowError('cover_changed','封面文件已变化，请重新上传',409)
        return dict(row)

    def asset(self,actor,asset_id):
        with self.db() as c:return self._asset(c,asset_id,actor)

    @staticmethod
    def asset_url(asset_id):return '/api/youtube-auto-publish/covers/'+str(asset_id)

    def _store_image(self,actor,data,*,generated=False):
        if not isinstance(data,bytes) or not data or len(data)>(32*1024*1024 if generated else MAX_IMAGE):
            raise WorkflowError('cover_size','封面图片不能超过 2 MB')
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ('JPEG','PNG') or image.width*image.height>25000000 or getattr(image,'n_frames',1)!=1:
                    raise ValueError('format')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image=ImageOps.exif_transpose(image)
                if generated and abs(image.width/image.height-16/9)>.03:
                    raise ValueError('ratio')
                if image.width<320 or image.height<180:raise ValueError('size')
                # Flatten alpha and remove metadata; immutable JPEG is safe for thumbnails.set.
                background=Image.new('RGB',image.size,'white')
                if image.mode in ('RGBA','LA') or 'transparency' in image.info:
                    rgba=image.convert('RGBA');background.paste(rgba,mask=rgba.getchannel('A'))
                else:background=image.convert('RGB')
                if max(background.size)>3840:background.thumbnail((3840,2160))
                result=b''
                for quality in (90,82,72):
                    out=io.BytesIO();background.save(out,format='JPEG',quality=quality,optimize=True)
                    result=out.getvalue()
                    if len(result)<=MAX_IMAGE:break
                if len(result)>MAX_IMAGE:raise ValueError('size')
        except Exception:
            raise WorkflowError('cover_invalid','请上传有效 JPG/PNG 封面；AI 封面必须为 16:9',400) from None
        tenant,owner=self._actor(actor);asset_id=uid();path=self.root/(asset_id+'.jpg')
        fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(result)
        digest=hashlib.sha256(result).hexdigest()
        with self.db(True) as c:
            c.execute('INSERT INTO youtube_auto_asset VALUES(?,?,?,?,?,?,?)',(asset_id,tenant,owner,str(path),digest,'image/jpeg',now()))
        return {'id':asset_id,'url':self.asset_url(asset_id)}

    def upload_cover(self,actor,payload):
        self._actor(actor)
        raw=payload.get('data','')
        if not isinstance(raw,str) or len(raw)>MAX_IMAGE*4//3+8:raise WorkflowError('cover_size','封面图片不能超过 2 MB')
        try:data=base64.b64decode(raw,validate=True)
        except Exception:raise WorkflowError('cover_invalid','图片数据无效') from None
        return {'asset':self._store_image(actor,data)}

    def settings(self,actor):
        tenant,_=self._actor(actor)
        with self.db() as c:row=c.execute('SELECT description FROM youtube_auto_setting WHERE tenant=?',(tenant,)).fetchone()
        return {'default_description':row['description'] if row else DEFAULT_DESCRIPTION}

    def save_settings(self,actor,payload):
        tenant,_=self._actor(actor)
        if actor.get('role')!='admin':raise WorkflowError('forbidden','仅管理员可修改默认描述',403)
        text=payload.get('default_description','')
        render(text,{},'description',allow_unresolved=True)
        with self.db(True) as c:
            c.execute('INSERT INTO youtube_auto_setting VALUES(?,?,?) ON CONFLICT(tenant) DO UPDATE SET description=excluded.description,updated_at=excluded.updated_at',(tenant,text.strip(),now()))
        return {'settings':self.settings(actor)}

    def channel_options(self,actor):
        self._actor(actor);_,state=self.source.configuration()
        channels=self.channels(actor) if state['configured'] else []
        safe=[{k:v for k,v in row.items() if k not in ('scopes','youtube_account_id')} for row in channels]
        return {'channels':safe}

    def bootstrap(self,actor,*,include_channels=True):
        self._actor(actor);_,state=self.source.configuration()
        channels=self.channel_options(actor)['channels'] if include_channels else []
        return {'settings':self.settings(actor),'source':state,'channels':channels,'channels_loaded':bool(include_channels),'can_manage_settings':actor.get('role')=='admin','enabled':self.enabled}

    def list_materials(self,actor,search=''):
        self._actor(actor);return self.source.list(search)

    def _dto(self,body,actor):
        value=json.loads(encode(body))
        value.pop('creator',None);value.pop('request',None);value.pop('lease_token',None)
        value['channel']={k:v for k,v in value['channel'].items() if k not in ('scopes','youtube_account_id')}
        ledger=None
        if body.get('publish_id'):
            ledger=self.engine_store.youtube_task(int(body['publish_id']))
        phase=value.get('phase','cover');status=value['status'];error=value.get('error',{})
        if ledger:
            phase=ledger.get('reviewed_phase') or ledger.get('status')
            ls=ledger['status']
            if ls=='published':
                status='published' if ledger.get('comment_status') in ('published','skipped') else 'uploading'
                if status=='uploading':phase='comment'
            elif ls in ('failed','unknown','partial_failed'):
                status='comment_failed' if phase=='comment' or ls=='partial_failed' else 'thumbnail_failed' if phase=='thumbnail' else 'publish_failed'
                error={'code':ledger.get('error_code') or ledger.get('video_error_code') or 'publish_failed','message':ledger.get('error_message') or ledger.get('video_error_message') or ledger.get('comment_error_message') or '发布未完成，请查看阶段状态'}
            else:status='uploading'
            value['video_id']=ledger.get('video_id','')
            value['unknown_outcome']=bool(ledger.get('unknown_outcome'))
            value['can_retry']=(ls in ('failed','partial_failed') and not value['unknown_outcome']) or (ls=='unknown' and phase=='public' and bool(ledger.get('video_id')) and ledger.get('comment_status')!='unknown')
        else:value['can_retry']=status in ('generation_failed','enqueue_failed')
        value.update(status=status,phase=phase,error=error)
        value['can_review']=body['status']=='review' and not body.get('publish_id')
        value['versions']=[dict(v,url=self.asset_url(v['asset_id'])) for v in value.get('versions',[]) if v.get('asset_id')]
        current=next((v for v in value['versions'] if v['number']==body['current_version']),None)
        value['cover_url']=current['url'] if current else ''
        with self.db() as c:n=c.execute('SELECT state,message FROM youtube_auto_notification WHERE task_id=? AND version=?',(body['id'],body['current_version'])).fetchone()
        value['notification']={'status':n['state'],'message':n['message']} if n else {'status':'none','message':''}
        phases=['upload','thumbnail','processing','public','comment']
        current_index=phases.index(phase) if phase in phases else -1
        steps=[]
        for index,(key,label) in enumerate(zip(phases,['上传视频（私享）','设置已审核封面','视频处理','公开视频','首条评论'])):
            state='pending';message='等待前一步完成'
            if ledger:
                if key=='upload' and ledger.get('video_id'):state='complete';message='视频已上传'
                elif key=='thumbnail' and ledger.get('thumbnail_status') in ('succeeded','complete','set','published'):state='complete';message='封面设置成功'
                elif key=='processing' and ledger.get('processing_status') in ('succeeded','complete'):state='complete';message='视频处理完成'
                elif key=='public' and ledger.get('video_state')=='published':state='complete';message='视频已公开'
                elif key=='comment' and ledger.get('comment_status')=='skipped':state='skipped';message='未填写，已跳过'
                elif key=='comment' and ledger.get('comment_status')=='published':state='complete';message='首评已发送'
                elif status=='published':state='complete';message='已完成'
                elif index<current_index:state='complete';message='已完成'
                elif index==current_index:state='error' if status.endswith('_failed') or value.get('unknown_outcome') else 'active';message=error.get('message','处理中') if state=='error' else '处理中'
            steps.append({'key':key,'label':label,'status':state,'message':message})
        value['steps']=steps
        return value

    def get_task(self,actor,task_id):
        with self.db() as c:_,body=self._row(c,task_id,actor)
        return {'task':self._dto(body,actor)}

    def list_tasks(self,actor,search='',status='all'):
        tenant,owner=self._actor(actor)
        with self.db() as c:
            rows=c.execute('SELECT body FROM youtube_auto_preparation WHERE tenant=?'+('' if actor.get('role')=='admin' else ' AND owner=?')+' ORDER BY created_at DESC LIMIT 200',(tenant,) if actor.get('role')=='admin' else (tenant,owner)).fetchall()
        items=[self._dto(json.loads(row['body']),actor) for row in rows]
        counts={'all':len(items),'review':0,'running':0,'published':0,'failed':0}
        for item in items:
            key='review' if item['status']=='review' else 'published' if item['status']=='published' else 'failed' if item['status'].endswith('_failed') else 'running'
            counts[key]+=1
            if item['status']=='comment_failed':counts['published']+=1
        search=str(search or '').casefold()[:200]
        selected=[]
        for item in items:
            if search and search not in (item['title']+' '+item['id']+' '+item['material']['name']).casefold():continue
            if status not in ('all','',item['status']) and not (status=='published' and item['status']=='comment_failed') and not (status=='running' and item['status'] in ('generating','queued_generation','enqueue_pending','uploading')) and not (status=='failed' and item['status'].endswith('_failed')):continue
            selected.append(item)
        return {'items':selected,'total':len(selected),'counts':counts,'limit':200,'bounded':len(items)==200}

    def create_task(self,actor,payload):
        tenant,owner=self._actor(actor)
        if not self.enabled:raise WorkflowError('feature_disabled','YouTube 自动发布暂未启用',503)
        if not isinstance(payload,dict):raise WorkflowError('invalid_request','请求格式无效')
        op=str(payload.get('operation_id') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',op):raise WorkflowError('operation_id_required','缺少有效提交标识，请刷新页面')
        allowed=('material_id','channel_id','title_template','description_template','comment_template','cover_source','requirements','cover_asset_id')
        request={key:payload.get(key,'') for key in allowed};digest=hashlib.sha256(encode(request).encode()).hexdigest()
        with self.db() as c:
            previous=c.execute('SELECT * FROM youtube_auto_preparation WHERE tenant=? AND owner=? AND operation_id=?',(tenant,owner,op)).fetchone()
        if previous:
            if previous['request_sha']!=digest:raise WorkflowError('idempotency_conflict','同一提交标识的内容已变化，请重新提交',409)
            return {'task':self._dto(json.loads(previous['body']),actor)}
        material=self.source.get(str(request['material_id']))
        channels=self.channels(actor)
        channel=next((x for x in channels if str(x['id'])==str(request['channel_id'])),None)
        if channel is None or not channel.get('eligible'):raise WorkflowError('channel_unavailable','请选择授权有效且支持封面设置的频道',409)
        if request['comment_template'] and not channel.get('comment_eligible'):raise WorkflowError('comment_scope_required','该频道尚未获得首评权限',409)
        # Validate all syntax/required fields before allocating a short link.
        for field in ('title','description','comment'):render(request[field+'_template'],material,field,allow_unresolved=True)
        cover_source=request['cover_source']; requirements=str(request['requirements'] or '').strip()
        if cover_source not in ('local','ai'):raise WorkflowError('cover_source_required','请选择封面来源')
        if cover_source=='ai' and (not requirements or len(requirements)>2000):raise WorkflowError('cover_requirements_required','请填写 1–2000 字封面要求')
        if cover_source=='local':
            with self.db() as c:asset=self._asset(c,request['cover_asset_id'],actor)
        # Resolve required drama metadata before a short-link side effect.
        needs_url=any('{url}' in str(request[field+'_template']) for field in ('title','description','comment'))
        if needs_url and not long_url(material):raise WorkflowError('source_association_missing','使用 {url} 需要有效的剧集 ID 和来源合成任务 ID',409)
        preflight=dict(material,macro_url='{url}')
        for field in ('title','description','comment'):
            render(request[field+'_template'],preflight,field)
        if needs_url:
            material['macro_url']=str(self.short_link(material))
        resolved={field:render(request[field+'_template'],material,field) for field in ('title','description','comment')}
        task_id=uid();created=now()
        body=dict(resolved,id=task_id,operation_id=op,material=material,channel=channel,creator=dict(actor),request=request,
                  title_template=request['title_template'],description_template=request['description_template'],comment_template=request['comment_template'],
                  cover_source=cover_source,requirements=requirements,created_at=created,updated_at=created,current_version=1,versions=[],publish_id=None,error={},phase='cover',notification={})
        state='queued_generation' if cover_source=='ai' else 'enqueue_pending'
        body['versions']=[{'number':1,'asset_id':asset['id'] if cover_source=='local' else '', 'feedback':'','created_at':created,'source':cover_source,'approved':cover_source=='local'}]
        body['status']=state
        with self.db(True) as c:
            try:c.execute('INSERT INTO youtube_auto_preparation(id,tenant,owner,operation_id,request_sha,state,version,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(task_id,tenant,owner,op,digest,state,1,encode(body),created,created))
            except sqlite3.IntegrityError:
                previous=c.execute('SELECT * FROM youtube_auto_preparation WHERE tenant=? AND owner=? AND operation_id=?',(tenant,owner,op)).fetchone()
                if previous is None or previous['request_sha']!=digest:raise WorkflowError('idempotency_conflict','提交内容冲突',409) from None
                body=json.loads(previous['body'])
        return {'task':self._dto(body,actor)}

    def review(self,actor,task_id,payload):
        action=payload.get('action');feedback=str(payload.get('feedback') or '').strip()
        if action not in ('approve','reject','manual'):raise WorkflowError('invalid_review','审核操作无效')
        if action=='reject' and (not feedback or len(feedback)>2000):raise WorkflowError('feedback_required','请填写 1–2000 字修改意见')
        with self.db(True) as c:
            row,body=self._row(c,task_id,actor)
            try:version=int(payload.get('version'))
            except (TypeError,ValueError):raise WorkflowError('version_required','请刷新后审核当前版本') from None
            if row['state']!='review' or version!=row['version'] or body.get('publish_id'):
                raise WorkflowError('review_conflict','当前版本已处理或已更新，请刷新后查看',409)
            current=body['versions'][-1]
            if action=='reject':
                body['current_version']+=1
                body['versions'].append({'number':body['current_version'],'asset_id':'','feedback':feedback,'created_at':now(),'source':'ai','approved':False})
                state='queued_generation'
            else:
                if action=='manual':
                    asset=self._asset(c,payload.get('cover_asset_id'),actor)
                    # A tenant admin can review another user's task. The final
                    # immutable copy belongs to that task's creator as well.
                    if asset['owner']!=row['owner']:
                        new_id=uid();new_path=self.root/(new_id+'.jpg')
                        fd=os.open(str(new_path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                        with os.fdopen(fd,'wb') as f:f.write(Path(asset['path']).read_bytes())
                        c.execute('INSERT INTO youtube_auto_asset VALUES(?,?,?,?,?,?,?)',(new_id,row['tenant'],row['owner'],str(new_path),asset['sha256'],asset['mime'],now()))
                        asset=dict(asset,id=new_id,path=str(new_path),owner=row['owner'])
                    body['current_version']+=1
                    current={'number':body['current_version'],'asset_id':asset['id'],'feedback':'人工替换封面','created_at':now(),'source':'manual','approved':True}
                    body['versions'].append(current)
                else:self._asset(c,current['asset_id'],body['creator'])
                current.update(approved=True,reviewer_user_id=str(actor['user_id']),approved_at=now())
                state='enqueue_pending'
            body['error']={};self._save(c,body,state)
        return {'task':self._dto(body,actor)}

    def retry(self,actor,task_id):
        with self.db(True) as c:
            _,body=self._row(c,task_id,actor)
            publish_id=body.get('publish_id')
            if not publish_id:
                if body['status'] not in ('generation_failed','enqueue_failed'):raise WorkflowError('retry_unavailable','当前阶段不能重试',409)
                body['error']={};self._save(c,body,'queued_generation' if body['status']=='generation_failed' else 'enqueue_pending')
        if publish_id:self.engine_store.retry_reviewed_youtube(int(publish_id))
        return self.get_task(actor,task_id)

    def _claim(self):
        with self.db(True) as c:
            # A interrupted generator is made visible for explicit retry; never silently make another paid generation.
            expired=c.execute("SELECT * FROM youtube_auto_preparation WHERE state='generating' AND lease_until<?",(time.time(),)).fetchall()
            for row in expired:
                body=json.loads(row['body']);body['error']={'code':'generation_interrupted','message':'封面生成中断，请重试'};self._save(c,body,'generation_failed')
            c.execute("UPDATE youtube_auto_notification SET state='unknown',message='提醒发送结果待核查，可从任务列表审核' WHERE state='sending' AND lease_until<?",(time.time(),))
            row=c.execute("SELECT * FROM youtube_auto_preparation WHERE state IN ('queued_generation','enqueue_pending') AND lease_until<? ORDER BY created_at LIMIT 1",(time.time(),)).fetchone()
            if not row:return None
            body=json.loads(row['body']);token=uid();generation=row['state']=='queued_generation'
            body['status']='generating' if generation else 'enqueue_pending'
            c.execute('UPDATE youtube_auto_preparation SET state=?,body=?,lease_token=?,lease_until=? WHERE id=?',(body['status'],encode(body),token,time.time()+1800,body['id']))
            return body,token,generation

    def run_once(self,worker_id='worker'):
        if not self.enabled:return {'claimed':False}
        notification=self._notify_once()
        if notification.get('claimed'):return notification
        claimed=self._claim()
        if claimed:
            body,token,generation=claimed;task_id=body['id'];version=body['current_version']
            try:
                if generation:
                    if self.generate is None:raise WorkflowError('generator_unconfigured','封面生图服务尚未配置',503)
                    output=self.generate(body,body['versions'][-1])
                    data=output if isinstance(output,bytes) else Path(output).read_bytes()
                    asset=self._store_image(body['creator'],data,generated=True)
                    with self.db(True) as c:
                        row,current=self._row(c,task_id)
                        if row['lease_token']!=token or row['state']!='generating' or row['version']!=version:return {'claimed':True,'stale':True}
                        current['versions'][-1]['asset_id']=asset['id'];current['versions'][-1]['created_at']=now();current['error']={}
                        self._save(c,current,'review')
                        c.execute('UPDATE youtube_auto_preparation SET lease_token=?,lease_until=0 WHERE id=?',('',task_id))
                        c.execute("INSERT OR IGNORE INTO youtube_auto_notification(task_id,version,state) VALUES(?,?,'pending')",(task_id,version))
                else:
                    approved=body['versions'][-1]
                    if not approved.get('approved'):raise WorkflowError('cover_not_approved','封面尚未审核',409)
                    with self.db() as c:asset=self._asset(c,approved['asset_id'],body['creator'])
                    m=body['material'];ch=body['channel']
                    ledger=self.engine_store.enqueue_reviewed_youtube(
                        preparation_id=task_id,source_material_id=m['id'],approved_cover_path=asset['path'],approved_cover_sha256=asset['sha256'],
                        operation_id='youtube-auto-'+task_id,job_id=m.get('source_job_id') or task_id,content_id=m.get('content_id') or ('custom_source:'+m['id']),app_id='1479',
                        channel_local_id=str(ch['id']),channel_id=ch['channel_id'],youtube_account_id=ch['youtube_account_id'],source_kind='custom_source',source_url=m['url'],title=body['title'],
                        description_template=body['description_template'],description_rendered=body['description'],comment_text=body['comment'],duplicate_confirmed=False,
                        scopes=ch['scopes'],operator_user_id=body['creator']['user_id'],operator_name=body['creator'].get('name',''))
                    with self.db(True) as c:
                        row,current=self._row(c,task_id)
                        if row['lease_token']!=token:return {'claimed':True,'stale':True}
                        current['publish_id']=ledger['id'];current['phase']='upload';current['error']={};self._save(c,current,'uploading')
                        c.execute('UPDATE youtube_auto_preparation SET lease_token=?,lease_until=0 WHERE id=?',('',task_id))
            except Exception as exc:
                with self.db(True) as c:
                    row,current=self._row(c,task_id)
                    if row['lease_token']==token:
                        current['error']={'code':getattr(exc,'code','generation_failed' if generation else 'enqueue_failed'), 'message':str(exc) if isinstance(exc,WorkflowError) else ('封面生成失败，请重试或手动上传' if generation else '发布准备失败，请检查频道与素材后重试')}
                        self._save(c,current,'generation_failed' if generation else 'enqueue_failed')
                        c.execute('UPDATE youtube_auto_preparation SET lease_token=?,lease_until=0 WHERE id=?',('',task_id))
            return {'claimed':True,'task_id':task_id}
        return self._notify_once()

    def _notify_once(self):
        with self.db(True) as c:
            row=c.execute("SELECT n.task_id,n.version,p.body FROM youtube_auto_notification n JOIN youtube_auto_preparation p ON p.id=n.task_id WHERE n.state='pending' ORDER BY p.created_at LIMIT 1").fetchone()
            if not row:return {'claimed':False}
            body=json.loads(row['body']);version=row['version']
            if body['status']!='review' or version!=body['current_version']:
                c.execute("UPDATE youtube_auto_notification SET state='superseded' WHERE task_id=? AND version=?",(body['id'],version));return {'claimed':True}
            c.execute("UPDATE youtube_auto_notification SET state='sending',lease_until=? WHERE task_id=? AND version=?",(time.time()+120,body['id'],version))
        state='sent';message='飞书审核提醒已发送'
        try:
            if self.notify is None:raise WorkflowError('notification_unconfigured','飞书提醒服务未配置',503)
            self.notify(body,version,self.public_base+'/youtube-publish.html?task_id='+body['id']+'&version='+str(version))
        except WorkflowError as exc:state='failed';message=str(exc)
        except Exception:state='unknown';message='提醒发送结果待核查，可从任务列表审核'
        with self.db(True) as c:c.execute('UPDATE youtube_auto_notification SET state=?,message=?,lease_until=0 WHERE task_id=? AND version=?',(state,message,body['id'],version))
        return {'claimed':True,'task_id':body['id'],'notification':state}
