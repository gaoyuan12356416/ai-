"""Three-product accounting: source owns output; actual delivery owns metrics."""
import datetime as dt
from collections import defaultdict
from decimal import Decimal

from analysis import totals, metrics, difference
from collector import FIELDS, ROOT_PRODUCTS


def same_totals(a, b, label):
    left, right = totals(a), totals(b)
    for key in FIELDS:
        if abs(left[key] - right[key]) > Decimal('0.000001'):
            raise ValueError('conservation_failed_' + label + '_' + key)
    return len(FIELDS)


def version_key(item):
    return (str(item['source_product_id']), item.get('identity') or 'formal:' + str(item['id']))


def first_versions(mapped):
    grouped = defaultdict(list)
    for item in mapped.values():
        grouped[version_key(item)].append(item)
    first = {}
    for key, rows in grouped.items():
        original = [row for row in rows if row.get('backfill_kind') != 'migration']
        if original:
            first[key] = min(original, key=lambda r: (str(r['created_at']), str(r['id'])))
    return first


def production(rows, start, end):
    owned = [r for r in rows if r.get('is_source_product', True)]
    fresh = [r for r in owned if start <= str(r['created_at'])[:10] <= end]
    migration = sum(r.get('backfill_kind') == 'migration' for r in fresh)
    historical = sum(bool(r.get('is_backfill')) and r.get('backfill_kind') != 'migration' for r in fresh)
    return dict(new_count=len(fresh), backfill_count=migration + historical,
                migration_count=migration, historical_backfill_count=historical,
                new_image_count=sum(bool(r.get('is_first_ingestion')) for r in fresh),
                dramas=len({(r['source_product_id'], r.get('content_id')) for r in fresh if r.get('content_id')}),
                total_asof=len(owned))


def make_groups(materials, rows, start, end, optimizer_names, period):
    groups = {}
    for group, field in [('sources', 'source'), ('owners', 'owner'), ('ratios', 'ratio'),
                         ('dramas', 'content_id'), ('languages', 'language')]:
        buckets = defaultdict(list)
        for material in materials:
            label = str(material.get(field) or '未知')
            if group == 'dramas':
                # Identical content IDs from independent product catalogs are not one drama.
                label = material['source_product_name'] + ' / ' + label
            buckets[label].append(material)
        groups[group] = []
        for label, entries in buckets.items():
            p = production(entries, start, end)
            data = [dict(r.get('week', {}) if period == 'week' else r, id=r['id']) for r in entries]
            groups[group].append(dict(name=label, count=p.pop('total_asof'), **p, **metrics(data)))
        groups[group].sort(key=lambda r: (-r['spend'], -r['new_count'], r['name']))
    optimizers = defaultdict(list)
    for row in rows:
        optimizers[str(row.get('optimizer') or 'unknown')].append(row)
    groups['optimizers'] = [dict(name=optimizer_names.get(k, k), count=len({r['id'] for r in entries}),
                                  new_count=None, new_image_count=None, backfill_count=None,
                                  migration_count=None, historical_backfill_count=None, **metrics(entries))
                            for k, entries in optimizers.items()]
    groups['optimizers'].sort(key=lambda r: (-r['spend'], r['name']))
    return groups


def observations(performance, output, other):
    lines = ['有消耗正式素材 %s 张；新增正式素材 %s 张，其中首次入库原图 %s 张。' %
             (performance['spending_materials'], output['new_count'], output['new_image_count'])]
    if performance['spend'] <= 0:
        lines.append('本期没有已确认 AI 图片消耗，暂无可比较的 ROAS。')
    elif other['spend'] > 0 and performance['roas'] is not None and other['roas'] is not None:
        delta = performance['roas'] - other['roas']
        lines.append('AI 图片 AF D0 ROAS %.2f%%，较匹配的其他图片%s %.2f 个百分点；这是表现差异，不代表因果增量。' %
                     (performance['roas'], '高' if delta >= 0 else '低', abs(delta)))
    else:
        lines.append('本期缺少相同应用、渠道的其他图片对照，不做效果优劣判断。')
    if output['migration_count']:
        lines.append('其中 %s 个正式 ID 来自接口迁移，未重复计入首次原图产出。' % output['migration_count'])
    return lines


