import json
import sqlite3
import unittest

from features.socialkit_analytics import drama_language, query
from features.socialkit_analytics.schema import stable_key
from tests.test_socialkit_query import QueryTests


class Reader:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def records(self, statements):
        self.statements.extend(statements)
        yield from self.rows


class DramaLanguageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = QueryTests('test_separate_facts_do_not_multiply_revenue')
        self.fixture.setUp()
        self.root = self.fixture.root

    def tearDown(self):
        self.fixture.tearDown()

    def test_exact_product_case_alias_conflicts_and_missing(self):
        keys = [(1479, 'Same'), (3302, 'Same'), (1479, 'same'), (1479, "missing'--")]
        reader = Reader([
            dict(app_id=1479, content_id='Same', language='JP'),
            dict(app_id=1479, content_id='Same', language='ja'),
            dict(app_id=3302, content_id='Same', language='es'),
            dict(app_id=3302, content_id='Same', language='en'),
            dict(app_id=979, content_id='Same', language='ko'),
        ])
        result = drama_language.collect_languages(reader, keys)
        self.assertEqual(result, {stable_key(1479, 'Same'): 'ja', stable_key(3302, 'Same'): '__conflict__',
                                  stable_key(1479, 'same'): '', stable_key(1479, "missing'--"): ''})
        self.assertTrue(all('FORCE INDEX(ac)' in sql for sql in reader.statements))
        self.assertNotIn("missing'--", ''.join(reader.statements))

    def test_group_filter_csv_and_snapshot_pin_preserve_facts(self):
        before = query.report(self.root, self.fixture.params())
        result = drama_language.enrich_snapshot(self.root, Reader([
            dict(app_id=1479, content_id='abc', language='JA'),
            dict(app_id=1479, content_id='abc', language='ja'),
        ]))
        for view in ['overview', 'effects', 'publications']:
            with self.subTest(view=view):
                report = query.report(self.root, self.fixture.params(view=view, group_by=['platform', 'drama_language']))
                metric = 'success' if view == 'publications' else 'revenue_cents'
                self.assertEqual(sum(row[metric] for row in report['all_rows']), before['totals'][metric])
                known = next(row for row in report['all_rows'] if row['drama_language'] == 'ja')
                self.assertEqual(known['labels']['drama_language'], '日语')
                csv = query.export_csv(report).decode('utf-8-sig')
                self.assertIn('剧语言', csv)
                self.assertIn('日语', csv)
                self.assertIn(result['version'], csv)
        filtered = query.report(self.root, self.fixture.params(drama_language=['ja'], group_by=['drama_language']))
        self.assertEqual(filtered['totals']['revenue_cents'], 1899)
        self.assertEqual(filtered['totals']['success'], 3)
        self.assertEqual(filtered['totals']['installs'], 1)
        missing = query.report(self.root, self.fixture.params(view='effects', drama_language=[''], group_by=['drama_language']))
        self.assertEqual(missing['totals']['unmatched_installs'], 1)
        self.assertEqual(missing['rows'][0]['labels']['drama_language'], '未提供')
        old = query.report(self.root, self.fixture.params(version='v1', group_by=['drama_language']))
        self.assertEqual(old['rows'][0]['drama_language'], '')
        self.assertEqual(old['totals'], before['totals'])
        self.assertIn('drama_language', query.options(self.root)['allowed_dimensions']['effects'])

    def test_failed_metadata_read_preserves_previous_values(self):
        conn = sqlite3.connect(self.root / 'snapshots/v1.sqlite3')
        conn.execute('INSERT INTO drama_languages VALUES(?,?)', (stable_key(1479, 'abc'), 'ja'))
        conn.commit()
        state = drama_language.update_languages(conn, object(), {'updated_at': 'earlier'})
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['updated_at'], 'earlier')
        self.assertEqual(conn.execute('SELECT drama_language FROM drama_languages').fetchone()[0], 'ja')
        conn.close()

    def test_legacy_snapshot_and_old_precomputed_options_are_readable(self):
        query.warm_snapshot(self.root, 'v1')
        path = self.root / 'precomputed/v1-options.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['payload']['allowed_dimensions']['overview'].remove('drama_language')
        path.write_text(json.dumps(payload), encoding='utf-8')
        query._OPTIONS_CACHE.clear()
        conn = sqlite3.connect(self.root / 'snapshots/v1.sqlite3')
        conn.execute('DROP TABLE drama_languages'); conn.close()
        self.assertIn('drama_language', query.options(self.root)['allowed_dimensions']['overview'])
        report = query.report(self.root, self.fixture.params(group_by=['drama_language']))
        self.assertEqual(report['totals']['revenue_cents'], 1899)
        self.assertEqual(report['rows'][0]['labels']['drama_language'], '未提供')


if __name__ == '__main__':
    unittest.main()
