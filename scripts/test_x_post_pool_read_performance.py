"""Regression coverage for the online pool lookup index and ledger preservation."""
import concurrent.futures
import contextlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from features.x_posts import service


class PoolLookupIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'posts.sqlite3'
        self.store=service.XPostStore(self.path)
        self.store.add_pool_materials(['9001'],validation_checks=[{'material_id':'9001','error_code':''}])

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_lookup_index_exists_and_schema_initialization_is_idempotent(self):
        before=self.store.query_pool()
        service.ensure_storage(self.path);service.ensure_storage(self.path)
        self.assertEqual(self.store.query_pool(),before)
        with contextlib.closing(sqlite3.connect(str(self.path))) as c:
            row=next(r for r in c.execute('PRAGMA index_list(x_post_queue)') if r[1]=='idx_x_post_queue_material_lookup')
            self.assertEqual(row[4],0)
            self.assertEqual([r[2] for r in c.execute('PRAGMA index_info(idx_x_post_queue_material_lookup)')],['material_key'])

    def test_pool_query_uses_both_lookup_indexes(self):
        statements=[];real=service._connect
        def traced(path):
            c=real(path);c.set_trace_callback(statements.append);return c
        with patch.object(service,'_connect',side_effect=traced):
            result=self.store.query_pool({'page_size':20})
        self.assertEqual(result['summary']['total'],1)
        statements=[s for s in statements if 'FROM x_post_material_pool p' in s]
        self.assertEqual(len(statements),3)
        with contextlib.closing(sqlite3.connect(str(self.path))) as c:
            for statement in statements:
                details='\n'.join(str(row[3]) for row in c.execute('EXPLAIN QUERY PLAN '+statement))
                self.assertIn('idx_x_post_queue_material_lookup',details)
                self.assertIn('ux_x_post_queue_pool_item_id',details)
                self.assertNotIn('SCAN TABLE x_post_queue',details)

    def test_online_index_helps_an_already_open_reader_without_changing_results(self):
        with contextlib.closing(sqlite3.connect(str(self.path))) as writer,contextlib.closing(sqlite3.connect(str(self.path))) as reader:
            writer.execute('DROP INDEX idx_x_post_queue_material_lookup');writer.commit()
            sql='SELECT COUNT(*) FROM x_post_material_pool p LEFT JOIN x_post_queue q ON q.pool_item_id=p.id OR q.material_key=p.material_key'
            old=reader.execute(sql).fetchall()
            writer.execute(service.POOL_MATERIAL_LOOKUP_INDEX_SQL);writer.commit()
            self.assertEqual(reader.execute(sql).fetchall(),old)
            details='\n'.join(str(r[3]) for r in reader.execute('EXPLAIN QUERY PLAN '+sql))
            self.assertIn('idx_x_post_queue_material_lookup',details)

    def test_duplicate_add_is_idempotent_and_does_not_enqueue(self):
        result=self.store.add_pool_materials(['9001'],validation_checks=[{'material_id':'9001','error_code':''}])
        self.assertEqual(result['created_count'],0)
        self.assertEqual(result['already_in_pool_count'],1)
        with contextlib.closing(sqlite3.connect(str(self.path))) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM x_post_queue').fetchone()[0],0)

    def test_read_and_duplicate_add_are_consistent_concurrently(self):
        def read():return self.store.query_pool()['summary']['total']
        def add():return self.store.add_pool_materials(['9001'])['created_count']
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures=[pool.submit(read if i%2==0 else add) for i in range(12)]
            self.assertEqual([f.result(timeout=3) for f in futures],[1,0]*6)

if __name__=='__main__':unittest.main()
