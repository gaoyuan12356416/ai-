"""Explicit, one-output material-source ingestion with a non-retrying outbox."""
import hashlib
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import quote, urlsplit

import requests

from . import VideoEditingError, require
from .store import can_manage, encode, now, owner
from .uploads import MAX_BYTES, _status


PRODUCT_ID = 3214
PRODUCT_NAME = 'Drama-社媒专用素材'
CATEGORY = 'dramawave_post'
READ_PORT = 63350
COUNTRY_FOR_LANGUAGE = dict(en='US', es='ES', pt='PT', fr='FR', de='DE', it='IT',
    pl='PL', ru='RU', tr='TR', ar='SA', ja='JP', ko='KR', id='ID', th='TH',
    vi='VN', cs='CZ', el='GR', ro='RO', **{'zh-tw':'TW','zh-cn':'CN'})
ACTIVE = {'queued','copying','submitting','verifying','unknown_outcome'}
SIMPLE_COPY_LIMIT = 5 * 1024**3
COPY_PART_BYTES = 1024**3


def _literal(value):
    """UTF-8 SQL literal without shell quoting or collation surprises."""
    return 'CONVERT(0x'+str(value).encode('utf-8').hex()+' USING utf8mb4) COLLATE utf8mb4_bin'


def _headers(value):
    return {str(k).lower():str(v) for k,v in (value or {}).items()}


def _positive(value):
    return type(value) is int and value > 0 or isinstance(value,str) and bool(re.fullmatch(r'[1-9][0-9]*',value))


def _language(value):
    value=str(value or '').strip().lower().replace('_','-')
    value={'zh-hant':'zh-tw','zh-hans':'zh-cn','jp':'ja','kr':'ko','vn':'vi','cz':'cs'}.get(value,value)
    return value if value in {'zh-tw','zh-cn'} else value.split('-')[0]


