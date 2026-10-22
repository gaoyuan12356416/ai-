"""Fixed, bounded X catalog reads using the existing exact-material policy."""
import os
import re
from collections import defaultdict
from urllib.parse import urlsplit
from features.x_accounts.language import canonical_drama_language
from features.x_posts.selector import (DramawaveCandidateSelector, PoolCandidateRejection,
                                      previous_source_date)
from .errors import PublishingError
from .transport import broker

LANGUAGES = [('en','English'),('es','Español'),('pt-br','Português (Brasil)'),('fr','Français'),
             ('de','Deutsch'),('ru','Русский'),('ja','日本語'),('ko','한국어'),('ar','العربية'),
             ('th','ไทย'),('id','Bahasa Indonesia'),('vi','Tiếng Việt'),('zh','中文'),
             ('tr','Türkçe'),('it','Italiano'),('ms','Bahasa Melayu'),('fil','Filipino')]

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
    def __init__(self, raw, dramas, deploy):
        super().__init__(None)
        self.raw, self.dramas, self.deploy = raw, dramas, deploy

    def _pool_material_rows(self, material_id, **kwargs):
        row = self.raw.get(material_id)
        if not row:
            return []
        # Metadata preview must not start an unbounded media download/probe.
        try:
            if float(row.get('video_duration') or 0) <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise PoolCandidateRejection('material_duration_missing', '素材时长缺失，请先完善素材信息')
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
        rows = self.mysql.select(f'''SELECT {self._projection()}
            FROM `{self.schema}`.ads_custom_source s WHERE s.id IN ({marks}) LIMIT 200''', material_ids)
        return self._resolve_rows(material_ids, rows)

    def _resolve_rows(self, material_ids, rows):
        # Listing already has these exact source rows: reuse its bounded read so
        # both resolve and search perform no more than three repository queries.
        raw = {str(r['material_id']):r for r in rows}
        contents = list(dict.fromkeys(str(r.get('content_id') or '') for r in rows if r.get('content_id')))
        dramas, deploy = defaultdict(list), defaultdict(list)
        if contents:
            marks = ','.join('%s' for _ in contents)
            resource_rows = self.mysql.select(f'''SELECT r.content_id,r.series_code,r.language,r.name drama_name,
                r.labels drama_labels,r.desc drama_description FROM `{self.schema}`.ads_drama_resource r
                WHERE r.content_id IN ({marks}) ORDER BY r.id LIMIT 4001''', contents)
            if len(resource_rows) > 4000:
                raise PublishingError('source_mapping_too_large','关联剧目数据超出校验范围，请缩小素材选择后重试',503)
            for r in resource_rows:
                dramas[str(r['content_id'])].append(r)
            deployment_rows = self.mysql.select(f'''SELECT i.content_id,i.app_id,i.app,i.language,i.deploy_time
                FROM `{self.schema}`.ads_drama_info i WHERE i.content_id IN ({marks}) AND i.app_id=1479
                ORDER BY i.app LIMIT 4001''', contents)
            if len(deployment_rows) > 4000:
                raise PublishingError('source_mapping_too_large','关联投放数据超出校验范围，请缩小素材选择后重试',503)
            for r in deployment_rows:
                deploy[str(r['content_id'])].append(r)
        selector = CachedSelector(raw,dramas,deploy)
        result, errors = {}, {}
        for start in range(0,len(material_ids),50):
            selected, rejected = selector.select_manual(material_ids[start:start+50],previous_source_date(),limit=50)
            for r in rejected:
                errors[r['material_id']] = {'code':r['error_code'],'message':r['error_message']}
            for r in selected:
                mid = r['material_id']
                if r.get('media_kind') != 'video':
                    errors[mid] = {'code':'material_not_video','message':'该发布入口仅支持视频'}
                    continue
                result[mid] = {**r,'id':mid,'language':r['material_language'],
                    'duration_seconds':r['source_duration'],'drama_description':r['description'],
                    'thumbnail_url':safe_url(raw[mid].get('thumbnail_url')),
                    'preview_url':safe_url(r['material_url'])}
        return {'items':result,'errors':errors}

    def listing(self, query):
        q = str(query.get('q') or '').strip()
        if len(q)>80:
            raise PublishingError('invalid_query','搜索内容最多 80 字',400)
        try:
            limit = min(50,max(1,int(query.get('limit') or 30)))
            cursor = str(query.get('cursor') or '')
            if cursor and not re.fullmatch(r'[1-9][0-9]{0,30}',cursor):
                raise ValueError()
            language = canonical_drama_language(query['language'], default='') if query.get('language') else ''
        except (ValueError,TypeError):
            raise PublishingError('invalid_query','分页或语言筛选无效',400) from None
        clauses, params = ['s.type=2'], []
        if cursor:
            clauses.append('s.id<%s');params.append(cursor)
        if re.fullmatch(r'[0-9]+',q):
            clauses.append('s.id=%s');params.append(q)
        elif q:
            clauses.append(f'''(LOCATE(%s,s.name)>0 OR EXISTS(SELECT 1 FROM `{self.schema}`.ads_drama_resource r
                WHERE r.content_id=s.data_source_id AND LOCATE(%s,r.name)>0))''');params.extend([q,q])
        if language:
            aliases = [language,'jp'] if language=='ja' else [language]
            clauses.append(f'''EXISTS(SELECT 1 FROM `{self.schema}`.ads_drama_resource r
                WHERE r.content_id=s.data_source_id AND LOWER(REPLACE(TRIM(r.language),'_','-'))
                IN ({','.join('%s' for _ in aliases)}))''');params.extend(aliases)
        raw = self.mysql.select(f'''SELECT {self._projection()} FROM `{self.schema}`.ads_custom_source s
            WHERE {' AND '.join(clauses)} ORDER BY s.id DESC LIMIT %s''', [*params,limit+1])
        selected = [str(r['material_id']) for r in raw[:limit]]
        resolved = self._resolve_rows(selected,raw[:limit]) if selected else {'items':{},'errors':{}}
        items = [resolved['items'][mid] for mid in selected if mid in resolved['items']]
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
