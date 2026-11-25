import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

from features.social_analytics.tiktok import AUTO_QUEUE_ID_NAMESPACE, campaign_key, read_tiktok
from features.youtube_analytics.report import ReportError


class TikTokAttributionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.post = self.root / 'post.db'
        self.auto = self.root / 'auto.db'
        self.users = self.root / 'users.db'
        self.app = dict(SOCIAL_ANALYTICS_TT_DB_PATH=self.post, SOCIAL_ANALYTICS_TT_AUTO_DB_PATH=self.auto,
                        JOB_DB_PATH=self.users, SOCIAL_ANALYTICS_WAREHOUSE_ROOT=self.root)
        self.sql(self.users, """CREATE TABLE drama_admin_user(user_id,tenant_key,name);
                 INSERT INTO drama_admin_user VALUES('creator','tenant','模板创建人');""")
        self.sql(self.post, """CREATE TABLE tt_post_queue(id,account_id,account_username,account_display_name,
            material_id,content_id,drama_name,material_language,publish_id,code,pool_item_id,status,unknown_outcome,privacy_level);
            CREATE TABLE tt_post_material_pool(id,material_id,created_by_user_id);
            CREATE TABLE tt_post_recurring_pool(id,queue_id,material_id,account_id,content_id,run_id,created_by_user_id);
            CREATE TABLE tt_post_schedule_run(id,queue_id,pool_item_id,account_id);
            CREATE TABLE tt_post_event(queue_id,to_status,details_json,created_at);
            CREATE TABLE tt_post_code_route(queue_id INTEGER PRIMARY KEY,code,af_channel,af_c_id,c,af_adset_id,
                af_ad_id,content_id,long_url,created_at);
            INSERT INTO tt_post_queue VALUES(42,'8','handle','Legacy channel','91','legacy','老剧','en',
                'receipt-L','LEGA',9,'published',0,'PUBLIC_TO_EVERYONE');
            INSERT INTO tt_post_event VALUES(42,'published','{"publish_id":"receipt-L"}','2026-11-23T00:10:00Z');
            INSERT INTO tt_post_material_pool VALUES(9,'91','');
            """)
        self.sql(self.auto, """CREATE TABLE tt_auto_task(id,account_id,account_username,account_display_name,
            material_id,content_id,drama_language,publish_id,published_at_utc,template_id,selection_json,run_id,
            template_version,status,unknown_outcome,account_settings_json);
            CREATE TABLE tt_auto_template(id,created_by_user_id,created_at);
            CREATE TABLE tt_auto_template_version(template_id,version,config_sha256,created_by_user_id,created_at);
            CREATE TABLE tt_auto_run(id,template_id,template_version,config_sha256);
            CREATE TABLE tt_auto_material_ledger(material_id,content_id,task_id,template_id,run_id);
            INSERT INTO tt_auto_template VALUES(1,'creator','2026-11-01T00:00:00Z');
            INSERT INTO tt_auto_template_version VALUES(1,1,'hash1','creator','2026-11-01T00:00:00Z');
            INSERT INTO tt_auto_template_version VALUES(1,2,'hash2','later-editor','2026-11-20T00:00:00Z');
            INSERT INTO tt_auto_run VALUES(100,1,2,'hash2');
            INSERT INTO tt_auto_material_ledger VALUES('92','auto',42,1,100);
            INSERT INTO tt_auto_task VALUES(42,'9','handle2','Auto channel','92','auto','en','receipt-A',
                '2026-11-25T00:30:00+08:00',1,'{"material":{"drama_name":"新剧"}}',100,2,'published',0,
                '{"privacy_level":"PUBLIC_TO_EVERYONE"}');""")
        self.route(42, 'LEGA', 'Legacy-42', '8', '91', 'legacy')
        self.route(AUTO_QUEUE_ID_NAMESPACE + 42, 'AUTO', 'Auto-42', '9', '92', 'auto')

    @staticmethod
    def sql(path, script):
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(script)

    def route(self, internal_id, code, name, account, material, drama):
        query = urlencode(dict(c=name, af_c_id='42', af_dp=drama, af_channel='TT',
                               af_adset_id=account, af_ad_id=material))
        with closing(sqlite3.connect(self.post)) as db, db:
            db.execute('INSERT INTO tt_post_code_route VALUES(?,?,?,?,?,?,?,?,?,?)', (
                internal_id, code, 'TT', '42', name, account, material, drama,
                'https://www.dramawavew2a.com/ads/101/2250/view?' + query, '2026-11-22T00:00:00Z'))

    def catalog(self):
        return {row['link_id']: row for row in read_tiktok(self.app)}

    def freeze(self):
        folder = self.root / 'snapshots'
        folder.mkdir(exist_ok=True)
        (self.root / 'current.json').write_text(json.dumps({'database': 'snapshots/frozen.sqlite3'}))
        db_path = folder / 'frozen.sqlite3'
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute('CREATE TABLE catalog(payload)')

    def test_same_local_id_distinct_campaign_and_receipt(self):
        result = self.catalog()
        self.assertEqual(set(result), {'tiktok:post:42', 'tiktok:auto:42'})
        auto, post = result['tiktok:auto:42'], result['tiktok:post:42']
        self.assertEqual(auto['key'][0], campaign_key('42', 'Auto-42'))
        self.assertNotEqual(auto['key'][0], post['key'][0])
        self.assertEqual((auto['owner'], auto['tenant']), ('creator', 'tenant'))
        self.assertEqual(auto['publications'], [{'id': 'tiktok:post:receipt-A', 'date': '2026-11-24', 'kind': 'video'}])
        self.assertEqual((post['owner'], post['tenant'], post['drama'], post['platform_only']), ('', '', 'legacy', True))
        self.assertEqual(post['publications'][0]['date'], '2026-11-23')
        self.assertEqual(auto['date'], '2026-11-22')

    def test_no_inference_from_id_name_or_partial_route(self):
        key = campaign_key('42', 'Auto-42')
        for source in [(None, 'Auto-42'), ('0001', 'TTpost'), ('42', ''), ('42', 'auto-42'), ('42', 'Auto-42 ')]:
            self.assertNotEqual(campaign_key(*source), key)
        self.sql(self.post, "UPDATE tt_post_code_route SET af_ad_id='91' WHERE code='AUTO'")
        row = self.catalog()['tiktok:auto:42']
        self.assertNotEqual(row['key'][0], key)
        self.assertEqual(row['publications'][0]['id'], 'tiktok:post:receipt-A')
        self.assertEqual(row['date'], '')

    def test_status_and_first_creator_evidence(self):
        self.sql(self.auto, "UPDATE tt_auto_template SET created_by_user_id='another'")
        self.assertTrue(self.catalog()['tiktok:auto:42']['platform_only'])
        self.sql(self.auto, "UPDATE tt_auto_task SET status='reconciling'")
        self.assertEqual(set(self.catalog()), {'tiktok:post:42'})
        self.sql(self.post, "UPDATE tt_post_queue SET privacy_level='SELF_ONLY'")
        self.assertEqual(self.catalog(), {})
        self.sql(self.auto, "UPDATE tt_auto_task SET status='published',unknown_outcome=1")
        self.assertEqual(self.catalog(), {})

    def test_an_unpublished_route_can_make_a_campaign_ambiguous(self):
        self.route(123, 'COPY', 'Auto-42', '9', '92', 'auto')
        row = self.catalog()['tiktok:auto:42']
        self.assertNotEqual(row['key'][0], campaign_key('42', 'Auto-42'))
        self.assertEqual(row['owner'], 'creator')
        self.assertEqual(len(row['publications']), 1)

    def test_prior_frozen_tenant_and_recycled_route(self):
        initial = self.catalog()
        self.freeze()
        with closing(sqlite3.connect(self.root / 'snapshots/frozen.sqlite3')) as db, db:
            db.executemany('INSERT INTO catalog VALUES(?)', [(json.dumps(row),) for row in initial.values()])
        self.sql(self.users, "UPDATE drama_admin_user SET tenant_key='another-tenant'")
        self.sql(self.post, "DELETE FROM tt_post_code_route WHERE code='AUTO'")
        auto = self.catalog()['tiktok:auto:42']
        self.assertEqual((auto['tenant'], auto['owner'], auto['key']),
                         ('tenant', 'creator', initial['tiktok:auto:42']['key']))
        self.sql(self.auto, "UPDATE tt_auto_template_version SET created_by_user_id='other' WHERE version=1")
        conflict = self.catalog()['tiktok:auto:42']
        self.assertTrue(conflict['platform_only'])
        self.assertNotEqual(conflict['key'], initial['tiktok:auto:42']['key'])

    def test_missing_receipt_time_does_not_become_empty_day(self):
        self.sql(self.post, 'DELETE FROM tt_post_event')
        with self.assertRaisesRegex(ReportError, '确认日期或平台回执'):
            read_tiktok(self.app)


if __name__ == '__main__':
    unittest.main()
