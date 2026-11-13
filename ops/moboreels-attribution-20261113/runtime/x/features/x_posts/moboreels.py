"""Explicit Moboreels source policy shared by the X and TikTok adapters.

The policy is scoped to one frozen template. It never changes the legacy URL
macro, nor treats a Moboreels story ID as an internal drama_info identifier.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from features.moboreels_library import macro_client

PREVIEW_URL = "https://ai.yingliangads.com/m/preview"
_SOURCE = ContextVar("moboreels_source", default=False)
LANGUAGES = {2:"zh-Hant",3:"en",4:"es",5:"pt-br",6:"fr",7:"ru",8:"it",9:"ja",
             10:"ar",11:"id",12:"th",13:"vi",14:"ko",15:"fil",16:"de",17:"hi",
             18:"bn",21:"ms",22:"tr"}


def contains_macro(*texts):
    return macro_client.contains_macro(*texts)


@contextmanager
def source_policy(enabled):
    token = _SOURCE.set(bool(enabled))
    try:
        yield
    finally:
        _SOURCE.reset(token)


def source_enabled():
    return _SOURCE.get()


def story_metadata(story_id):
    story = macro_client.lookup_story(str(story_id or "").strip())
    if str(story.get("story_id")) != str(story_id) or str(story.get("is_show")) != "1":
        raise macro_client.MoboreelsLinkError("Moboreels 剧目不存在或已下架")
    try:
        language = LANGUAGES[int(story.get("lang_id"))]
    except (ValueError, TypeError, KeyError):
        raise macro_client.MoboreelsLinkError("Moboreels 剧目语言尚未支持") from None
    name = str(story.get("story_name") or story.get("name") or "").strip()
    description = str(story.get("introduce") or story.get("description") or "").strip()
    if not name or not description:
        raise macro_client.MoboreelsLinkError("Moboreels 剧名或简介缺失")
    return {"drama_name": name, "description": description, "language": language,
            "labels": ["Moboreels"], "series_code": str(story_id)}


def validated_url(value):
    value = str(value or "")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or not parsed.path.startswith("/m/")):
        raise macro_client.MoboreelsLinkError("Moboreels 短链无效")
    return value


def freeze_url(actor, *, story_id, platform, account_id, task_id, material_id, purpose="post"):
    receipt = macro_client.resolve_m_url(actor, story_id=story_id, platform=platform,
        account_id=account_id, task_id=task_id, material_id=material_id, purpose=purpose)
    if receipt.get("status") != "succeeded" or str(receipt.get("story_id")) != str(story_id):
        raise macro_client.MoboreelsLinkError("Moboreels 推广短链回执不一致")
    return validated_url(receipt.get("short_url"))


def freeze_schedule_actor(conn, source_type, version, actor, body_template):
    """Bind a newly saved schedule version to its authenticated creator."""
    if not contains_macro(body_template):
        return
    frozen = {key: str((actor or {}).get(key) or '').strip()
              for key in ('tenant_key', 'user_id', 'name')}
    if not frozen['tenant_key'] or not frozen['user_id']:
        raise macro_client.MoboreelsLinkError("Moboreels 定时配置缺少可信创建者")
    conn.execute('CREATE TABLE IF NOT EXISTS x_post_moboreels_schedule_actor ('
                 'source_type TEXT NOT NULL,config_version INTEGER NOT NULL,actor_json TEXT NOT NULL,'
                 'PRIMARY KEY(source_type,config_version))')
    conn.execute('INSERT INTO x_post_moboreels_schedule_actor VALUES (?,?,?)',
                 (source_type, int(version), json.dumps(frozen, ensure_ascii=False)))


def resolve_x_url(db_path, queue, log):
    """Use the frozen task/template creator, never a pool uploader or URL fallback."""
    queue = dict(queue)
    if queue.get('schedule_run_id') and not queue.get('manual_run_id'):
        try:
            with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                row = conn.execute('SELECT a.actor_json FROM x_post_schedule_run r '
                    'JOIN x_post_moboreels_schedule_actor a ON a.source_type=r.source_type '
                    'AND a.config_version=r.config_version WHERE r.id=?',
                    (queue['schedule_run_id'],)).fetchone()
            frozen = json.loads(row[0]) if row else {}
        except (sqlite3.Error, ValueError, TypeError):
            raise macro_client.MoboreelsLinkError("定时任务缺少冻结创建者，请重新保存 Moboreels 定时配置") from None
        tenant, user = frozen.get('tenant_key'), frozen.get('user_id')
    elif queue.get('manual_run_id'):
        from features.x_posts.attribution import resolve_via_loopback
        context = resolve_via_loopback(queue["id"], log["id"])
        tenant, user = context.get("source_tenant"), context.get("source_owner")
    else:
        raise macro_client.MoboreelsLinkError("发布任务缺少冻结创建者")
    if not tenant or not user:
        raise macro_client.MoboreelsLinkError("发布任务缺少冻结创建者")
    actor = macro_client.resolve_actor(user, tenant)
    return freeze_url(actor, story_id=queue["content_id"], platform="x",
        account_id=queue["account_id"], task_id="x-queue:%s" % queue["id"],
        material_id=queue["material_id"])
