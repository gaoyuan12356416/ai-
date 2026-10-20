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
              'category', 'language', 'country', 'created_at', 'is_delete', 'user_id', 'initiator', 'designer', 'remark')
UPLOAD_LEDGER = '/mnt/data-disk/ai-cover-material-source/upload.sqlite3'


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


def canonical_language(value):
    """Match the upload contract while keeping this standalone report read-only."""
    value = str(value or '').strip().replace('_', '-').lower()
    aliases = {'zh-hant': 'zh-tw', 'zh-hans': 'zh-cn', 'zh': 'zh-cn',
               'jp': 'ja', 'kr': 'ko', 'in': 'id', 'vn': 'vi', 'cz': 'cs'}
    value = aliases.get(value, value)
    return value if value in ('zh-tw', 'zh-cn') else value.split('-')[0]


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
                unresolved.append(dict(id=mid, name=job.get('drama_name', ''), source='cover_synthesis', identity='screenshot:' + job['job_id'] + ':' + size, reason='已生成图片尚未确认正式入库'))
            else:
                # Multiple formal copies are real materials; keep each distinct ID once.
                for final in candidates.values():
                    assign(final, source_metadata('cover_synthesis', job.get('creator_name'), job.get('finished_at') or job['created_at'], job['content_id'], identity=job['job_id'] + ':' + size))
    for row in yt_rows:
        source = json.loads(row['source'])
        final = by_id.get(str(row['final_id']))
        if row['state'] != 'confirmed' or not final or final['type'] != 1 or final['data_source'] != 6 or str(final['data_source_id']) != str(source['content_id']):
            unresolved.append(dict(id=str(row.get('final_id') or row['material_id']), name=source.get('title', ''), source='youtube_cover', identity=row['key'], reason='YouTube 同步尚未确认正式入库'))
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


def read_upload_ledger(path):
    """Absent before rollout is compatible; present-but-unreadable is never zero."""
    try:
        Path(path).stat()
    except FileNotFoundError:
        return None
    db = readonly(path)
    try:
        rows = [dict(row) for row in db.execute(
            'SELECT key,source_json,request_json,state,api_id,final_id FROM uploads')]
    finally:
        db.close()
    for row in rows:
        row['source'] = json.loads(row.pop('source_json'))
        row['request'] = json.loads(row.pop('request_json') or '{}')
        if not isinstance(row['source'], dict) or not isinstance(row['request'], dict):
            raise ValueError('upload_ledger_invalid_json_object')
        if not row['source'].get('app_id'):
            raise ValueError('upload_ledger_missing_app_id')
    return rows


def merge_upload_lineage(mapped, unresolved, rows, finals, app_names):
    """Independently verify /material/source rows and alias explicit old IDs."""
    mapped = {key: dict(value) for key, value in mapped.items()}
    unresolved = list(unresolved)
    by_id = {str(row['id']): row for row in finals}
    by_sign = defaultdict(set)
    for row in by_id.values():
        if row.get('content_sign'):
            by_sign[row['content_sign']].add(str(row['id']))
    eligible, claims = [], defaultdict(set)
    for row in rows:
        source, request = row['source'], row['request']
        app_id = str(source['app_id'])
        if app_id not in app_names:
            continue
        kind = {'screenshot': 'cover_synthesis', 'youtube': 'youtube_cover'}.get(source.get('source_kind'))
        source_key = str(source.get('source_key') or '')
        final_id = str(row.get('final_id') or '')
        final = by_id.get(final_id)
        reason = ''
        if row['state'] != 'confirmed':
            reason = '新接口上传尚未确认正式入库：' + str(row['state'])
        elif (not kind or not source_key or not re.fullmatch('[0-9a-f]{64}', source.get('sha256', ''))
              or not isinstance(source.get('legacy_final_ids', []), list)):
            reason = '新接口来源身份或图片版本缺失'
        elif (not request.get('content_sign') or str(request.get('app_id')) != app_id
              or str(request.get('content_id') or '') != str(source.get('content_id') or '')
              or not source.get('content_id') or not request.get('category')
              or not all(request.get(key) for key in ('name', 'country', 'language'))
              or any(type(request.get(key)) is not int or request[key] <= 0
                     for key in ('user_id', 'initiator'))
              or (row.get('api_id') is not None
                  and (type(row['api_id']) is not int or row['api_id'] <= 0))):
            reason = '新接口请求与来源产品或剧目不一致'
        elif (not final or final['type'] != 1 or final['product'] != app_names[app_id]
              or str(final['data_source_id']) != str(source['content_id'])
              or final.get('content_sign') != request['content_sign']
              or by_sign[request['content_sign']] != {final_id}
              or final.get('category') != request.get('category')
              or final.get('name') != request['name']
              or any(str(final.get(key)) != str(request[key]) for key in ('user_id', 'initiator'))
              or canonical_language(final.get('language')) != canonical_language(request['language'])):
            reason = '新接口正式素材与签名、产品、剧目、名称、人员、语言或图片比例不一致'
        if reason:
            unresolved.append(dict(id=final_id or row['key'], name=source.get('name', ''),
                                   source=kind or 'unknown', identity=source_key, reason=reason))
            continue
        identity = source_key + ':' + source['sha256']
        legacy_ids = {str(value) for value in source.get('legacy_final_ids', [])}
        for key in legacy_ids | {final_id}:
            claims[key].add(identity)
        eligible.append((row, source, final, kind, identity, legacy_ids))
    resolved_sources = set()
    for row, source, final, kind, identity, legacy_ids in eligible:
        final_id = str(final['id'])
        # A legacy ID cannot prove two different image versions or source identities.
        if any(len(claims[key]) != 1 for key in legacy_ids | {final_id}):
            unresolved.append(dict(id=final_id, name=final['name'], source=kind,
                                   reason='新接口多个图片版本声明同一正式素材'))
            continue
        ancestors = [mapped[key] for key in legacy_ids if key in mapped]
        old_identity = source['source_key']
        if source['source_kind'] == 'screenshot':
            old_identity = old_identity.removeprefix('screenshot:')
        if any(item['source'] != kind or str(item.get('content_id')) != str(source['content_id'])
               or item.get('category') != final['category']
               or item['identity'] not in (old_identity, identity) for item in ancestors):
            unresolved.append(dict(id=final_id, name=final['name'], source=kind,
                                   reason='迁移旧素材的来源、剧目或图片比例不一致'))
            continue
        if final_id in mapped and final_id not in legacy_ids and mapped[final_id]['identity'] != identity:
            unresolved.append(dict(id=final_id, name=final['name'], source=kind,
                                   reason='新接口正式素材对应另一个来源身份'))
            continue
        for key in legacy_ids:
            if key in mapped:
                mapped[key]['identity'] = identity
            else:
                unresolved.append(dict(id=key, name=source.get('name', ''), source=kind,
                                       reason='迁移旧正式素材尚未验证来源，首次原图数不据此推算'))
        meta = source_metadata(kind, source.get('owner_name'), source.get('generated_at'),
                               source['content_id'], source.get('is_backfill', False), identity)
        migration = source.get('backfill_kind') == 'migration'
        # If the endpoint returns the already existing formal ID, it is not a new
        # migration ingestion and must retain its original provenance/date.
        existing = mapped.get(final_id)
        if existing and final_id in legacy_ids:
            meta['is_backfill'] = existing.get('is_backfill', False)
            migration = existing.get('backfill_kind') == 'migration'
        mapped[final_id] = dict(final, **meta, id=final_id, ratio=final['category'] or '未知',
                               api_version='material_source_v1',
                               backfill_kind='migration' if migration else '',
                               legacy_final_ids=sorted(legacy_ids),
                               # The API derives country from the drama catalog;
                               # its actual value (including blank/null) stays in final.
                               country_policy='drama_catalog_override',
                               requested_country=request['country'])
        resolved_sources.add(source['source_key'])
    unresolved = [item for item in unresolved if not (
        item.get('identity') in resolved_sources
        and item['reason'] in ('已生成图片尚未确认正式入库', 'YouTube 同步尚未确认正式入库'))]
    return mapped, unresolved


