#!/usr/bin/env python3
"""Independent read-only raw-cache reconciliation for the GG CPC/PIC+VID release.

Does not import generator, mapping, FX, or selection helpers. Warehouse mapping
and FX still require an additional live-source audit; this checks every cached
candidate, exclusions, selected set, metrics and frozen non-Google fields.
"""
import argparse
import collections
import contextlib
import hashlib
import json
import sqlite3
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path

from validate_v2_upgrade import (load_manifest, load_month, index_records,
    validate_metrics, assert_metric, exact_equal, require, MONTHS, APPS)


def cents(value):
    return int((value * 100).quantize(D('1'), rounding=ROUND_HALF_UP))


def assert_fields(record, expected, label):
    for field, value in expected.items():
        require(field in record and exact_equal(record[field], value), label + '.' + field + ' mismatch')


def missing_native(rows):
    amounts = collections.defaultdict(D)
    for row in rows:
        if row['usd_amount'] is None:
            amounts[row['currency'] or 'UNKNOWN'] += D(row['cost_micros']) / 1000000
    return {key: value.quantize(D('0.01'), rounding=ROUND_HALF_UP) for key, value in amounts.items()}


def raw_expected(connection, month):
    output = {}
    for app in APPS:
        facts = [dict(r) for r in connection.execute(
            'SELECT * FROM google_insight WHERE substr(dt,1,7)=? AND app=?', (month, app))]
        campaigns = [r for r in facts if r['row_type'] == 0]
        assets = [r for r in facts if r['row_type'] == 3 and r['asset_type'] in (2, 4)]
        campaign_days = {(r['dt'], r['account']) for r in campaigns}
        asset_days = {(r['dt'], r['account']) for r in assets
                      if D(r['cost_micros']) > 0 or r['impressions'] or r['clicks']}
        missing_days = asset_days - campaign_days
        spend = None if missing_days or any(r['usd_amount'] is None for r in campaigns) else cents(sum((D(r['usd_amount']) for r in campaigns), D(0)))
        campaign_clicks = None if missing_days else sum(r['clicks'] for r in campaigns)
        campaign_impressions = None if missing_days else sum(r['impressions'] for r in campaigns)
        clicks = sum(r['clicks'] for r in assets)
        impressions = sum(r['impressions'] for r in assets)
        groups = collections.defaultdict(list)
        for row in assets:
            if row['mapping_status'] == 'exact':
                groups[row['custom_source_id']].append(row)
        materials = {}
        incomplete = 0
        for ident, rows in groups.items():
            dim = connection.execute('SELECT product,material_type FROM material_dim WHERE custom_source_id=?', (ident,)).fetchone()
            require(dim and dim[0].casefold() == 'opay', 'invalid material dimension %s' % ident)
            require(all(r['asset_type'] == {1: 4, 2: 2}[dim[1]] for r in rows), 'invalid type %s' % ident)
            if any(r['usd_amount'] is None for r in rows):
                incomplete += 1
                continue
            materials[ident] = {'spend': cents(sum((D(r['usd_amount']) for r in rows), D(0))),
                'clicks': sum(r['clicks'] for r in rows), 'impressions': sum(r['impressions'] for r in rows),
                'conversions': sum((D(str(r['conversions'])) for r in rows), D(0)),
                'type': 'PIC' if dim[1] == 1 else 'VID',
                'source_rows': len(rows), 'asset_count': len({r['resource_id'] for r in rows}),
                'fx_sources': sorted({r['fx_status'] for r in rows})}
        eligible = sum(r['spend'] for r in materials.values())
        available = spend is not None and spend > 0 and bool(campaign_clicks) and eligible * 2 >= spend
        reason = ('平台月度USD消耗不完整' if spend is None else
                  '平台消耗或点击为0，CPC不可比较' if spend <= 0 or not campaign_clicks else
                  '美元完整且精确映射的素材消耗不足平台50%' if eligible * 2 < spend else '')
        top, ranks, cumulative = set(), {}, {}
        running = 0
        grouped = collections.defaultdict(list)
        for ident, material in materials.items():
            grouped[material['spend']].append(ident)
        if spend:
            for rank, amount in enumerate(sorted(grouped, reverse=True), 1):
                ids = grouped[amount]
                if running * 2 < spend:
                    top.update(ids)
                running += amount * len(ids)
                for ident in ids:
                    ranks[ident], cumulative[ident] = rank, D(running) / spend
        selected = {}
        for ident, row in materials.items():
            a = available and ident in top and row['clicks'] > 0 and row['spend'] * campaign_clicks < spend * row['clicks']
            b = row['spend'] > 500000 and row['impressions'] > 0 and (row['clicks'] > 0 if not impressions else row['clicks'] * impressions > clicks * row['impressions'])
            if a or b:
                selected[ident] = dict(row, rule='A+B' if a and b else 'A' if a else 'B',
                    a=bool(a), b=bool(b), in_top=bool(available and ident in top),
                    rank=ranks.get(ident), cumulative=cumulative.get(ident, D(0) if spend == 0 else None))
        status_counts = collections.Counter(r['mapping_status'] for r in assets)
        def status_spend(predicate):
            return cents(sum((D(r['usd_amount']) for r in assets
                              if r['usd_amount'] is not None and predicate(r['mapping_status'])), D(0)))
        exact_spend = status_spend(lambda status: status == 'exact')
        audit = {
            'status': 'success', 'rule_a_available': bool(available),
            'rule_a_metric': 'cpc', 'rule_a_unavailable_reason': reason,
            'metric_source': 'ads_google_insights:type=0',
            'rule_b_ctr_source': 'ads_google_insights:type=3,asset_type=2/4',
            'eligible_mapped_spend': D(eligible) / 100,
            'platform_spend': None if spend is None else D(spend) / 100,
            'exact_mapped_spend': D(exact_spend) / 100,
            'mapping_gap_spend': None if spend is None else D(max(0, spend - exact_spend)) / 100,
            'ambiguous_spend': D(status_spend(lambda status: status == 'ambiguous')) / 100,
            'out_of_scope_spend': D(status_spend(lambda status: status == 'out_of_scope')) / 100,
            'invalid_mapping_spend': D(status_spend(lambda status: status not in ('exact', 'ambiguous', 'out_of_scope'))) / 100,
            'fx_missing_rows': sum(r['usd_amount'] is None for r in assets),
            'platform_fx_missing_rows': sum(r['usd_amount'] is None for r in campaigns),
            'incomplete_material_count': incomplete, 'mapping_status_counts': dict(status_counts),
            'invalid_row_count': len(assets) - status_counts['exact'],
            'asset_count': len({r['resource_id'] for r in assets}),
            'baseline_missing_account_days': len(missing_days),
            'fx_missing_native_spend': missing_native(assets),
            'platform_fx_missing_native_spend': missing_native(campaigns),
            'picture_video_clicks': clicks, 'picture_video_impressions': impressions,
            'af_mapped': None, 'af_mapping_coverage': None,
        }
        output[app] = dict(selected=selected, available=bool(available), spend=spend,
            campaign_clicks=campaign_clicks, campaign_impressions=campaign_impressions,
            clicks=clicks, impressions=impressions, eligible=eligible, material_count=len(materials),
            audit=audit, coverage=None if spend is None else D(exact_spend) / spend if spend else D(0),
            reason=reason)
    return output


