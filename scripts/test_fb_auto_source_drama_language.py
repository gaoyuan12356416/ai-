"""Exercise source-language fallback through the actual repository SQL paths."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.metrics import MetricTotals, MetricWindow
from features.fb_auto_posts.repositories import MaterialRepository, RepositoryError
from features.fb_auto_posts.validation import normalize_template_payload
from scripts.test_fb_auto_material_product import payload, TARGET

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


class Catalog:
    schema = 'catalog'
    blacklist_schema = 'catalog'

    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.calls = []
        self.db.executescript('''
            CREATE TABLE ads_custom_source(id INTEGER PRIMARY KEY,data_source INTEGER,
                product TEXT,type INTEGER,is_delete INTEGER,data_source_id TEXT,url TEXT,
                language TEXT,name TEXT,tag_name TEXT,video_duration INTEGER);
            CREATE TABLE ads_drama_info(id INTEGER PRIMARY KEY,app_id TEXT,content_id TEXT,
                name TEXT,language TEXT,resource_type_v2 INTEGER,series_code TEXT,
                release_status INTEGER,deploy_time INTEGER);
            CREATE TABLE ads_drama_resource(app_id TEXT,content_id TEXT,type INTEGER,
                language TEXT,`desc` TEXT);
            CREATE TABLE ads_facebook_post_blacklist(id INTEGER,is_delete INTEGER,
                type INTEGER,content_id TEXT);
        ''')

    def add(self, material_id, content, material_language, drama_language, product='DramawaveSource'):
        self.db.execute('INSERT INTO ads_custom_source VALUES(?,6,?,2,0,?,?,?,\'Source\',\'\',60)',
            (material_id,product,content,
             'https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/example.mp4',material_language))
        self.db.execute('INSERT INTO ads_drama_info VALUES(?,\'1479\',?,\'Drama\',?,1,\'series\',1,?)',
                        (material_id,content,drama_language,int(NOW.timestamp())-3600))
        self.db.execute('INSERT INTO ads_drama_resource VALUES(\'1479\',?,2,?,\'Description\')',
                        (content,drama_language))

    def select(self, sql, params):
        self.calls.append((sql,params))
        # SQLite executes the same joins, filters and aggregation. Only MySQL
        # index/hint syntax, schema qualification and placeholders are adapted.
        sql = sql.replace('`catalog`.','').replace('%s','?').replace('BINARY TRIM','TRIM')
        sql = re.sub(r'FORCE INDEX\([A-Za-z0-9_]+\)', '', sql)
        return [dict(row) for row in self.db.execute(sql,params)]


class Metrics:
    def load_metric_window(self, **kwargs):
        return MetricWindow((1,),('2026-11-08',),
                            {'en-drama':MetricTotals(Decimal(10),Decimal(1))},{})


class LanguageFallbackTests(unittest.TestCase):
    def setUp(self):
        self.catalog=Catalog();self.addCleanup(self.catalog.db.close)
        raw=payload();raw['material_rule'].update(duration_min_seconds=1,duration_max_seconds=600)
        self.config={**normalize_template_payload(raw),**TARGET,'language':'en'}
        self.repo=MaterialRepository(self.catalog,Metrics(),now_fn=lambda:NOW)

    def test_complete_catalog_empty_null_and_whitespace_use_related_drama_language(self):
        for mid,empty in enumerate(('',None,'  '),10):self.catalog.add(mid,'en-'+str(mid),empty,'en')
        self.catalog.add(20,'pl-drama','','pl')
        result=self.repo.candidate_snapshot(self.config)
        self.assertEqual({m.material_id for m in result.candidates},{'10','11','12'})
        self.assertEqual({m.language for m in result.candidates},{'en'})
        self.assertTrue(all(m.drama_description=='Description' for m in result.candidates))
        self.assertTrue(all('FORCE INDEX(PRIMARY)' not in sql for sql,_ in self.catalog.calls))

    def test_indexed_scan_keeps_metric_ranking_and_fallback(self):
        self.catalog.add(10,'en-drama','','en')
        repo=MaterialRepository(self.catalog,Metrics(),now_fn=lambda:NOW,
            candidate_limit=1,metric_prefilter_min_content_ids=1)
        self.assertEqual([m.material_id for m in repo.candidate_snapshot(self.config).candidates],['10'])
        source_queries=[sql for sql,_ in self.catalog.calls if 'ads_custom_source' in sql]
        self.assertEqual(len(source_queries),1)
        self.assertIn('idx_source_type_source_id',source_queries[0])

    def test_complete_indexed_scan_keeps_zero_history_dramas_across_batches(self):
        for mid in (10,20,30):self.catalog.add(mid,'zero-'+str(mid),'','en')
        repo=MaterialRepository(self.catalog,Metrics(),now_fn=lambda:NOW,metric_prefilter_batch_size=1)
        self.assertEqual({m.material_id for m in repo.candidate_snapshot(self.config).candidates},{'10','20','30'})
        self.assertEqual(len([sql for sql,_ in self.catalog.calls if 'ads_custom_source' in sql]),3)

    def test_populated_source_language_is_not_overridden(self):
        self.catalog.add(10,'en-drama','pl','en')
        self.assertEqual(self.repo.candidate_snapshot(self.config).candidates,())
        with self.assertRaises(RepositoryError) as caught:self.repo.exact_material(self.config,'10')
        self.assertEqual(caught.exception.code,'fb_auto_material_drama_unavailable')
        self.assertEqual(self.repo._material_language('DramawaveSource','invalid language','en'),'')

    def test_missing_drama_language_or_unavailable_drama_is_not_guessed(self):
        self.catalog.add(10,'empty','','')
        self.catalog.add(20,'not-released','','en')
        self.catalog.db.execute('UPDATE ads_drama_info SET release_status=0 WHERE id=20')
        self.assertEqual(self.repo.candidate_snapshot(self.config).candidates,())
        for mid in ('10','20'):
            with self.subTest(mid=mid),self.assertRaises(RepositoryError):self.repo.exact_material(self.config,mid)

    def test_exact_material_uses_inherited_language_for_description(self):
        self.catalog.add(10,'en-drama',None,'en')
        result=self.repo.exact_material(self.config,'10')
        self.assertEqual((result.language,result.drama_description),('en','Description'))

    def test_other_products_keep_existing_language_requirement(self):
        self.catalog.add(10,'en-drama','','en',product='Dramawave')
        config={**self.config,'material_product':'Dramawave'}
        self.assertEqual(self.repo.candidate_snapshot(config).candidates,())
        with self.assertRaises(RepositoryError):self.repo.exact_material(config,'10')

    def test_language_comes_from_latest_eligible_target_app_record(self):
        self.catalog.add(10,'en-drama','','pl')
        self.catalog.db.execute('INSERT INTO ads_drama_info VALUES(20,\'1479\',\'en-drama\',\'Current\',\'en\',1,\'series\',1,?)',(int(NOW.timestamp())-60,))
        self.catalog.db.execute('INSERT INTO ads_drama_info VALUES(30,\'2940\',\'en-drama\',\'Other app\',\'fr\',1,\'series\',1,?)',(int(NOW.timestamp())-60,))
        self.catalog.db.execute("INSERT INTO ads_drama_resource VALUES('1479','en-drama',2,'en','Description')")
        result=self.repo.candidate_snapshot(self.config).candidates
        self.assertEqual([(m.language,m.drama_name) for m in result],[('en','Current')])
        self.assertEqual(self.repo.exact_material(self.config,'10').language,'en')

    def test_blacklist_and_duration_and_description_still_apply(self):
        self.catalog.add(10,'blocked','','en')
        self.catalog.add(20,'too-long','','en')
        self.catalog.add(30,'no-description','','en')
        self.catalog.db.execute("INSERT INTO ads_facebook_post_blacklist VALUES(1,0,1,'blocked')")
        self.catalog.db.execute('UPDATE ads_custom_source SET video_duration=601 WHERE id=20')
        self.catalog.db.execute("DELETE FROM ads_drama_resource WHERE content_id='no-description'")
        self.assertEqual(self.repo.candidate_snapshot(self.config).candidates,())


if __name__=='__main__':unittest.main()
