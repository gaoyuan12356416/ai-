"""Exercise ordering, filtering and full-pool uploader options with real SQL."""
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.youtube_auto_publish.source import MaterialSource, COLUMNS
from features.youtube_auto_publish.templates import WorkflowError


class MaterialPickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.create_function('LOCATE', 2, lambda needle, value: value.find(needle) + 1)
        self.db.execute('CREATE TABLE materials (' + ','.join(c + ' TEXT' for c in COLUMNS) + ')')
        for i in range(1, 131):
            row = dict.fromkeys(COLUMNS, '')
            row.update(id=str(i), name='Video ' + str(i), app_id='1479',
                       url='https://ai.yingliangads.com/video.mp4',
                       uploader_id='2' if i <= 20 else '1',
                       uploader_name='Uploader B' if i <= 20 else 'Uploader A')
            self.db.execute('INSERT INTO materials VALUES (' + ','.join('?' for _ in COLUMNS) + ')',
                            [row[c] for c in COLUMNS])
        self.queries = []
        def query(sql):
            self.queries.append(sql)
            # SQLite executes the actual predicate/order/limit. Adapt only the
            # MySQL UTF-8 literal syntax; no filtering or sorting in the fixture.
            sql = re.sub(r'CONVERT\(0x([0-9a-f]+) USING utf8mb4\)',
                         lambda m: "'" + bytes.fromhex(m[1]).decode().replace("'", "''") + "'", sql)
            return self.db.execute(sql).fetchall()
        self.path = Path(self.tmp.name) / 'source.sql'
        self.path.write_text('SELECT * FROM materials', encoding='utf-8')
        self.source = MaterialSource(self.path, query)

    def test_numeric_descending_order_before_limit(self):
        rows = self.source.list()['items']
        self.assertEqual([int(row['id']) for row in rows], list(range(130, 30, -1)))

    def test_uploader_beyond_first_page_is_listed_and_filterable(self):
        result = self.source.list()
        self.assertEqual({u['id'] for u in result['uploaders']}, {'1', '2'})
        self.assertEqual({m['uploader_id'] for m in result['items']}, {'1'})
        filtered = self.source.list(uploader_id='2')
        self.assertEqual([int(m['id']) for m in filtered['items']], list(range(20, 0, -1)))
        self.assertEqual(filtered['uploaders'], result['uploaders'])

    def test_keyword_uploader_intersection_cache_and_clear(self):
        self.assertEqual([m['id'] for m in self.source.list('Video 12', uploader_id='2')['items']], ['12'])
        count = len(self.queries)
        self.source.list('Video 12', uploader_id='2')
        self.assertEqual(len(self.queries), count)
        self.assertEqual([m['id'] for m in self.source.list('Video 12', uploader_id='1')['items']],
                         [str(i) for i in range(129, 119, -1)])
        self.assertEqual(len(self.source.list('Video 12')['items']), 11)
        self.assertEqual(self.source.list('missing', uploader_id='2')['items'], [])

    def test_invalid_uploader_cannot_reach_query(self):
        for value in ["1' OR 1=1", '-1', '1.0', '01', '1' * 20]:
            with self.subTest(value=value), self.assertRaises(WorkflowError) as error:
                self.source.list(uploader_id=value)
            self.assertEqual(error.exception.code, 'invalid_uploader_id')
        self.assertEqual(self.queries, [])

    def test_refresh_updates_options_and_missing_name_fallback(self):
        self.source.list()
        self.db.execute("UPDATE materials SET uploader_id='0',uploader_name='' WHERE id='130'")
        result = self.source.list(refresh=True, uploader_id='0')
        self.assertEqual([m['id'] for m in result['items']], ['130'])
        self.assertTrue(next(u['name'] for u in result['uploaders'] if u['id'] == '0'))

    def test_selected_material_get_remains_fresh_and_filter_independent(self):
        self.source.list(uploader_id='2')
        self.assertEqual(self.source.get('130')['uploader_id'], '1')
        self.db.execute("DELETE FROM materials WHERE id='130'")
        with self.assertRaises(WorkflowError) as error:
            self.source.get('130')
        self.assertEqual(error.exception.code, 'material_unavailable')


if __name__ == '__main__':
    unittest.main()
