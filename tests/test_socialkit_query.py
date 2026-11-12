import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

from features.socialkit_analytics import query, routes
from features.socialkit_analytics.schema import EFFECT_FIELDS, PUBLICATION_FIELDS, INTEGER_FIELDS, create_snapshot_schema


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'snapshots').mkdir()
        self.make_snapshot('v1', 1899)

    def tearDown(self):
        self.temp.cleanup()

    def make_snapshot(self, version, cents, publish=True):
        manifest = dict(version=version, database='snapshots/%s.sqlite3' % version, generated_at='2026-11-02T09:00:00Z', range={'start':'2026-10-31','end':'2026-11-01'}, coverage={kind:{'2026-10-31':'complete','2026-11-01':'complete'} for kind in ['effects','publications']})
        conn = sqlite3.connect(str(self.root / manifest['database']))
        create_snapshot_schema(conn)
        common = dict(date='2026-11-01',team_id=2,product='Dramawave',platform='tiktok',account='[2,"tiktok","a"]',account_name='=formula',external_account_id='a',drama='[1479,"abc"]',drama_name='剧 A',drama_app_id=1479,drama_content_id='abc')
        effect = {f:0 if f in INTEGER_FIELDS else '' for f in EFFECT_FIELDS}
        effect.update(common,site_id='2236',short_code='AbC',country='US',device_os='ios',material_type='ordinary',creation_source='automatic_material',matched=1,clicks=3,views=5,installs=1,conversions=1,revenue_cents=cents,dnu_revenue_cents=cents,dnu_conversions=1)
        self.insert(conn,'effects','effect',EFFECT_FIELDS,effect)
        unassigned = dict(effect,short_code='bad',account='',account_name='',external_account_id='',drama='',drama_name='',drama_content_id='',platform='unattributed',product='',team_id=0,matched=0,clicks=0,views=0,installs=1,conversions=0,revenue_cents=0,dnu_revenue_cents=0,dnu_conversions=0)
        self.insert(conn,'effects','unassigned',EFFECT_FIELDS,unassigned)
        for task in range(3):
            pub = {f:0 if f in INTEGER_FIELDS else '' for f in PUBLICATION_FIELDS}
            pub.update(common,task_id=task+1,language='EN',account_country='US',reason='ADS_MATERIAL',task_status=3,planned=1,success=1,tasks=1)
            self.insert(conn,'publications','task'+str(task),PUBLICATION_FIELDS,pub)
        conn.execute('INSERT INTO metadata VALUES (?,?)',('manifest',json.dumps(manifest)))
        conn.commit();conn.close()
        if publish:
            current=self.root/'current.json'
            if current.exists():
                (self.root/'previous.json').write_text(current.read_text(encoding='utf-8'),encoding='utf-8')
            current.write_text(json.dumps(manifest),encoding='utf-8')

    def insert(self,conn,table,key,fields,row):
        conn.execute('INSERT INTO '+table+' (row_key,'+','.join(fields)+') VALUES ('+','.join('?' for _ in range(len(fields)+1))+')',[key]+[row[f] for f in fields])

    def params(self,**extra):
        return dict(start='2026-11-01',end='2026-11-01',**extra)

    def add_publications(self, *variants):
        conn = sqlite3.connect(str(self.root / 'snapshots/v1.sqlite3'))
        conn.row_factory = sqlite3.Row
        original = dict(conn.execute('SELECT * FROM publications LIMIT 1').fetchone())
        for index, changes in enumerate(variants):
            row = dict(original, task_id=100+index, **changes)
            self.insert(conn, 'publications', 'extra'+str(index), PUBLICATION_FIELDS, row)
        conn.commit(); conn.close()

    def test_separate_facts_do_not_multiply_revenue(self):
        r=query.report(self.root,self.params())
        self.assertEqual(r['totals']['revenue'],18.99)
        self.assertEqual(r['totals']['success'],3)
        self.assertEqual(r['totals']['published_accounts'],1)
        self.assertEqual(r['totals']['installs'],2)
        self.assertEqual(len(r['rows']),2)
        self.assertEqual(sum(x['revenue'] for x in r['rows']),18.99)
        self.assertEqual(r['totals']['unmatched_rows'],1)
        self.assertEqual(r['totals']['unmatched_links'],1)
        self.assertEqual(r['totals']['unmatched_installs'],1)
        self.assertEqual(r['totals']['unmatched_revenue_cents'],0)

    def test_published_accounts_deduplicate_across_dates_products_dramas_and_names(self):
        self.add_publications(
            dict(date='2026-10-31', account_name='old name'),
            dict(product='HotDrama', drama_app_id=3302, drama='[3302,"other"]',
                 drama_content_id='other', drama_name='剧 B', account_name='new name'),
        )
        for view in ('overview', 'publications'):
            with self.subTest(view=view):
                result = query.report(self.root, dict(start='2026-10-31', end='2026-11-01',
                                      view=view, group_by=['product', 'drama']))
                self.assertEqual(result['totals']['published_accounts'], 1)
                self.assertEqual([r['published_accounts'] for r in result['trend']], [1, 1])
                rows = [r for r in result['rows'] if r['published_accounts']]
                self.assertEqual([r['published_accounts'] for r in rows], [1, 1])
                self.assertEqual(sum(r['published_accounts'] for r in result['breakdowns']['product']), 2)
        grouped = query.report(self.root, dict(start='2026-10-31', end='2026-11-01',
                               view='publications', group_by='account'))
        self.assertEqual(len(grouped['rows']), 1)
        self.assertEqual(grouped['rows'][0]['published_accounts'], 1)

    def test_published_accounts_use_identity_and_require_success_and_nonempty_account(self):
        self.add_publications(
            dict(account='[2,"tiktok","b"]', external_account_id='b'),
            dict(team_id=3, account='[3,"tiktok","a"]'),
            dict(platform='x', account='[2,"x","a"]', task_status=2),
            dict(account='[2,"tiktok","failed"]', external_account_id='failed', success=0, failed=1),
            dict(account='[2,"tiktok","planned"]', external_account_id='planned', success=0, task_status=1),
            dict(account='', external_account_id=''),
        )
        result = query.report(self.root, self.params(view='publications', group_by='platform'))
        self.assertEqual(result['totals']['published_accounts'], 4)
        self.assertEqual({r['platform']: r['published_accounts'] for r in result['rows']},
                         {'tiktok': 3, 'x': 1})
        empty = query.report(self.root, self.params(view='publications', account=''))
        self.assertEqual(empty['totals']['success'], 1)
        self.assertEqual(empty['totals']['published_accounts'], 0)

    def test_published_accounts_filters_sort_ranking_and_csv(self):
        self.add_publications(
            dict(platform='x', account='[2,"x","a"]', account_name='published X A', language='PT'),
            dict(platform='x', account='[2,"x","b"]', external_account_id='b',
                 account_name='published X B', language='PT'),
            dict(platform='x', account='[2,"x","c"]', external_account_id='c',
                 account_name='published X C', language='PT', success=0),
        )
        for view in ('overview', 'publications'):
            with self.subTest(view=view):
                result = query.report(self.root, self.params(view=view, group_by='platform',
                                      sort='published_accounts', ranking_metric='published_accounts'))
                self.assertEqual([(r['platform'], r['published_accounts']) for r in result['rows'][:2]],
                                 [('x', 2), ('tiktok', 1)])
                self.assertEqual(result['ranking'][0]['platform'], 'x')
                exported = list(csv.DictReader(io.StringIO(query.export_csv(result).decode('utf-8-sig'))))
                self.assertEqual([(r['平台'], r['发布账号数']) for r in exported[:2]], [('X', '2'), ('TikTok', '1')])
        filtered = query.report(self.root, self.params(view='publications', platform='x',
                                language='PT', search='published X B', group_by='account'))
        self.assertEqual(filtered['totals']['published_accounts'], 1)
        self.assertEqual(filtered['rows'][0]['external_account_id'], 'b')
        empty = query.report(self.root, self.params(view='publications', platform='absent'))
        self.assertEqual(empty['totals']['published_accounts'], 0)

    def test_published_accounts_are_not_an_effect_metric(self):
        result = query.report(self.root, self.params(view='effects'))
        self.assertIsNone(result['totals']['published_accounts'])
        self.assertTrue(all(r['published_accounts'] is None for r in result['rows'] + result['trend']))
        header = next(csv.reader(io.StringIO(query.export_csv(result).decode('utf-8-sig'))))
        self.assertNotIn('发布账号数', header)
        for param in ('sort', 'ranking_metric'):
            with self.subTest(param=param), self.assertRaises(query.QueryError):
                query.report(self.root, self.params(view='effects', **{param: 'published_accounts'}))

    def test_only_grouped_account_and_drama_labels_are_aggregated(self):
        conn, manifest, _ = query.snapshot(self.root)
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            parsed = query.parameters(self.params(view='effects'), manifest)
            for groups, expected_names in [(['country'], ()),
                    (['account'], ('account_name', 'external_account_id')),
                    (['account', 'drama'], ('account_name', 'external_account_id', 'drama_name', 'drama_content_id'))]:
                query._query(conn, 'effects', parsed, groups)
                sql = next(statement for statement in reversed(statements) if statement.startswith('SELECT'))
                for name in ('account_name', 'external_account_id', 'drama_name', 'drama_content_id'):
                    self.assertEqual('MIN(%s)' % name in sql, name in expected_names)
        finally:
            conn.close()
        report = query.report(self.root, self.params(view='effects', group_by=['account', 'drama']))
        assigned = next(row for row in report['rows'] if row['account'])
        self.assertEqual(assigned['labels'], {'account': '=formula', 'drama': '剧 A'})
        self.assertEqual(assigned['external_account_id'], 'a')
        self.assertEqual(assigned['drama_content_id'], 'abc')

    def test_request_reuses_group_aggregates_without_trend_mutation(self):
        for group in ('platform', 'date'):
            with patch.object(query, '_combined', wraps=query._combined) as combined:
                report = query.report(self.root, self.params(view='effects', group_by=[group]))
            group_calls = [tuple(call.args[2]) for call in combined.call_args_list]
            self.assertEqual(len(group_calls), 4)
            self.assertEqual(len(group_calls), len(set(group_calls)))
            self.assertTrue(all('source_status' not in row for row in report['all_rows']))
            self.assertTrue(all('source_status' not in row for row in report['ranking']))
            self.assertTrue(all('source_status' in row for row in report['trend']))

    def test_unmatched_request_cache_preserves_filtered_rows_links_and_metrics(self):
        conn = sqlite3.connect(str(self.root / 'snapshots/v1.sqlite3'))
        conn.row_factory = sqlite3.Row
        unmatched = dict(conn.execute("SELECT * FROM effects WHERE matched=0").fetchone())
        variants = [
            dict(date='2026-10-31', revenue_cents=100, clicks=4, installs=2),
            dict(site_id='999', country='CA', account_name='Needle', revenue_cents=200, clicks=3, installs=3),
            dict(site_id='999', country='CA', device_os='android', account_name='Needle', revenue_cents=300, clicks=5, installs=4),
        ]
        for index, change in enumerate(variants):
            self.insert(conn, 'effects', 'unmatched'+str(index), EFFECT_FIELDS, dict(unmatched, **change))
        conn.commit(); conn.close()
        scenarios = [
            ({}, (4, 2, 600, 10)),
            ({'country':['US']}, (2, 1, 100, 3)),
            ({'country':['CA'], 'device_os':['android']}, (1, 1, 300, 4)),
            ({'platform':['unattributed'], 'search':'needle'}, (2, 1, 500, 7)),
            ({'country':['absent']}, (0, 0, 0, 0)),
        ]
        for filters, expected in scenarios:
            params = dict(start='2026-10-31', end='2026-11-01', view='effects', group_by=['country','device_os'], **filters)
            report = query.report(self.root, params)
            totals = report['totals']
            self.assertEqual(tuple(totals[key] for key in ('unmatched_rows','unmatched_links','unmatched_revenue_cents','unmatched_installs')), expected)
            for key in ('unmatched_rows','unmatched_revenue_cents','unmatched_installs'):
                self.assertEqual(sum(row[key] for row in report['all_rows']), totals[key])
                self.assertEqual(sum(row.get(key, 0) for row in report['trend']), totals[key])
            # Distinct site+code keys must not be summed across country/device groups.
            if not filters:
                self.assertEqual(sum(row['unmatched_links'] for row in report['all_rows']), 3)
                self.assertEqual(totals['unmatched_links'], 2)

    def test_snapshot_pinning_keeps_old_export_after_switch(self):
        self.make_snapshot('v2',2500)
        old=query.report(self.root,self.params(version='v1'))
        new=query.report(self.root,self.params())
        self.assertEqual(old['totals']['revenue'],18.99)
        self.assertEqual(new['totals']['revenue'],25)
        self.assertEqual(old['meta']['latest_version'],'v2')
        self.assertIn('v1',query.export_csv(old).decode('utf-8-sig'))

    def test_missing_version_returns_conflict(self):
        with self.assertRaises(query.QueryError) as error:
            query.report(self.root,self.params(version='gone'))
        self.assertEqual(error.exception.status,409)

    def test_completed_but_unpublished_snapshot_cannot_be_read(self):
        self.make_snapshot('orphan',9999,publish=False)
        with self.assertRaises(query.QueryError) as error:
            query.report(self.root,self.params(version='orphan'))
        self.assertEqual(error.exception.status,409)
        self.assertEqual(query.report(self.root,self.params())['totals']['revenue'],18.99)

    def test_default_precomputed_reads_skip_fact_queries_and_refresh_failure_state(self):
        warmed=query.warm_snapshot(self.root,'v1')
        self.assertEqual(len(warmed['files']),2)
        (self.root/'status.json').write_text(json.dumps({'status':'failed','error':'new_failure'}))
        original_connect=sqlite3.connect
        class MetadataOnly:
            def __init__(self,connection):self.connection=connection
            @property
            def row_factory(self):return self.connection.row_factory
            @row_factory.setter
            def row_factory(self,value):self.connection.row_factory=value
            def execute(self,sql,*args):
                if ' FROM effects' in sql or ' FROM publications' in sql:
                    raise AssertionError('precomputed homepage must not query facts')
                return self.connection.execute(sql,*args)
            def close(self):self.connection.close()
        with patch.object(query.sqlite3,'connect',side_effect=lambda *a,**kw:MetadataOnly(original_connect(*a,**kw))):
            response=query.report(self.root,{})
            selectors=query.options(self.root)
        self.assertEqual(response['totals']['revenue'],18.99)
        self.assertEqual(response['totals']['published_accounts'],1)
        self.assertEqual(selectors['metrics']['published_accounts'],'发布账号数')
        self.assertTrue(response['meta']['stale'])
        self.assertEqual(selectors['refresh_status']['error'],'new_failure')

    def test_legacy_precomputed_without_published_accounts_falls_back_for_both_kinds(self):
        warmed = query.warm_snapshot(self.root, 'v1')
        def strip_metric(value):
            if isinstance(value, dict):
                value.pop('published_accounts', None)
                for nested in value.values():
                    strip_metric(nested)
            elif isinstance(value, list):
                for nested in value:
                    strip_metric(nested)
        for filename in warmed['files']:
            path = Path(filename)
            value = json.loads(path.read_text(encoding='utf-8'))
            value.pop('format_version')
            strip_metric(value['payload'])
            path.write_text(json.dumps(value), encoding='utf-8')
        query._CACHE.clear()
        query._OPTIONS_CACHE.clear()
        with patch.object(query, '_combined', wraps=query._combined) as combined:
            result = query.report(self.root, {})
        self.assertTrue(combined.called)
        self.assertEqual(result['totals']['published_accounts'], 1)
        self.assertTrue(all('published_accounts' in row for row in result['rows'] + result['trend']))
        self.assertEqual(query.options(self.root)['metrics']['published_accounts'], '发布账号数')

    def test_nondefault_parameters_do_not_use_precomputed_homepage(self):
        query.warm_snapshot(self.root,'v1')
        filtered=query.report(self.root,dict(platform='unattributed'))
        self.assertEqual(filtered['totals']['revenue'],0)
        self.assertEqual(filtered['totals']['installs'],1)
        self.assertEqual(filtered['parameters']['filters'],{'platform':['unattributed']})

    def test_warming_failure_preserves_published_version(self):
        before=(self.root/'current.json').read_bytes()
        with patch.object(query.os,'replace',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):query.warm_snapshot(self.root,'v1')
        self.assertEqual(before,(self.root/'current.json').read_bytes())
        self.assertEqual(query.report(self.root,{})['totals']['revenue'],18.99)
        self.assertEqual(list((self.root/'precomputed').glob('*.tmp')),[])

    def test_orphan_precomputed_files_never_bypass_publication_check(self):
        warmed=query.warm_snapshot(self.root,'v1')
        self.make_snapshot('orphan',9999,publish=False)
        cache=json.loads(Path(warmed['files'][0]).read_text(encoding='utf-8'))
        cache['version']='orphan';cache['payload']['meta']['version']='orphan'
        (self.root/'precomputed/orphan-overview.json').write_text(json.dumps(cache),encoding='utf-8')
        with self.assertRaises(query.QueryError) as error:
            query.report(self.root,{'version':'orphan'})
        self.assertEqual(error.exception.status,409)

    def test_zero_synced_day_and_missing_day_are_distinct(self):
        r=query.report(self.root,dict(start='2026-10-30',end='2026-11-01'))
        self.assertIsNone(r['trend'][0]['revenue'])
        self.assertEqual(r['trend'][1]['revenue'],0)
        self.assertIsNone(r['trend'][0]['published_accounts'])
        self.assertEqual(r['trend'][1]['published_accounts'],0)
        self.assertEqual(r['trend'][2]['published_accounts'],1)
        self.assertEqual(r['totals']['published_accounts'],1)
        self.assertTrue(r['meta']['partial'])
        self.assertEqual(r['totals']['revenue'],18.99)

    def test_entirely_missing_range_is_unknown_not_zero(self):
        r=query.report(self.root,dict(start='2026-10-29',end='2026-10-30'))
        self.assertIsNone(r['totals']['revenue'])
        self.assertIsNone(r['totals']['success'])
        self.assertIsNone(r['totals']['published_accounts'])

    def test_reject_cross_fact_dimension_in_overview(self):
        with self.assertRaises(query.QueryError):
            query.report(self.root,self.params(country='US'))
        with self.assertRaises(query.QueryError):
            query.report(self.root,self.params(group_by='country'))
        r=query.report(self.root,self.params(view='effects',country='US',group_by='country'))
        self.assertEqual(r['totals']['revenue'],18.99)
        self.assertIsNone(r['totals']['success'])

    def test_literals_never_become_sql(self):
        r=query.report(self.root,self.params(platform="tiktok' OR 1=1 --"))
        self.assertEqual(r['totals']['revenue'],0)
        with self.assertRaises(query.QueryError):
            query.report(self.root,self.params(group_by='revenue;DROP TABLE effects'))

    def test_csv_matches_groups_and_escapes_formulas(self):
        r=query.report(self.root,self.params(group_by='account'))
        rows=list(csv.reader(io.StringIO(query.export_csv(r).decode('utf-8-sig'))))
        self.assertEqual(len(rows),len(r['all_rows'])+1)
        self.assertEqual(rows[1][0],"'=formula")
        self.assertEqual(rows[1][-3],'v1')

    def test_failure_keeps_last_snapshot_and_marks_stale(self):
        query.report(self.root,self.params())
        (self.root/'status.json').write_text(json.dumps({'status':'failed','error':'source_timeout'}))
        r=query.report(self.root,self.params())
        self.assertTrue(r['meta']['stale'])
        self.assertEqual(r['totals']['revenue'],18.99)

    def test_query_does_not_mutate_sqlite(self):
        before=(self.root/'snapshots/v1.sqlite3').read_bytes()
        query.report(self.root,self.params())
        query.options(self.root)
        self.assertEqual(before,(self.root/'snapshots/v1.sqlite3').read_bytes())

    def test_metadata_labels_and_filters(self):
        opts=query.options(self.root)
        self.assertEqual(opts['options']['material_type'][0]['label'],'普通／无素材')
        r=query.report(self.root,self.params(view='publications',language='EN',group_by=['language','reason']))
        self.assertEqual(r['totals']['success'],3)
        self.assertIsNone(r['totals']['revenue'])

    def test_route_auth_and_method_and_no_all_rows_json(self):
        replies=[]
        app={'SOCIALKIT_ANALYTICS_ROOT':str(self.root),'json_response':lambda h,status,data,**kw:replies.append((status,data))}
        denied=SimpleNamespace(command='GET',_socialkit_analytics_actor=lambda:None)
        routes.dispatch(denied,urlparse('/api/socialkit-analytics/report'),app)
        self.assertEqual(replies,[])
        handler=SimpleNamespace(command='POST',_socialkit_analytics_actor=lambda:{'role':'admin'})
        routes.dispatch(handler,urlparse('/api/socialkit-analytics/report'),app)
        self.assertEqual(replies[-1][0],405)
        handler.command='GET'
        routes.dispatch(handler,urlparse('/api/socialkit-analytics/report?start=2026-11-01&end=2026-11-01'),app)
        self.assertEqual(replies[-1][0],200)
        self.assertNotIn('all_rows',replies[-1][1])


if __name__=='__main__':
    unittest.main()
