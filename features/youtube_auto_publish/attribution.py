"""Frozen per-publication W2A attribution; no platform writes or token access."""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlencode, quote

from features.drama_synthesis.core import SHORT_BASE_URL, utc_now
from .templates import WorkflowError

BASE_URL = 'https://www.dramawavew2a.com/ads/101/2284/view'
VERSION = 'youtube-attribution-v1'


def text(value, field, campaign=False):
    value = str(value or '').strip()
    if not value or len(value) > 512 or any(ord(c) < 32 for c in value) or (campaign and '*' in value):
        raise WorkflowError('attribution_invalid', '长链归因字段无效：' + field, 409)
    return value


def build_url(context, ledger):
    """Use the actual publication ledger ID and its UTC creation timestamp."""
    timestamp = int(datetime.fromisoformat(ledger['created_at_utc'].replace('Z', '+00:00')).timestamp())
    log_id = str(ledger['id'])
    if not log_id.isdigit() or int(log_id) <= 0:
        raise WorkflowError('attribution_invalid', '发布记录ID无效', 409)
    campaign = 'yingliang_post_CLV_VL_youtube_%s*%snone%s*%s*%s*%s' % (
        context['channel_id'], timestamp, context['language'], context['drama_name'], context['tag'], log_id)
    return BASE_URL + '?' + urlencode([
        ('c', campaign), ('af_adset', context['channel_name']), ('af_adset_id', context['channel_id']),
        ('af_ad', context['material_name'] + '_contentid[' + context['content_id'] + ']'),
        ('af_ad_id', context['material_id']), ('af_channel', context['sub_user_id']),
        ('af_c_id', context['task_id']), ('af_dp', context['content_id']),
    ], quote_via=quote, safe='*')


def mapped_user_id(rows):
    ids = {str(row[0]).strip() for row in rows}
    if not ids or any(not re.fullmatch(r'[1-9][0-9]*', item) for item in ids):
        raise WorkflowError('attribution_user_missing', '发布用户邮箱未匹配到有效的 sub_user_id', 409)
    if len(ids) != 1:
        raise WorkflowError('attribution_user_ambiguous', '发布用户邮箱匹配到多个 sub_user_id，请先修正用户映射', 409)
    return next(iter(ids))


class AttributionLinks:
    def __init__(self, db_path, store, publisher, resolve_user):
        self.db_path, self.store, self.publisher, self.resolve_user = str(db_path), store, publisher, resolve_user
        with self.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS youtube_link_attribution(
                task_id TEXT PRIMARY KEY, link_id INTEGER NOT NULL UNIQUE,
                context TEXT NOT NULL, created_at TEXT NOT NULL)''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.db_path, timeout=20)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def prepare(self, material, channel, actor, task_id):
        if not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise WorkflowError('attribution_invalid', '发布任务ID无效', 409)
        context = dict(version=VERSION, task_id=task_id,
            owner=text(actor.get('user_id'), 'owner'), tenant=text(actor.get('tenant_key'), 'tenant'),
            channel_id=text(channel.get('channel_id'), 'channel_id', True),
            channel_name=text(channel.get('name'), 'channel_name'),
            material_id=text(material.get('id'), 'material_id'), material_name=text(material.get('name'), 'material_name'),
            content_id=text(material.get('content_id'), 'content_id'), language=text(material.get('language'), 'language', True),
            drama_name=text(material.get('macro_name'), 'drama_name', True), tag=text(material.get('tag') or 'none', 'tag', True))
        with self.connect() as c:
            old = c.execute('SELECT * FROM youtube_link_attribution WHERE task_id=?', (task_id,)).fetchone()
        if old:
            frozen = json.loads(old['context'])
            if any(frozen.get(k) != v for k, v in context.items()):
                raise WorkflowError('attribution_conflict', '同一发布任务的归因信息已冻结', 409)
            context = frozen
        else:
            context['sub_user_id'] = mapped_user_id([(self.resolve_user(actor),)])
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM youtube_link_attribution WHERE task_id=?', (task_id,)).fetchone()
            if old:
                if old['context'] != encoded:
                    raise WorkflowError('attribution_conflict', '同一发布任务的归因信息已冻结', 409)
                link_id = old['link_id']
            else:
                if c.execute('SELECT 1 FROM drama_material_short_link WHERE job_id=? AND material_kind=?', (task_id, 'custom_source')).fetchone():
                    raise WorkflowError('attribution_conflict', '此任务已有历史短链，不能覆盖', 409)
                cursor = c.execute('''INSERT INTO drama_material_short_link
                    (job_id,material_kind,content_id,long_url,wrapper_sha256,publish_state,created_at_utc)
                    VALUES(?,?,?,'','','pending',?)''', (task_id, 'custom_source', context['content_id'], utc_now()))
                link_id = cursor.lastrowid
                c.execute('INSERT INTO youtube_link_attribution VALUES(?,?,?,?)', (task_id, link_id, encoded, utc_now()))
        material.update(attribution=context, link_job_id=task_id, long_url='', macro_url=SHORT_BASE_URL + '/' + str(link_id) + '.html')
        return material

    def finalize(self, material, ledger):
        context = material['attribution']
        with self.connect() as c:
            frozen = c.execute('SELECT context FROM youtube_link_attribution WHERE task_id=?', (context['task_id'],)).fetchone()
        if not frozen or json.loads(frozen['context']) != context:
            raise WorkflowError('attribution_conflict', '归因信息与冻结记录不一致', 409)
        if (ledger['preparation_id'] != context['task_id'] or ledger['channel_id'] != context['channel_id']
                or ledger['source_material_id'] != context['material_id'] or ledger['operator_user_id'] != context['owner']):
            raise WorkflowError('attribution_conflict', '发布记录与归因信息不一致', 409)
        target = build_url(context, ledger)
        link = self.store.ensure_short_link(context['task_id'], 'custom_source', context['content_id'], self.publisher, attribution_url=target)
        if link['short_url'] != material['macro_url']:
            raise WorkflowError('attribution_conflict', '短链ID与任务预留记录不一致', 409)
        material['long_url'] = target
        return material
