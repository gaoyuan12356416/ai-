"""Read-only adapter for the existing product/drama catalog."""
import hashlib
import ipaddress
import os
import re
import signal
import socket
import subprocess
import types
from urllib.parse import urlsplit
import requests

from . import VideoEditingError, require
from .store import owner


class Catalog:
    def __init__(self,store,app):
        self.store,self.app=store,app

    def products(self):
        return self._bounded_app_function('list_products')()

    def _bounded_app_function(self,name):
        function=getattr(self.app,name)
        if not isinstance(function,types.FunctionType) or not hasattr(self.app,'MYSQL_BASE_CMD'):
            return function
        # Reuse the live catalog's variant selection without mutating app
        # globals shared with concurrent production requests.
        scope=dict(function.__globals__);scope['run_mysql']=self._query
        for key in ('pick_drama_variant',name):
            original=scope.get(key)
            if isinstance(original,types.FunctionType):
                bounded=types.FunctionType(original.__code__,scope,original.__name__,original.__defaults__,original.__closure__)
                bounded.__kwdefaults__=original.__kwdefaults__
                scope[key]=bounded
        return scope[name]

    def _query(self,sql):
        if not hasattr(self.app,'MYSQL_BASE_CMD'):
            return self.app.run_mysql(sql)
        command=list(self.app.MYSQL_BASE_CMD)
        require(command and '63353' not in command,'catalog_read_endpoint_invalid','目录查询端点无效',503)
        env=os.environ.copy()
        if self.app.MYSQL_PASSWORD:env['MYSQL_PWD']=self.app.MYSQL_PASSWORD
        env['MYSQL_MAX_EXECUTION_TIME_MS']='5000'
        command=[command[0],'--connect-timeout=3',*command[1:],'SET SESSION MAX_EXECUTION_TIME=5000; '+sql]
        process=None
        try:
            # Keep the configured host-wide SQL gate; a process group lets a
            # timeout close the gate and its mysql child, not leave an orphan.
            process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env,
                                     start_new_session=os.name!='nt')
            stdout,_=process.communicate(timeout=15)
            require(process.returncode==0,'catalog_query_failed','目录查询未完成，请缩短条件或使用剧目编号',503)
            return [line.split('\t') for line in stdout.splitlines() if line.strip()]
        except subprocess.TimeoutExpired:
            if os.name!='nt':os.killpg(process.pid,signal.SIGKILL)
            else:process.kill()
            process.communicate()
            raise VideoEditingError('catalog_query_timeout','目录查询超时，请使用完整剧目编号或更长的剧名前缀',503) from None
        except VideoEditingError:raise
        except Exception:
            raise VideoEditingError('catalog_query_failed','目录查询暂不可用，请稍后重试',503) from None

    def require_product(self,app_id):
        require(str(app_id) in {str(p.get('app_id','')) for p in self.products()},'product_not_available','产品不在当前可用目录中',403)

    def dramas(self,app_id,query):
        app_id=str(app_id).strip(); query=str(query).strip()
        require(re.fullmatch(r'[A-Za-z0-9._-]{1,128}',app_id),'product_required','请选择产品')
        require(0<len(query)<=100,'query_required','请输入完整剧目编号或至少两个字的剧名前缀')
        numeric=bool(re.fullmatch(r'[0-9]{1,32}',query))
        identifier_query=bool(re.fullmatch(r'[A-Za-z0-9_-]{1,100}',query))
        require(numeric or len(query)>=2,'query_required','剧名前缀至少输入两个字')
        self.require_product(app_id)
        db=str(self.app.DB_NAME); table=str(self.app.SOURCE_TABLE)
        require(re.fullmatch(r'[A-Za-z0-9_]+',db) and re.fullmatch(r'[A-Za-z0-9_]+',table),'catalog_configuration_invalid','剧集目录配置无效',503)
        quote=lambda value: "'"+str(value).replace('\\','\\\\').replace("'","''")+"'"
        where='app_id='+quote(app_id)+" AND type=2 AND sub_number>0 AND sub_url<>''"
        def search(index,condition):
            sql='SELECT /*+ MAX_EXECUTION_TIME(5000) */ CAST(content_id AS CHAR),name,all_episodes_count,cover FROM `'+db+'`.`'+table+'` FORCE INDEX (`'+index+'`) WHERE '+where+condition+' LIMIT 500'
            return self._query(sql)
        # Existing drama IDs are commonly ten mixed-case alphanumeric chars.
        # Try the identifier index first, before interpreting a token as a title.
        rows=search('content_id',' AND content_id='+quote(query)) if identifier_query else []
        if not rows and not numeric:
            rows=search('name',' AND name LIKE '+quote(query.replace('%','\\%').replace('_','\\_')+'%'))
        items=[];seen=set()
        for row in rows:
            if str(row[0]) in seen:continue
            seen.add(str(row[0]));items.append({'app_id':app_id,'content_id':str(row[0]),'title':str(row[1]),'episode_count':int(row[2]),'cover_url':str(row[3])})
            if len(items)>=50:break
        return items

    def episodes(self,app_id,content_id,actor):
        require(re.fullmatch(r'[A-Za-z0-9._-]{1,128}',str(app_id)) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(content_id)),
                'drama_invalid','剧目编号无效')
        self.require_product(app_id)
        rows=self._bounded_app_function('fetch_drama_episodes')(str(content_id),app_id=str(app_id))
        result=[]
        with self.store.transaction() as db:
            for item in rows:
                url=str(item['episode_url']); parsed=urlsplit(url)
                require(parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password,'source_url_invalid','剧集地址不受支持',409)
                # A changed catalog URL gets a new immutable source identity.
                identity='\n'.join((str(app_id),str(content_id),str(item['episode_number']),url))
                sid='vep_'+hashlib.sha256(identity.encode()).hexdigest()[:32]
                row=self.store.get('source',sid,db)
                if row is None:
                    self.store.insert('source',{'id':sid,'kind':'episode','owner':owner(actor),
                        'name':str(item.get('drama_name',''))+' 第'+str(item['episode_number'])+'集','status':'unprobed',
                        'data':{'app_id':str(app_id),'content_id':str(content_id),'episode_number':int(item['episode_number']),
                                'title':str(item.get('drama_name','')),'url':url,'identity':{},'metadata':{}}},db)
                    row=self.store.get('source',sid,db)
                result.append(row)
        return result


def probe_identity(url):
    """Bounded HEAD only for a catalog-owned HTTPS URL; never download on CPU."""
    p=urlsplit(url)
    require(p.scheme=='https' and p.hostname and not p.username and not p.password and p.port in (None,443),
            'source_url_invalid','剧集地址无效',409)
    try:
        addresses=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
        require(addresses and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses),'source_host_invalid','剧集资源地址不可访问',409)
        with requests.Session() as session:
            session.trust_env=False
            response=session.head(url,allow_redirects=False,timeout=(5,20))
        require(response.status_code==200,'source_head_failed','剧集资源暂不可验证',503)
        size=int(response.headers.get('content-length','0')); etag=response.headers.get('etag','').strip('"')
        require(size>0 and etag,'source_identity_missing','剧集资源缺少稳定大小或 ETag',409)
        return {'size_bytes':size,'etag':etag}
    except VideoEditingError:
        raise
    except Exception:
        raise VideoEditingError('source_head_failed','剧集资源暂不可验证',503) from None
