import base64
import json
import unittest
from types import SimpleNamespace

from features.fb_video_publish.sources import SourceRepository, TEXT_SCAN_IDS
from features.fb_video_publish.query_broker import _validate_sources


class Catalog:
    schema = 'kunlunads_dev'
    blacklist_schema = 'ads_setting'

    def __init__(self, identity='', ids=(), rows=(), bound=50000, dramas=()):
        self.identity, self.ids, self.rows, self.bound = identity, ids, rows, bound
        self.dramas = dramas
        self.calls = []

    def select(self, sql, params):
        _validate_sources(sql)
        self.calls.append((sql, params))
        if 'scan_bound' in sql:
            return [{'scan_bound': str(self.bound)}]
        if 'LIMIT 1' in sql and 'LIMIT 1001' not in sql:
            code = 'data_source_id=BINARY' in sql
            return [{'material_id': '1'}] if self.identity == ('content_id' if code else 'material_name') else []
        if 'DISTINCT CAST(ds.content_id' in sql:
            return [{'content_id': value} for value in self.ids] if self.identity == 'drama_name' else []
        if 'DISTINCT ds.app_id' in sql:
            return list(self.dramas)
        return list(self.rows)


def repository(catalog):
    repo = SourceRepository(None, SimpleNamespace(mysql=catalog))
    repo._material = lambda row, **kw: row
    repo._enrich = lambda rows, **kw: rows
    return repo


def decode(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))


class MaterialSearchTests(unittest.TestCase):
    def test_complete_code_uses_source_index_even_when_language_has_no_rows(self):
        cat = Catalog('content_id')
        result = repository(cat).search_materials({'search': 'YobABVO7m4', 'language': 'en'}, manual=True)
        self.assertEqual(result['search_kind'], 'content_id')
        sql, params = cat.calls[-1]
        self.assertIn('FORCE INDEX(idx_source_type_source_id)', sql)
        self.assertNotIn('LOCATE', sql)
        self.assertIn('en', params)
        self.assertFalse(result['has_more'])

    def test_complete_title_preserves_product_and_language_match(self):
        cat = Catalog('drama_name', ids=['abc', 'abc'])
        result = repository(cat).search_materials({'search': "The Dragon General's Fox husband"}, manual=True)
        self.assertEqual(result['search_kind'], 'drama_name')
        sql, params = cat.calls[-1]
        self.assertIn('s.data_source_id IN (%s)', sql)
        self.assertIn('ds.app_id=COALESCE', sql)
        self.assertIn('ds.language', sql)
        self.assertNotIn('LOCATE', sql)
        self.assertEqual(params.count('abc'), 1)

    def test_multiple_ids_use_primary_keys_and_do_not_search_names(self):
        cat = Catalog(rows=[{'material_id': '8376138'}, {'material_id': '8357684'}])
        result = repository(cat).search_materials({'search': '8376138 8357684,8376138'}, manual=True)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(len(cat.calls), 1)
        sql, params = cat.calls[0]
        self.assertIn('s.id IN (%s,%s)', sql)
        self.assertEqual(params, ['6', 8376138, 8357684, 51])

    def test_empty_text_chunk_continues_without_skipping_boundary(self):
        cat = Catalog()
        repo = repository(cat)
        first = repo.search_materials({'search': 'partial title'}, manual=True)
        self.assertTrue(first['search_incomplete'])
        cursor = decode(first['next_cursor'])
        self.assertEqual(cursor['before'], str(50000 - TEXT_SCAN_IDS + 1))
        cat.bound = 90000
        repo.search_materials({'search': 'partial title', 'cursor': first['next_cursor']}, manual=True)
        sql, params = cat.calls[-1]
        self.assertEqual(params[-3:-1], [50000 - 2 * TEXT_SCAN_IDS, 50000 - TEXT_SCAN_IDS])
        self.assertIn('s.id>%s', sql)

    def test_full_text_page_resumes_at_last_returned_match(self):
        cat = Catalog(rows=[{'material_id': str(value), 'material_name': 'partial title'} for value in (49990, 49980, 49970)])
        result = repository(cat).search_materials({'search': 'partial title', 'limit': 2}, manual=True)
        self.assertEqual(decode(result['next_cursor'])['before'], '49980')
        self.assertEqual([r['material_id'] for r in result['items']], ['49990', '49980'])

    def test_last_text_chunk_reports_complete(self):
        cat = Catalog(bound=TEXT_SCAN_IDS // 2)
        result = repository(cat).search_materials({'search': 'no match'}, manual=True)
        self.assertFalse(result['search_incomplete'])
        self.assertFalse(result['has_more'])

    def test_partial_drama_match_requires_same_app_and_language(self):
        rows = [dict(material_id=str(49999-i), content_id='a', source_app_id=app,
                     language=lang, material_name='clip')
                for i, (app, lang) in enumerate([(1479, 'en'), (3200, 'en'), (1479, 'ja')])]
        cat = Catalog(rows=rows, dramas=[dict(app_id='1479', content_id='a', language='en')])
        result = repository(cat).search_materials({'search': 'Dragon'}, manual=True, enforce_blacklist=False)
        self.assertEqual([row['material_id'] for row in result['items']], ['49999'])
        source_sql = next(sql for sql, _ in cat.calls if 'ORDER BY s.id DESC' in sql)
        self.assertNotIn('LOCATE', source_sql)
        self.assertNotIn('EXISTS', source_sql)


if __name__ == '__main__':
    unittest.main()
