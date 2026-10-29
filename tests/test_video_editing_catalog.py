import os
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from features.video_editing import VideoEditingError
from features.video_editing.catalog import Catalog
from features.video_editing.store import Store


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
        self.query.side_effect=[[],[['123','Example',20,'cover']]]
        self.catalog.dramas('app','Example');sql=self.query.call_args.args[0]
        self.assertIn('FORCE INDEX (`name`)',sql);self.assertIn("name LIKE 'Example%'",sql)
        self.assertIn('MAX_EXECUTION_TIME(5000)',sql)
    def test_actual_alphanumeric_content_id_is_exact_without_name_scan(self):
        self.catalog.dramas('app','1wwAOKT9Tz');sql=self.query.call_args.args[0]
        self.assertIn("content_id='1wwAOKT9Tz'",sql);self.assertNotIn('LIKE',sql)
        self.assertEqual(self.query.call_count,1)
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


class ResourceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.query = Mock()
        self.catalog = Catalog(None, SimpleNamespace(DB_NAME='db', run_mysql=self.query))

    @staticmethod
    def rows(*items):
        return [[json.dumps(item, ensure_ascii=False).encode('utf-8').hex()] for item in items]

    def test_opaque_exact_id_with_punctuation_never_becomes_a_title_search(self):
        code = '{v3404/l144250/}'
        self.query.return_value = self.rows({'series_code':code, 'english_title':'Love Delivered on Time'})
        self.assertEqual(self.catalog.resources(code), [{'series_code':code, 'english_title':'Love Delivered on Time', 'english_title_status':'matched'}])
        sql = self.query.call_args.args[0]
        self.assertIn('FORCE INDEX (`scoo`)', sql)
        self.assertIn('BINARY series_code=BINARY', sql)
        self.assertIn(code.encode().hex(), sql)
        self.assertNotIn('LIKE', sql)
        self.assertEqual(self.query.call_count, 1)

    def test_english_prefix_uses_name_index_and_resolves_conflicting_other_names(self):
        self.query.side_effect = [[], self.rows({'series_code':'21481'}), self.rows(
            {'series_code':'21481','english_title':'Love & Revenge'},
            {'series_code':'21481','english_title':'Another English Name'})]
        result = self.catalog.resources('love')
        self.assertEqual(result[0]['english_title_status'], 'ambiguous')
        self.assertEqual(result[0]['english_title'], '')
        sql = self.query.call_args_list[1].args[0]
        self.assertIn('FORCE INDEX (`name`)', sql)
        self.assertIn("LOWER(language)='en'", sql)
        self.assertIn('utf8mb4_unicode_ci', sql)
        self.assertIn('6c6f766525', sql)  # love%, not %love%
        self.assertIn('LIMIT 51', sql)

    def test_like_metacharacters_are_literal_prefix_characters(self):
        self.query.side_effect = [[], []]
        self.assertEqual(self.catalog.resources('Love_100%'), [])
        sql = self.query.call_args_list[-1].args[0]
        self.assertIn('Love\\_100\\%%'.encode().hex(), sql)

    def test_missing_english_title_keeps_exact_resource_selectable(self):
        self.query.return_value = self.rows({'series_code':'21481','english_title':''})
        self.assertEqual(self.catalog.resource('21481')['english_title_status'], 'missing')

    def test_invalid_placeholders_never_query(self):
        for code in ('', '0', '-', '资源ID', 'a\nb', None, 'x' * 65):
            with self.subTest(code=code), self.assertRaises(VideoEditingError):
                self.catalog.resource(code)
        self.query.assert_not_called()

    def test_resource_variants_are_cross_product_and_do_not_depend_on_episode_rows(self):
        self.query.return_value = self.rows(
            {'app_id':'1479','content_id':'en01','series_code':'21481','title':'Love','language':'EN','country':'us'},
            {'app_id':'979','content_id':'es01','series_code':'21481','title':'Amor','language':'es','country':'es'})
        result = self.catalog.resource_variants('21481')
        self.assertEqual({row['app_id'] for row in result}, {'1479','979'})
        self.assertEqual(result[0]['language'], 'en')
        self.assertEqual(result[0]['country'], 'US')
        self.assertNotIn('ads_drama_resource', self.query.call_args.args[0])

    def test_ambiguous_version_does_not_hide_other_resource_versions(self):
        self.query.return_value = self.rows(
            {'app_id':'1479','content_id':'en01','series_code':'21481','title':'Love','language':'en','country':'US'},
            {'app_id':'1479','content_id':'en01','series_code':'21481','title':'Love','language':'en','country':'CA'},
            {'app_id':'1479','content_id':'es01','series_code':'21481','title':'Amor','language':'es','country':'ES'})
        self.assertEqual([row['content_id'] for row in self.catalog.resource_variants('21481')], ['es01'])

    def test_variant_revalidates_resource_and_uses_content_index(self):
        self.query.return_value = self.rows({'app_id':'1479','content_id':'en01','series_code':'21481','title':'Love','language':'en','country':'US'})
        self.assertEqual(self.catalog.variant('1479','en01','21481')['country'], 'US')
        self.assertIn('FORCE INDEX (`ac`)', self.query.call_args.args[0])
        with self.assertRaises(VideoEditingError) as caught:
            self.catalog.variant('1479','en01','different')
        self.assertEqual(caught.exception.code, 'drama_resource_mismatch')

    def test_variant_ambiguous_metadata_is_not_arbitrarily_selected(self):
        self.query.return_value = self.rows(
            {'app_id':'1479','content_id':'en01','series_code':'21481','title':'Love','language':'en','country':'US'},
            {'app_id':'1479','content_id':'en01','series_code':'21482','title':'Love','language':'en','country':'US'})
        with self.assertRaises(VideoEditingError) as caught:
            self.catalog.variant('1479','en01')
        self.assertEqual(caught.exception.code, 'drama_metadata_ambiguous')

    def test_missing_ingestion_metadata_and_title_variations_do_not_hide_versions(self):
        self.query.return_value = self.rows(
            {'app_id':'1479','content_id':'en01','series_code':'21481','title':'First Title','language':'','country':''},
            {'app_id':'1479','content_id':'en01','series_code':'21481','title':'Second Title','language':'','country':''})
        rows = self.catalog.resource_variants('21481')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title_status'], 'ambiguous')
        self.assertEqual(rows[0]['title'], '')
        self.assertEqual(self.catalog.variant('1479','en01')['series_code'], '21481')

    def test_source_resource_only_requires_unambiguous_resource_identity(self):
        self.query.return_value = self.rows({'series_code':'21481'})
        self.assertEqual(self.catalog.source_resource('1479','en01'), {'series_code':'21481'})
        sql = self.query.call_args.args[0]
        self.assertNotIn('country', sql)
        self.assertNotIn('language', sql)
        self.assertNotIn('name', sql)
        self.query.return_value = self.rows({'series_code':'21481'}, {'series_code':'21482'})
        with self.assertRaises(VideoEditingError) as caught:
            self.catalog.source_resource('1479','en01')
        self.assertEqual(caught.exception.code, 'drama_resource_ambiguous')

    def test_episode_resource_enrichment_is_best_effort_and_updates_existing_source(self):
        actor={'tenant_key':'tenant','user_id':'user'}
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'catalog.sqlite3')
            app=SimpleNamespace(list_products=lambda:[{'app_id':'1479'}],
                fetch_drama_episodes=lambda content_id,app_id:[{'episode_url':'https://cdn.example.test/ep1.mp4',
                                                               'episode_number':1,'drama_name':'Local Title'}])
            catalog=Catalog(store,app)
            with patch.object(catalog,'source_resource',side_effect=VideoEditingError('catalog_query_timeout','Timeout',503)):
                original=catalog.episodes('1479','content1',actor)[0]
            self.assertNotIn('series_code',original['data'])
            with patch.object(catalog,'source_resource',return_value={'series_code':'21481'}),\
                 patch.object(catalog,'resource',return_value={'series_code':'21481','english_title':'English Title'}):
                enriched=catalog.episodes('1479','content1',actor)[0]
            self.assertEqual(enriched['id'],original['id'])
            self.assertEqual(enriched['status'],original['status'])
            self.assertEqual(enriched['data']['series_code'],'21481')
            self.assertEqual(enriched['data']['english_title'],'English Title')
            self.assertEqual(store.list('source')[1],1)


if __name__=='__main__':unittest.main()