class ReadOnlyCatalog:
    """Use the configured 63350 replica for personnel, drama and formal readback."""
    def __init__(self,app):
        self.app=app
        self.database=str(getattr(app,'ADMIN_MAPPING_MYSQL_DATABASE','') or getattr(app,'DB_NAME','')).strip()
        require(re.fullmatch(r'[A-Za-z0-9_]+',self.database),'material_catalog_unconfigured','只读素材目录未配置',503)

    def query(self,statement):
        app=self.app
        host=str(getattr(app,'ADMIN_MAPPING_MYSQL_HOST','')).strip()
        port=str(getattr(app,'ADMIN_MAPPING_MYSQL_PORT','')).strip()
        user=str(getattr(app,'ADMIN_MAPPING_MYSQL_USER','')).strip()
        require(host and port==str(READ_PORT) and user,'material_catalog_unconfigured','只读素材目录未配置',503)
        env=os.environ.copy()
        env['MYSQL_PWD']=str(getattr(app,'ADMIN_MAPPING_MYSQL_PASSWORD',''))
        env['MYSQL_MAX_EXECUTION_TIME_MS']='5000'
        env['SQL_GATE_JOB_NAME']='video-editing-material-source'
        command=['mysql','-h',host,'-P',port,'-u',user,'--connect-timeout=3',
                 '--default-character-set=utf8mb4','-N','-B','--raw','--skip-column-names','-e',
                 'SET SESSION MAX_EXECUTION_TIME=5000; SET SESSION TRANSACTION READ ONLY; '
                 'START TRANSACTION; SELECT IF(@@read_only=1,1,0); '+statement+'; COMMIT;']
        try:
            result=subprocess.run(command,env=env,capture_output=True,text=True,encoding='utf-8',timeout=25)
            require(result.returncode==0,'material_catalog_unavailable','只读素材目录暂不可用',503)
            lines=[line for line in result.stdout.splitlines() if line.strip()]
            require(lines and lines[0]=='1','material_catalog_not_readonly','素材目录只读校验失败',503)
            return [json.loads(line) for line in lines[1:]]
        except VideoEditingError:
            raise
        except Exception:
            raise VideoEditingError('material_catalog_unavailable','只读素材目录暂不可用',503) from None

    def initiator(self,email):
        email=str(email or '').strip().lower()
        require(email and '@' in email and len(email)<=255,'initiator_missing','登录账号没有可用于人员匹配的邮箱',409)
        rows=self.query('SELECT JSON_OBJECT(\'id\',CAST(sub_user_id AS CHAR)) FROM `'+self.database+'`.admin_user_group '
            'WHERE status=0 AND LOWER(TRIM(email))='+_literal(email)+' AND sub_user_id IS NOT NULL '
            'GROUP BY sub_user_id LIMIT 2')
        require(len(rows)==1 and _positive(rows[0].get('id')),'initiator_mapping_ambiguous','登录人未唯一对应有效人员 ID',409)
        return int(rows[0]['id'])

    def drama(self,app_id,content_id):
        require(re.fullmatch(r'[1-9][0-9]{0,18}',str(app_id)) and
                re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(content_id)),
                'drama_identity_invalid','成片原剧身份无效',409)
        table=str(getattr(self.app,'SOURCE_TABLE','ads_drama_resource'))
        require(re.fullmatch(r'[A-Za-z0-9_]+',table),'material_catalog_unconfigured','剧集目录未配置',503)
        rows=self.query('SELECT JSON_OBJECT(\'country\',country,\'language\',language,\'count\',COUNT(*)) '
            'FROM `'+self.database+'`.`'+table+'` WHERE app_id='+_literal(app_id)+' AND content_id='+_literal(content_id)+
            " AND type=2 AND sub_number>0 AND sub_url<>'' GROUP BY country,language "
            'ORDER BY COUNT(*) DESC,MAX(updated_at) DESC LIMIT 2')
        require(rows,'drama_metadata_missing','原剧没有可用的国家与语言',409)
        require(len(rows)==1,'drama_metadata_ambiguous','原剧有多个国家或语言版本，请先确认归属',409)
        language=_language(rows[0].get('language'))
        require(language in COUNTRY_FOR_LANGUAGE,'drama_language_unknown','原剧语言尚未支持素材入库',409)
        country=str(rows[0].get('country') or '').strip().upper()
        require(re.fullmatch(r'[A-Z]{2}',country),'drama_country_unknown','原剧国家无效，无法入库',409)
        return country,language

    def formal(self,content_sign):
        require(re.fullmatch(r'[0-9a-f]{64}',content_sign),'content_sign_invalid','成片入库签名无效',409)
        fields=('id','product','type','data_source_id','content_sign','category','country','language',
                'url','name','user_id','initiator','is_delete')
        expression=','.join("'"+field+"',`"+field+'`' for field in fields)
        return self.query('SELECT JSON_OBJECT('+expression+') FROM `'+self.database+'`.ads_custom_source '
                          'WHERE content_sign='+_literal(content_sign)+' LIMIT 3')

    def receipt(self,receipt_id):
        require(_positive(receipt_id),'material_receipt_invalid','素材接口回执 ID 无效',409)
        return self.query("SELECT JSON_OBJECT('id',id,'source_id',source_id,'source_type',source_type) "
                          'FROM `'+self.database+'`.ads_source WHERE id='+str(int(receipt_id))+' LIMIT 2')


