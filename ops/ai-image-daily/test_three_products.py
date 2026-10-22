"""Business-level regression fixtures for the three-product report.

No production connections or writes are used. Facts deliberately separate the
material's source owner from the application where it actually spent money.
"""
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import collector
from analysis import build_report


DAY = '2026-10-21'
GENERATED = '2026-10-22T09:00:00+08:00'
PRODUCT_NAMES = {'1479': 'DramaWave', '2373': 'MoboShort', '3360': 'MoboReels'}


def app_rows():
    return [
        dict(id=1479, name='Dramawave', ads_app_id=0),
        dict(id=1723, name='[W2A]Dramawave', ads_app_id=1479),
        dict(id=2373, name='Moboshort', ads_app_id=0),
        dict(id=2374, name='[w2a]Moboshort', ads_app_id=2373),
        dict(id=3360, name='Moboreels', ads_app_id=0),
        dict(id=3361, name='W2aPage[3361]', ads_app_id=3360),
        dict(id=3346, name='DramawaveMinis', ads_app_id=1479),
        dict(id=979, name='Minis', ads_app_id=0),
        dict(id=980, name='[W2A]Minis', ads_app_id=979),
        dict(id=9000, name='OtherProduct', ads_app_id=0),
    ]


def material(identity, source_product='1479', created=DAY, image_identity=None, **extra):
    row = dict(id=str(identity), name='same drama title', url='https://example.com/image.jpg',
               source='cover_synthesis', owner='Owner', content_id='shared-drama-id',
               language='en', country='US', ratio='16:9', category='16:9',
               created_at=created + ' 10:00:00', generated_at='2026-10-01 10:00:00',
               is_backfill=False, is_delete=0, backfill_kind='', legacy_final_ids=[],
               product=PRODUCT_NAMES[source_product], type=1, data_source=6,
               data_source_id='shared-drama-id', content_sign='',
               identity=image_identity or 'image:' + str(identity),
               source_product_id=source_product, source_product_name=PRODUCT_NAMES[source_product])
    row.update(extra)
    return row


def fact(identity='1', app='Dramawave', day=DAY, spend=10, revenue=5,
         kind='ai', platform=0, **extra):
    row = dict(kind=kind, id=str(identity), dt=day, spend=spend, af_revenue_d0=revenue,
               impressions=spend * 100, clicks=spend * 5, installs=spend / 2,
               af_installs=spend / 2, purchases=spend / 5, product='Formal product',
               platform=platform, app_id=app, optimizer=123, resource_type=1,
               latest_updated='2026-10-22 08:45:00')
    row.update(extra)
    return row


def snapshot(ai_rows=(), market_rows=None, extra_rows=()):
    """Supply independently aggregated pool cells, plus an inclusive market."""
    ai_rows = [dict(row) for row in ai_rows]
    cells = defaultdict(list)
    for row in ai_rows:
        cells[(row['dt'], row['app_id'], row['platform'])].append(row)
    verified, pools, automatic_market = [], [], []
    for (day, app, platform), rows in cells.items():
        sums = {key: sum(Decimal(str(row.get(key, 0))) for row in rows)
                for key in collector.FIELDS}
        verified.append(dict(kind='verified_pool', dt=day, app_id=app,
                             platform=platform, **sums))
        automatic_market.append(fact(app=app, day=day, platform=platform, kind='market',
                                     **{key: float(value * 3) for key, value in sums.items()}))
        for row in rows:
            if row['product'] == 'Drama-AI素材':
                pools.append(dict(row, kind='pool'))
    return ai_rows + verified + pools + (
        automatic_market if market_rows is None else list(market_rows)) + list(extra_rows)


def report(mapped, rows=(), unresolved=()):
    return build_report(mapped, list(unresolved), list(rows), DAY, GENERATED,
                        {'123': 'Optimizer'}, scope=collector.resolve_scope(app_rows()))


