from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from features.ai_video_editing.catalog import Catalog, source_key
from features.ai_video_editing.catalog_cache import BrowseCache
from features.ai_video_editing.runtime import Fault


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'browse.sqlite3'
        self.caches = []

    def tearDown(self):
        for cache in self.caches:
            cache.close()
        self.tmp.cleanup()

    def cache(self):
        result = BrowseCache(self.path)
        self.caches.append(result)
        return result

    @staticmethod
    def data(name='Story'):
        return {'product':'dramawave', 'items':[{'number':1,'name':name,'url':'https://example.com/video?signature=secret'}]}

    def test_restart_reuses_only_public_metadata_and_returns_independent_values(self):
        cache=self.cache()
        first=cache.get('story',self.data)
        first['items'][0]['name']='Edited in browser'
        self.assertEqual(cache.get('story',lambda: self.fail('hot read must not query'))['items'][0]['name'],'Story')
        restarted=self.cache()
        result=restarted.get('story',lambda: self.fail('restart must reuse durable metadata'))
        self.assertEqual(result['cache']['state'],'fresh')
        with closing(sqlite3.connect(self.path)) as db:
            stored=db.execute('SELECT data FROM browse_cache').fetchone()[0]
        self.assertNotIn('signature',stored)
        self.assertNotIn('url',result['items'][0])

    def test_stale_returns_immediately_and_refreshes_once(self):
        cache=self.cache();cache.get('story',self.data)
        cache.entries['story']=(time.time()-301, self.data())
        entered=threading.Event();release=threading.Event();calls=[]
        def slow():
            calls.append(1);entered.set();release.wait(3);return self.data('Updated')
        start=time.monotonic()
        try:
            first=cache.get('story',slow)
            self.assertLess(time.monotonic()-start,.1)
            self.assertTrue(entered.wait(1))
            second=cache.get('story',slow)
            self.assertEqual(first['cache']['state'],'stale')
            self.assertEqual(second['items'][0]['name'],'Story')
            self.assertEqual(len(calls),1)
        finally:release.set()
        cache.pool.submit(lambda:None).result(3)
        self.assertEqual(cache.get('story',slow)['items'][0]['name'],'Updated')

    def test_cold_concurrent_reads_share_upstream_and_force_refresh_observes_new_data(self):
        cache=self.cache();entered=threading.Event();release=threading.Event();calls=[]
        def slow():
            calls.append(1);entered.set();release.wait(3);return self.data()
        with ThreadPoolExecutor(max_workers=5) as pool:
            requests=[pool.submit(cache.get,'story',slow) for _ in range(5)]
            self.assertTrue(entered.wait(1));release.set()
            for request in requests:self.assertEqual(len(request.result(3)['items']),1)
        self.assertEqual(len(calls),1)
        self.assertEqual(cache.get('story',lambda:self.data('New'),refresh=True)['items'][0]['name'],'New')

    def test_refresh_error_keeps_previous_metadata_without_retry_storm(self):
        cache=self.cache();cache.get('story',self.data)
        cache.entries['story']=(time.time()-301,self.data())
        calls=[]
        def failed():
            calls.append(1);raise Fault('catalog_timeout','剧库暂不可用',503)
        cache.get('story',failed);cache.pool.submit(lambda:None).result(3)
        result=cache.get('story',failed)
        self.assertEqual(result['items'][0]['name'],'Story')
        self.assertTrue(result['cache']['error'])
        self.assertEqual(len(calls),1)
        with self.assertRaises(Fault):cache.get('story',failed,refresh=True)

    def test_over_day_old_results_wait_for_authoritative_refresh(self):
        cache=self.cache();cache.get('story',self.data)
        cache.entries['story']=(time.time()-86401,self.data())
        result=cache.get('story',lambda:self.data('Current'))
        self.assertEqual(result['items'][0]['name'],'Current')
        self.assertEqual(result['cache']['state'],'updated')

    def test_browse_cache_never_extends_download_url_lifetime(self):
        catalog=Catalog(types.SimpleNamespace(DB_NAME='db',SOURCE_TABLE='table'),self.path)
        self.caches.append(catalog.browse)
        key=source_key(['42','DramaWave','TW','zh-TW'])
        rows=[{'number':1,'name':'Story','language':'zh-TW','url':'https://example.com/old?signature=old'}]
        with patch.object(catalog,'query',return_value=rows) as query:
            catalog.episodes('dramawave',key)
            catalog.invalidate_episodes('dramawave',key)
            rows[0]['url']='https://example.com/new?signature=new'
            public=catalog.episodes('dramawave',key)
            self.assertEqual(query.call_count,1)
            private=catalog.episodes('dramawave',key,private=True)
            self.assertEqual(query.call_count,2)
        self.assertNotIn('url',public['items'][0])
        self.assertIn('signature=new',private['items'][0]['url'])

    def test_provider_scope_changes_cannot_reuse_previous_catalog(self):
        catalog=Catalog(types.SimpleNamespace(DB_NAME='db',SOURCE_TABLE='table'),self.path)
        self.caches.append(catalog.browse)
        catalog.settings={'MOBOREELS_INSTITUTION_USER_ID':'one'}
        first=catalog.browse_key('dramas','moboreels','',1)
        catalog.settings['MOBOREELS_INSTITUTION_USER_ID']='two'
        second=catalog.browse_key('dramas','moboreels','',1)
        self.assertNotEqual(first,second)


if __name__=='__main__':unittest.main()
