"""Current Moboreels eligibility at admission and before new platform writes."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from features.moboreels_library import macro_client
from .templates import WorkflowError


def assert_body_publishable(body):
    material = body.get('material') or {}
    request = body.get('request') or {}
    enabled = (material.get('promotion_provider') == 'moboreels'
               or bool(material.get('moboreels_promotion'))
               or macro_client.contains_macro(*(request.get(key + '_template') for key in ('title', 'description', 'comment'))))
    if not enabled:
        return
    story = str(material.get('content_id') or '')
    if material.get('moboreels_story_id') and str(material['moboreels_story_id']) != story:
        raise WorkflowError('moboreels_drama_not_publishable', '素材与冻结的 Moboreels 剧 ID 不一致，已拦截发布', 409)
    try:
        macro_client.assert_publishable(story)
    except macro_client.MoboreelsLinkError as exc:
        raise WorkflowError('moboreels_drama_not_publishable', str(exc), 409) from None


def assert_task_publishable(db_path, task):
    preparation = str(task.get('preparation_id') or '')
    if not preparation:
        return  # The synthesis workflow has no Moboreels material/template.
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
            row = conn.execute('SELECT body FROM youtube_auto_preparation WHERE id=?', (preparation,)).fetchone()
        if not row:
            raise ValueError()
        body = json.loads(row[0])
        if not isinstance(body, dict):
            raise ValueError()
    except (sqlite3.Error, ValueError, TypeError):
        raise WorkflowError('publish_material_snapshot_unavailable', '发布素材快照暂不可核实，未提交新的平台操作', 409) from None
    assert_body_publishable(body)
