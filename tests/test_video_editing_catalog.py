import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from features.video_editing import VideoEditingError
from features.video_editing.catalog import Catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.query=Mock(return_value=[['123','Example',20,'cover'],['123','Example',20,'cover']])
        self.app=SimpleNamespace(DB_NAME='db',SOURCE_TABLE='source',list_products=lambda:[{'app_id':'app'}],run_mysql=self.query)
        self.catalog=Catalog(None,self.app)
    def test_empty_or_short_name_search_never_queries_database(self):
        for query in ('',' ','x'):
            with self.assertRaises(VideoEditingError):self.catalog.dramas('app',query)
        self.query.assert_not_called()
    def test_exact_id_uses_content_index_no_aggregate_and_deduplicates(self):
        result=self.catalog.dramas('app','123');sql=self.query.call_args.args[0]
        self.assertEqual(len(result),1);self.assertIn('FORCE INDEX (`content_id`)',sql)
        self.assertIn("content_id='123'",sql);self.assertIn('LIMIT 500',sql)
        self.assertNotIn('GROUP BY',sql);self.assertNotIn('ORDER BY',sql)
    def test_name_search_uses_bounded_index_prefix(self):
        self.catalog.dramas('app','Example');sql=self.query.call_args.args[0]
        self.assertIn('FORCE INDEX (`name`)',sql);self.assertIn("name LIKE 'Example%'",sql)
        self.assertIn('MAX_EXECUTION_TIME(5000)',sql)
    def test_mysql_timeout_closes_process_group_and_keeps_gate(self):
        self.app.MYSQL_BASE_CMD=['/usr/bin/mysql','-P','63350','-e'];self.app.MYSQL_PASSWORD='dummy'
        process=Mock(pid=123);process.communicate.side_effect=[subprocess.TimeoutExpired('mysql',15),('','')]
        with patch('features.video_editing.catalog.subprocess.Popen',return_value=process) as spawn,patch('features.video_editing.catalog.os.killpg',create=True) as kill:
            with self.assertRaises(VideoEditingError) as raised:self.catalog._query('SELECT 1')
        self.assertEqual(raised.exception.code,'catalog_query_timeout')
        self.assertEqual(spawn.call_args.args[0][0],'/usr/bin/mysql')
        self.assertIn('--connect-timeout=3',spawn.call_args.args[0])
        self.assertEqual(spawn.call_args.kwargs['env']['MYSQL_MAX_EXECUTION_TIME_MS'],'5000')
        if os.name=='nt':process.kill.assert_called_once()
        else:kill.assert_called_once()
    def test_existing_catalog_helpers_are_bounded_without_global_mutation(self):
        scope={}
        exec('def run_mysql(q):\n raise AssertionError("unbounded")\ndef pick_drama_variant(value,app_id=None):\n return run_mysql("variant")\ndef fetch_drama_episodes(value,app_id=None):\n pick_drama_variant(value,app_id=app_id)\n return run_mysql("episodes")',scope)
        self.app.MYSQL_BASE_CMD=['mysql'];self.app.fetch_drama_episodes=scope['fetch_drama_episodes']
        with patch.object(self.catalog,'_query',return_value=['bounded']) as query:
            result=self.catalog._bounded_app_function('fetch_drama_episodes')('123',app_id='app')
        self.assertEqual(result,['bounded']);self.assertEqual(query.call_count,2)
        with self.assertRaises(AssertionError):self.app.fetch_drama_episodes('123')


if __name__=='__main__':unittest.main()
