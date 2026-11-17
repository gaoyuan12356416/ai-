"""Bounded gated replica reads and a pinned verified social warehouse snapshot."""
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from .runtime import shared, literal
shared()
from features.social_analytics.warehouse_refresh import environment
from features.meta_page_recovery.sources import Reader
from features.social_analytics.report import campaign_index, resolve
from features.social_analytics.backend_scope import eligible
from .classification import PRODUCTS

CONFIG_DB = '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'
WAREHOUSE = '/mnt/data-disk/social-analytics-warehouse'


def configured(path=CONFIG_DB):
    with closing(sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True)) as db:
        return sorted({r[0] for r in db.execute('SELECT page_id FROM fb_video_page WHERE deleted=0')})


def page_scope(reader, path=CONFIG_DB):
    rows = reader.query(["""SELECT DISTINCT HEX(JSON_OBJECT('page_id',i.page_id))
        FROM kunlunads_dev.ads_facebook_page_group_ins i
        JOIN kunlunads_dev.ads_facebook_page_group g ON g.id=i.group_id AND g.is_delete=0
        WHERE i.group_id IN (24,25,57) AND i.deleted_at=0 AND TRIM(i.page_id)<>'' LIMIT 10001"""])
    if len(rows)>10000:raise RuntimeError('page_scope_overflow')
    return sorted(set(configured(path)) | {str(r['page_id']) for r in rows})


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
            result.append(dict(page_id=pid, date=r['date'], app_id=1479, content_id=row['drama'], language=row['language'],
                          revenue_cents=r['revenue_cents'], posts=sorted({p['id'] for c in candidates for p in c.get('publications',[]) if p['date']<=r['date']})))
    return result, meta['version']


def series_ranges(rows, target=5000):
    """Collation-ordered half-open ranges cover new/empty series as well."""
    lower, count, ranges = None, 0, []
    for row in rows:
        if count >= target:
            ranges.append((lower, row['series_code']))
            lower, count = row['series_code'], 0
        count += int(row['rows'])
    ranges.append((lower, None))
    return ranges


def merge_paid(rows):
    merged = {}
    for row in rows:
        key = (row['page_id'], row['date'], row['app_id'], row['content_id'], row['language'])
        value = merged.setdefault(key, dict(row, spend=Decimal(0), revenue=Decimal(0)))
        value['spend'] += Decimal(str(row['spend']))
        value['revenue'] += Decimal(str(row['revenue']))
    return [dict(value, spend=str(value['spend']), revenue=str(value['revenue'])) for value in merged.values()]


