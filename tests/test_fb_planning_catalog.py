from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from features.fb_auto_posts.planning_catalog import open_for_plan
from features.fb_auto_posts.repositories import RepositoryError


class CatalogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root/'enabled').touch()
        self.path=self.root/'current.sqlite3'
        self.db=sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE generation(started,source_epoch,finished);
            INSERT INTO generation VALUES(100,100,200);
            CREATE TABLE material(id INTEGER PRIMARY KEY,cid,lang,product,duration,url,name,tag);
            CREATE TABLE resource(cid PRIMARY KEY,n,items);
            INSERT INTO resource VALUES('c',2,'[{"description":"text"}]');
            INSERT INTO material VALUES(1,'c','en','Dramawave',50,'https://example.org/v','a','t'),
                (2,'c','','DramawaveSource',50,'https://example.org/s','b','t'),
                (3,'c','','Dramawave',50,'https://example.org/en','x','t'),
                (4,'c','fr','Dramawave',50,'http://example.org/f','d','t'),
                (5,'c','en','Dramawave',150,'https://example.org/d','g','t');
        """)
        self.config={'app_id':'1479','material_data_source':6,'material_products':['Dramawave','DramawaveSource'],
                     'material_rule':{'duration_min_seconds':1,'duration_max_seconds':100},
                     '_catalog_trigger':self.time(99).isoformat()}

    @staticmethod
    def time(n):
        return datetime.fromtimestamp(n,timezone.utc)

    def test_scopes_urls_and_metadata(self):
        cat=open_for_plan(self.config,self.time(210),self.root)
        self.addCleanup(cat.db.close)
        args=SimpleNamespace(config=self.config,products=self.config['material_products'],languages=['en'])
        self.assertEqual(dict(cat.index(args)),{('c','en'):[1],('c',''):[2]})
        self.assertEqual(set(cat.hydrate([1,2,4,404])),{1,2})
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(cat.resources,[['c'],['c']])),[[{'description':'text'}]]*2)

    def test_trigger_freshness_and_fallback(self):
        newer=dict(self.config,_catalog_trigger=self.time(150).isoformat())
        with self.assertRaises(RepositoryError):
            open_for_plan(newer,self.time(200),self.root)
        self.assertIsNone(open_for_plan(newer,self.time(3000),self.root))
        self.assertIsNone(open_for_plan(dict(self.config,material_products=['Moboreels']),self.time(200),self.root))
        self.assertIsNone(open_for_plan(dict(self.config,_catalog_trigger=None),self.time(200),self.root))
        (self.root/'enabled').unlink()
        self.assertIsNone(open_for_plan(self.config,self.time(200),self.root))


    def test_release_window_metadata_is_shared_and_picks_latest_per_raw_id(self):
        from features.fb_auto_posts.planning_catalog import WindowMetadata
        from unittest.mock import Mock
        mysql=Mock(schema='kunlunads_dev')
        mysql.select.return_value=[{'content_id':'a','drama_name':'latest'}]
        window=WindowMetadata(10)
        with ThreadPoolExecutor(3) as pool:
            results=list(pool.map(lambda _:window.load(mysql,1479,9,20,['2']),range(5)))
        self.assertEqual(mysql.select.call_count,1)
        self.assertEqual(results[0],{'a':{'content_id':'a','drama_name':'latest'}})
        sql,args=mysql.select.call_args.args
        self.assertIn('MAX(d0.id)',sql)
        self.assertIn('GROUP BY d0.content_id',sql)
        self.assertEqual(args,(1479,9,20,'2'))