class ScopeTests(unittest.TestCase):
    def test_roots_and_only_w2a_children_are_included(self):
        scope = collector.resolve_scope(app_rows())
        self.assertEqual({str(row['id']) for row in scope['products']}, set(PRODUCT_NAMES))
        self.assertEqual(set(scope['app_names']), {
            'Dramawave', '[W2A]Dramawave', 'Moboshort', '[w2a]Moboshort',
            'Moboreels', 'W2aPage[3361]'})
        self.assertEqual(str(scope['app_to_product']['[W2A]Dramawave']), '1479')
        self.assertEqual(str(scope['app_to_product']['W2aPage[3361]']), '3360')
        self.assertTrue(any(str(row['id']) == '3346' for row in scope['excluded_apps']))

    def test_scope_uses_relationship_not_name_similarity(self):
        rows = app_rows() + [dict(id=9001, name='[W2A]DramawaveExtra', ads_app_id=9000)]
        scope = collector.resolve_scope(rows)
        self.assertNotIn('[W2A]DramawaveExtra', scope['app_to_product'])

    def test_sql_root_alias_supported(self):
        rows = [dict(id=row['id'], name=row['name'], root=row['ads_app_id']) for row in app_rows()]
        self.assertEqual(collector.resolve_scope(rows)['app_to_product'],
                         collector.resolve_scope(app_rows())['app_to_product'])

    def test_missing_root_is_failure_not_empty_product(self):
        with self.assertRaises(ValueError):
            collector.resolve_scope([row for row in app_rows() if row['id'] != 2373])

    def test_same_application_name_cannot_belong_to_two_roots(self):
        rows = app_rows() + [dict(id=4000, name='[W2A]Dramawave', ads_app_id=3360)]
        with self.assertRaises(ValueError):
            collector.resolve_scope(rows)

    def test_same_application_id_with_conflicting_root_is_failure(self):
        rows = app_rows() + [dict(id=1723, name='[W2A]Dramawave', ads_app_id=2373)]
        with self.assertRaises(ValueError):
            collector.resolve_scope(rows)


class SourceOwnershipTests(unittest.TestCase):
    def test_same_formal_id_claimed_by_two_sources_is_unresolved(self):
        parts = [(dict(id=root, name=PRODUCT_NAMES[root]), {'1': material('1', root)}, [], {})
                 for root in ('1479', '2373')]
        mapped, unresolved, counts = collector.merge_product_lineages(parts)
        self.assertNotIn('1', mapped)
        self.assertTrue(any(str(row['id']) == '1' for row in unresolved))

    def test_different_formal_ids_keep_source_ownership(self):
        parts = [(dict(id=root, name=PRODUCT_NAMES[root]), {key: material(key, root)}, [], {})
                 for root, key in [('1479', '1'), ('2373', '2'), ('3360', '3')]]
        mapped, unresolved, counts = collector.merge_product_lineages(parts)
        self.assertEqual({key: str(row['source_product_id']) for key, row in mapped.items()},
                         {'1': '1479', '2': '2373', '3': '3360'})
        self.assertFalse(unresolved)

    def test_collects_three_product_upload_receipts_excluding_minis(self):
        from test_report import upload_fixture
        roots = [('1479', 'Dramawave'), ('2373', 'Moboshort'),
                 ('3360', 'Moboreels'), ('979', 'Minis')]
        uploads, finals = [], []
        for index, (root, name) in enumerate(roots, 1):
            row, final = upload_fixture(str(index + 10))
            row['key'] = 'source-' + root
            row['source'].update(app_id=int(root), source_key='screenshot:' + root + ':square_1x1')
            row['request']['app_id'] = int(root)
            final['product'] = name
            uploads.append(row)
            finals.append(final)
        class FakeCatalog:
            def query(self, sql):
                return app_rows()
            def materials(self, where):
                return finals if 'content_sign IN' in where else []
        with tempfile.TemporaryDirectory() as folder:
            source, sync, upload = (Path(folder, name) for name in ('source.db', 'sync.db', 'upload.db'))
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE drama_screenshot_job (app_id INTEGER)')
                db.execute('CREATE TABLE ad_material_asset (task_id TEXT,status TEXT)')
                db.execute('CREATE TABLE ad_material_task (task_id TEXT,creator_name TEXT,app_id INTEGER)')
            db.close()
            with sqlite3.connect(sync) as db:
                db.execute('CREATE TABLE cover_sync (key TEXT,source TEXT,state TEXT,final_id INTEGER,material_id TEXT)')
            db.close()
            with sqlite3.connect(upload) as db:
                db.execute('CREATE TABLE uploads (key TEXT,source_json TEXT,request_json TEXT,state TEXT,api_id INTEGER,final_id INTEGER)')
                db.executemany('INSERT INTO uploads VALUES (?,?,?,?,?,?)', [
                    (row['key'], json.dumps(row['source']), json.dumps(row['request']),
                     row['state'], row['api_id'], row['final_id']) for row in uploads])
            db.close()
            before = {path.name: path.read_bytes() for path in (source, sync, upload)}
            mapped, unresolved, scope, counts = collector.collect_lineage(
                FakeCatalog(), source, sync, upload, multi_product=True)
            self.assertEqual({key: row['source_product_id'] for key, row in mapped.items()},
                             {'11': '1479', '12': '2373', '13': '3360'})
            self.assertFalse(unresolved)
            self.assertEqual(counts['formal_images'], 3)
            self.assertNotIn('Minis', scope['app_names'])
            self.assertEqual(before, {path.name: path.read_bytes() for path in (source, sync, upload)})