def paid(reader, scope, start, end, cache=None):
    result = []
    ids = ','.join(map(literal,scope))
    text_literal = lambda value: '_utf8mb4 ' + literal(value)
    query=reader.query if cache is None else lambda statements,timeout=45:cache.query(reader,statements,timeout)
    for i in range(7):
        day = (date.fromisoformat(start)+timedelta(days=i)).isoformat()
        for app_id, product in PRODUCTS.items():
            counts = query(["SELECT HEX(JSON_OBJECT('series_code',series_code,'rows',COUNT(*))) "
                "FROM kunlunads_dev.ads_custom_source_insight FORCE INDEX(pss) "
                "WHERE product=%s AND dt=%s GROUP BY series_code ORDER BY series_code" % (text_literal(product),text_literal(day))],timeout=60)
            statements = []
            for lower, upper in series_ranges(counts):
                bounds = ('' if lower is None else ' AND series_code>='+text_literal(lower))
                if upper is not None:
                    bounds += (' AND (series_code<'+text_literal(upper)+' OR series_code IS NULL)'
                               if lower is None else ' AND series_code<'+text_literal(upper))
                statements.append("""SELECT HEX(JSON_OBJECT('page_id',page_id,'date',CAST(dt AS CHAR),'app_id',%d,
           'content_id',MIN(data_source_id),'language',MIN(COALESCE(NULLIF(drama_language,''),NULLIF(language,''),'unknown') COLLATE utf8mb4_bin),
           'spend',SUM(spend),'revenue',SUM(af_revenue0)))
           FROM kunlunads_dev.ads_custom_source_insight FORCE INDEX(pss)
           WHERE product=%s AND platform=0 AND dt=%s AND page_id IN (%s)%s
           GROUP BY page_id,dt,BINARY data_source_id,BINARY COALESCE(NULLIF(drama_language,''),NULLIF(language,''),'unknown')""" % (app_id,text_literal(product),text_literal(day),ids,bounds))
            # Bound each read independently. The old 25k-row / entire-day
            # batch repeatedly hit 3024 and discarded all preceding ranges.
            # A skewed series is subdivided by exact Page sets, without omitting
            # any Page or treating a timed-out range as empty.
            def read_partition(statement, ids_scope):
                try:
                    return query([statement],timeout=45)
                except Exception as exc:
                    if '3024' not in str(exc) or len(ids_scope)<=1:raise
                    middle=len(ids_scope)//2
                    result=[]
                    old_ids=','.join(map(literal,ids_scope))
                    for part in (ids_scope[:middle],ids_scope[middle:]):
                        child=statement.replace('page_id IN ('+old_ids+')','page_id IN ('+','.join(map(literal,part))+')')
                        if child==statement:raise RuntimeError('partition_not_narrowed')
                        result.extend(read_partition(child,part))
                    return result
            rows=[]
            for position,statement in enumerate(statements,1):
                print(json.dumps(dict(event='paid_partition',day=day,product=product,position=position,total=len(statements))),flush=True)
                rows.extend(read_partition(statement,scope))
            rows=merge_paid(rows)
            if len(rows)>100000: raise RuntimeError('paid_day_overflow')
            result.extend(rows)
            print(json.dumps(dict(event='paid_day',day=day,product=product,rows=len(rows),queries=len(statements))),flush=True)
    return result


def collect(env_path, config_path=CONFIG_DB, warehouse=WAREHOUSE):
    env = environment(env_path)
    reader = Reader(env)
    # Complete report dates, aligned to the social warehouse's UTC day.
    end = datetime.now(timezone.utc).date()-timedelta(days=1)
    start = end-timedelta(days=6)
    scope = page_scope(reader,config_path)
    if not scope: raise RuntimeError('no_configured_pages')
    raw = dict(start=str(start), end=str(end), page_ids=scope)
    raw['social'], raw['social_version'] = social(set(scope),str(start),str(end),warehouse)
    from .partition_cache import PartitionCache
    cache=PartitionCache(scope,str(start),str(end))
    raw['paid'] = paid(reader,scope,str(start),str(end),cache)
    raw['collection']=cache.metadata()
    for table,index,columns,key in [('ads_drama_resource','content_id',['content_id','app_id','language','series_code','labels'],'resources'),
                              ('ads_drama_info','ac',['content_id','app_id','language','series_code','resource_type','name'],'infos')]:
        rows = []
        fields = ','.join("'%s',%s" % (c,c) for c in columns)
        for app_id in PRODUCTS:
            ids = sorted({str(r['content_id']) for kind in ('paid','social') for r in raw[kind] if r.get('content_id') and r.get('app_id',1479)==app_id})
            for offset in range(0,len(ids),100):
                statement = 'SELECT DISTINCT HEX(JSON_OBJECT('+fields+')) FROM kunlunads_dev.'+table+' FORCE INDEX('+index+') WHERE app_id='+str(app_id)+' AND content_id IN ('+','.join(map(literal,ids[offset:offset+100]))+') LIMIT 20001'
                batch = reader.query([statement],timeout=60)
                if len(batch)>20000: raise RuntimeError('drama_metadata_overflow')
                rows.extend(batch)
        raw[key] = rows
    # This snapshot has an explicit frozen scope. Mid-run pool additions enter
    # the next refresh instead of invalidating all already collected portraits.
    return raw, env, reader
