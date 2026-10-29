"""Read-only adapter for the existing product/drama catalog."""
import hashlib
import ipaddress
import json
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


RESOURCE_PLACEHOLDERS = {'', '0', '-', '资源id'}


def resource_code(value):
    """Resource identifiers are opaque catalog strings, not URL path parts."""
    require(isinstance(value, str), 'resource_required', '请选择短剧资源')
    value = value.strip()
    require(0 < len(value) <= 64 and value.casefold() not in RESOURCE_PLACEHOLDERS and
            not any(ord(c) < 32 or ord(c) == 127 for c in value),
            'resource_invalid', '短剧资源 ID 无效')
    return value


def _literal(value):
    # Keep the indexed column unmodified; use a matching connection collation.
    return 'CONVERT(0x' + str(value).encode('utf-8').hex() + ' USING utf8mb4) COLLATE utf8mb4_unicode_ci'


def _language(value):
    value = str(value or '').strip().lower().replace('_', '-')
    value = {'zh-hant': 'zh-tw', 'zh-hans': 'zh-cn', 'jp': 'ja', 'kr': 'ko', 'vn': 'vi', 'cz': 'cs'}.get(value, value)
    return value if value in {'zh-tw', 'zh-cn'} else value.split('-')[0]


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

    def _info_table(self):
        database = str(getattr(self.app, 'ADMIN_MAPPING_MYSQL_DATABASE', '') or self.app.DB_NAME)
        require(re.fullmatch(r'[A-Za-z0-9_]+', database), 'catalog_configuration_invalid', '资源目录配置无效', 503)
        return '`' + database + '`.`ads_drama_info`'

    def _info_rows(self, fields, where, *, index='scoo', limit=1001):
        columns = ','.join("'" + key + "'," + expression for key, expression in fields.items())
        rows = self._query('SELECT /*+ MAX_EXECUTION_TIME(5000) */ DISTINCT HEX(JSON_OBJECT(' + columns +
                           ')) FROM ' + self._info_table() + ' FORCE INDEX (`' + index + '`) WHERE ' +
                           where + ' LIMIT ' + str(limit))
        try:
            return [json.loads(bytes.fromhex(row[0]).decode('utf-8')) for row in rows]
        except (ValueError, TypeError, IndexError, UnicodeError):
            raise VideoEditingError('catalog_response_invalid', '资源目录返回内容无效，请稍后重试', 503) from None

    @staticmethod
    def _resource_summary(code, rows):
        titles = {str(row.get('english_title') or '').strip() for row in rows}
        titles.discard('')
        return {'series_code': code, 'english_title': next(iter(titles)) if len(titles) == 1 else '',
                'english_title_status': 'matched' if len(titles) == 1 else 'ambiguous' if titles else 'missing'}

    def _resource_rows(self, code):
        literal = _literal(code)
        # The first comparison uses scoo; the binary check preserves ID case.
        return self._info_rows({'series_code': 'series_code',
                               'english_title': "CASE WHEN LOWER(language)='en' THEN TRIM(name) ELSE '' END"},
                              'series_code=' + literal + ' AND BINARY series_code=BINARY ' + literal, limit=4)

    def resource(self, series_code):
        code = resource_code(series_code)
        rows = self._resource_rows(code)
        require(rows, 'resource_not_found', '未找到此短剧资源，请重新搜索选择', 404)
        return self._resource_summary(code, rows)

    def resources(self, query):
        require(isinstance(query, str), 'query_required', '请输入资源 ID 或英文剧名前缀')
        query = query.strip()
        require(0 < len(query) <= 100 and not any(ord(c) < 32 or ord(c) == 127 for c in query),
                'query_required', '请输入资源 ID 或英文剧名前缀（最多 100 字）')
        # Exact IDs take priority even when they contain punctuation or spaces.
        if len(query) <= 64 and query.casefold() not in RESOURCE_PLACEHOLDERS:
            rows = self._resource_rows(query)
            if rows:
                return [self._resource_summary(query, rows)]
        require(len(query) >= 2, 'query_required', '英文剧名前缀至少输入两个字')
        prefix = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        candidates = self._info_rows({'series_code': 'series_code'},
            "LOWER(language)='en' AND series_code NOT IN ('','0','-','资源ID') AND name LIKE " + _literal(prefix),
            index='name', limit=51)
        codes = []
        for row in candidates:
            code = str(row.get('series_code') or '').strip()
            if code and code not in codes:
                codes.append(resource_code(code))
            if len(codes) >= 50:
                break
        if not codes:
            return []
        # Resolve all English titles of each match, rather than hiding conflicts
        # when only one of a resource's English names matched the prefix.
        titles = self._info_rows({'series_code': 'series_code', 'english_title': 'TRIM(name)'},
            "LOWER(language)='en' AND series_code IN (" + ','.join(_literal(code) for code in codes) + ')')
        require(len(titles) <= 1000, 'catalog_result_too_large', '资源名称结果过多，请使用完整资源 ID', 503)
        return [self._resource_summary(code, [row for row in titles if row.get('series_code') == code]) for code in codes]

    def _variant_rows(self, where, *, index='scoo'):
        rows = self._info_rows({'app_id': 'CAST(app_id AS CHAR)', 'content_id': 'content_id',
                               'series_code': 'series_code', 'title': 'TRIM(name)',
                               'language': 'language', 'country': 'country'}, where, index=index)
        require(len(rows) <= 1000, 'catalog_result_too_large', '资源版本过多，请缩小选择范围', 503)
        result = []
        for row in rows:
            row = {key: str(value or '').strip() for key, value in row.items()}
            row['language'] = _language(row.get('language'))
            row['country'] = row.get('country', '').upper()
            if row not in result:
                result.append(row)
        return result

    def resource_variants(self, series_code):
        code = resource_code(series_code)
        literal = _literal(code)
        rows = self._variant_rows('series_code=' + literal + ' AND BINARY series_code=BINARY ' + literal)
        require(rows, 'resource_not_found', '未找到此短剧资源，请重新搜索选择', 404)
        groups = {}
        for row in rows:
            groups.setdefault((row.get('app_id'), row.get('content_id')), []).append(row)
        # A problematic language version does not hide the resource's other
        # valid versions. A direct selection is revalidated by variant().
        variants = [self._unique_variant(items) for items in groups.values()]
        return sorted((row for row in variants if row and row.get('app_id') and row.get('content_id')),
                      key=lambda row: (row['app_id'], row['language'], row['content_id']))

    @staticmethod
    def _unique_variant(rows):
        identities = {tuple(row.get(key, '') for key in ('app_id', 'content_id', 'series_code', 'language', 'country')) for row in rows}
        if len(identities) != 1:
            return None
        result = dict(rows[0])
        titles = {row.get('title', '') for row in rows}
        titles.discard('')
        result['title'] = next(iter(titles)) if len(titles) == 1 else ''
        result['title_status'] = 'matched' if len(titles) == 1 else 'ambiguous' if titles else 'missing'
        return result

    def variant(self, app_id, content_id, series_code=None):
        app_id, content_id = str(app_id).strip(), str(content_id).strip()
        require(re.fullmatch(r'[1-9][0-9]{0,18}', app_id) and 0 < len(content_id) <= 64 and
                not any(ord(c) < 32 or ord(c) == 127 for c in content_id), 'drama_invalid', '剧目版本无效')
        literal = _literal(content_id)
        rows = self._variant_rows('app_id=' + app_id + ' AND content_id=' + literal +
                                  ' AND BINARY content_id=BINARY ' + literal, index='ac')
        require(rows, 'drama_not_found', '未找到此剧目版本', 404)
        row = self._unique_variant(rows)
        require(row is not None, 'drama_metadata_ambiguous', '此剧目版本的资源或语言归属不唯一，请选择其他版本', 409)
        row['series_code'] = resource_code(row.get('series_code'))
        if series_code is not None:
            require(row['series_code'] == resource_code(series_code), 'drama_resource_mismatch', '所选剧目版本不属于当前资源', 409)
        return row

    def source_resource(self, app_id, content_id):
        app_id, content_id = str(app_id).strip(), str(content_id).strip()
        require(re.fullmatch(r'[1-9][0-9]{0,18}', app_id) and 0 < len(content_id) <= 64 and
                not any(ord(c) < 32 or ord(c) == 127 for c in content_id), 'drama_invalid', '剧目版本无效')
        literal = _literal(content_id)
        rows = self._info_rows({'series_code': 'series_code'}, 'app_id=' + app_id +
                              ' AND content_id=' + literal + ' AND BINARY content_id=BINARY ' + literal,
                              index='ac', limit=3)
        require(rows, 'drama_not_found', '未找到此剧目版本', 404)
        codes = {str(row.get('series_code') or '').strip() for row in rows}
        require(len(codes) == 1, 'drama_resource_ambiguous', '此剧目对应多个短剧资源，请核对资源归属', 409)
        return {'series_code': resource_code(next(iter(codes)))}

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
        resource = {}
        try:
            resource = self.source_resource(app_id, content_id)
            try:
                resource.update(self.resource(resource['series_code']))
            except VideoEditingError:
                pass
        except VideoEditingError:
            pass
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
                                'title':str(item.get('drama_name','')),'url':url,'identity':{},'metadata':{},
                                **({'series_code':resource['series_code'],'english_title':resource.get('english_title','')} if resource else {})}},db)
                    row=self.store.get('source',sid,db)
                elif resource:
                    data = dict(row['data'])
                    data.update(series_code=resource['series_code'], english_title=resource.get('english_title',''))
                    if data != row['data']:
                        self.store.update('source',sid,{'data':data},db)
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