def build_report_v3(mapped, unresolved, facts, report_date, generated_at, optimizer_names, snapshot_kind, scope):
    roots = {str(p['id']): p['name'] for p in scope['products']}
    if set(roots) != set(ROOT_PRODUCTS):
        raise ValueError('three_product_scope_required')
    app_to_root = {name: str(root) for name, root in scope['app_to_product'].items()}
    if set(app_to_root.values()) - set(roots):
        raise ValueError('invalid_delivery_product_scope')
    end = dt.date.fromisoformat(report_date)
    days = [(end - dt.timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
    start, previous_date = days[0], (end - dt.timedelta(days=1)).isoformat()
    mapped = {str(key): dict(item, id=str(key)) for key, item in mapped.items()}
    for item in mapped.values():
        root = str(item.get('source_product_id') or '')
        if root not in roots:
            raise ValueError('missing_source_product')
        item['source_product_id'], item['source_product_name'] = root, roots[root]
    first = first_versions(mapped)
    kinds = defaultdict(list)
    for fact in facts:
        if fact['dt'] not in days:
            raise ValueError('fact_date_outside_report')
        row = dict(fact)
        if row['kind'] in ('ai', 'market', 'pool', 'verified_pool'):
            if row.get('app_id') not in app_to_root:
                raise ValueError('unknown_delivery_app')
            row['delivery_product_id'] = app_to_root[row['app_id']]
        if row['kind'] == 'ai':
            row['id'] = str(row['id'])
            if row['id'] not in mapped:
                raise ValueError('unmapped_ai_fact')
            if str(mapped[row['id']]['created_at'])[:10] > row['dt'] and any(totals([row]).values()):
                raise ValueError('spend_precedes_formal_ingestion_' + row['id'])
        kinds[row['kind']].append(row)
    ai, market = kinds['ai'], kinds['market']
    checks = 0
    # Independent aggregate and legacy pool are checked at date/app/channel granularity.
    def cell(row):
        return row['dt'], row.get('app_id'), str(row.get('platform'))
    ai_cells, verified_cells, pool_cells = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in ai:
        ai_cells[cell(row)].append(row)
    for row in kinds['verified_pool']:
        verified_cells[cell(row)].append(row)
    for row in kinds['pool']:
        if str(row['id']) in mapped:
            pool_cells[cell(row)].append(row)
    for key in set(ai_cells) | set(verified_cells):
        checks += same_totals(ai_cells[key], verified_cells[key], 'independent_pool')
    for key in set(ai_cells) | set(pool_cells):
        checks += same_totals([r for r in ai_cells[key] if r.get('product') == 'Drama-AI素材'],
                              pool_cells[key], 'legacy_pool')
    # Explicit successful query coverage distinguishes empty data from a skipped date.
    if kinds['coverage'] and {r['dt'] for r in kinds['coverage'] if r.get('ok') == 1} != set(days):
        raise ValueError('date_coverage_incomplete')
    by_material = defaultdict(list)
    for row in ai:
        by_material[row['id']].append(row)
    base_materials = []
    for key, item in mapped.items():
        if str(item['created_at'])[:10] > report_date:
            continue
        public = {field: item.get(field, '') for field in (
            'id', 'name', 'url', 'source', 'owner', 'content_id', 'language', 'country',
            'requested_country', 'country_policy', 'ratio', 'created_at', 'generated_at',
            'is_backfill', 'is_delete', 'backfill_kind', 'legacy_final_ids',
            'source_product_id', 'source_product_name')}
        if public['url'].startswith('http://'):
            public['url'] = 'https://' + public['url'][7:]
        representative = first.get(version_key(item))
        public['is_first_ingestion'] = bool(representative and str(representative['id']) == key)
        public['first_ingestion_at'] = representative['created_at'] if representative else ''
        deliveries = sorted({r['delivery_product_id'] for r in by_material[key] if r['spend'] > 0})
        public['delivery_product_ids'] = deliveries
        public['delivery_product_names'] = [roots[r] for r in deliveries]
        base_materials.append(public)
    unknown_pool = defaultdict(list)
    for row in kinds['pool']:
        if str(row['id']) not in mapped:
            unknown_pool[(str(row['id']), row['delivery_product_id'])].append(row)
    pending = list(unresolved)
    for (key, root), rows in unknown_pool.items():
        today_rows = [r for r in rows if r['dt'] == report_date]
        pending.append(dict(id=key, source='unknown', name='', delivery_product_id=root,
                            delivery_product_name=roots[root], reason='AI 产品池存在投放但后台来源尚未确认',
                            **metrics(today_rows), week=metrics(rows)))
    views = {}
    for root, name in [('all', '三产品合计')] + list(roots.items()):
        select = lambda row: root == 'all' or row['delivery_product_id'] == root
        selected_ai = [r for r in ai if select(r)]
        selected_market = [r for r in market if select(r)]
        current = [r for r in selected_ai if r['dt'] == report_date]
        previous = [r for r in selected_ai if r['dt'] == previous_date]
        detail = []
        for item in base_materials:
            all_rows = by_material[item['id']]
            rows = [r for r in all_rows if select(r)]
            owned = root == 'all' or item['source_product_id'] == root
            if not owned and not rows:
                continue
            daily_rows = [r for r in rows if r['dt'] == report_date]
            entry = dict(item, is_source_product=owned, **metrics(daily_rows), week=metrics(rows))
            entry['optimizers'] = sorted({optimizer_names.get(str(r['optimizer']), str(r['optimizer'])) for r in daily_rows})
            entry['week']['optimizers'] = sorted({optimizer_names.get(str(r['optimizer']), str(r['optimizer'])) for r in rows})
            detail.append(entry)
        detail.sort(key=lambda r: (-r['spend'], -r['week']['spend'], r['id']))
        day_output, week_output = production(detail, report_date, report_date), production(detail, start, report_date)
        trend, other_days = [], []
        for date in days:
            daily_ai = [r for r in selected_ai if r['dt'] == date]
            daily_market = [r for r in selected_market if r['dt'] == date]
            cells = {(r['app_id'], r['platform']) for r in daily_ai if r['spend'] > 0}
            same = [r for r in daily_market if int(r['resource_type']) == 1 and (r['app_id'], r['platform']) in cells]
            excluded = [r for r in daily_ai if (r['app_id'], r['platform']) in cells]
            other = difference(same, excluded) if cells else metrics([])
            other_days.append(other)
            output = production(detail, date, date)
            trend.append(dict(dt=date, ai=metrics(daily_ai), market=metrics(daily_market),
                              other_images=other, uploads=output['new_count'],
                              migration_count=output['migration_count'], new_image_count=output['new_image_count']))
        groups = make_groups(detail, current, report_date, report_date, optimizer_names, 'yesterday')
        week_groups = make_groups(detail, selected_ai, start, report_date, optimizer_names, 'week')
        for grouped, rows, output in [(groups, current, day_output), (week_groups, selected_ai, week_output)]:
            for kind, entries in grouped.items():
                checks += same_totals(entries, rows, 'group_' + root + '_' + kind)
                if kind != 'optimizers':
                    for field in ('new_count', 'new_image_count', 'backfill_count', 'migration_count', 'historical_backfill_count'):
                        if sum(r[field] for r in entries) != output[field]:
                            raise ValueError('production_group_conservation_' + field)
                        checks += 1
        checks += same_totals(detail, current, 'materials_' + root)
        checks += same_totals([r['week'] for r in detail], selected_ai, 'weekly_materials_' + root)
        perf, weekly = metrics(current), metrics(selected_ai)
        other_week = metrics(other_days)
        view_pending = [r for r in pending if root == 'all' or str(r.get('source_product_id')) == root
                        or str(r.get('delivery_product_id')) == root or root in r.get('source_product_ids', [])]
        views[root] = dict(id=root, name=name, summary=dict(production=day_output, performance=perf),
                           comparisons=dict(previous=metrics(previous), market=trend[-1]['market'], other_images=trend[-1]['other_images']),
                           week=dict(production=week_output, performance=weekly,
                                     comparisons=dict(market=metrics(selected_market), other_images=other_week),
                                     groups=week_groups, insights=observations(weekly, week_output, other_week)),
                           trend=trend, groups=groups, materials=detail, unresolved=view_pending,
                           insights=observations(perf, day_output, trend[-1]['other_images']))
    for window in ('summary', 'week'):
        checks += same_totals([views[k][window]['performance'] for k in roots],
                              [views['all'][window]['performance']], 'product_total_' + window)
        for key in ('new_count', 'new_image_count', 'backfill_count', 'migration_count', 'historical_backfill_count', 'total_asof', 'dramas'):
            if sum(views[k][window]['production'][key] for k in roots) != views['all'][window]['production'][key]:
                raise ValueError('product_production_conservation_' + key)
            checks += 1
    for date in days:
        entries = [next(r for r in views[k]['trend'] if r['dt'] == date)['ai'] for k in roots]
        expected = [next(r for r in views['all']['trend'] if r['dt'] == date)['ai']]
        checks += same_totals(entries, expected, 'trend_products_' + date)
    latest = lambda rows: max((r.get('latest_updated') or '' for r in rows), default='')
    return dict(schema_version=3, report_date=report_date, generated_at=generated_at,
                timezone='Asia/Shanghai', currency='USD', period=[start, report_date], snapshot_kind=snapshot_kind,
                products=scope['products'], app_scope=scope, views=views,
                summary=views['all']['summary'], freshness=dict(ai_updated_at=latest(ai), market_updated_at=latest(market)),
                validation=dict(checks=checks, status='passed',
                                unknown_spending_materials=len({k[0] for k, rows in unknown_pool.items() if any(r['spend'] > 0 for r in rows)})),
                notes=[
                    '范围为三个主 App 及各自 W2A，排除 Minis；应用清单随每份报告冻结。',
                    '金额沿用后台美元口径，按日表 dt 汇总；北京时间生成，不重新换算广告账号当地自然日。',
                    '产出按来源产品、投放效果按实际应用归属；跨产品使用保留两个维度。合计素材数按正式 ID 去重，不能直接加总各产品使用数。',
                    '新增正式 ID 含迁移和历史补传；首次入库原图按来源产品和图片版本去重，不把迁移计为新原图，也不是生成张数。',
                    'AF D0 ROAS 为合计 AF D0 收入除以合计消耗，CTR/CPI 同样重算；零分母显示 —，不混加平台或 IAA 收入。',
                    '同类对照仅取各日 AI 有消耗的同产品、同应用、同渠道其他图片，排除全部已识别 AI 图片；差异不代表因果增量。',
                    '来源未确认、归属冲突单列待核对；新旧正式 ID 和已删除素材的真实历史效果均保留。',
                    '近 7 天每日重读归因数据，历史快照保持生成当时结果。首次生成不补造历史 09:00 快照。'])
