"""Read-only lineage and consistent daily advertising snapshots. Python 3.9."""
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))
FIELDS = ('spend', 'af_revenue_d0', 'impressions', 'clicks', 'installs', 'af_installs', 'purchases')
METRIC_SQL = {'spend': 'spend', 'af_revenue_d0': 'af_revenue0', 'impressions': 'impressions',
              'clicks': 'clicks', 'installs': 'installs', 'af_installs': 'af_installs', 'purchases': 'purchase'}
CAT_FIELDS = ('id', 'product', 'type', 'data_source', 'data_source_id', 'content_sign', 'name', 'url',
              'category', 'language', 'country', 'created_at', 'is_delete', 'initiator', 'designer', 'remark')


def literal(value):
    return "CONVERT(0x" + str(value).encode('utf-8').hex() + " USING utf8mb4) COLLATE utf8mb4_bin" if str(value) else "_utf8mb4'' COLLATE utf8mb4_bin"


def projection(fields):
    return ','.join("'%s',`%s`" % (k, k) for k in fields)


def chunks(values, size=250):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def local_time(value):
    if not value:
        return ''
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')


class Catalog:
    def __init__(self, env):
        self.env = env
        if env.get('ADMIN_MAPPING_MYSQL_HOST') != '101.32.56.53' or int(env.get('ADMIN_MAPPING_MYSQL_PORT', '63350')) != 63350:
            raise ValueError('read_replica_required')
        self.queries = 0

    def query(self, sql):
        process_env = os.environ.copy()
        process_env.pop('SQL_GATE_BYPASS', None)
        process_env.update(SQL_GATE_JOB_NAME='ai-image-daily', SQL_GATE_WAIT_TIMEOUT_SECONDS='90',
                           SQL_GATE_EXEC_TIMEOUT_SECONDS='100', SQL_GATE_RETRY_DELAYS_SECONDS='',
                           MYSQL_PWD=self.env['ADMIN_MAPPING_MYSQL_PASSWORD'])
        args = ['/usr/bin/mysql', '-h', '101.32.56.53', '-P', '63350', '-u', self.env['ADMIN_MAPPING_MYSQL_USER'],
                '--default-character-set=utf8mb4', '--connect-timeout=5', '--batch', '--raw', '--skip-column-names',
                'kunlunads_dev']
        # Check the server before running any data SQL, in this same gated session.
        guard = "SET SESSION MAX_EXECUTION_TIME=25000; SET SESSION time_zone='+08:00'; SELECT IF(@@read_only=1,1,JSON_EXTRACT('invalid_json','$')); "
        # The endpoint is fixed, statements themselves are SELECT-only and transaction read-only.
        script = guard + 'SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ; SET SESSION TRANSACTION READ ONLY; START TRANSACTION WITH CONSISTENT SNAPSHOT; ' + sql + '; COMMIT;'
        proc = subprocess.run(args, input=script, env=process_env, text=True, encoding='utf-8',
                              capture_output=True, timeout=200)
        self.queries += 1
        if proc.returncode:
            code = re.search(r'ERROR (\d+)', proc.stderr)
            raise RuntimeError('catalog_query_failed_%s_code_%s' % (self.queries,code.group(1) if code else 'unknown'))
        lines = proc.stdout.splitlines()
        if not lines or lines[0] != '1':
            raise RuntimeError('catalog_not_readonly')
        return [json.loads(line) for line in lines[1:] if line.strip()]

    def materials(self, where):
        return self.query('SELECT JSON_OBJECT(' + projection(CAT_FIELDS) + ') FROM ads_custom_source WHERE ' + where)


def source_metadata(source, owner, generated_at, content_id='', backfill=False, identity=''):
    return dict(source=source, owner=owner or '未映射', generated_at=local_time(generated_at),
                content_id=str(content_id or ''), is_backfill=bool(backfill), identity=identity)


