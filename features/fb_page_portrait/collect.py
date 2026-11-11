"""Bounded gated replica reads and a pinned verified social warehouse snapshot."""
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from .runtime import shared, literal
shared()
from features.social_analytics.warehouse_refresh import Reader, environment
from features.social_analytics.report import campaign_index, resolve
from features.social_analytics.backend_scope import eligible

CONFIG_DB = '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'
WAREHOUSE = '/mnt/data-disk/social-analytics-warehouse'


def configured(path=CONFIG_DB):
    with closing(sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True)) as db:
        return sorted({r[0] for r in db.execute('SELECT page_id FROM fb_video_page WHERE deleted=0')})


def social(scope, start, end, root=WAREHOUSE):
    root = Path(root)
    meta = json.loads((root/'current.json').read_text())
    path = (root/meta['database']).resolve()
    if not path.is_relative_to(root.resolve()): raise ValueError('invalid_warehouse_path')
    if meta.get('catalog_errors'): raise RuntimeError('social_catalog_incomplete')
    if (datetime.now(timezone.utc)-datetime.fromisoformat(meta['generated_at'])).total_seconds() > 10800:
        raise RuntimeError('social_snapshot_stale')
    days = {(date.fromisoformat(start)+timedelta(days=i)).isoformat() for i in range(7)}
    for site in ('2049','2310'):
        if not days.issubset(set(meta['coverage'].get('facebook:'+site, []))):
            raise RuntimeError('social_days_incomplete_'+site)
    with closing(sqlite3.connect('file:'+str(path)+'?mode=ro&immutable=1', uri=True)) as db:
        catalog = [json.loads(r[0]) for r in db.execute("SELECT payload FROM catalog")]
        catalog = [r for r in catalog if r['platform']=='facebook']
        # Resolve against the full catalog BEFORE filtering to visible Pages.
        index = campaign_index(catalog)
        result = []
        for record, in db.execute("SELECT payload FROM effects WHERE platform='facebook' AND dt BETWEEN ? AND ?", (start,end)):
            r = json.loads(record)
            key = ('facebook', str(r['site_id']), str(r['campaign_id']))
            candidates = index.get(key, [])
            row = resolve(candidates)
            if row is None: raise RuntimeError('verified_social_campaign_unresolved')
            pid = str(row['channel']).removeprefix('facebook:')
            if pid not in scope: continue
            if not any(eligible(c, r['date']) for c in candidates): raise RuntimeError('social_publication_date_mismatch')
            result.append(dict(page_id=pid, date=r['date'], content_id=row['drama'], language=row['language'],
                          revenue_cents=r['revenue_cents'], posts=sorted({p['id'] for c in candidates for p in c.get('publications',[]) if p['date']<=r['date']})))
    return result, meta['version']


def paid(reader, scope, start, end):
    result = []
    ids = ','.join(map(literal,scope))
    for i in range(7):
        day = (date.fromisoformat(start)+timedelta(days=i)).isoformat()
        sql = """SELECT HEX(JSON_OBJECT('page_id',page_id,'date',CAST(dt AS CHAR),
           'content_id',MIN(data_source_id),'language',MIN(COALESCE(NULLIF(drama_language,''),NULLIF(language,''),'unknown')),
           'spend',SUM(spend),'revenue',SUM(af_revenue0)))
           FROM kunlunads_dev.ads_custom_source_insight FORCE INDEX(pss)
           WHERE product='Dramawave' AND platform=0 AND dt='%s' AND page_id IN (%s)
           GROUP BY page_id,dt,BINARY data_source_id,BINARY COALESCE(NULLIF(drama_language,''),NULLIF(language,''),'unknown')""" % (day,ids)
        rows = reader.query([sql], timeout=60)
        if len(rows)>100000: raise RuntimeError('paid_day_overflow')
        result.extend(rows)
        print(json.dumps(dict(event='paid_day',day=day,rows=len(rows))),flush=True)
    return result


def collect(env_path, config_path=CONFIG_DB, warehouse=WAREHOUSE):
    env = environment(env_path)
    reader = Reader(env)
    # Complete report dates, aligned to the social warehouse's UTC day.
    end = datetime.now(timezone.utc).date()-timedelta(days=1)
    start = end-timedelta(days=6)
    scope = configured(config_path)
    if not scope: raise RuntimeError('no_configured_pages')
    raw = dict(start=str(start), end=str(end), page_ids=scope)
    raw['social'], raw['social_version'] = social(set(scope),str(start),str(end),warehouse)
    raw['paid'] = paid(reader,scope,str(start),str(end))
    ids = sorted({str(r['content_id']) for kind in ('paid','social') for r in raw[kind] if r.get('content_id')})
    for table,columns,key in [('ads_drama_resource',['content_id','app_id','language','series_code','labels'],'resources'),
                              ('ads_drama_info',['content_id','app_id','language','series_code','resource_type','name'],'infos')]:
        rows = []
        fields = ','.join("'%s',%s" % (c,c) for c in columns)
        for offset in range(0,len(ids),100):
            statement = 'SELECT DISTINCT HEX(JSON_OBJECT('+fields+')) FROM kunlunads_dev.'+table+' WHERE app_id=1479 AND content_id IN ('+','.join(map(literal,ids[offset:offset+100]))+') LIMIT 20001'
            batch = reader.query([statement],timeout=60)
            if len(batch)>20000: raise RuntimeError('drama_metadata_overflow')
            rows.extend(batch)
        raw[key] = rows
    if configured(config_path) != scope: raise RuntimeError('page_scope_changed_during_collection')
    return raw, env, reader
