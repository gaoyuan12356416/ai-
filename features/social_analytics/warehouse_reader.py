"""Immutable serving copy of verified ads_ai facts; HTTP never queries source SQL."""
import json
import os
import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .adapters import CatalogErrors
from .backend_scope import restrict, SCOPE
from .report import ReportError, campaign_ownership, parameters, summarize

DEFAULT_ROOT = '/mnt/data-disk/social-analytics-warehouse'
_lock = threading.Lock()
_catalog_cache = {}
_ownership_lock = threading.Lock()
_ownership_cache = {}
_recent_lock = threading.Lock()
_recent = OrderedDict()


def configured_root(app):
    return app.get('SOCIAL_ANALYTICS_WAREHOUSE_ROOT') or os.environ.get('SOCIAL_ANALYTICS_WAREHOUSE_ROOT')


def current(root):
    try:
        manifest = json.loads((Path(root) / 'current.json').read_text(encoding='utf-8'))
        path = (Path(root) / manifest['database']).resolve()
        if path.parent != (Path(root) / 'snapshots').resolve() or path.suffix != '.sqlite3':
            raise ValueError('invalid_snapshot_path')
        return manifest, path
    except (OSError, ValueError, KeyError):
        raise ReportError('数仓报表尚未完成首次同步，请稍后刷新', 503) from None


def connect(path):
    db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=3)
    db.execute('PRAGMA query_only=ON')
    return db


def catalogs(root, platforms, snapshot=None):
    manifest, path = snapshot or current(root)
    key = str(path)
    with _lock:
        cached = _catalog_cache.get(key)
        if cached is None:
            with closing(connect(path)) as db:
                cached = [json.loads(row[0]) for row in db.execute('SELECT payload FROM catalog ORDER BY row_key')]
            _catalog_cache.clear()
            _catalog_cache[key] = cached
    errors = CatalogErrors()
    errors.update(manifest.get('catalog_errors', {}))
    errors.source_metadata = manifest.get('source_metadata', {})
    if 'tiktok' in platforms and 'tiktok:2250' not in manifest.get('coverage', {}):
        errors['tiktok:2250'] = 'TikTok 历史数据尚未完成首次同步'
    return [row for row in cached if row['platform'] in platforms], errors, manifest, path


def read_catalogs(app, platforms):
    catalog, errors, _, _ = catalogs(configured_root(app), platforms, app.get('SOCIAL_ANALYTICS_FROZEN_SNAPSHOT'))
    return catalog, errors


def scoped_ownership(path, version, platforms, catalog, end):
    # Only immutable, actor-independent catalog information may be shared.
    # Tenant, owner, visibility and report filters are still evaluated per request.
    key = (path, version, tuple(platforms), end)
    with _ownership_lock:
        if key not in _ownership_cache:
            scoped, _, _, _ = restrict(catalog, [], end)
            entry = scoped, campaign_ownership(scoped)
            _ownership_cache.clear()
            _ownership_cache[key] = entry
        return _ownership_cache[key]


