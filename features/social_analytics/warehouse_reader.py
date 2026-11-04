"""Immutable serving copy of verified ads_ai facts; HTTP never queries source SQL."""
import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .adapters import CatalogErrors
from .backend_scope import restrict, SCOPE
from .report import ReportError, parameters, summarize

DEFAULT_ROOT = '/mnt/data-disk/social-analytics-warehouse'
_lock = threading.Lock()
_catalog_cache = {}


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


def catalogs(root, platforms):
    manifest, path = current(root)
    key = str(path)
    with _lock:
        cached = _catalog_cache.get(key)
    if cached is None:
        with closing(connect(path)) as db:
            cached = [json.loads(row[0]) for row in db.execute('SELECT payload FROM catalog ORDER BY row_key')]
        with _lock:
            _catalog_cache.clear()
            _catalog_cache[key] = cached
    errors = CatalogErrors()
    errors.update(manifest.get('catalog_errors', {}))
    errors.source_metadata = manifest.get('source_metadata', {})
    return [row for row in cached if row['platform'] in platforms], errors, manifest, path


def read_catalogs(app, platforms):
    catalog, errors, _, _ = catalogs(configured_root(app), platforms)
    return catalog, errors


def build_report(app, actor, query, *, include_titles=False, export=False):
    params = parameters(query)
    catalog, errors, manifest, path = catalogs(configured_root(app), params['platforms'])
    # Retain global collision resolution before applying the actor's scope.
    catalog, _, _, _ = restrict(catalog, [], params['end'])
    placeholders = ','.join('?' for _ in params['platforms'])
    with closing(connect(path)) as db:
        metrics = [json.loads(row[0]) for row in db.execute(
            'SELECT payload FROM effects WHERE dt BETWEEN ? AND ? AND platform IN (' + placeholders + ')',
            [params['start'], params['end']] + params['platforms'])]
        title_values = {row[0]: json.loads(row[1]) for row in db.execute('SELECT content_id,payload FROM titles')}
    coverage = {key: [day for day in values if params['start'] <= day <= params['end']]
                for key, values in manifest['coverage'].items()}
    report = summarize(catalog, metrics, actor, params, manifest['generated_at'],
                       catalog_errors=errors, source_metadata=errors.source_metadata,
                       metric_coverage=coverage)
    age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(manifest['generated_at'])).total_seconds())
    report['meta'].update(content_scope=SCOPE, data_source='ads_ai_hourly_warehouse',
                          warehouse_version=manifest['version'], refresh_interval_seconds=3600,
                          snapshot_age_seconds=round(age), snapshot_stale=age > 7200,
                          range_start=manifest['start'], range_end=manifest['end'])
    report['warnings'][:2] = [
        '仅统计 AI 后台已确认成功发布的内容及 YouTube 手动短链；包含历史内容在所选 UTC 日期产生的效果。',
        '数仓每小时更新一次；人员按冻结 campaign ID 归属，Facebook 同时核对冻结 Page 和素材 ID。',
    ]
    if age > 7200:
        report['warnings'].append('数仓更新已延迟，当前显示上次成功同步的数据；请核对更新时间。')
    if params['start'] < manifest['start'] or params['end'] > manifest['end']:
        report['warnings'].append('所选日期包含尚未入仓的日期，缺失部分不按零统计。')
    if include_titles and 'drama' in params['groups']:
        for row in report['all_rows']:
            row.update(title_values.get(row['drama'], {'drama_english_name': '', 'drama_english_status': 'missing'}))
    return report