def map_lineage(jobs, intakes, finals, yt_rows, ad_rows):
    """Only verified final images enter the cohort; ambiguous identities fail closed."""
    by_id = {str(r['id']): r for r in finals}
    by_name = defaultdict(list)
    by_sign = defaultdict(list)
    for row in by_id.values():
        if row['type'] == 1:
            by_name[row['name']].append(row)
            if row.get('content_sign'):
                by_sign[row['content_sign']].append(row)
    intake_ids = defaultdict(list)
    for row in intakes:
        intake_ids[str(row['data_source_id'])].append(row)
    mapped, unresolved, conflicts = {}, [], set()

    def assign(final, meta):
        key = str(final['id'])
        if key in conflicts:
            return
        if key in mapped and mapped[key]['identity'] != meta['identity']:
            conflicts.add(key)
            mapped.pop(key)
            unresolved.append(dict(id=key, name=final['name'], source=meta['source'], reason='正式素材对应多个来源身份'))
            return
        mapped[key] = dict(final, **meta, id=key, ratio=final['category'] or '未知')

    for job in jobs:
        for size in ('square_1x1', 'landscape_1_91x1', 'portrait_4x5'):
            if not job.get(size + '_url'):
                continue
            mid = str(int(hashlib.sha1((job['job_id'] + ':' + size).encode()).hexdigest()[:15], 16))
            candidates = {}
            for intake in intake_ids[mid]:
                # Modern importer stores the authoritative final ID in remark.
                direct = by_id.get(str(intake.get('remark', '')).strip())
                options = [direct] if direct else by_name.get(intake['name'], [])
                for final in options:
                    if final and final['type'] == 1 and final['data_source'] == 6 and str(final['data_source_id']) == str(job['content_id']) and final['name'] == intake['name'] and final['category'] == intake['category']:
                        candidates[str(final['id'])] = final
            if not candidates:
                unresolved.append(dict(id=mid, name=job.get('drama_name', ''), source='cover_synthesis', reason='已生成图片尚未确认正式入库'))
            else:
                # Multiple formal copies are real materials; keep each distinct ID once.
                for final in candidates.values():
                    assign(final, source_metadata('cover_synthesis', job.get('creator_name'), job.get('finished_at') or job['created_at'], job['content_id'], identity=job['job_id'] + ':' + size))
    for row in yt_rows:
        source = json.loads(row['source'])
        final = by_id.get(str(row['final_id']))
        if row['state'] != 'confirmed' or not final or final['type'] != 1 or final['data_source'] != 6 or str(final['data_source_id']) != str(source['content_id']):
            unresolved.append(dict(id=str(row.get('final_id') or row['material_id']), name=source.get('title', ''), source='youtube_cover', reason='YouTube 同步尚未确认正式入库'))
            continue
        assign(final, source_metadata('youtube_cover', source.get('owner_name'), source['asset_created_at'], source['content_id'], source.get('source_scope') != 'published', row['key']))
    for row in ad_rows:
        direct = by_id.get(str(row.get('source_api_id', '')).strip())
        options = [direct] if direct else by_sign.get(row['asset_id'], [])
        options = [r for r in options if r and r['type'] == 1 and r.get('content_sign') == row['asset_id']]
        if len(options) != 1:
            unresolved.append(dict(id=str(row.get('source_api_id') or row['asset_id']), name=row['name'], source='ad_material', reason='广告素材正式图片映射缺失或不唯一'))
        else:
            assign(options[0], source_metadata('ad_material', row.get('creator_name'), row['created_at'], identity=row['asset_id']))
    return mapped, unresolved