def collect_lineage(catalog, source_db, sync_db, upload_db=UPLOAD_LEDGER):
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
    upload_rows = read_upload_ledger(upload_db)
    if upload_rows is not None:
        upload_rows = [row for row in upload_rows if str(row['source']['app_id']) in app_ids]
        confirmed = [row for row in upload_rows if row['state'] == 'confirmed']
        new_finals = []
        for part in chunks(confirmed):
            ids = sorted({str(row.get('final_id') or '') for row in part} - {''})
            signs = sorted({row['request'].get('content_sign', '') for row in part} - {''})
            clauses = []
            if ids:
                clauses.append('id IN (' + ','.join(literal(value) for value in ids) + ')')
            if signs:
                clauses.append('content_sign IN (' + ','.join(literal(value) for value in signs) + ')')
            if clauses:
                new_finals += catalog.materials(' OR '.join(clauses))
        mapped, unresolved = merge_upload_lineage(mapped, unresolved, upload_rows, new_finals,
                                                   {str(row['id']): row['name'] for row in apps})
    return mapped, unresolved, app_names, dict(jobs=len(jobs),youtube_ledger=len(yt_rows),formal_images=len(mapped),
                                               upload_ledger_present=upload_rows is not None,
                                               upload_ledger=len(upload_rows) if upload_rows is not None else None)


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
            # An independent aggregate covers every verified ID regardless of its
            # product label. The old product pool remains for unknown-lineage audits.
            sqls.append("SELECT JSON_OBJECT('kind','verified_pool','dt'," + literal(day) +
                        ",'latest_updated',MAX(updated_at)," + fact_projection() +
                        ') FROM ads_custom_source_insight FORCE INDEX(idx_ridstype) WHERE ' + common +
                        ' AND source_type=3 AND resource_type=1 AND resource_id IN (' +
                        ','.join(literal(x) for x in part) + ')')
        sqls.append("SELECT JSON_OBJECT('kind','market','dt',dt,'platform',platform,'app_id',app_id,'resource_type',resource_type,'latest_updated',MAX(updated_at)," + fact_projection() + ') FROM ads_custom_source_insight FORCE INDEX(`as`) WHERE ' + common + ' GROUP BY dt,platform,app_id,resource_type')
        sqls.append("SELECT JSON_OBJECT('kind','pool','dt',dt,'id',resource_id,'latest_updated',MAX(updated_at)," + fact_projection() + ') FROM ads_custom_source_insight FORCE INDEX(pss) WHERE ' + common + ' AND product=' + literal('Drama-AI素材') + ' AND source_type=3 AND resource_type=1 GROUP BY dt,resource_id')
        rows = catalog.query(';'.join(sqls))
        if not any(r['kind'] == 'market' for r in rows):
            raise RuntimeError('market_date_missing_' + day)
        results += rows
        print(json.dumps({'phase':'daily_snapshot','date':day,'rows':len(rows)}), flush=True)
    return results
