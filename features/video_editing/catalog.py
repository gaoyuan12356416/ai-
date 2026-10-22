"""Read-only adapter for the existing product/drama catalog."""
import hashlib
import ipaddress
import re
import socket
from urllib.parse import urlsplit
import requests

from . import VideoEditingError, require
from .store import owner


class Catalog:
    def __init__(self,store,app):
        self.store,self.app=store,app

    def products(self):
        return self.app.list_products()

    def require_product(self,app_id):
        require(str(app_id) in {str(p.get('app_id','')) for p in self.products()},'product_not_available','产品不在当前可用目录中',403)

    def dramas(self,app_id,query):
        app_id=str(app_id).strip(); query=str(query).strip()
        require(re.fullmatch(r'[A-Za-z0-9._-]{1,128}',app_id),'product_required','请选择产品')
        self.require_product(app_id)
        require(len(query)<=100,'query_invalid','搜索内容过长')
        db=str(self.app.DB_NAME); table=str(self.app.SOURCE_TABLE)
        require(re.fullmatch(r'[A-Za-z0-9_]+',db) and re.fullmatch(r'[A-Za-z0-9_]+',table),'catalog_configuration_invalid','剧集目录配置无效',503)
        quote=lambda value: "'"+str(value).replace('\\','\\\\').replace("'","''")+"'"
        where='app_id='+quote(app_id)+" AND type=2 AND sub_number>0 AND sub_url<>''"
        if query:
            where+=' AND (CAST(content_id AS CHAR)='+quote(query)+' OR name LIKE '+quote('%'+query.replace('%','\\%').replace('_','\\_')+'%')+')'
        sql='SELECT CAST(content_id AS CHAR),MAX(name),COUNT(DISTINCT sub_number),MAX(cover) FROM `'+db+'`.`'+table+'` WHERE '+where+' GROUP BY content_id ORDER BY MAX(updated_at) DESC LIMIT 50'
        rows=self.app.run_mysql(sql)
        return [{'app_id':app_id,'content_id':str(r[0]),'title':str(r[1]),'episode_count':int(r[2]),'cover_url':str(r[3])} for r in rows]

    def episodes(self,app_id,content_id,actor):
        require(re.fullmatch(r'[A-Za-z0-9._-]{1,128}',str(app_id)) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(content_id)),
                'drama_invalid','剧目编号无效')
        self.require_product(app_id)
        rows=self.app.fetch_drama_episodes(str(content_id),app_id=str(app_id))
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
