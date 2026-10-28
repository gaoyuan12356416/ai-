import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from tests.test_x_attribution import Fixture
from features.x_posts import attribution as a
from features.x_posts import pool_owner_migration as m
from features.x_posts.pool_ownership import UNASSIGNED


class PoolOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.fx=Fixture(self.temp.name)
        self.before=[self.fx.resolver.resolve(i,i) for i in range(1,5)]
        with closing(sqlite3.connect(self.fx.x)) as c:
            c.executescript('''ALTER TABLE x_post_queue ADD COLUMN source_type TEXT DEFAULT 'material';
            ALTER TABLE x_post_queue ADD COLUMN pool_item_id INTEGER;
            ALTER TABLE x_post_queue ADD COLUMN drama_pool_item_id INTEGER;
            ALTER TABLE x_post_queue ADD COLUMN schedule_run_id INTEGER;
            CREATE TABLE x_post_material_pool(id INTEGER PRIMARY KEY,material_id TEXT,created_by_user_id TEXT,created_by_name TEXT,created_at TEXT);
            CREATE TABLE x_post_drama_pool(id INTEGER PRIMARY KEY,content_id TEXT,created_by_user_id TEXT,created_by_name TEXT,created_at TEXT);
            INSERT INTO x_post_material_pool VALUES(10,'903','editor','入池人','2026-10-18T00:00:00Z');
            INSERT INTO x_post_drama_pool VALUES(20,'104','creator','剧目入池人','2026-10-18T00:00:00Z');
            UPDATE x_post_queue SET pool_item_id=10,schedule_run_id=1 WHERE id=3;
            UPDATE x_post_queue SET source_type='drama',drama_pool_item_id=20,schedule_run_id=2 WHERE id=4;''')
            c.commit()

    def freeze_old(self):
        for i,context in enumerate(self.before,1):a.freeze(self.fx.x,self.fx.log(i),context)

    def test_new_pool_uses_exact_material_and_drama_creator(self):
        for i,owner,sub in [(3,'editor','43'),(4,'creator','42')]:
            ctx=self.fx.resolver.resolve(i,i)
            self.assertEqual((ctx['owner'],ctx['source_owner'],ctx['sub_user_id']),(owner,owner,sub))
            self.assertEqual(ctx['owner_basis'],'x-pool-creator-v1')

    def test_manual_and_template_keep_their_own_source(self):
        self.fx.execute(self.fx.x,'UPDATE x_post_queue SET pool_item_id=10 WHERE id IN (1,2)')
        self.assertEqual(self.fx.resolver.resolve(1,1)['owner'],'creator')
        self.assertEqual(self.fx.resolver.resolve(2,2)['owner'],'editor')

    def test_mapping_missing_does_not_transfer_pool_owner(self):
        self.fx.ids.side_effect=lambda _:[]
        self.fx.resolver._id_cache.clear()
        ctx=self.fx.resolver.resolve(3,3)
        self.assertEqual((ctx['owner'],ctx['sub_user_id']),('editor','248'))
        self.assertEqual(ctx['fallback_reason'],'pool_url_publisher_mapping_missing')

    def test_mismatch_and_ambiguous_tenant_fail_instead_of_admin_fallback(self):
        self.fx.execute(self.fx.x,"UPDATE x_post_material_pool SET material_id='other'")
        with self.assertRaises(a.AttributionError):self.fx.resolver.resolve(3,3)
        self.fx.execute(self.fx.x,"UPDATE x_post_material_pool SET material_id='903'")
        self.fx.execute(self.fx.users,"INSERT INTO drama_admin_user VALUES('editor','other','other@example.test','other')")
        with self.assertRaises(a.AttributionError):self.fx.resolver.resolve(3,3)

    def test_history_apply_idempotent_and_rollback_preserves_urls_and_publications(self):
        self.freeze_old();facts=self.fx.facts();logs=self.fx.rows('x_post_publish_log')
        manifest=m.plan(self.fx.x,self.fx.users)
        self.assertEqual(len(manifest['rows']),2)
        self.assertEqual(m.apply(self.fx.x,manifest)['changed'],2)
        self.assertEqual(m.apply(self.fx.x,manifest)['changed'],0)
        self.assertEqual(m.plan(self.fx.x,self.fx.users)['rows'],[])
        self.assertEqual(self.fx.resolver.resolve(3,3)['owner'],'editor')
        self.assertEqual(self.fx.resolver.resolve(3,3)['sub_user_id'],'248')
        for i in (3,4):self.assertEqual(a.build_url(self.before[i-1],self.fx.log(i)),a.build_url(a.load_frozen(self.fx.x,i),self.fx.log(i)))
        self.assertEqual(self.fx.facts(),facts);self.assertEqual(self.fx.rows('x_post_publish_log'),logs)
        self.assertEqual(m.apply(self.fx.x,manifest,rollback=True)['changed'],2)
        self.assertEqual(m.apply(self.fx.x,manifest,rollback=True)['changed'],0)
        self.assertEqual(a.load_frozen(self.fx.x,3),self.before[2])

    def test_historical_service_actor_is_explicitly_unassigned(self):
        self.freeze_old();self.fx.execute(self.fx.x,"UPDATE x_post_drama_pool SET created_by_user_id='recovery-service'")
        manifest=m.plan(self.fx.x,self.fx.users);m.apply(self.fx.x,manifest)
        ctx=a.load_frozen(self.fx.x,4)
        self.assertEqual(ctx['owner'],UNASSIGNED)
        self.assertEqual(ctx['source_owner'],'recovery-service')
        self.assertEqual(ctx['fallback_reason'],'pool_creator_not_found')

    def test_plan_drift_is_atomic(self):
        self.freeze_old();manifest=m.plan(self.fx.x,self.fx.users)
        self.fx.execute(self.fx.x,"UPDATE x_post_drama_pool SET created_by_user_id='editor'")
        with self.assertRaises(a.AttributionError):m.apply(self.fx.x,manifest)
        self.assertEqual(a.load_frozen(self.fx.x,3),self.before[2])


if __name__=='__main__':unittest.main()