def build_report(app, actor, query, *, include_titles=False, export=False):
    params = parameters(query)
    root = configured_root(app)
    snapshot = app.get('SOCIAL_ANALYTICS_FROZEN_SNAPSHOT') or current(root)
    from .prepared_views import pack_report, present, read_prepared
    view_key = None
    if not app.get('SOCIAL_ANALYTICS_BUILDING_VIEWS') and not actor.get('_system_report'):
        prepared = read_prepared(root, snapshot[0], actor, params, include_titles)
        if prepared is not None:
            return prepared
        now = datetime.now(timezone.utc)
        view_key = (snapshot[1], snapshot[0]['version'], now.date(), actor.get('tenant_key'), actor.get('role'),
                    actor.get('user_id'), params['start'], params['end'], tuple(params['groups']), params['search'],
                    tuple((k, tuple(sorted(v))) for k, v in params['filters'].items()), include_titles)
        with _recent_lock:
            payload = _recent.get(view_key)
            if payload is not None:
                _recent.move_to_end(view_key)
        if payload is not None:
            cached = present(json.loads(payload), snapshot[0], params, include_titles, now)
            if cached is not None:
                return cached
    catalog, errors, manifest, path = catalogs(root, params['platforms'], snapshot)
    # Retain global collision resolution before applying the actor's scope.
    catalog, ownership = scoped_ownership(path, manifest['version'], params['platforms'], catalog, params['end'])
    placeholders = ','.join('?' for _ in params['platforms'])
    with closing(connect(path)) as db:
        metrics = [json.loads(row[0]) for row in db.execute(
            'SELECT payload FROM effects WHERE dt BETWEEN ? AND ? AND platform IN (' + placeholders + ')',
            [params['start'], params['end']] + params['platforms'])]
        attach_titles = include_titles and 'drama' in params['groups']
        if attach_titles:
            title_values = {row[0]: json.loads(row[1]) for row in db.execute('SELECT content_id,payload FROM titles')}
    coverage = {key: [day for day in values if params['start'] <= day <= params['end']]
                for key, values in manifest['coverage'].items()}
    report = summarize(catalog, metrics, actor, params, manifest['generated_at'],
                       catalog_errors=errors, source_metadata=errors.source_metadata,
                       metric_coverage=coverage, iaa_coverage=manifest.get('iaa_coverage', {}), ownership=ownership)
    age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(manifest['generated_at'])).total_seconds())
    report['meta'].update(content_scope=SCOPE, data_source='ads_ai_hourly_warehouse',
                          warehouse_version=manifest['version'], refresh_interval_seconds=3600,
                          snapshot_age_seconds=round(age), snapshot_stale=age > 7200,
                          range_start=manifest['start'], range_end=manifest['end'])
    report['warnings'][:2] = [
        '按所选 UTC 日期统计历史内容产生的效果，成功发布按已确认的回执去重。',
        '数仓每小时更新。TikTok 按站点、campaign ID 和完整活动名匹配已发布短链；待归属效果单列在授权管理员的渠道总额内。' if 'tiktok' in params['platforms'] else
        '人员按冻结 campaign ID 归属，Facebook 同时核对冻结 Page 和素材 ID。数仓每小时更新。',
    ]
    if age > 7200:
        report['warnings'].append('数仓更新已延迟，当前显示上次成功同步的数据；请核对更新时间。')
    from .bill_income import FIRST_DAY
    expected = {source: set(dates) for source, dates in coverage.items()
                if source.split(':')[0] in params['platforms']}
    missing = {source: sorted(day for day in dates if day >= FIRST_DAY
                              and day not in manifest.get('iaa_coverage', {}).get(source, ()))
               for source, dates in expected.items()}
    report['meta']['iaa_missing_dates'] = {source: dates for source, dates in missing.items() if dates}
    tt_pending = set(missing.get('tiktok:2250', ()))
    if tt_pending:
        report['quality']['complete'] = False
        quality = report['quality']['platforms']['tiktok']
        for state in (quality, quality['sources']['2250']):
            if state['state'] == 'complete':
                state['state'] = 'partial'
        for row in [report['totals'], report['platform_totals']['tiktok'], quality['sources']['2250']['totals'],
                    *report['trend'], *report['all_rows'], *report['ranking']]:
            if ('tiktok' in row.get('platforms', params['platforms'])
                    and ('date' not in row or row['date'] in tt_pending)):
                row['iaa_pending'] = True
                if row['data_status'] == 'complete':
                    row['data_status'] = 'partial'
    if any(missing.values()):
        report['warnings'].append('部分站点或日期的 IAA 广告收入待回传。总收入包含已到的充值和广告收入。')
    if params['start'] < manifest['start'] or params['end'] > manifest['end']:
        report['warnings'].append('所选日期包含尚未入仓的日期，缺失部分不按零统计。')
    if attach_titles:
        for row in report['all_rows']:
            row.update(title_values.get(row['drama'], {'drama_english_name': '', 'drama_english_status': 'missing'}))
    if view_key is not None and len(report['all_rows']) <= 1500:
        payload = pack_report(report)
        if len(payload) <= 2 * 1024 * 1024:
            with _recent_lock:
                _recent[view_key] = payload
                _recent.move_to_end(view_key)
                while len(_recent) > 12:
                    _recent.popitem(last=False)
    return report
