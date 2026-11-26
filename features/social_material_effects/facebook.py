"""Facebook publications are identified by Page and the confirmed Graph object."""
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from . import data

FB_DB = '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'
WAREHOUSE = '/mnt/data-disk/social-analytics-warehouse'
MATERIAL_CATALOG = '/mnt/data-disk/fb-planner-catalog/current.sqlite3'


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=8)
    db.row_factory = sqlite3.Row
    return db


def identity(page, graph):
    page, graph = str(page), str(graph)
    prefix, separator, obj = graph.rpartition('_')
    if not page.isdigit() or not obj.isdigit() or (separator and prefix != page):
        return None
    return page + '_' + obj


def discover(path=FB_DB):
    auto = """SELECT t.page_id,t.graph_post_id,t.material_id,t.content_id,p.language,t.completed_at_utc published_at,
             'auto_template' source,e.post_id native,e.media_views views,e.collected_at_utc metrics_at,
             json_extract(t.moboreels_json,'$.material.material_name') material_name,
             json_extract(t.moboreels_json,'$.material.drama_name') drama_name,
             json_extract(t.moboreels_json,'$.link.koc_code') mobo_code,
             '' owner,'' tenant
          FROM fb_auto_task t JOIN fb_auto_run_page p USING(run_id,page_id)
          JOIN fb_auto_publish_ledger l ON l.task_id=t.id AND l.page_id=t.page_id AND l.material_id=t.material_id
             AND l.graph_post_id=t.graph_post_id AND l.status='published' AND l.unknown_outcome=0
          LEFT JOIN fb_auto_post_effect e ON e.task_id=t.id
         WHERE t.status='published' AND t.unknown_outcome=0 AND t.completed_at_utc<>''"""
    manual = """SELECT page_id,graph_post_id,material_id,content_id,
             COALESCE(json_extract(snapshot_json,'$.material.language'),json_extract(snapshot_json,'$.page.language')) language,
             updated_at_utc published_at,source_kind source,NULL native,NULL views,NULL metrics_at,
             json_extract(snapshot_json,'$.material.material_name') material_name,
             json_extract(snapshot_json,'$.material.drama_name') drama_name,
             json_extract(snapshot_json,'$.moboreels.koc_code') mobo_code,owner_user_id owner,tenant_key tenant
          FROM fb_video_task WHERE status='published' AND unknown_outcome=0 AND updated_at_utc<>''"""
    posts, conflicts = {}, set()
    with closing(readonly(path)) as db:
        db.execute('BEGIN')
        for row in db.execute(auto + ' UNION ALL ' + manual):
            r = dict(row)
            page, graph = str(r['page_id']), str(r['graph_post_id'])
            key = identity(page, graph)
            if not key or not str(r['material_id']).isdigit() or not r['content_id']:
                continue
            previous = posts.get(key)
            if previous:
                if (previous['material_id'], previous['content_id']) != (str(r['material_id']), str(r['content_id'])):
                    conflicts.add(key)
                continue
            native = r['native'] or (graph if '_' in graph else '')
            obj = key.split('_')[1]
            posts[key] = dict(post_id=key, namespace='ads_custom_source', material_id=str(r['material_id']),
                content_id=str(r['content_id']), material_name=r['material_name'] or '', drama_name=r['drama_name'] or '',
                product='', catalog='moboreels' if r['mobo_code'] else 'dramawave', language=(r['language'] or '').lower(),
                account_id=page, account_username='', published_at=r['published_at'], native=native, object_id=obj,
                source=r['source'], owner=r['owner'] or '', tenant=r['tenant'] or '', views=r['views'],
                metrics_at=r['metrics_at'], mobo_code=r['mobo_code'], revenue_through=None,
                post_url=('https://www.facebook.com/' + page + '/posts/' + native.split('_')[-1] if native else
                          'https://www.facebook.com/watch/?v=' + obj))
    for key in conflicts:
        del posts[key]
    return posts, len(conflicts)


