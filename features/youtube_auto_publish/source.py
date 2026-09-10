"""Replaceable, server-owned SELECT contract. Unconfigured means no material."""
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from .templates import WorkflowError, long_url

COLUMNS = ('id','name','url','thumbnail_url','content_id','source_job_id','source_kind','macro_name','macro_desc','language','duration','size','app_id')
DEFAULT_HOSTS = ('advertising-1306474899.cos.ap-hongkong.myqcloud.com','ai.yingliangads.com','gy.g2flow.com','socialkit-cdn.yingliang.tech')


def safe_url(value, hosts):
    text = str(value or '').strip()
    u = urlsplit(text)
    return bool(len(text) < 4096 and u.scheme == 'https' and u.hostname in hosts and not u.username and not u.password and not u.fragment and (u.port is None or u.port == 443))


def validate_sql(sql):
    text = str(sql).strip().rstrip(';').strip()
    if len(text) > 32768 or not re.match(r'^SELECT\s', text, re.I):
        raise WorkflowError('material_sql_invalid', '素材筛选 SQL 配置无效，请联系管理员', 503)
    # Only administrator-maintained SELECTs. No comments/session commands, output files or multiple statements.
    if re.search(r';|--|/\*|\*/|#|\b(?:INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|TRUNCATE|INTO|OUTFILE|DUMPFILE|LOAD_FILE|SLEEP|BENCHMARK|GET_LOCK|FOR\s+UPDATE|LOCK\s+IN)\b', text, re.I):
        raise WorkflowError('material_sql_invalid', '素材筛选 SQL 必须为单条只读 SELECT', 503)
    return text


class MaterialSource:
    def __init__(self, sql_file, query_runner, *, allowed_hosts=DEFAULT_HOSTS):
        self.sql_file, self.query_runner = str(sql_file or ''), query_runner
        self.allowed_hosts = tuple(allowed_hosts)
        self.lock = threading.Lock()
        self.cache = None

    def configuration(self):
        if not self.sql_file:
            return '', {'configured':False,'message':'素材筛选规则待配置，请等待管理员提供最终 SQL。'}
        try:
            path = Path(self.sql_file)
            if not path.is_file() or path.stat().st_size > 32768:
                raise ValueError('invalid file')
            text = path.read_text(encoding='utf-8-sig').strip()
        except (OSError, ValueError):
            raise WorkflowError('material_config_unavailable','素材筛选配置暂不可用，请联系管理员',503) from None
        # An empty file or a comments-only reserved file is intentionally unconfigured.
        if not text or all(not line.strip() or line.lstrip().startswith('--') for line in text.splitlines()):
            return '', {'configured':False,'message':'素材筛选规则待配置，请等待管理员提供最终 SQL。'}
        return validate_sql(text), {'configured':True,'message':''}

    def _read(self, sql, material_id='', search=''):
        projection = ','.join("'%s',COALESCE(CAST(pool.`%s` AS CHAR),'')" % (key,key) for key in COLUMNS)
        condition = "CAST(pool.app_id AS CHAR)='1479'"
        if material_id:
            if re.fullmatch(r'[1-9][0-9]{0,18}', material_id) is None:
                raise WorkflowError('invalid_material_id','素材标识无效')
            condition += " AND CAST(pool.id AS CHAR)='%s'" % material_id
        if search:
            literal=str(search).encode('utf-8').hex()
            condition += " AND LOCATE(LOWER(CONVERT(0x%s USING utf8mb4)),LOWER(CONCAT(CAST(pool.id AS CHAR),' ',COALESCE(pool.name,''))))>0" % literal
        query = "SELECT HEX(JSON_OBJECT(%s)) FROM (%s) AS pool WHERE %s LIMIT 100" % (projection,sql,condition)
        try:
            rows = self.query_runner(query)
            items = []
            for row in rows:
                item = json.loads(bytes.fromhex(row[0]).decode('utf-8'))
                if not isinstance(item,dict) or re.fullmatch(r'[1-9][0-9]{0,18}',str(item.get('id',''))) is None:
                    continue
                if not safe_url(item.get('url'), self.allowed_hosts):
                    continue
                item = {key:str(item.get(key) or '') for key in COLUMNS}
                if not safe_url(item['thumbnail_url'],self.allowed_hosts): item['thumbnail_url']=''
                item['macro_name'] = item['macro_name'] or item['name']
                item['macro_url'] = ''
                item['long_url'] = long_url(item)
                items.append(item)
            ids=[item['id'] for item in items]
            if len(ids)!=len(set(ids)):
                raise WorkflowError('material_source_ambiguous','素材查询存在重复 ID，请管理员调整 SQL',503)
            return items
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError('material_query_failed','素材查询失败，请联系管理员检查筛选配置',503) from None

    def list(self, search=''):
        sql, state = self.configuration()
        if not sql: return dict(state,items=[])
        search=str(search or '').strip().casefold()[:200]
        cache_key=(sql,search)
        with self.lock:
            if not self.cache or self.cache[0]!=cache_key or time.monotonic()-self.cache[1]>20:
                self.cache=(cache_key,time.monotonic(),self._read(sql,search=search))
            rows=[dict(row) for row in self.cache[2]]
        return dict(state,items=rows)

    def get(self, material_id):
        sql, state = self.configuration()
        if not sql:
            raise WorkflowError('material_source_unconfigured',state['message'],409)
        rows=self._read(sql,str(material_id))
        if len(rows)!=1:
            raise WorkflowError('material_unavailable','素材不在当前可发布范围内，请重新选择',409)
        return rows[0]
