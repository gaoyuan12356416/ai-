"""Browser-to-COS multipart transport; CPU receives no media body."""
import base64
import hashlib
import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import VideoEditingError, require
from .catalog import resource_code
from .store import can_manage, encode, identifier, now, owner

MAX_BYTES = 8 * 1024**3
PART_BYTES = 16 * 1024**2
SIGN_SECONDS = 600


def _status(exc):
    getter = getattr(exc, 'get_status_code', None)
    try:
        return int(getter() if callable(getter) else getattr(exc, 'status_code', 0))
    except (TypeError, ValueError):
        return 0


class Uploads:
    def __init__(self, store, client, bucket, region, prefix='video-editing/v1'):
        self.store, self.client, self.bucket, self.region = store, client, bucket, region
        self.prefix = prefix.strip('/')
        self.resource_resolver = None

    def configured(self):
        require(self.client is not None and self.bucket and self.region, 'cos_not_configured', 'COS 尚未配置，不能上传', 503)

    def verify_private(self,key):
        """Read the effective object ACL; never accept inherited public grants."""
        try:
            acl=self.client.get_object_acl(Bucket=self.bucket,Key=key)
            grants=acl.get('AccessControlList',{}).get('Grant',[])
            if isinstance(grants,dict):grants=[grants]
            require(isinstance(grants,list) and grants,'cos_acl_unverified','COS 对象访问权限尚未验证',409)
            for grant in grants:
                grantee=grant.get('Grantee',{})
                uri=str(grantee.get('URI',''))
                require(not uri and str(grantee.get('Type','')).lower()!='group','cos_object_public','COS 对象存在公开授权，已停止入库',409)
        except VideoEditingError:
            raise
        except Exception:
            raise VideoEditingError('cos_acl_unverified','COS 对象访问权限尚未验证',503) from None

    def get(self, upload_id, actor, db=None):
        row = self.store.get('upload', upload_id, db)
        require(row is not None and can_manage(row, actor), 'upload_not_found', '上传会话不存在', 404)
        return row

    @staticmethod
    def dto(row):
        d = row['data']
        return {'id': row['id'], 'asset_id': row['asset_id'], 'status': row['status'], 'name': d['name'],
                'size_bytes': d['size_bytes'], 'part_size': PART_BYTES, 'part_count': d['part_count'],
                'uploaded_parts': d.get('uploaded_parts', []), 'created_at': row['created_at'],
                'library_kind': d.get('library_kind', 'material'), 'series_code': d.get('series_code', ''),
                'english_title': d.get('english_title', ''),
                'expires_at': d.get('expires_at'), 'error_code': d.get('error_code', '')}

    def create(self, payload, actor, *, allow_create=True):
        self.configured()
        operation = str(payload.get('operation_id', ''))
        require(re.fullmatch(r'[A-Za-z0-9_-]{8,128}', operation), 'operation_id_required', '请提供上传操作标识')
        size = payload.get('size_bytes')
        require(type(size) is int and 0 < size <= MAX_BYTES, 'file_size_invalid', '视频大小须在 1 字节至 8 GiB 之间')
        name = str(payload.get('name', '')).strip()
        require(0 < len(name) <= 240 and not any(ord(c) < 32 for c in name), 'name_invalid', '文件名称无效')
        content_type = str(payload.get('content_type', '')).lower()
        require(content_type in {'video/mp4','video/quicktime','video/webm','video/x-matroska','video/x-msvideo','video/mpeg','application/octet-stream'}, 'video_type_invalid', '请上传视频文件')
        identity = {'name': name, 'size_bytes': size, 'content_type': content_type}
        def existing(db):
            found = db.execute('SELECT * FROM ve_upload WHERE owner=? AND operation_id=?', (owner(actor), operation)).fetchone()
            if not found:
                return None
            row = self.store.row(found)
            require(all(row['data'][k] == v for k,v in identity.items()), 'operation_conflict', '同一上传操作的文件已变化', 409)
            for field, default in (('library_kind', 'material'), ('series_code', '')):
                if field in payload:
                    supplied = payload[field].strip() if field == 'series_code' and isinstance(payload[field], str) else payload[field]
                    require(supplied == row['data'].get(field, default), 'operation_conflict', '同一上传操作的资源或栏目已变化', 409)
            return row
        # Old and uncertain sessions must remain recoverable even when the
        # catalog is unavailable or the new upload-tag fields were not sent.
        with self.store.transaction() as db:
            row = existing(db)
            if row:
                return row
        require(allow_create,'video_editing_intake_disabled','新剪辑上传已暂停，已有上传可继续核查',503)
        library_kind = payload.get('library_kind')
        require(library_kind in {'material', 'free_episode'}, 'library_kind_invalid', '请选择素材库或免费集栏目')
        code = resource_code(payload.get('series_code'))
        require(callable(self.resource_resolver), 'resource_catalog_unavailable', '资源目录暂不可用，请稍后重试', 503)
        resource = self.resource_resolver(code)
        require(isinstance(resource, dict) and resource.get('series_code') == code,
                'resource_catalog_invalid', '资源目录返回内容无效，请重新搜索选择', 503)
        tags = {'library_kind': library_kind, 'series_code': code,
                'english_title': str(resource.get('english_title') or '')[:255]}
        with self.store.transaction() as db:
            # Another request may have initialized the same operation while
            # this request was resolving the catalog; never create a second COS upload.
            row = existing(db)
            if row:
                return row
            upload_id, asset_id = identifier('vu_'), identifier('va_')
            key = self.prefix+'/sources/'+asset_id+'/source'
            data = {**identity, **tags, 'key': key, 'bucket': self.bucket, 'part_count': math.ceil(size/PART_BYTES),
                    'parts': {}, 'uploaded_parts': [], 'expires_at': now()+7*86400, 'upload_id': ''}
            self.store.insert('source', {'id':asset_id,'kind':'asset','owner':owner(actor),'name':name,'status':'uploading',
                'data':{**tags,'size_bytes':size,'content_type':content_type,'identity':{'bucket':self.bucket,'key':key,'size_bytes':size},'upload_session_id':upload_id,
                        'owner_user_id':str(actor['user_id']),'owner_name':str(actor.get('name',''))}}, db)
            self.store.insert('upload', {'id':upload_id,'owner':owner(actor),'operation_id':operation,'asset_id':asset_id,'status':'initializing','data':data}, db)
        try:
            result = self.client.create_multipart_upload(Bucket=self.bucket, Key=key, ACL='private', ContentType=content_type,
                Metadata={'x-cos-meta-ve-upload':upload_id, 'x-cos-meta-ve-size':str(size)})
            remote_id = str(result.get('UploadId',''))
            require(remote_id, 'cos_initialize_unknown', 'COS 初始化结果待核查', 503)
        except Exception:
            with self.store.transaction() as db:
                row = self.get(upload_id, actor, db); row['data']['error_code']='cos_initialize_unknown'
                self.store.update('upload',upload_id,{'status':'initializing_unknown','data':row['data']},db)
            raise VideoEditingError('cos_initialize_unknown','上传初始化结果待核查，请刷新会话，不要重新上传',503) from None
        with self.store.transaction() as db:
            row = self.get(upload_id,actor,db); row['data']['upload_id']=remote_id
            self.store.update('upload',upload_id,{'status':'uploading','data':row['data']},db)
        return self.get(upload_id,actor)

    def sign(self, upload_id, part_number, payload, actor):
        self.configured()
        md5 = str(payload.get('content_md5',''))
        try:
            digest = base64.b64decode(md5, validate=True)
        except Exception:
            digest = b''
        require(len(digest)==16 and base64.b64encode(digest).decode()==md5,'part_md5_invalid','分片校验值无效')
        with self.store.transaction() as db:
            row = self.get(upload_id,actor,db); d=row['data']
            require(row['status']=='uploading' and now()<d['expires_at'],'upload_not_writable','上传会话不可写，请刷新状态',409)
            require(type(part_number) is int and 1<=part_number<=d['part_count'],'part_number_invalid','分片编号无效')
            size = min(PART_BYTES,d['size_bytes']-(part_number-1)*PART_BYTES)
            old = d['parts'].get(str(part_number))
            require(old is None or old['content_md5']==md5,'part_content_changed','同一分片内容已改变，请重新选择原文件',409)
            d['parts'][str(part_number)]={'content_md5':md5,'size_bytes':size}
            self.store.update('upload',upload_id,{'data':d},db)
        headers={'Content-MD5':md5,'Content-Type':'application/octet-stream','x-cos-psize-min':str(size),'x-cos-psize-max':str(size)}
        try:
            signed=self.client.get_presigned_url(Bucket=self.bucket,Key=d['key'],Method='PUT',Expired=SIGN_SECONDS,
                Params={'uploadId':d['upload_id'],'partNumber':str(part_number)},Headers=headers)
            # Use the SDK's canonical signature as a REST Authorization header.
            # Never distribute either permanent or temporary secret keys.
            url=urlsplit(signed); pairs=parse_qsl(url.query,keep_blank_values=True)
            authorization='&'.join(k+'='+v for k,v in pairs if k.startswith('q-'))
            require(authorization and 'partnumber' in authorization and 'uploadid' in authorization,'cos_signing_invalid','COS 签名校验失败',503)
            headers['Authorization']=authorization
            unsigned=urlunsplit((url.scheme,url.netloc,url.path,urlencode([(k,v) for k,v in pairs if not k.startswith('q-')]),''))
        except Exception:
            raise VideoEditingError('cos_signing_failed','获取分片上传凭证失败，请重试',503) from None
        return {'url':unsigned,'headers':headers,'expires_at':now()+SIGN_SECONDS,'part_number':part_number,'size_bytes':size}

    def _head(self,d):
        try:
            value=self.client.head_object(Bucket=self.bucket,Key=d['key'])
        except Exception as exc:
            if _status(exc)==404:
                return None
            raise VideoEditingError('cos_unavailable','暂时无法核查 COS 对象',503) from None
        return {str(k).lower():v for k,v in value.items()}

    def _accept_head(self,row,head,db):
        d=row['data']
        require(str(head.get('x-cos-meta-ve-upload',''))==row['id'] and int(head.get('content-length',-1))==d['size_bytes'],
                'cos_object_conflict','COS 对象身份或大小不匹配',409)
        etag=str(head.get('etag','')).strip('"')
        require(etag,'cos_object_unverified','COS 对象尚未验证',409)
        source=self.store.get('source',row['asset_id'],db); sd=source['data']
        sd['identity']={**sd['identity'],'etag':etag}; sd['url']='https://'+self.bucket+'.cos.'+self.region+'.myqcloud.com/'+d['key']
        if source['status']!='deleted':
            self.store.update('source',source['id'],{'status':'uploaded','data':sd},db)
        d.pop('error_code',None)
        self.store.update('upload',row['id'],{'status':'completed','data':d},db)

    def _parts(self,d):
        marker=0; items=[]
        for _ in range(12):
            result=self.client.list_parts(Bucket=self.bucket,Key=d['key'],UploadId=d['upload_id'],MaxParts=1000,PartNumberMarker=marker)
            for p in result.get('Part',[]):
                items.append({'part_number':int(p['PartNumber']),'etag':str(p['ETag']),'size_bytes':int(p['Size'])})
            if str(result.get('IsTruncated','false')).lower()!='true':
                return items
            next_marker=int(result['NextPartNumberMarker'])
            require(next_marker>marker,'cos_parts_invalid','分片列表无效',503); marker=next_marker
        raise VideoEditingError('cos_parts_invalid','分片列表过大',503)

    def reconcile(self,upload_id,actor):
        self.configured(); row=self.get(upload_id,actor); d=row['data']
        if row['status'] in {'completed','aborted'}:
            return row
        head=self._head(d)
        if head:
            self.verify_private(d['key'])
            with self.store.transaction() as db:
                current=self.get(upload_id,actor,db)
                if current['status']!='completed': self._accept_head(current,head,db)
            return self.get(upload_id,actor)
        if row['status'] in {'completing','completing_unknown','aborting','abort_unknown'}:
            if row['status']=='completing' and now()-d.get('completion_started_at',row['updated_at'])>=120:
                # A crashed API process may leave the pre-call marker behind.
                # Expiry only enables explicit retry of the frozen same upload.
                with self.store.transaction() as db:
                    current=self.get(upload_id,actor,db)
                    if current['status']=='completing' and now()-current['data'].get('completion_started_at',current['updated_at'])>=120:
                        self.store.update('upload',upload_id,{'status':'completing_unknown'},db)
                return self.get(upload_id,actor)
            if row['status']=='aborting' and now()-d.get('abort_started_at',row['updated_at'])>=120:
                with self.store.transaction() as db:
                    current=self.get(upload_id,actor,db)
                    if current['status']=='aborting' and now()-current['data'].get('abort_started_at',current['updated_at'])>=120:
                        self.store.update('upload',upload_id,{'status':'abort_unknown'},db)
                return self.get(upload_id,actor)
            return row
        if not d['upload_id']:
            # Reconcile the unique server-generated key; an uncertain create
            # never blindly creates another multipart upload.
            try:
                result=self.client.list_multipart_uploads(Bucket=self.bucket,Prefix=d['key'],MaxUploads=1000)
                matches=[x for x in result.get('Upload',[]) if x.get('Key')==d['key']]
            except Exception:
                raise VideoEditingError('cos_reconcile_failed','上传初始化仍待核查',503) from None
            require(len(matches)==1,'cos_initialize_unknown','上传初始化结果尚未确认，请稍后核查',409)
            d['upload_id']=str(matches[0]['UploadId'])
        try:
            parts=self._parts(d)
        except Exception:
            raise VideoEditingError('cos_reconcile_failed','无法核查上传分片，请稍后重试',503) from None
        with self.store.transaction() as db:
            current=self.get(upload_id,actor,db)
            if current['status'] in {'initializing','initializing_unknown','uploading'}:
                current['data']['upload_id']=d['upload_id']; current['data']['uploaded_parts']=parts
                self.store.update('upload',upload_id,{'status':'uploading','data':current['data']},db)
        return self.get(upload_id,actor)

    def complete(self,upload_id,actor):
        self.configured(); row=self.reconcile(upload_id,actor)
        if row['status']=='completed': return row
        # A lost completion response can mean that COS never received it. Only
        # retry the same UploadId after reading the exact originally signed
        # parts; never initialize another session or overwrite another object.
        recovered_parts=None
        if row['status']=='completing_unknown':
            try:
                recovered_parts=self._parts(row['data'])
            except Exception:
                # NoSuchUpload may also mean a previous completion succeeded.
                # A second HEAD is authoritative; an absent object stays unknown.
                checked=self.reconcile(upload_id,actor)
                if checked['status']=='completed':return checked
                raise VideoEditingError('cos_complete_unknown','COS 合并状态仍待核查，请稍后重试',503) from None
        with self.store.transaction() as db:
            row=self.get(upload_id,actor,db); d=row['data']
            if row['status']=='completed':return row
            require(row['status']=='uploading' or (row['status']=='completing_unknown' and recovered_parts is not None),
                    'upload_not_completable','上传尚不可完成',409)
            parts=sorted(recovered_parts if recovered_parts is not None else d['uploaded_parts'],key=lambda p:p['part_number'])
            require([p['part_number'] for p in parts]==list(range(1,d['part_count']+1)),'parts_incomplete','部分分片尚未上传',409)
            for p in parts:
                expected=d['parts'].get(str(p['part_number']))
                require(expected and p['size_bytes']==expected['size_bytes'] and p['etag'].strip('"').lower()==base64.b64decode(expected['content_md5']).hex(),
                    'part_identity_mismatch','分片内容校验不一致',409)
            manifest=[{'PartNumber':p['part_number'],'ETag':p['etag']} for p in parts]
            require(not d.get('completion_parts') or d['completion_parts']==manifest,
                    'part_identity_mismatch','完成请求的分片身份已改变',409)
            d['completion_parts']=manifest
            d['completion_started_at']=now()
            self.store.update('upload',upload_id,{'status':'completing','data':d},db)
        try:
            self.client.complete_multipart_upload(Bucket=self.bucket,Key=d['key'],UploadId=d['upload_id'],
                MultipartUpload={'Part':manifest},
                Metadata={'x-cos-forbid-overwrite':'true'})
            head=self._head(d)
            require(head is not None,'cos_complete_unknown','COS 合并结果待核查',503)
            self.verify_private(d['key'])
            with self.store.transaction() as db: self._accept_head(self.get(upload_id,actor,db),head,db)
        except Exception:
            with self.store.transaction() as db:
                current=self.get(upload_id,actor,db)
                if current['status']!='completed':
                    current['data']['error_code']='cos_complete_unknown'
                    self.store.update('upload',upload_id,{'status':'completing_unknown','data':current['data']},db)
            raise VideoEditingError('cos_complete_unknown','COS 合并结果待核查，请刷新会话',503) from None
        return self.get(upload_id,actor)

    def abort(self,upload_id,actor):
        self.configured(); row=self.get(upload_id,actor); d=row['data']
        require(row['status'] not in {'completed','completing','completing_unknown'},'upload_cannot_abort','已完成或正在合并的上传不能取消',409)
        if row['status']=='aborted': return row
        require(d.get('upload_id'),'upload_reconcile_required','请先核查上传会话后再取消',409)
        with self.store.transaction() as db:
            current=self.get(upload_id,actor,db)
            require(current['status'] in {'uploading','initializing_unknown','abort_unknown'},'upload_cannot_abort','上传正在处理，请稍后刷新',409)
            current['data']['abort_started_at']=now()
            self.store.update('upload',upload_id,{'status':'aborting','data':current['data']},db)
        try: self.client.abort_multipart_upload(Bucket=self.bucket,Key=d['key'],UploadId=d['upload_id'])
        except Exception as exc:
            if _status(exc)!=404:
                with self.store.transaction() as db:self.store.update('upload',upload_id,{'status':'abort_unknown'},db)
                raise VideoEditingError('cos_abort_unknown','取消结果待核查',503) from None
        with self.store.transaction() as db:
            self.store.update('upload',upload_id,{'status':'aborted'},db)
            self.store.update('source',row['asset_id'],{'status':'deleted'},db)
        return self.get(upload_id,actor)
