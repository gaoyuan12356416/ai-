import json
import re
import sqlite3
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from features.youtube_auto_publish.source import MaterialSource
from features.youtube_auto_publish.templates import WorkflowError


class UploaderIndexTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.create_function('HEX', 1, lambda value: value.encode().hex())
        self.db.execute('CREATE TABLE materials (id INTEGER PRIMARY KEY, user_id INTEGER, type INTEGER, name TEXT)')
        self.db.execute('CREATE INDEX idx_user_id ON materials(user_id)')
        self.db.executemany('INSERT INTO materials VALUES (?,?,?,?)',
                            [(1, 947, 2, 'one'), (2, 904, 2, 'two'), (3, 947, 2, 'three'),
                             (4, 947, 1, 'image'), (5, 0, 2, 'unknown'), (6, None, 2, 'null')])
        self.sql = """SELECT id,user_id AS uploader_id,name,
            'https://ai.yingliangads.com/video.mp4' AS url,'' AS thumbnail_url,
            '' AS content_id,'' AS source_job_id,'' AS source_kind,'' AS macro_name,
            '' AS macro_desc,'en' AS language,'' AS duration,'' AS size,1479 AS app_id,
            '' AS uploader_name FROM materials WHERE type=2"""
        self.plans = []
        def query(sql):
            self.plans.append(self.db.execute('EXPLAIN QUERY PLAN ' + sql).fetchall())
            return self.db.execute(sql).fetchall()
        self.source = MaterialSource('', query)

    def test_sparse_uploader_uses_index_and_keeps_descending_video_scope(self):
        rows = self.source._read(self.sql, uploader_id='947')
        self.assertEqual([r['id'] for r in rows], ['3', '1'])
        self.assertEqual({r['uploader_id'] for r in rows}, {'947'})
        self.assertIn('idx_user_id', str(self.plans[-1]))

    def test_unknown_uploader_preserves_zero_and_null_membership(self):
        self.assertEqual([r['id'] for r in self.source._read(self.sql, uploader_id='0')], ['6', '5'])

    def test_exact_id_and_keyword_stay_inside_uploader(self):
        self.assertEqual([r['id'] for r in self.source._read(self.sql, uploader_id='947', search='3')], ['3'])
        self.assertEqual(self.source._read(self.sql, uploader_id='904', search='3'), [])

    def test_filter_rejects_non_numeric_input_before_read(self):
        for value in ('947 OR 1=1', '-1', '1.5', '000947'):
            with self.assertRaises(WorkflowError): self.source.list(uploader_id=value)
        self.assertEqual(self.plans, [])

    def test_deployed_projection_keeps_numeric_uploader(self):
        sql = (Path(__file__).parents[1] / 'ops/youtube-material-refresh-20261120/material-source.sql').read_text(encoding='utf-8')
        self.assertRegex(sql, r'\bs\.user_id AS uploader_id\b')

    def test_compact_cards_skip_both_catalogs_but_full_get_resolves(self):
        resolver = Mock(side_effect=lambda items: [dict(item, drama_status='available') for item in items])
        self.source.drama_resolver = resolver
        self.source.configuration = lambda: (self.sql, {'configured': True})
        self.source._read_uploaders = lambda sql: []
        cards = self.source.list(uploader_id='947', compact=True)['items']
        self.assertEqual([r['id'] for r in cards], ['3', '1'])
        self.assertTrue(all(r['metadata_pending'] and not r['link_ready'] and not r['macro_name'] for r in cards))
        resolver.assert_not_called()
        full = self.source.list(uploader_id='947')['items']
        self.assertEqual(full[0]['drama_status'], 'available')
        self.assertNotIn('metadata_pending', full[0])
        self.assertEqual(resolver.call_count, 1)
        self.source.get('3')
        self.assertEqual(resolver.call_count, 2)
        with patch('features.moboreels_library.macro_client.lookup_story') as lookup:
            self.source._read(self.sql, moboreels=True, compact=True)
            lookup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
