"""Durable creator and caption binding for the explicit TikTok m_url macro."""
import json
import sqlite3
from contextlib import closing

from features.moboreels_library import macro_client
from features.x_posts.moboreels import contains_macro, freeze_url


def _schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS tt_moboreels_context (
        task_key TEXT PRIMARY KEY, actor_json TEXT NOT NULL,
        short_url TEXT NOT NULL DEFAULT '', caption TEXT NOT NULL DEFAULT '')""")


def freeze_queue_actor(conn, queue_id, actor, *, recurring_run_id=None):
    """Called inside queue creation's transaction, before the queue is visible."""
    actor = dict(actor or {})
    if recurring_run_id:
        run = conn.execute("SELECT * FROM tt_post_schedule_run WHERE id=?", (recurring_run_id,)).fetchone()
        if not run:
            raise macro_client.MoboreelsLinkError("TikTok 排期创建者记录缺失")
        run = dict(run)
        if run.get('config_version'):
            owner = conn.execute("SELECT * FROM tt_post_auto_publish_config WHERE id=1").fetchone()
        else:
            owner = conn.execute("SELECT * FROM tt_post_daily_schedule WHERE account_id=?", (run['account_id'],)).fetchone()
        owner = dict(owner or {})
        actor = {'user_id': owner.get('created_by_user_id'), 'name': owner.get('created_by_name'),
                 'tenant_key': owner.get('created_by_tenant_key', '')}
    if not str(actor.get('user_id') or '').strip():
        raise macro_client.MoboreelsLinkError("TikTok 任务缺少冻结创建者，请重新创建任务")
    _schema(conn)
    conn.execute("INSERT INTO tt_moboreels_context(task_key,actor_json) VALUES (?,?)",
                 ('tt-queue:%s' % queue_id, json.dumps(actor, ensure_ascii=False)))


def prepare_caption(store, current, claim_token, *, direct=False):
    current = dict(current)
    if not contains_macro(current.get('caption_template')):
        return current
    table = 'tt_post_direct_test' if direct else 'tt_post_queue'
    key = ('tt-direct:%s' if direct else 'tt-queue:%s') % current['id']
    with closing(sqlite3.connect(store.db_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        _schema(conn)
        row = conn.execute('SELECT * FROM %s WHERE id=?' % table, (current['id'],)).fetchone()
        row = dict(row or {})
        if (row.get('claim_token') != claim_token or row.get('publish_id')
                or row.get('unknown_outcome') or row.get('status') != 'publishing'):
            raise macro_client.MoboreelsLinkError('TikTok 发布租约或结果状态不允许生成推广链接')
        binding = conn.execute('SELECT * FROM tt_moboreels_context WHERE task_key=?', (key,)).fetchone()
        if binding is None and direct:
            actor = {'user_id': row.get('created_by_user_id'), 'name': row.get('created_by_name'),
                     'tenant_key': row.get('created_by_tenant_key', '')}
            if not actor['user_id']:
                raise macro_client.MoboreelsLinkError('TikTok 测试任务缺少冻结创建者')
            conn.execute('INSERT INTO tt_moboreels_context(task_key,actor_json) VALUES (?,?)',
                         (key, json.dumps(actor, ensure_ascii=False)))
            binding = conn.execute('SELECT * FROM tt_moboreels_context WHERE task_key=?', (key,)).fetchone()
        if binding is None:
            raise macro_client.MoboreelsLinkError('TikTok 任务缺少冻结创建者，请重新创建任务')
        binding = dict(binding)
        conn.commit()
    macro_client.assert_publishable(row.get('content_id'))
    if binding['short_url']:
        if row['caption'] != binding['caption'] or binding['short_url'] not in row['caption']:
            raise macro_client.MoboreelsLinkError('TikTok 冻结推广文案不一致')
        current['caption'] = row['caption']
        return current
    if not contains_macro(row.get('caption')):
        raise macro_client.MoboreelsLinkError('TikTok 待冻结推广宏缺失')
    creator = json.loads(binding['actor_json'])
    actor = macro_client.resolve_actor(creator.get('user_id'), creator.get('tenant_key', ''))
    short_url = freeze_url(actor, story_id=row['content_id'], platform='tt',
        account_id=row['account_id'], task_id=key, material_id=row['material_id'])
    caption = row['caption'].replace('{{m_url}}', short_url)
    if len(caption.encode('utf-16-le')) // 2 > 2200:
        raise macro_client.MoboreelsLinkError('TikTok 推广文案展开后超过 2200 字符')
    with closing(sqlite3.connect(store.db_path, timeout=30)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        changed = conn.execute("UPDATE %s SET caption=? WHERE id=? AND caption=? AND claim_token=? "
            "AND status='publishing' AND publish_id='' AND unknown_outcome=0" % table,
            (caption, row['id'], row['caption'], claim_token)).rowcount
        if changed != 1:
            raise macro_client.MoboreelsLinkError('TikTok 推广文案冻结冲突，请核对原任务')
        conn.execute('UPDATE tt_moboreels_context SET short_url=?,caption=? WHERE task_key=? AND short_url=\'\'',
                     (short_url, caption, key))
        conn.commit()
    current['caption'] = caption
    return current


def auto_url(store, task, template):
    """Only the template's immutable creator owns automatic promotions."""
    if not contains_macro(template):
        return None
    with closing(sqlite3.connect('file:' + str(store.db_path) + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT created_by_user_id,created_by_name FROM tt_auto_template WHERE id=?',
                           (task.template_id,)).fetchone()
    if not row or not row['created_by_user_id']:
        raise macro_client.MoboreelsLinkError('TikTok 自动模板缺少创建者')
    actor = macro_client.resolve_actor(row['created_by_user_id'])
    short_url = freeze_url(actor, story_id=task.content_id, platform='tt',
        account_id=task.account_id, task_id='tt-auto:%s' % task.id, material_id=task.material_id)
    return short_url