def collect_lineage(catalog, source_db, sync_db):
    apps = catalog.query("SELECT JSON_OBJECT('id',id,'name',name) FROM ads_apps_setting WHERE id=1479 OR ads_app_id=1479")
    if not any(r['id'] == 1479 for r in apps):
        raise ValueError('dramawave_root_missing')
    app_ids, app_names = {str(r['id']) for r in apps}, sorted({r['name'] for r in apps})
    db = readonly(source_db)
    try:
        placeholders = ','.join('?' for _ in app_ids)
        jobs = [dict(r) for r in db.execute('SELECT * FROM drama_screenshot_job WHERE app_id IN (' + placeholders + ')', sorted(app_ids))]
        ad_rows = [dict(r) for r in db.execute('SELECT a.*,t.creator_name,t.app_id FROM ad_material_asset a JOIN ad_material_task t ON t.task_id=a.task_id WHERE a.status=? AND t.app_id IN (' + placeholders + ')', ['uploaded'] + sorted(app_ids))]
    finally:
        db.close()
    db = readonly(sync_db)
    try:
        yt_rows = [dict(r) for r in db.execute('SELECT key,source,state,final_id,material_id FROM cover_sync')]
    finally:
        db.close()
    finals = catalog.materials('product=' + literal('Drama-AI素材') + ' AND type=1')
    mids = sorted({str(int(hashlib.sha1((j['job_id'] + ':' + size).encode()).hexdigest()[:15], 16)) for j in jobs for size in ('square_1x1','landscape_1_91x1','portrait_4x5') if j.get(size + '_url')})
    intakes = []
    for part in chunks(mids, 500):
        intakes += catalog.materials('data_source=102 AND data_source_id IN (' + ','.join(literal(x) for x in part) + ')')
    for part in chunks(ad_rows):
        if part:
            finals += catalog.materials('content_sign IN (' + ','.join(literal(x['asset_id']) for x in part) + ')')
    mapped, unresolved = map_lineage(jobs, intakes, finals, yt_rows, ad_rows)
    return mapped, unresolved, app_names, dict(jobs=len(jobs),youtube_ledger=len(yt_rows),formal_images=len(mapped))


def fact_projection():
    return ','.join("'%s',COALESCE(SUM(%s),0)" % (k, v) for k,v in METRIC_SQL.items())


def collect_days(catalog, ids, apps, dates):
    """Three daily views share one MVCC snapshot, avoiding live-update drift."""
    app_filter = ','.join(literal(x) for x in apps)
    results = []
    for day in dates:
        common = 'dt=' + literal(day) + ' AND app_id IN (' + app_filter + ')'
        sqls = []
        for part in chunks(sorted(ids), 500):
            sqls.append("SELECT JSON_OBJECT('kind','ai','dt',dt,'id',resource_id,'platform',platform,'app_id',app_id,'optimizer',optimizer,'product',product,'latest_updated',MAX(updated_at)," + fact_projection() + ') FROM ads_custom_source_insight FORCE INDEX(idx_ridstype) WHERE ' + common + ' AND source_type=3 AND resource_type=1 AND resource_id IN (' + ','.join(literal(x) for x in part) + ') GROUP BY dt,resource_id,platform,app_id,optimizer,product')
        sqls.append("SELECT JSON_OBJECT('kind','market','dt',dt,'platform',platform,'app_id',app_id,'resource_type',resource_type,'latest_updated',MAX(updated_at)," + fact_projection() + ') FROM ads_custom_source_insight FORCE INDEX(`as`) WHERE ' + common + ' GROUP BY dt,platform,app_id,resource_type')
        sqls.append("SELECT JSON_OBJECT('kind','pool','dt',dt,'id',resource_id,'latest_updated',MAX(updated_at)," + fact_projection() + ') FROM ads_custom_source_insight FORCE INDEX(pss) WHERE ' + common + ' AND product=' + literal('Drama-AI素材') + ' AND source_type=3 AND resource_type=1 GROUP BY dt,resource_id')
        rows = catalog.query(';'.join(sqls))
        if not any(r['kind'] == 'market' for r in rows):
            raise RuntimeError('market_date_missing_' + day)
        results += rows
        print(json.dumps({'phase':'daily_snapshot','date':day,'rows':len(rows)}), flush=True)
    return results