class ProductAnalysisTests(unittest.TestCase):
    def test_schema_and_seven_complete_days(self):
        result = report({})
        self.assertEqual(result['schema_version'], 3)
        self.assertEqual(set(result['views']), {'all', '1479', '2373', '3360'})
        self.assertEqual(result['period'], ['2026-10-15', DAY])
        self.assertEqual([row['dt'] for row in result['views']['all']['trend']],
                         [(dt.date(2026, 10, 15) + dt.timedelta(days=i)).isoformat() for i in range(7)])

    def test_source_owns_production_delivery_app_owns_spend(self):
        result = report({'1': material('1')}, snapshot([fact(app='Moboshort', spend=12)]))
        source, delivery = result['views']['1479'], result['views']['2373']
        self.assertEqual(source['summary']['production']['new_count'], 1)
        self.assertEqual(source['summary']['performance']['spend'], 0)
        self.assertEqual(delivery['summary']['production']['new_count'], 0)
        self.assertEqual(delivery['summary']['performance']['spend'], 12)
        self.assertTrue(source['materials'][0]['is_source_product'])
        self.assertFalse(delivery['materials'][0]['is_source_product'])
        self.assertEqual(delivery['materials'][0]['source_product_id'], '1479')

    def test_w2a_spend_stays_with_its_root(self):
        result = report({'1': material('1', '3360')},
                        snapshot([fact(app='W2aPage[3361]', spend=7)]))
        self.assertEqual(result['views']['3360']['summary']['performance']['spend'], 7)
        self.assertEqual(result['views']['all']['summary']['performance']['spend'], 7)

    def test_amounts_add_but_cross_product_spending_ids_are_distinct(self):
        result = report({'1': material('1')}, snapshot([
            fact(app='Dramawave', spend=10, revenue=9),
            fact(app='Moboshort', spend=30, revenue=1)]))
        views = result['views']
        self.assertEqual(views['all']['summary']['performance']['spending_materials'], 1)
        self.assertEqual(sum(views[root]['summary']['performance']['spending_materials']
                             for root in PRODUCT_NAMES), 2)
        for key in collector.FIELDS:
            self.assertAlmostEqual(views['all']['summary']['performance'][key],
                                   sum(views[root]['summary']['performance'][key]
                                       for root in PRODUCT_NAMES))
        self.assertEqual(views['all']['summary']['performance']['roas'], 25)
        self.assertEqual(views['1479']['summary']['performance']['roas'], 90)

    def test_week_and_previous_day_recompute_from_actual_rows(self):
        result = report({'1': material('1', created='2026-10-15')}, snapshot([
            fact(day='2026-10-20', spend=40, revenue=10),
            fact(day=DAY, spend=10, revenue=15)]))
        view = result['views']['all']
        self.assertEqual(view['summary']['performance']['spend'], 10)
        self.assertEqual(view['comparisons']['previous']['spend'], 40)
        self.assertEqual(view['week']['performance']['spend'], 50)
        self.assertEqual(view['week']['performance']['roas'], 50)
        self.assertEqual(view['week']['performance']['spending_materials'], 1)
        self.assertEqual(view['materials'][0]['week']['spend'], 50)

    def test_source_version_migration_counts_once_keeps_old_and_new_spend(self):
        old = material('1', created='2026-10-20', image_identity='same-source-version',
                       product='Drama-AI素材', is_delete=1)
        new = material('2', image_identity='same-source-version', is_backfill=True,
                       backfill_kind='migration', legacy_final_ids=['1'],
                       api_version='material_source_v1')
        result = report({'1': old, '2': new}, snapshot([
            fact('1', spend=10, product='Drama-AI素材'), fact('2', spend=20)]))
        view = result['views']['1479']
        self.assertEqual(view['summary']['production']['new_count'], 1)
        self.assertEqual(view['summary']['production']['new_image_count'], 0)
        self.assertEqual(view['summary']['production']['migration_count'], 1)
        self.assertEqual(view['week']['production']['new_image_count'], 1)
        self.assertEqual(view['summary']['performance']['spend'], 30)
        self.assertEqual(view['summary']['performance']['spending_materials'], 2)
        self.assertEqual(next(row for row in view['materials'] if row['id'] == '1')['is_delete'], 1)

    def test_missing_migration_ancestor_does_not_invent_original(self):
        new = material('2', image_identity='version', is_backfill=True,
                       backfill_kind='migration', legacy_final_ids=['missing'])
        unresolved = [dict(id='missing', source_product_id='1479', source='cover_synthesis',
                           reason='迁移旧正式素材尚未验证来源，首次原图数不据此推算')]
        result = report({'2': new}, unresolved=unresolved)
        view = result['views']['1479']
        self.assertEqual(view['summary']['production']['new_image_count'], 0)
        self.assertEqual(view['summary']['production']['migration_count'], 1)
        self.assertEqual(view['materials'][0]['first_ingestion_at'], '')
        self.assertTrue(any(row['id'] == 'missing' for row in view['unresolved']))

    def test_history_backfill_is_new_formal_and_first_original(self):
        result = report({'1': material('1', '2373', source='youtube_cover',
                                      is_backfill=True, backfill_kind='historical')})
        production = result['views']['2373']['summary']['production']
        self.assertEqual(production['new_count'], 1)
        self.assertEqual(production['new_image_count'], 1)
        self.assertEqual(production['backfill_count'], 1)
        self.assertEqual(production['migration_count'], 0)
        self.assertEqual(production['historical_backfill_count'], 1)

    def test_same_drama_id_in_two_source_products_remains_two_dramas(self):
        result = report({'1': material('1'), '2': material('2', '2373')})
        all_view = result['views']['all']
        self.assertEqual(all_view['summary']['production']['dramas'], 2)
        self.assertEqual(len(all_view['groups']['dramas']), 2)
        self.assertEqual(result['views']['1479']['summary']['production']['dramas'], 1)
        self.assertEqual(result['views']['2373']['summary']['production']['dramas'], 1)

    def test_empty_spend_is_zero_with_undefined_ratios(self):
        result = report({'1': material('1', '3360')})
        view = result['views']['3360']
        self.assertEqual(view['summary']['production']['new_count'], 1)
        self.assertEqual(view['summary']['performance']['spend'], 0)
        for key in ('roas', 'ctr', 'cpi'):
            self.assertIsNone(view['summary']['performance'][key])

    def test_comparison_matches_each_day_product_app_and_channel(self):
        ai = [fact(app='[W2A]Dramawave', spend=10, revenue=4),
              fact(app='Moboshort', spend=20, revenue=5, platform=1)]
        market = [fact(kind='market', app='[W2A]Dramawave', spend=30, revenue=12),
                  fact(kind='market', app='Dramawave', spend=100, revenue=50),
                  fact(kind='market', app='[W2A]Dramawave', spend=300, revenue=100, platform=1),
                  fact(kind='market', app='Moboshort', spend=80, revenue=30, platform=1),
                  fact(kind='market', app='Moboshort', spend=900, revenue=500, resource_type=2),
                  fact(kind='market', app='Moboreels', spend=700, revenue=250)]
        result = report({'1': material('1')}, snapshot(ai, market))
        self.assertEqual(result['views']['1479']['comparisons']['other_images']['spend'], 20)
        self.assertEqual(result['views']['2373']['comparisons']['other_images']['spend'], 60)
        self.assertEqual(result['views']['3360']['comparisons']['other_images']['spend'], 0)
        self.assertEqual(result['views']['all']['comparisons']['other_images']['spend'], 80)

    def test_negative_comparison_is_data_failure_not_zero(self):
        with self.assertRaisesRegex(ValueError, 'negative_comparison'):
            report({'1': material('1')}, snapshot([fact(spend=10)], [fact(kind='market', spend=1)]))

    def test_unmapped_ai_id_is_failure(self):
        with self.assertRaises(ValueError):
            report({}, snapshot([fact('not-in-lineage')]))

    def test_out_of_scope_delivery_is_failure(self):
        for app in ('DramawaveMinis', 'Minis', 'Unknown app'):
            with self.subTest(app=app), self.assertRaises(ValueError):
                report({'1': material('1')}, snapshot([fact(app=app)]))

    def test_unknown_pool_is_visible_but_not_counted_as_verified_ai(self):
        unknown = fact('unknown', kind='pool', app='Moboshort', spend=50)
        result = report({'1': material('1')}, snapshot([fact()], extra_rows=[unknown]))
        self.assertEqual(result['views']['all']['summary']['performance']['spend'], 10)
        self.assertEqual(result['views']['2373']['summary']['performance']['spend'], 0)
        self.assertTrue(any(row['id'] == 'unknown' for row in result['views']['2373']['unresolved']))

    def test_independent_pool_mismatch_blocks_success(self):
        rows = snapshot([fact()])
        next(row for row in rows if row['kind'] == 'verified_pool')['spend'] += Decimal(1)
        with self.assertRaises(ValueError):
            report({'1': material('1')}, rows)

    def test_compensating_pool_errors_cannot_hide_behind_equal_global_total(self):
        rows = snapshot([fact(app='Dramawave', spend=10),
                         fact(app='Moboshort', spend=20)])
        independent = [row for row in rows if row['kind'] == 'verified_pool']
        independent[0]['spend'] += Decimal(1)
        independent[1]['spend'] -= Decimal(1)
        with self.assertRaisesRegex(ValueError, 'independent_pool'):
            report({'1': material('1')}, rows)

    def test_partial_successful_day_coverage_is_failure(self):
        rows = [dict(kind='coverage', dt=DAY, ok=1)]
        with self.assertRaisesRegex(ValueError, 'coverage'):
            report({}, rows)

    def test_proved_empty_week_is_valid_zero_spend(self):
        rows = [dict(kind='coverage', dt=(dt.date(2026, 10, 15) + dt.timedelta(days=i)).isoformat(), ok=1)
                for i in range(7)]
        result = report({}, rows)
        self.assertEqual(result['views']['all']['week']['performance']['spend'], 0)

    def test_all_group_axes_conserve_metrics(self):
        mapped = {'1': material('1'), '2': material('2', '2373', language='fr')}
        result = report(mapped, snapshot([fact('1', spend=11), fact('2', app='Moboshort', spend=17)]))
        for view in result['views'].values():
            for period, expected in [(view, view['summary']['performance']),
                                     (view['week'], view['week']['performance'])]:
                self.assertIn('languages', period['groups'])
                for axis, rows in period['groups'].items():
                    for key in collector.FIELDS:
                        with self.subTest(view=view['id'], axis=axis, metric=key):
                            self.assertAlmostEqual(sum(row[key] for row in rows), expected[key])


