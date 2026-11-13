"""Read-only Meta feedback in SQLite; evidence never creates a publishing task."""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .strategy import bucket


def ensure_schema(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS fb_auto_post_effect (
        task_id INTEGER PRIMARY KEY, post_id TEXT NOT NULL, language TEXT NOT NULL,
        created_at_utc TEXT NOT NULL, collected_at_utc TEXT NOT NULL, age_hours REAL NOT NULL,
        media_views INTEGER NOT NULL, link_clicks INTEGER NOT NULL,
        avg_watch_seconds REAL, dnu_revenue_d3 REAL, revenue_window TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE IF NOT EXISTS fb_auto_post_effect_snapshot (
        task_id INTEGER NOT NULL, stage TEXT NOT NULL, collected_at_utc TEXT NOT NULL,
        payload_json TEXT NOT NULL, PRIMARY KEY(task_id,stage)
      );
      CREATE TABLE IF NOT EXISTS fb_auto_post_effect_attempt (
        task_id INTEGER PRIMARY KEY, checked_at_utc TEXT NOT NULL, retry_at_utc TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT ''
      );
    """)


def write_effect(conn, task_id, item, *, stage='latest'):
    """Import only a confirmed task with matching Page/video and a real Post ID."""
    row = conn.execute("""SELECT t.*,p.language FROM fb_auto_task t
        JOIN fb_auto_run_page p ON p.run_id=t.run_id AND p.page_id=t.page_id
        JOIN fb_auto_publish_ledger l ON l.task_id=t.id AND l.status='published'
        WHERE t.id=? AND t.status='published' AND l.graph_post_id=t.graph_post_id""", (int(task_id),)).fetchone()
    if not row or str(item.get('page_id')) != row['page_id'] or str(item.get('video_id')) != row['graph_post_id']:
        raise ValueError('effect_task_identity_mismatch')
    post_id = str(item['post_id'])
    parts = post_id.split('_')
    if len(parts) != 2 or parts[0] != row['page_id'] or not parts[1].isdigit() or parts[1] == row['graph_post_id']:
        raise ValueError('effect_real_post_id_required')
    language = str(row['language']).strip().lower()
    if str(item['language']).strip().lower() != language:
        raise ValueError('effect_language_mismatch')
    created, collected = (datetime.fromisoformat(str(item[k]).replace('Z', '+00:00')) for k in ('created_at_utc','collected_at_utc'))
    if created.tzinfo is None or collected.tzinfo is None or created > collected:
        raise ValueError('effect_timestamps_invalid')
    age = (collected - created).total_seconds() / 3600
    values = []
    for key in ('media_views','link_clicks'):
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('effect_metrics_incomplete')
        values.append(value)
    revenue = item.get('dnu_revenue_d3')
    watch = item.get('avg_watch_seconds')
    for value in (revenue,watch):
        if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
            raise ValueError('effect_metric_invalid')
    window = str(item.get('revenue_window') or '')
    if revenue is not None and window != 'beijing_publish_day_plus_2':
        raise ValueError('effect_revenue_window_invalid')
    payload = {'task_id':int(task_id),'post_id':post_id,'language':language,
               'created_at_utc':created.astimezone(timezone.utc).isoformat(timespec='seconds'),
               'collected_at_utc':collected.astimezone(timezone.utc).isoformat(timespec='seconds'),
               'age_hours':age,'media_views':values[0],'link_clicks':values[1],
               'avg_watch_seconds':watch,'dnu_revenue_d3':revenue,'revenue_window':window}
    if stage in ('24h','72h','7d'):
        threshold = {'24h':24,'72h':72,'7d':168}[stage]
        if not threshold <= age <= threshold + 6:
            raise ValueError('effect_snapshot_outside_age_window')
    elif stage not in ('baseline','latest'):
        raise ValueError('effect_snapshot_stage_invalid')
    conn.execute("""INSERT INTO fb_auto_post_effect VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET post_id=excluded.post_id,language=excluded.language,
          created_at_utc=excluded.created_at_utc,collected_at_utc=excluded.collected_at_utc,
          age_hours=excluded.age_hours,media_views=excluded.media_views,link_clicks=excluded.link_clicks,
          avg_watch_seconds=excluded.avg_watch_seconds,dnu_revenue_d3=excluded.dnu_revenue_d3,
          revenue_window=excluded.revenue_window
        WHERE excluded.collected_at_utc>fb_auto_post_effect.collected_at_utc""", tuple(payload.values()))
    if stage != 'latest':
        conn.execute('INSERT OR IGNORE INTO fb_auto_post_effect_snapshot VALUES(?,?,?,?)',
                     (int(task_id),stage,payload['collected_at_utc'],json.dumps(payload,sort_keys=True)))
    return payload


def load_scores(conn, config, now):
    """Use only mature, fresh auto-Post samples from this template's Page groups."""
    if not config.get('feedback_selection',{}).get('enabled'):
        return {}
    groups = list(config['group_ids'])
    rows = conn.execute("""SELECT e.*,t.page_id,t.material_id,t.content_id
        FROM fb_auto_post_effect e JOIN fb_auto_task t ON t.id=e.task_id
        JOIN fb_auto_run r ON r.id=t.run_id WHERE t.status='published' AND r.trigger_type='auto'
        AND t.group_id IN (""" + ','.join('?' for _ in groups) + """) AND e.age_hours>=48
        AND e.collected_at_utc>=? AND e.collected_at_utc<=? AND e.created_at_utc>=?
        ORDER BY e.task_id""", (*groups,(now-timedelta(hours=72)).isoformat(timespec='seconds'),
        now.isoformat(timespec='seconds'),(now-timedelta(days=21)).isoformat(timespec='seconds'))).fetchall()
    grouped = defaultdict(list)
    for row in rows:
        for kind in ('material_id','content_id'):
            grouped[(row['language'],kind,row[kind])].append(row)
    scores = {}
    for key, samples in grouped.items():
        # Cross-Page evidence: a single viral Page never graduates a material.
        if len(samples)<5 or len({r['page_id'] for r in samples})<3:
            continue
        by_page = defaultdict(list)
        for row in samples:
            by_page[row['page_id']].append(row)
        rates, revenues = [], []
        for page_rows in by_page.values():
            views = sum(r['media_views'] for r in page_rows)
            clicks = sum(r['link_clicks'] for r in page_rows)
            rates.append(1000 * clicks / (views + 1000))
            known = [float(r['dnu_revenue_d3']) for r in page_rows if r['dnu_revenue_d3'] is not None]
            if known:
                revenues.append(statistics.mean(known))
        scores[key] = {'clicks_per_1000_smoothed':statistics.median(rates),
                       'd3_revenue_per_post':statistics.median(revenues) if len(revenues)>=3 else None,
                       'posts':len(samples),'pages':len(by_page)}
    return scores


def choose_material(candidates, excluded, *, materials, config, page_id, slot_key, scores):
    rule = config.get('feedback_selection', {})
    available = [m for m in candidates if m.material_id not in excluded]
    if not available:
        return None, {'arm':'none','reason':'no_eligible_material'}
    if not rule.get('enabled'):
        return materials.choose_from(available, ()), {'arm':'control','reason':'paid_metric_order'}
    if bucket(f'explore:{page_id}:{slot_key}') < rule['exploration_percent']:
        # Equal opportunity across dramas; a drama with 500 edits gets one vote.
        by_drama = defaultdict(list)
        for item in available:
            by_drama[item.content_id].append(item)
        content = sorted(by_drama)[bucket(f'drama:{page_id}:{slot_key}', len(by_drama))]
        items = by_drama[content]
        return items[bucket(f'material:{page_id}:{slot_key}',len(items))], {'arm':'explore','reason':'same_language_drama_uniform'}
    if bucket(f'feedback:{page_id}:{slot_key}') >= rule['rollout_percent']:
        return materials.choose_from(available, ()), {'arm':'control','reason':'paid_metric_order'}
    ranked = []
    for material in available:
        evidence = scores.get((material.language,'material_id',material.material_id))
        level = 'material'
        if evidence is None:
            evidence = scores.get((material.language,'content_id',material.content_id))
            level = 'drama'
        if evidence is not None and (evidence['clicks_per_1000_smoothed'] > 0 or (evidence['d3_revenue_per_post'] or 0) > 0):
            ranked.append((material,evidence,level))
    if not ranked:
        return materials.choose_from(available, ()), {'arm':'fallback','reason':'insufficient_mature_feedback'}
    # Click yield leads. D3 calendar revenue breaks equal-click ties only, so
    # incomplete/unknown revenue is never treated as zero or used to penalize.
    ranked.sort(key=lambda r:(r[1]['clicks_per_1000_smoothed'],
                             r[1]['d3_revenue_per_post'] if r[1]['d3_revenue_per_post'] is not None else -1),reverse=True)
    top = ranked[:min(10,len(ranked))]
    item, evidence, level = top[bucket(f'exploit:{page_id}:{slot_key}',len(top))]
    return item, {'arm':'feedback','reason':level+'_cross_page_feedback',**evidence}