class MaterialSource:
    def __init__(self,service,catalog=None,http=None,cos=None):
        self.service=service
        self.store=service.store
        self.catalog=catalog
        self.http=http
        self.cos=cos

    @staticmethod
    def dto(row):
        if row is None:return None
        data=row['data']
        result={'status':row['status'],'error':data.get('error','')}
        if data.get('source_variant'):
            result['source_variant']=dict(data['source_variant'])
        if row['status']=='succeeded' and _positive(data.get('material_id')):
            result['material_id']=str(data['material_id'])
        return result

    def for_task(self,task):
        if task['kind']!='render' or task['status']!='completed':return None
        output_id=task['data'].get('output_id')
        return self.dto(self.store.get('material_upload',output_id)) if output_id else None

    def _catalog(self):
        if self.catalog is None:
            require(self.service.app is not None,'material_catalog_unconfigured','素材目录未配置',503)
            self.catalog=ReadOnlyCatalog(self.service.app)
        return self.catalog

    def settings(self):
        bucket=os.environ.get('VIDEO_EDITING_MATERIAL_COS_BUCKET','').strip()
        region=os.environ.get('VIDEO_EDITING_MATERIAL_COS_REGION','').strip()
        prefix=os.environ.get('VIDEO_EDITING_MATERIAL_COS_PREFIX','video-editing/material-source/v1').strip('/')
        domain=os.environ.get('VIDEO_EDITING_MATERIAL_COS_DOMAIN','').strip()
        if domain.startswith('https://'):domain=domain[8:]
        require(re.fullmatch(r'[a-z0-9-]+-[0-9]{5,}',bucket) and
                re.fullmatch(r'ap-[a-z0-9-]+',region) and
                re.fullmatch(r'[A-Za-z0-9_./-]{1,120}',prefix) and '..' not in prefix.split('/') and
                domain==bucket+'.cos.'+region+'.myqcloud.com',
                'material_cos_unconfigured','公开 COS 存储配置无效',503)
        return bucket,region,prefix,domain

    def enqueue(self,task_id,actor,payload=None):
        task=self.service.task(task_id,actor)
        require(task['kind']=='render' and task['status']=='completed','material_task_not_ready','仅已完成的制作任务可以入库',409)
        output_id=task['data'].get('output_id')
        require(output_id,'material_output_missing','成片记录不存在',409)
        existing=self.store.get('material_upload',output_id)
        if existing:return self.dto(existing)
        marker=Path(os.environ.get('VIDEO_EDITING_MATERIAL_SOURCE_DISABLED_FILE','/etc/video-editing/material-source.disabled'))
        require(not marker.exists(),'material_source_intake_disabled','素材入库已暂停',503)
        output=self.store.get('output',output_id)
        result=task['data'].get('result') or {}
        artifact=result.get('artifact') or {}
        require(output and output['task_id']==task_id and output['status']=='ready' and
                output['data'].get('artifact')==artifact and artifact,
                'material_output_unverified','成片账本尚未完成校验',409)
        require(artifact.get('bucket')==self.service.uploads.bucket and
                str(artifact.get('key','')).startswith(self.service.uploads.prefix+'/') and
                type(artifact.get('size_bytes')) is int and 0<artifact['size_bytes']<=MAX_BYTES and
                re.fullmatch(r'[0-9a-f]{64}',str(artifact.get('sha256',''))),
                'material_artifact_invalid','成片 COS 身份无效',409)
        source_variant=None
        if task['data'].get('schema_version')==2:
            payload={} if payload is None else payload
            require(isinstance(payload,dict),'material_variant_required','请选择成片所属的产品和语言剧目版本',409)
            require(payload.get('app_id') and payload.get('content_id'),
                    'material_variant_required','请选择成片所属的产品和语言剧目版本',409)
            catalog=self.service.catalog
            require(catalog is not None,'material_catalog_unconfigured','资源版本目录暂不可用',503)
            selected=catalog.variant(payload['app_id'],payload['content_id'],series_code=task['data'].get('series_code'))
            country=str(selected.get('country') or '').strip().upper()
            language=_language(selected.get('language'))
            require(language in COUNTRY_FOR_LANGUAGE,'drama_language_unknown','所选剧目语言尚未支持素材入库',409)
            require(re.fullmatch(r'[A-Z]{2}',country),'drama_country_unknown','所选剧目国家无效，无法入库',409)
            content_id=str(selected['content_id'])
            source_variant={key:str(selected.get(key) or '') for key in ('app_id','content_id','series_code','title')}
            source_variant.update(country=country,language=language)
        else:
            country,language=self._catalog().drama(task['data'].get('app_id'),task['data'].get('content_id'))
            content_id=str(task['data']['content_id'])
        initiator=self._catalog().initiator(actor.get('email'))
        bucket,region,prefix,domain=self.settings()
        sign=hashlib.sha256(('video-editing-material-source-v1\0'+output_id+'\0'+artifact['sha256']).encode()).hexdigest()
        key=prefix+'/'+output_id+'-'+artifact['sha256'][:16]+'.mp4'
        url='https://'+domain+'/'+quote(key,safe='/')
        name=(str(task['data'].get('drama_title') or content_id).strip()[:180]
              +'_'+output_id+'.mp4')
        request={'app_id':PRODUCT_ID,'country':country,'language':language,'content_id':content_id,
                 'content_sign':sign,'url':url,'name':name,'user_id':248,'initiator':initiator,'category':CATEGORY}
        data={'artifact':artifact,'target_bucket':bucket,'target_region':region,'target_key':key,
              'request':request,'attempts':0,'receipt_id':'','material_id':'','error':'','next_check_at':0}
        if source_variant:data['source_variant']=source_variant
        with self.store.transaction() as db:
            current=self.store.get('task',task_id,db)
            require(current and can_manage(current,actor) and current['status']=='completed' and
                    current['data'].get('output_id')==output_id,'material_task_changed','制作任务状态已变化',409)
            existing=self.store.get('material_upload',output_id,db)
            if existing:return self.dto(existing)
            self.store.insert('material_upload',{'id':output_id,'output_id':output_id,'task_id':task_id,
                'owner':owner(actor),'status':'queued','data':data},db)
        return self.dto(self.store.get('material_upload',output_id))

    def _cos(self,region):
        if self.cos is not None:return self.cos
        app=self.service.app
        require(app and getattr(app,'COS_SECRET_ID','') and getattr(app,'COS_SECRET_KEY',''),
                'material_cos_unconfigured','COS 凭证未配置',503)
        from qcloud_cos import CosConfig,CosS3Client
        self.cos=CosS3Client(CosConfig(Region=region,SecretId=app.COS_SECRET_ID,
                                     SecretKey=app.COS_SECRET_KEY,Scheme='https',Timeout=60,KeepAlive=False),retry=0)
        return self.cos

    def _source_head(self,artifact):
        source=self.service.uploads
        source.configured()
        require(artifact['bucket']==source.bucket and artifact['key'].startswith(source.prefix+'/'),
                'material_artifact_invalid','成片 COS 身份无效',409)
        head=_headers(source.client.head_object(Bucket=source.bucket,Key=artifact['key']))
        require(int(head.get('content-length','-1'))==artifact['size_bytes'] and
                head.get('x-cos-meta-sha256')==artifact['sha256'],
                'material_artifact_changed','成片大小或 SHA 与账本不符',409)
        source.verify_private(artifact['key'])
        return head

    def _target_head(self,row,client):
        data=row['data']
        try:head=_headers(client.head_object(Bucket=data['target_bucket'],Key=data['target_key']))
        except Exception as exc:
            if _status(exc)==404:return None
            raise
        artifact=data['artifact']
        require(int(head.get('content-length','-1'))==artifact['size_bytes'] and
                head.get('x-cos-meta-sha256')==artifact['sha256'],
                'material_public_copy_conflict','公开 COS 副本与成片身份不符',409)
        return head

    def _public_head(self,row):
        data=row['data'];url=data['request']['url']
        parsed=urlsplit(url)
        require(parsed.scheme=='https' and not parsed.query and not parsed.fragment and
                parsed.hostname==data['target_bucket']+'.cos.'+data['target_region']+'.myqcloud.com',
                'material_public_url_invalid','公开 COS 地址无效',409)
        session=self.http or requests.Session()
        if self.http is None:session.trust_env=False
        try:
            response=session.head(url,allow_redirects=False,timeout=(5,30))
            require(response.status_code==200 and int(response.headers.get('Content-Length','-1'))==data['artifact']['size_bytes'],
                    'material_public_unreadable','公开 COS 副本尚不可匿名读取',503)
        finally:
            if self.http is None:session.close()

    @staticmethod
    def _public_acl(row,client):
        data=row['data']
        acl=client.get_object_acl(Bucket=data['target_bucket'],Key=data['target_key'])
        grants=acl.get('AccessControlList',{}).get('Grant',[])
        if isinstance(grants,dict):grants=[grants]
        require(isinstance(grants,list) and any(
            str(grant.get('Grantee',{}).get('Type','')).lower()=='group' and
            'AllUsers' in str(grant.get('Grantee',{}).get('URI','')) and
            str(grant.get('Permission','')).upper() in {'READ','FULL_CONTROL'}
            for grant in grants),
            'material_public_acl_missing','公开 COS 副本没有明确的公共读取权限',409)

    def _multipart_copy(self,row,client,source):
        data=row['data'];bucket=data['target_bucket'];key=data['target_key']
        upload_id=str(data.get('multipart_upload_id') or '')
        if not upload_id:
            created=client.create_multipart_upload(Bucket=bucket,Key=key,ACL='public-read',
                ContentType='video/mp4',Metadata={'x-cos-meta-sha256':data['artifact']['sha256']})
            upload_id=str(created.get('UploadId') or '')
            require(upload_id,'material_copy_session_unknown','COS 分块复制会话未返回编号',503)
            row=self.change(row,'copying',multipart_upload_id=upload_id,copy_parts={})
            data=row['data']
        total=data['artifact']['size_bytes']
        parts=dict(data.get('copy_parts') or {})
        if data.get('multipart_upload_id') and parts:
            # The process may have died after receiving a part but before
            # saving it. Remote list is the authority for this upload ID.
            remote=client.list_parts(Bucket=bucket,Key=key,UploadId=upload_id)
            require(str(remote.get('IsTruncated','false')).lower() not in {'true','1'},
                    'material_copy_parts_unknown','COS 分块列表不完整',503)
            confirmed={}
            for part in remote.get('Part',[]) or []:
                number=int(part.get('PartNumber',0))
                start=(number-1)*COPY_PART_BYTES
                expected=min(COPY_PART_BYTES,total-start)
                require(1<=number<=len(range(0,total,COPY_PART_BYTES)) and
                        int(part.get('Size',-1))==expected and str(part.get('ETag') or ''),
                        'material_copy_part_conflict','COS 分块身份不匹配',409)
                confirmed[str(number)]=str(part['ETag'])
            parts=confirmed
            row=self.change(row,'copying',copy_parts=parts)
        for number,start in enumerate(range(0,total,COPY_PART_BYTES),1):
            if str(number) in parts:continue
            end=min(total-1,start+COPY_PART_BYTES-1)
            receipt=client.upload_part_copy(Bucket=bucket,Key=key,PartNumber=number,UploadId=upload_id,
                CopySource=source,CopySourceRange=f'bytes={start}-{end}')
            etag=str(receipt.get('ETag') or '')
            require(etag,'material_copy_part_unknown','COS 分块回执缺少 ETag',503)
            parts[str(number)]=etag
            row=self.change(row,'copying',copy_parts=parts)
        manifest=[{'PartNumber':number,'ETag':parts[str(number)]}
                  for number in range(1,len(range(0,total,COPY_PART_BYTES))+1)]
        self.change(row,'copying',multipart_complete_started_at=now())
        client.complete_multipart_upload(Bucket=bucket,Key=key,UploadId=upload_id,
                                         MultipartUpload={'Part':manifest})

    def ensure_public_copy(self,row):
        data=row['data'];artifact=data['artifact']
        source_head=self._source_head(artifact)
        bucket,region,_,domain=self.settings()
        require(bucket==data['target_bucket'] and region==data['target_region'] and
                data['request']['url'].startswith('https://'+domain+'/'),
                'material_cos_config_changed','公开 COS 配置与冻结任务不一致',409)
        client=self._cos(region)
        target=self._target_head(row,client)
        if target is None:
            source={'Bucket':artifact['bucket'],'Key':artifact['key'],'Region':self.service.uploads.region}
            if artifact['size_bytes']<SIMPLE_COPY_LIMIT:
                client.copy_object(Bucket=bucket,Key=data['target_key'],CopySource=source,
                    CopyStatus='Replaced',ACL='public-read',ContentType='video/mp4',
                    Metadata={'x-cos-meta-sha256':artifact['sha256']})
            else:
                # SDK 1.9.44 copy() can choose single-copy on same-region
                # objects even above 5 GiB; explicitly use Upload Part Copy.
                self._multipart_copy(row,client,source)
            target=self._target_head(row,client)
            require(target is not None,'material_copy_missing','公开 COS 副本尚未生成',503)
        source_crc=source_head.get('x-cos-hash-crc64ecma')
        target_crc=target.get('x-cos-hash-crc64ecma')
        if source_crc and target_crc:
            require(source_crc==target_crc,'material_public_copy_conflict','公开 COS 副本 CRC64 校验失败',409)
        self._public_acl(row,client)
        self._public_head(row)

    def post(self,request):
        app=self.service.app
        endpoint=str(getattr(app,'AD_MATERIAL_SOURCE_API_URL','') or '').strip()
        token=str(getattr(app,'AD_MATERIAL_SOURCE_API_TOKEN','') or '').strip()
        require(endpoint=='https://aa.yingliangads.com/api/material/source' and token,
                'material_source_api_unconfigured','素材入库接口尚未配置',503)
        session=self.http or requests.Session()
        if self.http is None:session.trust_env=False
        try:
            response=session.post(endpoint,json=request,
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},
                timeout=(5,60),allow_redirects=False)
        except requests.RequestException:
            return 'unknown','', '接口连接中断，正在按成片签名核实'
        finally:
            if self.http is None:session.close()
        if response.status_code in {401,403}:
            return 'failed','', '素材接口认证失败'
        try:data=response.json()
        except ValueError:return 'unknown','', '接口回执无法解析，正在核实正式素材'
        if not isinstance(data,dict):return 'unknown','', '接口回执无效，正在核实正式素材'
        code=data.get('code')
        try:code=int(code)
        except (TypeError,ValueError):pass
        if code==403:
            return 'failed','', '素材接口限制了当前产品入库'
        if code==-1:
            message=str(data.get('message') or data.get('msg') or '')
            if any(word in message.lower() for word in ('已经存在','已存在','duplicate')):
                return 'unknown','', '接口提示素材已存在，正在按签名核实'
            return 'failed','', '素材接口拒绝了入库参数'
        if code==-2 or response.status_code>=500 or response.status_code<200 or response.status_code>=300:
            return 'unknown','', '接口处理结果不明确，正在核实正式素材'
        receipt=data.get('data',{}).get('id') if isinstance(data.get('data'),dict) else None
        if not _positive(receipt):return 'unknown','', '接口未返回有效回执 ID，正在核实正式素材'
        return 'accepted',str(receipt),''

    def reconcile(self,row):
        data=row['data'];request=data['request']
        rows=self._catalog().formal(request['content_sign'])
        if not rows:
            if row['status']=='verifying' and now()-int(data.get('submitted_at',now()))>1800:
                return self.change(row,'unknown_outcome',error='接口已接收，但正式素材超过 30 分钟尚未出现')
            return row
        if len(rows)!=1:
            return self.change(row,'unknown_outcome',error='正式素材签名对应多条记录，需要人工核实')
        formal=rows[0]
        expected={'product':PRODUCT_NAME,'type':2,'data_source_id':request['content_id'],
                  'content_sign':request['content_sign'],'category':CATEGORY,'url':request['url'],
                  'name':request['name'],'user_id':request['user_id'],'initiator':request['initiator'],'is_delete':0}
        mismatch=[field for field,value in expected.items() if str(formal.get(field))!=str(value)]
        if _language(formal.get('language'))!=request['language']:mismatch.append('language')
        if mismatch:
            return self.change(row,'unknown_outcome',error='正式素材字段不匹配：'+','.join(mismatch),
                               material_id=str(formal.get('id') or ''))
        if not _positive(data.get('receipt_id')):
            return self.change(row,'unknown_outcome',error='已找到正式素材，但缺少有效接口回执 ID',
                               material_id=str(formal.get('id') or ''))
        require(_positive(formal.get('id')),'material_formal_id_invalid','正式素材 ID 无效',503)
        receipts=self._catalog().receipt(data['receipt_id'])
        if len(receipts)!=1 or str(receipts[0].get('source_id'))!=str(formal['id']) or str(receipts[0].get('source_type'))!='3':
            return self.change(row,'unknown_outcome',error='接口回执未关联当前正式素材',
                               material_id=str(formal['id']))
        return self.change(row,'succeeded',error='',material_id=str(formal['id']))

    def change(self,row,status,**values):
        with self.store.transaction() as db:
            current=self.store.get('material_upload',row['id'],db)
            if current is None:return row
            data=current['data'];data.update(values)
            self.store.update('material_upload',row['id'],{'status':status,'data':data},db)
            return self.store.get('material_upload',row['id'],db)


