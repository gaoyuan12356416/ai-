"""Fixed, bounded X catalog reads using the existing exact-material policy."""
import os
import json
import re
import time
from collections import defaultdict
from urllib.parse import urlsplit
from features.x_accounts.language import canonical_drama_language
from features.x_posts.selector import (DramawaveCandidateSelector, PoolCandidateRejection,
                                      previous_source_date, normalize_material_url)
from features.x_posts.source_duration import probe_source_metadata_duration
from features.x_posts.service import XPostError
from .errors import PublishingError
from .transport import broker

LANGUAGES = [('en','English'),('es','Español'),('pt-br','Português (Brasil)'),('fr','Français'),
             ('de','Deutsch'),('ru','Русский'),('ja','日本語'),('ko','한국어'),('ar','العربية'),
             ('th','ไทย'),('id','Bahasa Indonesia'),('vi','Tiếng Việt'),('zh','中文'),
             ('tr','Türkçe'),('it','Italiano'),('ms','Bahasa Melayu'),('fil','Filipino')]
DEFAULT_PRODUCT = 'Drama-社媒专用素材'
CATALOG_PRODUCTS = (DEFAULT_PRODUCT, 'Dramawave')

def ids(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 200
            or any(not isinstance(x, str) or not re.fullmatch(r'[1-9][0-9]{0,30}', x) for x in values)):
        raise PublishingError('invalid_material_ids', '素材 ID 必须为 1 至 200 个十进制字符串', 400)
    return list(dict.fromkeys(values))

def safe_url(value):
    try:
        value = str(value or '')
        parsed = urlsplit(value)
        if (len(value) > 4096 or parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in {None,443} or any(ord(x) < 32 for x in value)):
            return ''
        return value
    except ValueError:
        return ''

class CachedSelector(DramawaveCandidateSelector):
    def __init__(self, raw, dramas, deploy, *, probe_deadline=None):
        super().__init__(None)
        self.raw, self.dramas, self.deploy = raw, dramas, deploy
        self.probe_deadline = probe_deadline

    def _pool_material_rows(self, material_id, **kwargs):
        row = self.raw.get(material_id)
        if not row:
            return []
        # Browsing never probes a page of media. Explicit selections may read
        # bounded MP4 metadata, but never trigger the legacy full-download fallback.
        try:
            if float(row.get('video_duration') or 0) <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            if self.probe_deadline is None:
                raise PoolCandidateRejection('material_duration_missing', '素材未记录时长，请输入完整素材 ID 查询并读取视频时长')
            try:
                duration = probe_source_metadata_duration(
                    normalize_material_url(str(row.get('material_url') or '')),
                    deadline=self.probe_deadline)
            except (XPostError, ValueError, OSError):
                raise PoolCandidateRejection('material_duration_missing', '素材已找到，但暂未读到视频时长，请稍后按完整素材 ID 重试') from None
            row = {**row, 'video_duration': duration}
        return [row]

    def _pool_drama_rows(self, content_id, language=None):
        rows = self.dramas.get(str(content_id), [])
        return rows if language is None else [r for r in rows if str(r.get('language','')).lower() == language.lower()]

    def _drama_deploy_rows(self, content_id, language, *, canonical_language=False):
        def match(row):
            try:
                return canonical_drama_language(row.get('language'),default='') == canonical_drama_language(language,default='')
            except ValueError:
                return False
        return [r for r in self.deploy.get(str(content_id), []) if match(r)]