class DataReadFailureTests(unittest.TestCase):
    def test_catalog_failure_propagates_from_daily_collection(self):
        class BrokenCatalog:
            def query(self, sql):
                raise RuntimeError('replica_read_failed')
        with self.assertRaisesRegex(RuntimeError, 'replica_read_failed'):
            collector.collect_days(BrokenCatalog(), {'1': {}}, ['Dramawave'], [DAY])

    def test_absent_daily_market_does_not_publish_false_zero(self):
        class EmptyCatalog:
            def query(self, sql):
                return []
        with self.assertRaisesRegex(RuntimeError, 'market_date_missing'):
            collector.collect_days(EmptyCatalog(), {}, ['Dramawave'], [DAY])

    def test_multi_product_query_retains_independent_app_channel_cells(self):
        class EmptyButConfirmedCatalog:
            sql = ''
            def query(self, sql):
                self.sql = sql
                return [dict(kind='coverage', dt=DAY, ok=1)]
        catalog = EmptyButConfirmedCatalog()
        scope = collector.resolve_scope(app_rows())
        rows = collector.collect_days(catalog, {'1': {}}, scope, [DAY])
        self.assertEqual(rows, [dict(kind='coverage', dt=DAY, ok=1)])
        independent = next(sql for sql in catalog.sql.split(';') if "'kind','verified_pool'" in sql)
        self.assertIn('GROUP BY app_id,platform', independent)
        self.assertNotIn('product=', independent)
        pool = next(sql for sql in catalog.sql.split(';') if "'kind','pool'" in sql)
        self.assertIn('GROUP BY dt,resource_id,app_id,platform', pool)
        self.assertNotIn(collector.literal('DramawaveMinis'), catalog.sql)


if __name__ == '__main__':
    unittest.main()
