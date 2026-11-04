#!/usr/bin/env python3
"""Read-only acceptance against the existing collector and the serving snapshot."""
import argparse
import csv
import io
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.social_analytics import routes, warehouse_reader
from features.social_analytics.report import FIELDS, COUNTS, csv_export
from features.social_analytics.warehouse_refresh import environment, atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=warehouse_reader.DEFAULT_ROOT)
    parser.add_argument('--day', default='2026-11-03')
    parser.add_argument('--range-start')
    parser.add_argument('--compare-live', action='store_true')
    args = parser.parse_args()
    env = environment('/root/drama_material_service/.env')
    env['SOCIAL_ANALYTICS_WAREHOUSE_ROOT'] = args.root
    cat, catalog_errors, manifest, _ = warehouse_reader.catalogs(args.root, ['youtube', 'x', 'facebook'])
    actor = dict(tenant_key='149bb4a14b57975f', user_id='acceptance-admin', role='admin')
    if not any(row['tenant'] == actor['tenant_key'] for row in cat):
        raise RuntimeError('expected_tenant_not_found')
    query = dict(start=[args.day], end=[args.day], group_by=['owner,platform'], sort=['clicks'], ranking_metric=['clicks'])
    outputs = {}
    for name in ['cold', 'warm']:
        begin = time.monotonic()
        with patch.object(routes, 'query_rows', side_effect=AssertionError('serving_path_queried_mysql')):
            result = routes.build_report(env, actor, query, ai_backend_only=True, include_titles=True)
        outputs[name + '_seconds'] = round(time.monotonic() - begin, 4)
    outputs['totals'] = result['totals']
    outputs['version'] = manifest['version']
    outputs['warehouse_tables'] = manifest['tables']
    if args.compare_live:
        with patch.object(warehouse_reader, 'configured_root', return_value=None), patch.object(
                routes, 'read_catalogs', return_value=(cat, catalog_errors)):
            routes._cache.clear()
            begin = time.monotonic()
            old = routes.build_report(env, actor, query, ai_backend_only=True)
            outputs['live_seconds'] = round(time.monotonic() - begin, 4)
        for field in FIELDS + COUNTS:
            if old['totals'][field] != result['totals'][field]:
                raise RuntimeError('live_parity_failed:' + field)
        outputs['live_parity'] = True
    exported = list(csv.DictReader(io.StringIO(csv_export(result).decode('utf-8-sig'))))
    assert len(exported) == result['pagination']['total']
    assert sum(int(row['落地页点击']) for row in exported) == result['totals']['clicks']
    owner = next(row['owner'] for row in result['all_rows'] if row.get('owner') and row.get('revenue_cents', 0))
    own = dict(actor, user_id=owner, role='user')
    filtered = dict(query, owner=[owner])
    admin_owner = warehouse_reader.build_report(env, actor, filtered)
    normal_owner = warehouse_reader.build_report(env, own, query)
    assert normal_owner['totals'] == admin_owner['totals']
    denied = warehouse_reader.build_report(env, dict(own, tenant_key='nonexistent-tenant'), query)
    assert denied['totals']['revenue_cents'] == 0 and not denied['rows']
    outputs['csv_and_scope_verified'] = True
    if args.range_start:
        begin = time.monotonic()
        multi = warehouse_reader.build_report(env, actor, dict(query, start=[args.range_start]))
        outputs['range_seconds'] = round(time.monotonic() - begin, 4)
        outputs['range'] = [args.range_start, args.day]
        outputs['range_totals'] = multi['totals']
    atomic_json(Path(args.root) / 'acceptance.json', outputs)
    print(json.dumps(outputs, ensure_ascii=False))


if __name__ == '__main__':
    main()