def enrich(posts, reader, catalog_path=MATERIAL_CATALOG):
    by_material = defaultdict(list)
    for p in posts.values():
        by_material[p['material_id']].append(p)
    def apply(rows):
        for row in rows:
            for p in by_material.get(str(row['id']), ()):
                if p['content_id'] == str(row['cid']):
                    p['product'] = row['product'] or ''
                    p['material_name'] = p['material_name'] or row['name'] or ''
                    if p['product'].casefold() in ('moboreels','moboshort'):
                        p['catalog'] = 'moboreels'
    ids = list(by_material)
    if Path(catalog_path).is_file():
        with closing(readonly(catalog_path)) as db:
            for i in range(0,len(ids),450):
                batch = ids[i:i+450]
                apply(db.execute('SELECT id,cid,product,name FROM material WHERE id IN (' + ','.join('?' for _ in batch) + ')',batch))
    missing = [mid for mid,rows in by_material.items() if any(not p['product'] for p in rows)]
    for i in range(0,len(missing),300):
        batch = missing[i:i+300]
        if not all(re.fullmatch(r'[0-9]+', v) for v in batch):
            raise ValueError('invalid_material_key')
        sql = ("SELECT HEX(JSON_OBJECT('id',id,'cid',data_source_id,'product',product,'name',name)) "
               "FROM kunlunads_dev.ads_custom_source WHERE id IN (" + ','.join(batch) + ')')
        apply(reader.query([sql],timeout=60))


def revenue(posts, warehouse_root=WAREHOUSE):
    from features.social_analytics.warehouse_reader import current, connect
    from features.social_analytics.report import campaign_index, resolve
    meta, path = current(warehouse_root)
    end = min(meta['end'], (datetime.now(timezone.utc).date()-timedelta(days=1)).isoformat())
    start = date.fromisoformat(meta['start'])
    days = {(start + timedelta(days=i)).isoformat() for i in range((date.fromisoformat(end)-start).days+1)}
    available = {s for s in ('2049','2310') if not (meta.get('catalog_errors') or {}).get('facebook:'+s)
                 and days.issubset(meta['coverage'].get('facebook:'+s, []))}
    with closing(connect(path)) as db:
        catalog = [json.loads(r[0]) for r in db.execute("SELECT payload FROM catalog WHERE json_extract(payload,'$.platform')='facebook'")]
        index = campaign_index(catalog)
        mapped = {}
        for key, rows in index.items():
            row = resolve(rows)
            if not row:
                continue
            found = {}
            for item in rows:
                for fact in item.get('publications', ()):
                    parts = fact['id'].split(':')
                    if len(parts) != 4 or parts[:2] != ['facebook','post']:
                        continue
                    pid = identity(parts[2], parts[3])
                    if pid:
                        found[pid] = min(found.get(pid, fact['date']), fact['date'])
            if len(found) != 1:
                continue
            pid, first_day = next(iter(found.items()))
            p = posts.get(pid)
            if not p or p['content_id'] != row['drama'] or p['material_id'] != str((row.get('effect_source') or {}).get('ad_id')) or p['account_id'] != str((row.get('effect_source') or {}).get('adset_id')):
                continue
            p.update(drama_name=p['drama_name'] or row.get('drama_label'), account_username=row.get('channel_label') or '',
                     owner=row['owner'], tenant=row['tenant'])
            sites = {'2049','2310'} if row['site_id']=='2049' and row['link_type']=='auto_template' else {row['site_id']}
            if first_day > end or not sites.issubset(available) or p.get('mobo_code'):
                continue
            mapped[key] = (p, first_day)
            p.update(revenue_cents=0, refund_cents=0, revenue_through=end)
        assigned = unassigned = 0
        for r in db.execute("SELECT payload FROM effects WHERE platform='facebook' AND dt<=?",(end,)):
            fact = json.loads(r[0])
            key = ('facebook',str(fact['site_id']),str(fact['campaign_id']))
            target = mapped.get(key)
            cents = int(fact['revenue_cents'])
            if target:
                p, first_day = target
                if first_day <= fact['date'] and (p['account_id'],p['material_id']) == (str(fact['adset_id']),str(fact['ad_id'])):
                    p['revenue_cents'] += cents
                    p['refund_cents'] += int(fact['refund_cents'])
                    assigned += cents
                    continue
            unassigned += cents
    if assigned != sum(p.get('revenue_cents') or 0 for p in posts.values()):
        raise ValueError('facebook_revenue_not_conserved')
    return dict(revenue_source_version=meta['version'],revenue_source_generated_at=meta['generated_at'],
                revenue_through=end,assigned_revenue_cents=assigned,unassigned_revenue_cents=unassigned,
                revenue_incomplete_sites=sorted({'2049','2310'}-available))
