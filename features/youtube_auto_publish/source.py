"""Replaceable, server-owned SELECT contract. Unconfigured means no material."""
import json
from features.publishing_products import is_mobo_product
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from .templates import WorkflowError, has_link_source, long_url
from .cache import ReadCache
from .direct_media import is_tianmai_video


def fill_direct_languages(items, query_runner):
    # Resolve only the bounded cards, after the indexed uploader/ID lookup.
    by_content = {}
    for item in items:
        if is_tianmai_video(item['url']) and item['content_id'] and not item['language']:
            by_content.setdefault(item['content_id'], []).append(item)
    if not by_content:
        return
    literals = ','.join("CONVERT(0x%s USING utf8mb4) COLLATE utf8mb4_unicode_ci" %
                        key.encode('utf-8').hex() for key in by_content)
    sql = ("SELECT HEX(MIN(content_id)),HEX(MIN(language)) FROM kunlunads_dev.ads_drama_resource "
           "FORCE INDEX (content_id) WHERE app_id=1479 AND content_id IN (" + literals + ") "
           "GROUP BY BINARY content_id HAVING COUNT(DISTINCT language)=1")
    for key, language in query_runner(sql):
        for item in by_content.get(bytes.fromhex(key).decode(), []):
            item['language'] = bytes.fromhex(language).decode()

