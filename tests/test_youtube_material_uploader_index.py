import sqlite3
import unittest
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
        self.sql = """SELECT m.id,COALESCE(CAST(uploaders.user_id AS CHAR),'0') AS uploader_id,m.name,
            'https://ai.yingliangads.com/video.mp4' AS url,'' AS thumbnail_url,
            '' AS content_id,'' AS source_job_id,'' AS source_kind,'' AS macro_name,
            '' AS macro_desc,'en' AS language,'' AS duration,'' AS size,1479 AS app_id,
            '' AS uploader_name FROM materials m
            LEFT JOIN (SELECT DISTINCT user_id FROM materials) uploaders ON uploaders.user_id=m.user_id
            WHERE m.type=2"""
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

    def test_deployed_projection_uses_complete_indexed_uploader_dimension(self):
        sql = (Path(__file__).parents[1] / 'ops/youtube-material-refresh-20261120/material-source.sql').read_text(encoding='utf-8')
        self.assertIn('SELECT DISTINCT user_id FROM `kunlunads_dev`.`ads_custom_source`', sql)
        self.assertIn('ON uploaders.user_id=s.user_id', sql)
        self.assertIn("COALESCE(CAST(uploaders.user_id AS CHAR), '0') AS uploader_id", sql)


if __name__ == '__main__':
    unittest.main()
