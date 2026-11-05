"""Confirmed manual/Page-pool FB video targets, including prior-plan arrivals."""
from collections import Counter
from .common import parse_time, in_window
from .accounting import read_rows, daily_override, reason_label


def collect_video(conn, window, context, seen_success):
    tasks = read_rows(conn, 'fb_video_task')
    attempts = read_rows(conn, 'fb_video_attempt')
    receipts = {}
    for r in attempts:
        at = parse_time(r.get('created_at_utc'))
        if r.get('result_kind') == 'success' and r.get('graph_post_id') and at:
            key = (r['task_id'], str(r['graph_post_id']))
            receipts[key] = max(at, receipts.get(key, at))
    groups, reasons, prior = {}, {}, 0
    labels = {'manual': '手动视频发布', 'pool': 'Page 素材池发布', 'group_pool': '分组素材池发布'}
    for task in tasks:
        due = parse_time(task.get('planned_at_utc'))
        if not due or due >= window.end:
            continue
        source_kind = task.get('source_kind') or 'manual'
        source = 'video:' + source_kind
        row = groups.setdefault(source, {'source':source, 'label':labels.get(source_kind,'视频素材池发布'),
              'form':'Page 视频', 'expected':0,'published':0,'late':0,'pending':0,'blocked':0,
              'unknown':0,'failed':0,'policy_skipped':0,'frequency_excluded':0,'raw_targets':0,
              'superseded_slots':0,'reasons':[],'policy_reasons':[],'warnings':[]})
        reasons.setdefault(source, Counter())
        post = str(task.get('graph_post_id') or '')
        identity = (str(task.get('page_id')), post)
        completed = receipts.get((task['task_id'], post))
        fact = context.get('publications',{}).get('fb:video:' + str(task['task_id']))
        if fact and str(fact.get('publish_id')) == post:
            completed = parse_time(fact.get('published_at'))
        confirmed = task.get('status') == 'published' and post and not task.get('unknown_outcome') and completed
        # An accepted POST alone is not confirmation: native status must have
        # reached published through the readback path (or a canonical receipt).
        if due < window.start:
            if confirmed and in_window(completed,window.start,window.end) and identity not in seen_success:
                prior += 1; seen_success.add(identity)
            continue
        row['expected'] += 1; row['raw_targets'] += 1
        if confirmed and completed < window.cutoff and identity not in seen_success:
            row['published' if completed < window.end else 'late'] += 1
            seen_success.add(identity); continue
        if task.get('unknown_outcome') or task.get('status') == 'unknown' or task.get('status') == 'published':
            bucket, code = 'unknown', 'fb_report_confirmation_mismatch' if not confirmed or identity in seen_success else 'fb_report_after_cutoff'
        else:
            override = daily_override(context,'fb:video:'+str(task['task_id']),window.cutoff)
            if override:
                bucket,code,_ = override
            elif task.get('status') in {'cancelled','canceled','skipped'}:
                bucket,code = 'policy_skipped','source_closed'
            elif task.get('status') == 'failed':
                bucket,code = 'failed',task.get('error_code') or 'publish_failed'
            elif task.get('link_state') not in (None,'ready') and task.get('prepare_state') == 'ready':
                bucket,code = 'blocked','canonical_link_pending'
            else:
                bucket,code = 'pending',task.get('error_code') or 'execution_pending'
        row[bucket] += 1; reasons[source][code] += 1
    for source,row in groups.items():
        row['reasons'] = [{'code':c,'reason':reason_label(c),'count':n,'suggestion':'核对原任务及执行记录；未知结果先对账，不自动重发','confidence':'confirmed'} for c,n in reasons[source].most_common()]
    return list(groups.values()),prior