COLUMNS = ('id','name','url','thumbnail_url','content_id','source_job_id','source_kind','macro_name','macro_desc','language','duration','size','app_id','uploader_id','uploader_name')
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
    def __init__(self, sql_file, query_runner, *, allowed_hosts=DEFAULT_HOSTS, drama_resolver=None, async_cache=False, uploader_ids_provider=None):
        self.sql_file, self.query_runner = str(sql_file or ''), query_runner
        self.allowed_hosts = tuple(allowed_hosts)
        self.drama_resolver = drama_resolver
        self.uploader_ids_provider = uploader_ids_provider
        self.lock = threading.Lock()
        self.cache = None
        self.read_cache = ReadCache() if async_cache else None
        # Options and material reads share the existing two-worker budget.
        self.uploader_cache = self.read_cache
        self.uploader_snapshot = None

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

    def _read(self, sql, material_id='', search='', uploader_id='', moboreels=False, compact=False):
        projection = ','.join("'%s',COALESCE(CAST(pool.`%s` AS CHAR),'')" % (key,key) for key in COLUMNS)
        condition = "1=1"
        if material_id:
            if re.fullmatch(r'[1-9][0-9]{0,18}', material_id) is None:
                raise WorkflowError('invalid_material_id','素材标识无效')
            condition += " AND pool.id='%s'" % material_id
        if re.fullmatch(r'[1-9][0-9]{0,18}', search):
            condition += " AND pool.id='%s'" % search
        elif search:
            literal=str(search).encode('utf-8').hex()
            condition += " AND LOCATE(LOWER(CONVERT(0x%s USING utf8mb4)),LOWER(CONCAT(CAST(pool.id AS CHAR),' ',COALESCE(pool.name,''))))>0" % literal
        if uploader_id:
            # Keep the source's numeric user_id visible to its existing index.
            # Casting this column scans the catalog for sparse uploaders.
            condition += (" AND (pool.uploader_id=0 OR pool.uploader_id IS NULL)" if uploader_id == '0'
                          else " AND pool.uploader_id=%s" % uploader_id)
        query = "SELECT HEX(JSON_OBJECT(%s)) FROM (%s) AS pool WHERE %s ORDER BY pool.id DESC LIMIT 100"
        try:
            rows = [(False, row) for row in self.query_runner(query % (projection, sql, condition))]
            direct_path = Path(self.sql_file).with_suffix('.direct.sql') if self.sql_file else None
            if direct_path and direct_path.is_file():
                direct_sql = validate_sql(direct_path.read_text(encoding='utf-8-sig'))
                visibility = "(pool.intake_deleted=0 AND pool.receipt='')"
                if material_id or re.fullmatch(r'[1-9][0-9]{0,18}', search):
                    # An already selected original ID stays valid after the external importer
                    # retires it, only when its receipt proves the exact live successor.
                    visibility = "(" + visibility + " OR (pool.intake_deleted=1 AND pool.receipt_id IS NOT NULL))"
                direct_query = query % (projection, direct_sql, condition + " AND " + visibility)
                rows.extend((True, row) for row in self.query_runner(direct_query))
            items = []
            for direct, row in rows:
                item = json.loads(bytes.fromhex(row[0]).decode('utf-8'))
                if not isinstance(item,dict) or re.fullmatch(r'[1-9][0-9]{0,18}',str(item.get('id',''))) is None:
                    continue
                url = item.get('url')
                if direct and not is_tianmai_video(url):
                    continue
                if not direct and (not safe_url(url, self.allowed_hosts) or is_tianmai_video(url)):
                    continue
                item = {key:str(item.get(key) or '') for key in COLUMNS}
                if not safe_url(item['thumbnail_url'],self.allowed_hosts): item['thumbnail_url']=''
                item['macro_name'] = item['macro_name'] or item['name']
                item['macro_url'] = ''
                items.append(item)
            ids=[item['id'] for item in items]
            if len(ids)!=len(set(ids)):
                raise WorkflowError('material_source_ambiguous','素材查询存在重复 ID，请管理员调整 SQL',503)
            items.sort(key=lambda item: int(item['id']), reverse=True)
            items = items[:100]
            fill_direct_languages(items, self.query_runner)
            if compact:
                # Cards need only video fields. Resolve drama/link/cover metadata
                # for the selected ID through the full read before applying it.
                for item in items:
                    item.update(metadata_pending=True, macro_name='', macro_desc='',
                                long_url='', link_ready=False)
                return items
            mobo_items = [item for item in items if moboreels or is_mobo_product("", item.get("app_id"))]
            if mobo_items:
                from features.moboreels_library.macro_client import lookup_story, MoboreelsLinkError
                from .reference import normalize_reference_url
                for item in mobo_items:
                    try:
                        drama = lookup_story(item['content_id'])
                    except MoboreelsLinkError as exc:
                        raise WorkflowError('moboreels_drama_unavailable', str(exc), 409) from None
                    item.update(promotion_provider='moboreels', moboreels_story_id=item['content_id'],
                                macro_name=drama.get('story_name') or item['name'], macro_desc=drama.get('introduce') or '',
                                drama_status='moboreels', drama_message='',
                                drama_cover_url=normalize_reference_url(drama.get('img_src')))
                    item['drama_cover_status'] = 'available' if item['drama_cover_url'] else 'missing'
                    item['drama_cover_message'] = '' if item['drama_cover_url'] else 'Moboreels 原剧封面暂不可用，请选择本地上传'
            regular = [item for item in items if not moboreels and not is_mobo_product("", item.get("app_id"))]
            if regular and self.drama_resolver:
                resolved = {item["id"]: item for item in self.drama_resolver(regular)}
                items = [resolved.get(item["id"], item) for item in items]
            for item in items:
                item['long_url'] = long_url(item)
                item['link_ready'] = has_link_source(item)
            return items
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError('material_query_failed','素材查询失败，请联系管理员检查筛选配置',503) from None

    def _read_uploaders(self, sql):
        # Read the entire configured pool, independently of the first 100 videos
        # and the current keyword/uploader filter. Never expose unrelated users.
        query = ("SELECT HEX(JSON_OBJECT('id',CAST(pool.uploader_id AS CHAR),'name',MAX(pool.uploader_name))) "
                 "FROM (%s) AS pool "
                 "GROUP BY pool.uploader_id ORDER BY MAX(pool.uploader_name),pool.uploader_id" % sql)
        try:
            # Large pools can provide an equivalent indexed options SELECT next
            # to the source SQL, without grouping every material and metadata join.
            options_path = Path(self.sql_file).with_suffix('.uploaders.sql') if self.sql_file else None
            if options_path is not None and options_path.is_file():
                if options_path.stat().st_size > 32768:
                    raise ValueError('invalid uploader SQL file')
                query = validate_sql(options_path.read_text(encoding='utf-8-sig'))
            options = []
            for row in self.query_runner(query):
                item = json.loads(bytes.fromhex(row[0]).decode('utf-8'))
                value = str(item.get('id') or '0')
                if re.fullmatch(r'0|[1-9][0-9]{0,18}', value):
                    name = str(item.get('name') or '').strip() or ('未知上传人' if value == '0' else '用户 ' + value)
                    options.append({'id': value, 'name': name})
            if self.uploader_ids_provider is not None:
                allowed = self.uploader_ids_provider()
                options = [item for item in options if item['id'] in allowed]
            return options
        except Exception:
            raise WorkflowError('material_query_failed','上传人查询失败，请稍后刷新',503) from None

    def _list_snapshot(self, sql, search, uploader_id, compact):
        return {'items': self._read(sql, search=search, uploader_id=uploader_id, compact=compact)}

    def list(self, search='', *, refresh=False, uploader_id='', compact=False):
        uploader_id = str(uploader_id or '').strip()
        if uploader_id and re.fullmatch(r'0|[1-9][0-9]{0,18}', uploader_id) is None:
            raise WorkflowError('invalid_uploader_id','上传人筛选条件无效')
        sql, state = self.configuration()
        if not sql: return dict(state,items=[],uploaders=[])
        search=str(search or '').strip().casefold()[:200]
        cache_key=(sql,search,uploader_id,compact)
        if self.read_cache is not None:
            uploaders, uploader_meta = self.uploader_cache.read(sql, lambda: self._read_uploaders(sql), refresh=refresh)
            snapshot, metadata = self.read_cache.read(cache_key, lambda: self._list_snapshot(sql,search,uploader_id,compact), refresh=refresh)
            metadata['refreshing'] = metadata['refreshing'] or uploader_meta['refreshing']
            metadata['error'] = metadata['error'] or uploader_meta['error']
            return dict(state,uploaders=uploaders,cache=metadata,**(snapshot or {'items': []}))
        with self.lock:
            if refresh or not self.uploader_snapshot or self.uploader_snapshot[0]!=sql or time.monotonic()-self.uploader_snapshot[1]>300:
                self.uploader_snapshot=(sql,time.monotonic(),self._read_uploaders(sql))
            uploaders=[dict(row) for row in self.uploader_snapshot[2]]
            if refresh or not self.cache or self.cache[0]!=cache_key or time.monotonic()-self.cache[1]>20:
                self.cache=(cache_key,time.monotonic(),self._read(sql,search=search,uploader_id=uploader_id,compact=compact))
            rows=[dict(row) for row in self.cache[2]]
        return dict(state,items=rows,uploaders=uploaders)

    def get(self, material_id, *, moboreels=False):
        sql, state = self.configuration()
        if not sql:
            raise WorkflowError('material_source_unconfigured',state['message'],409)
        rows=self._read(sql,str(material_id),moboreels=moboreels)
        if len(rows)!=1:
            raise WorkflowError('material_unavailable','素材不在当前可发布范围内，请重新选择',409)
        return rows[0]
