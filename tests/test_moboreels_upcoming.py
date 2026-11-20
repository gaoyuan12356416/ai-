import re
import sqlite3
import unittest
from unittest.mock import patch

from features import moboreels_upcoming as upcoming


class UpcomingCatalogTests(unittest.TestCase):
    now = 1_800_000_000

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
            ATTACH ':memory:' AS ads_ai;
            CREATE TABLE ads_ai.cdreader_koc_drama(
                institution_user_id TEXT,project_type INTEGER,story_id TEXT,
                is_show INTEGER,lang_id INTEGER,story_name TEXT);
            CREATE TABLE ads_drama_info(app_id INTEGER,content_id TEXT,release_status INTEGER,
                language TEXT,deploy_time INTEGER,published_at INTEGER,scheduled_publish_time INTEGER);
            CREATE TABLE ads_custom_source(id INTEGER, data_source_id TEXT, data_source INTEGER,
                product TEXT COLLATE NOCASE, language TEXT, type INTEGER, is_delete INTEGER,
                video_duration INTEGER, url TEXT);
        """)
        self.addCleanup(patch.stopall)
        patch.object(upcoming, 'institution_id', return_value='studio').start()

    def select(self, sql, params):
        sql = re.sub(r' FORCE INDEX\(\w+\)', '', sql).replace('%s', '?')
        return [dict(row) for row in self.db.execute(sql, params)]

    def package(self, cid, *, after=3600, live=0, deploy=0, language='en', app=2373, enabled=1):
        self.db.execute('INSERT INTO ads_drama_info VALUES(?,?,?,?,?,?,?)',
                        (app,cid,enabled,language,deploy,live,self.now + after))

    def listing(self, cid, shown=1, lang_id=3, institution='studio'):
        self.db.execute('INSERT INTO ads_ai.cdreader_koc_drama VALUES(?,2,?,?,?,?)',
                        (institution,cid,shown,lang_id,'title '+cid))

    def discover(self, window='-1~0', **kwargs):
        return upcoming.discover(self.select, 'main', window, self.now, **kwargs)

    def assess(self, plan, when, language='en'):
        return upcoming.assess(self.select, 'main', plan, language, when)

    def test_window_and_identity_are_product_scoped_and_inclusive(self):
        for cid, after in [('start',0), ('end',86400), ('outside',86401), ('past',-1)]:
            self.package(cid, after=after)
        self.package('old', live=self.now-90)
        self.package('foreign', app=1479)
        self.package('disabled', enabled=0)
        self.package('conflict', after=1)
        self.package('conflict', after=96000)
        self.package('different-language', language='en')
        self.package('different-language', language='es')
        self.package('duplicate', after=60)
        self.package('duplicate', after=60)
        self.assertEqual(set(self.discover()['stories']), {'start','end','duplicate'})
        self.assertIn('old', self.discover('-1~1')['stories'])
        self.assertFalse(any(upcoming.applies(v) for v in (0,7,'0~7',None,'')))
        self.assertTrue(upcoming.applies('【-2～-1】'))

    def test_recheck_uses_original_ids_and_latest_release_and_visibility(self):
        self.package('a', after=1800)
        self.listing('a', shown=0, lang_id=-1)
        plan = self.discover()
        self.package('b', after=1800)
        self.listing('b')
        state = self.assess(plan, self.now)
        self.assertEqual(set(state.pending), {'a'})
        self.db.execute("UPDATE ads_drama_info SET scheduled_publish_time=? WHERE content_id='a'", (self.now+90000,))
        self.db.execute("UPDATE ads_ai.cdreader_koc_drama SET is_show=1,lang_id=3 WHERE story_id='a'")
        self.assertFalse(self.assess(plan,self.now+1800).ready)
        self.db.execute("UPDATE ads_drama_info SET published_at=? WHERE content_id='a'", (self.now+80000,))
        self.assertFalse(self.assess(plan,self.now+85000).ready)
        self.assertEqual(self.assess(plan,self.now+90000).ready, {'a':{'name':'title a','at':self.now+90000}})
        self.db.execute("UPDATE ads_drama_info SET release_status=0 WHERE content_id='a'")
        self.assertIsNone(self.assess(plan,self.now+90001).retry_at)

    def test_provider_can_lag_first_release_and_explicit_delivery_overrides_plan(self):
        self.package('a', after=20)
        self.listing('a', shown=0, lang_id=-1)
        plan = self.discover()
        self.db.execute('UPDATE ads_drama_info SET published_at=?', (self.now+20,))
        self.assertIn('a', self.assess(plan,self.now+70).pending)
        self.db.execute('UPDATE ads_ai.cdreader_koc_drama SET is_show=1,lang_id=3')
        self.db.execute('UPDATE ads_drama_info SET deploy_time=?',(self.now+80,))
        self.assertFalse(self.assess(plan,self.now+70).ready)
        self.assertEqual(self.assess(plan,self.now+80).ready['a']['at'],self.now+80)

    def test_wrong_institution_or_language_cannot_release(self):
        self.package('x', after=0, language='es')
        self.listing('x', lang_id=4, institution='other')
        self.listing('x', lang_id=3)
        self.package('p', after=0, language='pl')
        self.listing('p',lang_id=23)
        plan = self.discover()
        self.assertFalse(self.assess(plan,self.now,'es').ready)
        self.assertFalse(self.assess(plan,self.now,'en').ready)
        self.assertIn('p',self.assess(plan,self.now,'pl').ready)

    def test_material_hints_dont_reserve_or_gate_new_arrivals(self):
        self.package('a', after=0, language='pt')
        self.listing('a', lang_id=5)
        values = [(i,'a',6,product,lang,2,0,duration,'https://media.test/a') for i,product,lang,duration in
                  [(1,'Moboreels','pt',300),(2,'Dramawave','pt',300),(3,'Moboreels','en',300),
                   (4,'Moboreels','pt',7000)]]
        self.db.executemany('INSERT INTO ads_custom_source VALUES(?,?,?,?,?,?,?,?,?)',values)
        config = {'material_products':['Moboreels'], 'material_rule':{'duration_min_seconds':300,'duration_max_seconds':6000}}
        plan = self.discover(material_config=config)
        self.assertEqual(plan['stories']['a']['source_material_count'],1)
        self.assertEqual(plan['stories']['a']['example_material_id'],'1')
        self.db.execute('DELETE FROM ads_custom_source')
        self.assertIn('a',self.assess(plan,self.now,'pt-br').ready)
        self.db.execute('DROP TABLE ads_custom_source')
        with self.assertLogs(upcoming.__name__, level='WARNING'):
            partial = self.discover(material_config=config)
        self.assertEqual(partial['material_scan'],'unavailable')
        self.assertNotIn('source_material_count',partial['stories']['a'])
        self.assertIn('a',self.assess(partial,self.now,'pt-br').ready)
