"""Tenant-shared channel copy; never consulted by an existing publish task."""
from datetime import datetime, timezone

from .templates import WorkflowError, render

FIELDS = ('title_template', 'description_template', 'comment_template')


class ChannelTemplateStore:
    def __init__(self, db):
        self.db = db
        with db() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS youtube_channel_template(
                tenant TEXT NOT NULL, channel_id TEXT NOT NULL,
                title_template TEXT NOT NULL DEFAULT '',
                description_template TEXT NOT NULL DEFAULT '',
                comment_template TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL, updated_by TEXT NOT NULL,
                updated_by_name TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant,channel_id))''')

    @staticmethod
    def empty(channel_id):
        return dict(channel_id=channel_id, version=0, updated_by='',
                    updated_by_name='', updated_at='', **{f: '' for f in FIELDS})

    @staticmethod
    def summary(value):
        return {**{k: value[k] for k in ('version', 'updated_by', 'updated_by_name', 'updated_at')},
                'configured_fields': [f.removesuffix('_template') for f in FIELDS if value[f].strip()]}

    def all(self, tenant):
        with self.db() as c:
            return {r['channel_id']: dict(r) for r in c.execute(
                'SELECT * FROM youtube_channel_template WHERE tenant=?', (tenant,))}

    def get(self, tenant, channel_id):
        with self.db() as c:
            row = c.execute('SELECT * FROM youtube_channel_template WHERE tenant=? AND channel_id=?',
                            (tenant, channel_id)).fetchone()
        value = dict(row) if row else self.empty(channel_id)
        value.pop('tenant', None)
        return value

    def save(self, actor, channel_id, payload):
        if not isinstance(payload, dict):
            raise WorkflowError('invalid_request', '请求格式无效')
        if payload.get('channel_id') != channel_id:
            raise WorkflowError('channel_identity_changed', '频道身份已变化，请重新打开模板', 409)
        version = payload.get('version')
        if type(version) is not int or version < 0:
            raise WorkflowError('version_required', '请重新加载模板后保存')
        values = {}
        for field in FIELDS:
            text = payload.get(field)
            if not isinstance(text, str):
                raise WorkflowError('invalid_template', '请提交完整的三个模板文本字段')
            # Empty templates mean no override, unlike required final publish text.
            if len(text.encode('utf-8')) > 16000:
                raise WorkflowError('template_too_long', '文案模板过长')
            values[field] = render(text, {}, field[:-9], allow_unresolved=True) if text.strip() else ''
        tenant = actor['tenant_key']
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        with self.db(True) as c:
            row = c.execute('SELECT version FROM youtube_channel_template WHERE tenant=? AND channel_id=?',
                            (tenant, channel_id)).fetchone()
            if (row['version'] if row else 0) != version:
                raise WorkflowError('template_conflict', '模板已被其他人修改，当前输入已保留，请重新加载后核对', 409)
            c.execute('''INSERT INTO youtube_channel_template
                (tenant,channel_id,title_template,description_template,comment_template,
                 version,updated_by,updated_by_name,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant,channel_id) DO UPDATE SET
                title_template=excluded.title_template,description_template=excluded.description_template,
                comment_template=excluded.comment_template,version=excluded.version,
                updated_by=excluded.updated_by,updated_by_name=excluded.updated_by_name,updated_at=excluded.updated_at''',
                (tenant, channel_id, *(values[f] for f in FIELDS), version + 1,
                 actor['user_id'], actor.get('name', ''), stamp))
        # Return this exact write, even if another publisher saves immediately after it.
        return dict(channel_id=channel_id, **values, version=version + 1,
                    updated_by=actor['user_id'], updated_by_name=actor.get('name', ''), updated_at=stamp)