def table_digest(connection, table):
    # Legacy tables have stable schemas/PK column order; no GROUP BY shortcuts.
    count = len(connection.execute('PRAGMA table_info(%s)' % table).fetchall())
    digest = hashlib.sha256()
    for row in connection.execute('SELECT * FROM %s ORDER BY %s' % (table, ','.join(str(i+1) for i in range(count)))):
        digest.update(json.dumps(list(row), ensure_ascii=True, separators=(',', ':')).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def validate(baseline_dir, candidate_dir, cache_db, baseline_cache=None):
    baseline_dir, candidate_dir = Path(baseline_dir).resolve(), Path(candidate_dir).resolve()
    old, old_entries, old_sha = load_manifest(baseline_dir, 2, 'baseline')
    new, new_entries, new_sha = load_manifest(candidate_dir, 2, 'candidate')
    result = {'status': 'PASS', 'baseline_version': old['data_version'], 'candidate_version': new['data_version'], 'months': []}
    with contextlib.closing(sqlite3.connect(Path(cache_db).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'cache integrity')
        if baseline_cache:
            with contextlib.closing(sqlite3.connect(Path(baseline_cache).resolve().as_uri() + '?mode=ro', uri=True)) as old_db:
                for table in ('platform_daily', 'af_daily', 'material_daily', 'daily_audit', 'ads_source_dim'):
                    require(table_digest(old_db, table) == table_digest(db, table), 'frozen cache changed: ' + table)
        for month in MONTHS:
            require(db.execute('SELECT 1 FROM google_month_refresh WHERE month=?', (month,)).fetchone(), 'missing Google refresh: ' + month)
            before, _ = load_month(baseline_dir, old, old_entries[month], 2, 'baseline')
            after, _ = load_month(candidate_dir, new, new_entries[month], 2, 'candidate')
            require(after['selection_policy']['google']['version'] == 'cpc_picvid_v1', 'policy version')
            for field in ('rows', 'benchmarks', 'audits'):
                a, b = index_records(before, field, month, 'old'), index_records(after, field, month, 'new')
                a = {k: v for k, v in a.items() if k[0] != 'Google'}
                b = {k: v for k, v in b.items() if k[0] != 'Google'}
                require(exact_equal(a, b), 'all frozen non-Google fields changed: ' + month + '.' + field)
            for row in after['rows'] + after['benchmarks']:
                validate_metrics(row, month)
            expected = raw_expected(db, month)
            summary = {'month': month, 'google': [], 'preserved_non_google': sum(r['channel'] != 'Google' for r in after['rows'])}
            for app, exp in expected.items():
                actual = {r['custom_source_id']: r for r in after['rows'] if r['channel'] == 'Google' and r['app'] == app}
                require(set(actual) == set(exp['selected']), '%s %s selected set mismatch' % (month, app))
                audit = next(r for r in after['audits'] if r['channel'] == 'Google' and r['app'] == app)
                bench = next(r for r in after['benchmarks'] if r['channel'] == 'Google' and r['app'] == app)
                assert_fields(audit, exp['audit'], 'audit')
                require(audit['status'] == 'success', 'scope not refreshed successfully')
                assert_fields(audit, {'selected_count': len(actual)}, 'audit')
                assert_metric(audit['mapping_coverage'], exp['coverage'], 'audit mapping coverage', 8)
                require(bench['spend'] == (None if exp['spend'] is None else D(exp['spend']) / 100), 'campaign USD mismatch')
                require(bench['clicks'] == exp['campaign_clicks'] and bench['impressions'] == exp['campaign_impressions'], 'campaign facts mismatch')
                require(bench['picture_video_clicks'] == exp['clicks'] and bench['picture_video_impressions'] == exp['impressions'], 'PIC/VID benchmark mismatch')
                ctr = D(exp['clicks']) / exp['impressions'] if exp['impressions'] else D(0)
                assert_metric(bench['picture_video_ctr'], ctr, 'picture-video CTR', 8)
                platform_cpc = D(exp['spend']) / 100 / exp['campaign_clicks'] if exp['spend'] is not None and exp['campaign_clicks'] else None
                assert_metric(bench['cpc'], platform_cpc, 'platform CPC', 6)
                assert_metric(audit['picture_video_ctr'], ctr, 'audit picture-video CTR', 8)
                assert_metric(audit['platform_cpc'], platform_cpc, 'audit platform CPC', 6)
                for ident, row in actual.items():
                    ex = exp['selected'][ident]
                    require(row['spend'] == D(ex['spend']) / 100 and row['clicks'] == ex['clicks'] and row['impressions'] == ex['impressions'], 'material facts mismatch')
                    require(row['selection_rule'] == ex['rule'] and row['material_type'] == ex['type'], 'rule/type mismatch')
                    require(row['installs'] is None and row['af_d0_first_transactions'] is None, 'missing AF/install not null')
                    require(abs(D(str(row['platform_conversions'])) - ex['conversions']) < D('1e-8'), 'conversion mismatch')
                    evidence = row['evidence']
                    assert_fields(evidence, {
                        'rule_a_pass': ex['a'], 'rule_b_pass': ex['b'],
                        'rule_a_available': exp['available'], 'in_top_50_percent': ex['in_top'],
                        'spend_rank': ex['rank'], 'eligible_mapped_spend': D(exp['eligible']) / 100,
                        'rule_a_metric': 'cpc', 'rule_a_unavailable_reason': exp['reason'],
                        'mapping_status': 'exact', 'usd_status': 'verified',
                        'metric_source': 'ads_google_insights:type=3',
                        'af_status': 'missing_asset_attribution', 'installs_status': 'missing_asset_installs',
                        'platform_ctr_scope': 'google_picture_video_assets',
                        'picture_video_clicks': exp['clicks'], 'picture_video_impressions': exp['impressions'],
                        'source_row_count': ex['source_rows'], 'ad_day_count': ex['source_rows'],
                        'asset_count': ex['asset_count'], 'fx_sources': ex['fx_sources'],
                        'material_cpa': None, 'material_cpa_finite': None,
                        'platform_cpa_available': exp['spend'] is not None,
                        'platform_cpa_finite': None if exp['spend'] is None else bench['af_d0_first_transactions'] > 0,
                    }, 'evidence')
                    assert_metric(evidence['material_ctr'], D(ex['clicks']) / ex['impressions'] if ex['impressions'] else D(0), 'material CTR', 8)
                    assert_metric(evidence['exact_mapping_spend_coverage'], exp['coverage'], 'evidence mapping coverage', 8)
                    assert_metric(evidence['cumulative_spend_ratio'], ex['cumulative'], 'cumulative', 8)
                    assert_metric(evidence['platform_ctr'], ctr, 'CTR evidence', 8)
                    assert_metric(evidence['platform_cpc'], platform_cpc, 'CPC evidence', 6)
                    assert_metric(evidence['material_cpc'], D(ex['spend']) / 100 / ex['clicks'] if ex['clicks'] else None, 'material CPC', 6)
                summary['google'].append({'app': app, 'count': len(actual), 'types': dict(collections.Counter(r['material_type'] for r in actual.values())),
                    'rules': dict(collections.Counter(r['selection_rule'] for r in actual.values())),
                    'picture_video_ctr': str(ctr), 'a_available': exp['available'], 'eligible_materials': exp['material_count'],
                    'spend': str(sum((r['spend'] for r in actual.values()), D(0)))})
            result['months'].append(summary)
    require(hashlib.sha256((baseline_dir / 'latest.json').read_bytes()).hexdigest() == old_sha, 'baseline manifest drift')
    require(hashlib.sha256((candidate_dir / 'latest.json').read_bytes()).hexdigest() == new_sha, 'candidate manifest drift')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-dir', required=True)
    parser.add_argument('--candidate-dir', required=True)
    parser.add_argument('--cache-db', required=True)
    parser.add_argument('--baseline-cache')
    args = parser.parse_args()
    print(json.dumps(validate(**vars(args)), ensure_ascii=False))
