"""Owner-scoped manual promotional links, independent of video publication."""
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import quote, urlencode

from features.drama_synthesis.core import SHORT_BASE_URL, render_wrapper_html, utc_now
from .attribution import BASE_URL, mapped_user_id, text
from .templates import WorkflowError


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def operation(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
            raise ValueError()
        return value.lower()
    except (ValueError, AttributeError):
        raise WorkflowError('invalid_request', '生成操作标识无效，请重新打开弹窗', 400) from None


def selection(payload):
    channel = str(payload.get('channel_local_id') or '')
    content = str(payload.get('content_id') or '')
    language = str(payload.get('language') or '').strip().lower()
    if (not re.fullmatch(r'[1-9][0-9]{0,18}', channel)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', content)
            or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,31}', language)):
        raise WorkflowError('invalid_request', '请选择有效的频道、短剧和语言', 400)
    return {'channel_local_id': channel, 'content_id': content, 'language': language}


class DramaCatalog:
    """Indexed name-prefix / exact-content search; never scan the episode catalog on open."""
    def __init__(self, query_runner, schema='kunlunads_dev'):
        if not re.fullmatch(r'[A-Za-z0-9_]+', schema):
            raise ValueError('Invalid schema')
        self.query, self.table = query_runner, '`%s`.ads_drama_resource' % schema

    @staticmethod
    def literal(value):
        return 'CONVERT(0x%s USING utf8mb4) COLLATE utf8mb4_unicode_ci' % value.encode('utf-8').hex()

    def _rows(self, conditions):
        sources = ["SELECT content_id,LOWER(TRIM(language)) AS language,TRIM(name) AS name "
                   "FROM %s FORCE INDEX(%s) WHERE app_id=1479 AND type=2 AND %s "
                   "AND content_id<>'' AND TRIM(language)<>'' AND TRIM(name)<>''" % (self.table, index, condition)
                   for index, condition in conditions]
        return ' UNION ALL '.join(sources)

    def _read(self, sql):
        try:
            return [json.loads(bytes.fromhex(row[0]).decode('utf-8')) for row in self.query(sql)]
        except Exception:
            raise WorkflowError('drama_query_failed', '剧库查询暂不可用，请稍后重试或输入更完整的剧名', 503) from None

    def search(self, search='', page=1):
        search = str(search).strip()
        if len(search) > 200 or any(ord(c) < 32 for c in search):
            raise WorkflowError('invalid_request', '搜索内容无效', 400)
        if type(page) is not int or not 1 <= page <= 1000:
            raise WorkflowError('invalid_request', '页码无效', 400)
        if len(search) < 2:
            return {'items': [], 'page': page, 'has_more': False, 'search_required': True}
        prefix = search.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        sources = self._rows([('name', 'name LIKE %s ESCAPE 0x21' % self.literal(prefix)),
                              ('content_id', 'content_id=%s' % self.literal(search))])
        # Binary grouping keeps case-sensitive content IDs and distinct localized names.
        sql = ("SELECT HEX(JSON_OBJECT('content_id',MIN(content_id),'language',MIN(language),"
               "'name',MIN(name),'ambiguous',COUNT(DISTINCT BINARY name)>1)) FROM (%s) d "
               "GROUP BY BINARY content_id,BINARY language ORDER BY BINARY content_id,BINARY language "
               "LIMIT 51 OFFSET %d" % (sources, (page-1)*50))
        items = self._read(sql)
        for item in items:
            item['selectable'] = not bool(item.pop('ambiguous', False))
        return {'items': items[:50], 'page': page, 'has_more': len(items)>50, 'search_required': False}

    def resolve(self, content_id, language):
        sources = self._rows([('content_id', 'content_id=%s AND LOWER(TRIM(language))=%s' %
                              (self.literal(content_id), self.literal(language)))])
        rows = self._read("SELECT DISTINCT HEX(JSON_OBJECT('content_id',content_id,'language',language,'name',name)) "
                          "FROM (%s) d LIMIT 101" % sources)
        rows = [r for r in rows if r['content_id'] == content_id and r['language'] == language]
        names = {r['name'] for r in rows}
        if len(names) != 1 or len(rows) >= 101:
            raise WorkflowError('drama_unavailable', '短剧已移除或该语言的剧名存在冲突，请重新选择', 409)
        return {'content_id': content_id, 'language': language, 'name': next(iter(names))}


def build_manual_url(context, link_id, created_at):
    stamp = int(datetime.fromisoformat(created_at.replace('Z', '+00:00')).timestamp())
    campaign = 'yingliang_post_CLV_VL_youtube_%s*%snone%s*%s*none*manual_%s' % (
        context['channel_id'], stamp, context['language'], context['drama_name'], link_id)
    return BASE_URL + '?' + urlencode([
        ('c', campaign), ('af_adset', context['channel_name']), ('af_adset_id', context['channel_id']),
        ('af_ad', context['drama_name']+'_contentid['+context['content_id']+']'), ('af_ad_id', 'none'),
        ('af_channel', context['sub_user_id']), ('af_c_id', 'yt_manual_'+context['operation_id']),
        ('af_dp', context['content_id']),
    ], quote_via=quote, safe='*')


class ManualLinks:
    def __init__(self, db_path, publisher, channels, catalog, resolve_user):
        self.db_path, self.publisher = str(db_path), publisher
        self.channels, self.catalog, self.resolve_user = channels, catalog, resolve_user
        with self.db() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS youtube_manual_short_link(
                tenant TEXT NOT NULL, owner TEXT NOT NULL, operation_id TEXT NOT NULL,
                request_json TEXT NOT NULL, context_json TEXT NOT NULL,
                link_id INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL,
                PRIMARY KEY(tenant,owner,operation_id))''')

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.db_path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def actor(actor):
        return text(actor.get('tenant_key'), 'tenant'), text(actor.get('user_id'), 'owner')

    def _row(self, c, actor, op):
        tenant, owner = self.actor(actor)
        return c.execute('SELECT m.*,s.publish_state,s.short_url,s.error_message FROM youtube_manual_short_link m '
                         'JOIN drama_material_short_link s ON s.id=m.link_id '
                         'WHERE m.tenant=? AND m.owner=? AND m.operation_id=?', (tenant, owner, op)).fetchone()

    @staticmethod
    def dto(row):
        context = json.loads(row['context_json'])
        return {'operation_id': row['operation_id'], 'status': row['publish_state'],
                'short_url': row['short_url'] if row['publish_state']=='published' else '',
                'channel_name': context['channel_name'], 'drama_name': context['drama_name'],
                'content_id': context['content_id'], 'language': context['language'],
                'created_at': row['created_at'], 'message': row['error_message']}

    def get(self, actor, op):
        op = operation(op)
        with self.db() as c:
            row = self._row(c, actor, op)
        if not row:
            raise WorkflowError('not_found', '尚未找到本次生成记录，可按原选择重试', 404)
        return {'link': self.dto(row)}

    def create(self, actor, payload):
        tenant, owner = self.actor(actor)
        op, request = operation(payload.get('operation_id')), selection(payload)
        request_json = encode(request)
        with self.db() as c:
            old = self._row(c, actor, op)
        if old:
            if old['request_json'] != request_json:
                raise WorkflowError('operation_conflict', '本次生成的选择已固定，请先核对生成结果', 409)
            if old['publish_state']=='published':
                return {'link': self.dto(old)}
        # No link is allocated until all current permission / identity checks pass.
        channel = self.channels.validate(actor, request['channel_local_id'])
        drama = self.catalog.resolve(request['content_id'], request['language'])
        if old:
            context = json.loads(old['context_json'])
            if channel['channel_id'] != context['channel_id']:
                raise WorkflowError('channel_identity_changed', '频道身份已变化，不能继续本次生成', 409)
        else:
            context = dict(request, operation_id=op, version='youtube-manual-link-v1',
                           channel_id=text(channel['channel_id'], 'channel_id', True),
                           channel_name=text(channel['name'], 'channel_name'),
                           drama_name=text(drama['name'], 'drama_name', True),
                           sub_user_id=mapped_user_id([(self.resolve_user(actor),)]))
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._row(c, actor, op)
            if row:
                if row['request_json'] != request_json:
                    raise WorkflowError('operation_conflict', '本次生成的选择已固定', 409)
                if row['publish_state']=='published':
                    return {'link': self.dto(row)}
                context, created_at, link_id = json.loads(row['context_json']), row['created_at'], row['link_id']
                if channel['channel_id'] != context['channel_id']:
                    raise WorkflowError('channel_identity_changed', '频道身份已变化', 409)
            else:
                created_at = utc_now()
                job_id = uuid.uuid5(uuid.NAMESPACE_URL, encode([tenant, owner, op])).hex
                link_id = c.execute('''INSERT INTO drama_material_short_link
                    (job_id,material_kind,content_id,long_url,wrapper_sha256,publish_state,created_at_utc)
                    VALUES(?, 'youtube_manual', ?, '', '', 'pending', ?)''',
                    (job_id, context['content_id'], created_at)).lastrowid
                c.execute('INSERT INTO youtube_manual_short_link VALUES(?,?,?,?,?,?,?)',
                          (tenant, owner, op, request_json, encode(context), link_id, created_at))
            target = build_manual_url(context, link_id, created_at)
            body = render_wrapper_html('manual', context['content_id'], target=target)
            digest = hashlib.sha256(body).hexdigest()
            stored = c.execute('SELECT long_url,wrapper_sha256 FROM drama_material_short_link WHERE id=?', (link_id,)).fetchone()
            if stored['long_url'] and (stored['long_url'] != target or stored['wrapper_sha256'] != digest):
                raise WorkflowError('short_link_conflict', '短链冻结信息不一致', 409)
            c.execute('UPDATE drama_material_short_link SET long_url=?,wrapper_sha256=? WHERE id=?', (target,digest,link_id))
        try:
            if self.publisher is None:
                raise ValueError('publisher unavailable')
            result = self.publisher.publish(link_id, body)
            if result.get('sha256') != digest:
                raise ValueError('publisher readback mismatch')
        except Exception:
            with self.db() as c:
                c.execute("UPDATE drama_material_short_link SET publish_state='failed',error_code='manual_link_write_failed',"
                          "error_message='短链生成暂未完成，请按原选择重试' WHERE id=? AND publish_state<>'published'", (link_id,))
                row = self._row(c, actor, op)
                if row['publish_state']=='published':
                    return {'link': self.dto(row)}
            raise WorkflowError('manual_link_write_failed', '短链生成暂未完成，请按原选择重试', 503) from None
        with self.db() as c:
            c.execute("UPDATE drama_material_short_link SET publish_state='published',short_url=?,published_at_utc=?,"
                      "error_code='',error_message='' WHERE id=? AND long_url=? AND wrapper_sha256=?",
                      (SHORT_BASE_URL+'/'+str(link_id)+'.html', utc_now(), link_id, target, digest))
            row = self._row(c, actor, op)
        return {'link': self.dto(row)}
