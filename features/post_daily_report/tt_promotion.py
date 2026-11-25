"""Merge independent TT plans into the existing template/trigger/form totals."""
import os
import sqlite3
from contextlib import closing

from . import tt


DEFAULT_DB = '/mnt/data-disk/tt-promotion/promotion.sqlite3'
AUTO_DB = '/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3'


def _claimed_receipts(paths, receipts):
    """Historical owners retain their confirmed receipt when both ledgers reference it."""
    found = set()
    values = sorted(receipts)
    if not values:
        return found
    for path_key, tables in [('tt_auto', ['tt_auto_task']), ('tt', ['tt_post_queue', 'tt_post_direct_test'])]:
        with closing(tt._db(paths[path_key])) as db:
            for table in tables:
                for i in range(0, len(values), 400):
                    batch = values[i:i+400]
                    placeholders = ','.join('?' for _ in batch)
                    sql = f"SELECT publish_id FROM {table} WHERE status='published' AND publish_id IN ({placeholders})"
                    found.update(r[0] for r in db.execute(sql, batch))
    return found


def collect_promotion(paths, window, output):
    path = paths.get('tt_promotion') or os.environ.get('TT_PROMOTION_REPORT_DB')
    if not path:
        if str(paths.get('tt_auto')) != AUTO_DB:
            return
        path = DEFAULT_DB
    try:
        with closing(tt._db(path)) as db:
            slots = [dict(row) for row in db.execute("""
                SELECT p.slot_key,p.task_id,p.video_template,p.trigger_type,p.scheduled_at_utc,p.slot_state,p.slot_reason,
                  t.id,t.account_id,t.status,t.publish_id,t.published_at_utc,t.unknown_outcome,t.error_code
                FROM tt_promotion_report_plan p LEFT JOIN tt_promotion_report_task t
                ON t.id=p.task_id AND t.template_id=p.template_id AND t.account_id=p.account_id
                WHERE julianday(p.scheduled_at_utc)>=julianday(?) AND julianday(p.scheduled_at_utc)<julianday(?)
                ORDER BY p.scheduled_at_utc,p.slot_key""", (window.start.isoformat(), window.end.isoformat()))]
            earlier = {row[0] for row in db.execute("""SELECT DISTINCT publish_id FROM tt_promotion_report_task
                  WHERE status='published' AND unknown_outcome=0 AND COALESCE(publish_id,'')<>''
                  AND julianday(scheduled_at_utc)<julianday(?)
                  AND julianday(published_at_utc)>=julianday(?) AND julianday(published_at_utc)<julianday(?)""",
                  (window.start.isoformat(), window.start.isoformat(), window.end.isoformat()))}
        seen = _claimed_receipts(paths, earlier | {r['publish_id'] for r in slots if r['publish_id']})
        if len({row['slot_key'] for row in slots}) != len(slots):
            raise ValueError('duplicate_promotion_plan_slot')
        seen_tasks = [r['task_id'] for r in slots if r['task_id'] is not None]
        if len(set(seen_tasks)) != len(seen_tasks):
            raise ValueError('duplicate_promotion_plan_task')
        output['prior_completed'] += len(earlier - seen)
        seen.update(earlier)
        groups = {row['source']: row for row in output['rows']}
        for task in slots:
            manual = task['trigger_type'] == 'manual'
            video = task['video_template'] or 'unspecified'
            source = ('auto_template_manual:' if manual else 'auto_template:') + video
            if source not in groups:
                form = tt.FORMS.get(video, '其他模板视频')
                groups[source] = tt._row(source, ('手动执行模板·' if manual else '自动模板·') + form, form)
                output['rows'].append(groups[source])
            row = groups[source]
            if row['expected'] is not None:
                row['expected'] += 1
            if task['id'] is None:
                if task['slot_state'] in ('skipped','missed','failed','canceled'):
                    # Apply the same existing policy-skipped/missed mapping to a closed pre-enqueue slot.
                    tt._account(row,dict(task,status=task['slot_state'],error_message=task['slot_reason']),
                                None,window)
                else:
                    tt._missing(row, 1)
                continue
            if task['status'] == 'published' and (task['unknown_outcome'] or task['publish_id'] in seen):
                task = dict(task, status='unknown', unknown_outcome=1)
            if task['status'] == 'published' and task['publish_id']:
                seen.add(task['publish_id'])
            tt._account(row, task, task['published_at_utc'], window)
        output['evidence'].append({'source':'tt_promotion', 'date':window.date,
                                   'frozen_slots':len(slots), 'tasks':len(seen_tasks)})
    except (sqlite3.Error, OSError, KeyError, ValueError, TypeError) as exc:
        output['warnings'].append(f'TT 自动模板新增账本读取不完整（{type(exc).__name__}），不可视为零发布。')
        missing = tt._row('unavailable:tt_promotion', '自动模板·数据不可用', '未知')
        missing.update(expected=None, data_available=False)
        output['rows'].append(missing)
