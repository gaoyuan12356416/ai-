"""Read the immutable Auto origin before allowing a future hit publication."""
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3


def assert_due(auto_db, post_store, queue_id, now=None):
    from features.x_posts.service import XPostError
    queue = post_store.get_queue(int(queue_id))
    if not queue.get('manual_run_id'):
        return
    # Use the immutable external-task key too: queue creation precedes the
    # Auto execution_queue_id update, so that update is not an admission gate.
    with closing(sqlite3.connect(str(post_store.db_path), timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT trigger_source,external_task_key FROM x_post_manual_run WHERE id=?', (queue['manual_run_id'],)).fetchone()
        run = dict(row) if row else {}
    if run.get('trigger_source') != 'auto_template':
        return
    key = str(run.get('external_task_key') or '')
    if not key.startswith('x-auto-task-') or not key[12:].isdigit():
        raise XPostError('x_auto_hit_origin_invalid', '自动发布来源无效', 409)
    with closing(sqlite3.connect(str(auto_db)+'?mode=ro', uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT account_id,material_id,selection_json,scheduled_at_utc FROM x_auto_task WHERE id=?', (int(key[12:]),)).fetchone()
    if not row:
        raise XPostError('x_auto_hit_origin_invalid', '自动发布来源不存在', 409)
    selection = json.loads(row['selection_json'] or '{}')
    timing = selection.get('hit_publish_timing')
    if not timing:
        return
    if str(row['account_id']) != str(queue['account_id']) or str(row['material_id']) != str(queue['material_id']):
        raise XPostError('x_auto_hit_origin_invalid', '自动发布来源与队列不一致', 409)
    due = datetime.fromisoformat(timing['publish_at_utc'])
    if due != datetime.fromisoformat(row['scheduled_at_utc']):
        raise XPostError('x_auto_hit_timing_invalid', '预约时间与冻结任务不一致', 409)
    if (now or datetime.now(timezone.utc)) < due:
        raise XPostError('x_auto_hit_not_due', '爆款发布尚未到预约时间', 409)