class MaterialSourceWorker:
    """One bounded item per tick; unknown writes are readback-only forever."""
    def __init__(self,material):
        self.material=material;self.store=material.store
        self.holder='material_'+hashlib.sha256(os.urandom(32)).hexdigest()[:24]

    def tick(self):
        if not self.store.lease(self.holder,ttl=120,name='material_source'):return False
        with closing(self.store.connect()) as db:
            values=','.join('?' for _ in ACTIVE)
            row=db.execute('SELECT * FROM ve_material_upload WHERE status IN ('+values+
                ") AND json_extract(data,'$.next_check_at')<=? ORDER BY created_at,output_id LIMIT 1",
                (*sorted(ACTIVE),now())).fetchone()
        if not row:return False
        row=self.store.row(row)
        stop=threading.Event()
        def heartbeat():
            while not stop.wait(30):
                try:self.store.lease(self.holder,ttl=120,name='material_source')
                except Exception:logging.warning('material source lease heartbeat failed')
        thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
        try:self.process(row)
        finally:stop.set();thread.join(timeout=1)
        return True

    def process(self,row):
        m=self.material;status=row['status']
        if status=='submitting':
            row=m.change(row,'unknown_outcome',error='接口提交中断，正在按签名核实',next_check_at=now()+60)
            status='unknown_outcome'
        if status in {'unknown_outcome','verifying'}:
            try:
                checked=m.reconcile(row)
                if checked['status'] in {'unknown_outcome','verifying'}:
                    m.change(checked,checked['status'],next_check_at=now()+60)
            except Exception:
                logging.warning('material source formal readback unavailable for %s',row['id'])
                m.change(row,status,next_check_at=now()+60)
            return
        if status=='queued':row=m.change(row,'copying',error='')
        try:m.ensure_public_copy(row)
        except VideoEditingError as exc:
            if exc.status in {400,409}:
                m.change(row,'failed',error=exc.message);return
            self.retry_copy(row,exc.message);return
        except Exception:
            logging.warning('material source COS copy uncertain for %s',row['id'])
            self.retry_copy(row,'COS 复制结果待核查');return
        try:
            prior=m._catalog().formal(row['data']['request']['content_sign'])
        except Exception:
            m.change(row,'copying',error='正式素材预核查暂不可用',next_check_at=now()+60)
            return
        if prior:
            m.change(row,'unknown_outcome',error='正式素材已存在，正在核实原回执',next_check_at=now()+60)
            return
        row=m.change(row,'submitting',error='')
        # Persist the outbound boundary before opening the HTTP connection.
        with self.store.transaction() as db:
            current=self.store.get('material_upload',row['id'],db)
            if current['status']!='submitting' or current['data'].get('attempts'):
                return
            data=current['data'];data['attempts']=1;data['submitted_at']=now()
            data['next_check_at']=now()+60
            self.store.update('material_upload',row['id'],{'status':'unknown_outcome','data':data},db)
        try:result,receipt,error=m.post(row['data']['request'])
        except Exception:
            logging.warning('material source API outcome uncertain for %s',row['id'])
            result,receipt,error='unknown','','接口结果待核查'
        current=self.store.get('material_upload',row['id'])
        if result=='accepted':
            current=m.change(current,'verifying',receipt_id=receipt,error='',next_check_at=0)
            try:m.reconcile(current)
            except Exception:logging.warning('material source formal readback unavailable for %s',row['id'])
        elif result=='failed':m.change(current,'failed',error=error)
        else:m.change(current,'unknown_outcome',error=error,next_check_at=now()+60)

    def retry_copy(self,row,message):
        tries=int(row['data'].get('copy_attempts',0))+1
        self.material.change(row,'failed' if tries>=3 else 'copying',copy_attempts=tries,
                             error=message,next_check_at=now()+60)