class SourceRepository:
    def __init__(self, mysql):
        self.mysql = mysql
        if not re.fullmatch(r'[A-Za-z0-9_]+', mysql.schema):
            raise PublishingError('source_not_configured','素材库配置无效',503)
        self.schema = mysql.schema

    @staticmethod
    def _projection():
        return '''CAST(s.id AS CHAR) material_id,s.product,s.type material_type,
            s.is_delete material_is_deleted,s.url material_url,s.name material_name,s.language material_language,
            TRIM(s.data_source_id) content_id,s.tag_name source_tag_name,s.video_duration,s.cover thumbnail_url'''

    def resolve(self, material_ids):
        material_ids = ids(material_ids)
        marks = ','.join('%s' for _ in material_ids)
        rows, mappings = self._read_exact_bundle(f's.id IN ({marks})', material_ids)
        return self._resolve_rows(material_ids, rows, recover_duration=True, mappings=mappings)

    def _read_exact_bundle(self, where, params):
        """Fetch source + both mapping sets in one gated connection/statement.

        Each bounded branch keeps its own rows. Joining resource and deployment
        rows together would multiply duplicates and hide the original limits.
        Only exact-ID callers may use this path; broad catalog scans stay separate.
        """
        contents = f'''SELECT DISTINCT TRIM(s.data_source_id) content_id
            FROM `{self.schema}`.ads_custom_source s WHERE {where}
            AND TRIM(s.data_source_id)<>'' LIMIT 200'''
        records = self.mysql.select(f'''SELECT bundle.row_kind,bundle.row_json FROM (
            (SELECT 'material' row_kind,JSON_OBJECT(
                'material_id',CAST(s.id AS CHAR),'product',s.product,'material_type',s.type,
                'material_is_deleted',s.is_delete,'material_url',s.url,'material_name',s.name,
                'material_language',s.language,'content_id',TRIM(s.data_source_id),
                'source_tag_name',s.tag_name,'video_duration',s.video_duration,
                'thumbnail_url',s.cover) row_json
             FROM `{self.schema}`.ads_custom_source s WHERE {where} LIMIT 200)
            UNION ALL
            (SELECT 'drama' row_kind,JSON_OBJECT(
                'content_id',r.content_id,'series_code',r.series_code,'language',r.language,
                'drama_name',r.name,'drama_labels',r.labels,'drama_description',r.desc) row_json
             FROM ({contents}) selected_contents
             STRAIGHT_JOIN `{self.schema}`.ads_drama_resource r FORCE INDEX(content_id)
                ON r.content_id=selected_contents.content_id
             WHERE r.app_id=1479 ORDER BY r.id LIMIT 4001)
            UNION ALL
            (SELECT 'deploy' row_kind,JSON_OBJECT(
                'content_id',i.content_id,'app_id',i.app_id,'app',i.app,
                'language',i.language,'deploy_time',i.deploy_time) row_json
             FROM ({contents}) selected_contents
             STRAIGHT_JOIN `{self.schema}`.ads_drama_info i FORCE INDEX(ac)
                ON i.content_id=selected_contents.content_id
             WHERE i.app_id=1479 ORDER BY i.app LIMIT 4001)
            ) bundle''', [*params,*params,*params])
        buckets = {'material': [], 'drama': [], 'deploy': []}
        try:
            for record in records:
                payload = json.loads(record['row_json'])
                if not isinstance(payload, dict):
                    raise ValueError()
                buckets[record['row_kind']].append(payload)
            if len(buckets['material']) > 200:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise PublishingError('source_result_invalid','素材数据格式无效，请稍后重试',503) from None
        return buckets['material'], (buckets['drama'], buckets['deploy'])

    def _resolve_rows(self, material_ids, rows, *, recover_duration=False, mappings=None):
        # Exact-ID reads supply both mapping sets from one SQL snapshot. Ordinary
        # browsing reuses its source rows and fetches the same bounded mappings.
        raw = {str(r['material_id']):r for r in rows}
        contents = list(dict.fromkeys(str(r.get('content_id') or '') for r in rows if r.get('content_id')))
        dramas, deploy = defaultdict(list), defaultdict(list)
        resource_rows, deployment_rows = mappings if mappings is not None else ([], [])
        if contents and mappings is None:
            marks = ','.join('%s' for _ in contents)
            resource_rows = self.mysql.select(f'''SELECT r.content_id,r.series_code,r.language,r.name drama_name,
                r.labels drama_labels,r.desc drama_description
                FROM `{self.schema}`.ads_drama_resource r FORCE INDEX(content_id)
                WHERE r.content_id IN ({marks}) AND r.app_id=1479
                ORDER BY r.id LIMIT 4001''', contents)
            deployment_rows = self.mysql.select(f'''SELECT i.content_id,i.app_id,i.app,i.language,i.deploy_time
                FROM `{self.schema}`.ads_drama_info i FORCE INDEX(ac) WHERE i.content_id IN ({marks}) AND i.app_id=1479
                ORDER BY i.app LIMIT 4001''', contents)
        if len(resource_rows) > 4000 or len(deployment_rows) > 4000:
            raise PublishingError('source_mapping_too_large','关联剧目或投放数据超出校验范围，请缩小素材选择后重试',503)
        for r in resource_rows:
            dramas[str(r['content_id'])].append(r)
        for r in deployment_rows:
            deploy[str(r['content_id'])].append(r)
        selector = CachedSelector(raw,dramas,deploy,
                                  probe_deadline=time.monotonic()+8 if recover_duration else None)
        result, errors = {}, {}
        for start in range(0,len(material_ids),50):
            selected, rejected = selector.select_manual(material_ids[start:start+50],previous_source_date(),limit=50)
            for r in rejected:
                errors[r['material_id']] = {'code':r['error_code'],'message':r['error_message']}
                if r['error_code'] == 'drama_mapping_ambiguous':
                    errors[r['material_id']]['message'] = '同一 Dramawave 剧目存在不同的剧名、剧集编号或简介，请核对源库剧目信息'
            for r in selected:
                mid = r['material_id']
                if r.get('media_kind') != 'video':
                    errors[mid] = {'code':'material_not_video','message':'该发布入口仅支持视频'}
                    continue
                # Request/page position is not a material fact. Pool admission
                # resolves the accepted subset again after skipping bad rows;
                # propagating this ordinal makes unchanged materials differ.
                material = {key:value for key,value in r.items() if key != 'manual_item_id'}
                result[mid] = {**material,'id':mid,'language':r['material_language'],
                    'duration_seconds':r['source_duration'],'drama_description':r['description'],
                    'thumbnail_url':safe_url(raw[mid].get('thumbnail_url')),
                    'preview_url':safe_url(r['material_url'])}
        return {'items':result,'errors':errors}

    def listing(self, query):
        q = str(query.get('q') or '').strip()
        if len(q)>80:
            raise PublishingError('invalid_query','搜索内容最多 80 字',400)
        product = str(query.get('product') or DEFAULT_PRODUCT).strip()
        if product not in CATALOG_PRODUCTS:
            raise PublishingError('invalid_query','请选择 Dramawave 或 Drama-社媒专用素材',400)
        try:
            limit = min(50,max(1,int(query.get('limit') or 30)))
            cursor = str(query.get('cursor') or '')
            if cursor and not re.fullmatch(r'[1-9][0-9]{0,30}',cursor):
                raise ValueError()
            language = canonical_drama_language(query['language'], default='') if query.get('language') else ''
        except (ValueError,TypeError):
            raise PublishingError('invalid_query','分页或语言筛选无效',400) from None
        clauses, params = ['s.type=2'], []
        exact_id = re.fullmatch(r'[0-9]+',q) is not None
        # Browse/name searches stay in one indexed product. Exact IDs retain
        # the existing cross-product manual publishing policy.
        if not exact_id:
            clauses.append('s.product=%s');params.append(product)
        if cursor:
            clauses.append('s.id<%s');params.append(cursor)
        source_from = f'`{self.schema}`.ads_custom_source s'
        if exact_id:
            clauses.append('s.id=%s');params.append(q)
        elif q:
            # A localized title identifies a series, not only that language's
            # content ID. Expand through the small, indexed drama-info catalog;
            # retain direct title matches when a series code is absent. Each
            # branch is bounded, and full per-content validation still follows.
            titles = self.mysql.select(f'''SELECT DISTINCT matched.content_id FROM (
                (SELECT DISTINCT r.content_id
                 FROM `{self.schema}`.ads_drama_resource r FORCE INDEX(name)
                 WHERE r.app_id=1479 AND r.name=%s LIMIT 101)
                UNION ALL
                (SELECT DISTINCT i.content_id
                 FROM (SELECT DISTINCT r.series_code
                       FROM `{self.schema}`.ads_drama_resource r FORCE INDEX(name)
                       WHERE r.app_id=1479 AND r.name=%s
                         AND TRIM(r.series_code) NOT IN ('','0') LIMIT 101) title_series
                 STRAIGHT_JOIN `{self.schema}`.ads_drama_info i FORCE INDEX(scoo)
                   ON i.series_code=title_series.series_code
                 WHERE i.app_id=1479 AND TRIM(i.content_id)<>'' LIMIT 101)
                ) matched LIMIT 101''', [q,q])
            if len(titles) > 100:
                raise PublishingError('source_mapping_too_large','同名剧目过多，请使用完整素材 ID 查询',503)
            if titles:
                content_ids = list(dict.fromkeys(str(r['content_id']) for r in titles))
                source_from = f'''(SELECT DISTINCT data_source FROM `{self.schema}`.ads_custom_source
                    FORCE INDEX(idx_source_type_source_id)) source_kinds
                    STRAIGHT_JOIN `{self.schema}`.ads_custom_source s FORCE INDEX(idx_source_type_source_id)
                    ON s.data_source <=> source_kinds.data_source'''
                clauses.append(f"s.data_source_id IN ({','.join('%s' for _ in content_ids)})")
                params.extend(content_ids)
            else:
                clauses.append(f'''(LOCATE(%s,s.name)>0 OR EXISTS(SELECT 1 FROM `{self.schema}`.ads_drama_resource r FORCE INDEX(content_id)
                    WHERE r.content_id=s.data_source_id AND r.app_id=1479 AND LOCATE(%s,r.name)>0))''');params.extend([q,q])
        if language:
            aliases = [language,'jp'] if language=='ja' else [language]
            # Every eligible result also needs a same-language deployment row.
            # Use its small indexed set for the prefilter; full resource identity
            # and language validation still runs below before returning a video.
            clauses.append(f'''EXISTS(SELECT 1 FROM `{self.schema}`.ads_drama_info i FORCE INDEX(ac)
                WHERE i.content_id=s.data_source_id AND i.app_id=1479 AND LOWER(REPLACE(TRIM(i.language),'_','-'))
                IN ({','.join('%s' for _ in aliases)}))''');params.extend(aliases)
        mappings = None
        if exact_id:
            raw, mappings = self._read_exact_bundle(' AND '.join(clauses), params)
        else:
            raw = self.mysql.select(f'''SELECT {self._projection()} FROM {source_from}
                WHERE {' AND '.join(clauses)} ORDER BY s.id DESC LIMIT %s''', [*params,limit+1])
        selected = [str(r['material_id']) for r in raw[:limit]]
        resolved = self._resolve_rows(selected,raw[:limit],recover_duration=exact_id,mappings=mappings) if selected else {'items':{},'errors':{}}
        items = [resolved['items'][mid] for mid in selected if mid in resolved['items']
                 and (not language or resolved['items'][mid]['language'] == language)]
        return {'items':items,'next_cursor':selected[-1] if len(raw)>limit else '',
                'empty_reason':next(iter(resolved['errors'].values()),{}).get('message','没有符合条件的视频') if not items else ''}

class SourceClient:
    def __init__(self, token):
        self.token = token
    def resolve(self, material_ids):
        return broker('source',{'action':'resolve','material_ids':ids(material_ids)},self.token)
    def listing(self, query):
        return broker('source',{'action':'list','query':query},self.token)

def for_main(app):
    from .runtime_source import GatedReadOnlyMySQL
    if str(app.get('ADMIN_MAPPING_MYSQL_HOST')) != '101.32.56.53' or str(app.get('ADMIN_MAPPING_MYSQL_PORT')) != '63350':
        raise PublishingError('source_not_configured','素材查询必须使用只读副本',503)
    env = {'X_VIDEO_PUBLISH_MYSQL_USER':app.get('ADMIN_MAPPING_MYSQL_USER',''),
           'X_VIDEO_PUBLISH_MYSQL_PASSWORD':app.get('ADMIN_MAPPING_MYSQL_PASSWORD',''),
           'X_VIDEO_PUBLISH_MYSQL_DATABASE':'kunlunads_dev'}
    return SourceRepository(GatedReadOnlyMySQL(env))
