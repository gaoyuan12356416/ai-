"""Offline tests for durable cross-channel drama profile assignments."""
import contextlib
import json
import sqlite3
import sys
import unittest
import tempfile
from features.drama_audience import store as audience_store
from pathlib import Path
from datetime import datetime
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from features.x_posts import service, drama_profile
from features import social_random_schedule as target_random
from scripts import test_x_post_multi_schedule_store as fixtures


def snapshot(weights=None, version='portrait-one'):
    weights=weights or {2:(20,90),3:(90,80),4:(90,50)}
    data={'accounts':{}}
    for account_id,(audience,theme) in weights.items():
        data['accounts'][str(9000+account_id)]={
            'hierarchy_version':drama_profile.HIERARCHY_VERSION,'revenue_cents':10000,
            'audiences':[{'key':'female','matchable':True,'revenue_cents':audience*100,'share_bp':audience*100,
                'children':[{'key':'ceo_wealth','matchable':True,'revenue_cents':audience*theme,'share_bp':theme*100}]}]}
    return data,{'version':version,'hierarchy_version':drama_profile.HIERARCHY_VERSION,
                 'tag_mapping_version':drama_profile.TAG_VERSION,'audience_taxonomy_version':drama_profile.TAXONOMY_VERSION}


class DramaProfileTests(unittest.TestCase):
    def setUp(self):
        self.audience_tmp=tempfile.TemporaryDirectory()
        self.audience_db=str(Path(self.audience_tmp.name)/'audience.sqlite3')
        audience_store.initialize(self.audience_db)
        self.audience_env=mock.patch.dict('os.environ',{'DRAMA_AUDIENCE_DB':self.audience_db});self.audience_env.start()
        self.fixture=fixtures.XPostMultiScheduleStoreTests(methodName='runTest');self.fixture.setUp()
        self.store=self.fixture.store;self.path=self.fixture.db_path
        self.fixture.save_schedule('drama',[2,3,4],['09:00','10:00'])
        self.fixture.claim_schedule_slot('2026-07-27','09:00')
        self.accounts=[{'id':v,'x_user_id':str(9000+v),'drama_language':'en','publish_eligible':True} for v in [2,3,4]]

    def tearDown(self):
        self.fixture.tearDown();self.audience_env.stop();self.audience_tmp.cleanup()

    def add(self,name='D1',labels='female,CEO'):
        result=self.fixture.add_drama(name,labels=labels)
        audience_store.save(dict(content_id=result['content_id'],languages=['en'],audience_labels=['女性'],audience='female',status='matched',name=name),self.audience_db)
        return result

    def freeze(self,profiles=None):
        return self.store.prepare_drama_profile_plan('2026-07-27','09:00',2,self.accounts,profiles or snapshot())

    def available(self,key,ids=None,languages=None):
        return self.store.available_drama_pool_items(1000,account_ids=ids or [2,3,4],
            configured_account_ids=[2,3,4],account_languages=languages or {2:'en',3:'en',4:'en'},profile_scope_key=key)

    def test_highest_audience_then_theme_picks_channel_not_first_slot(self):
        pool=self.add();key=self.freeze()
        self.assertEqual([],self.available(key,[2]))
        chosen=self.available(key,[3])
        self.assertEqual([(pool['id'],3)],[(v['id'],v['candidate_account_id']) for v in chosen])
        self.assertEqual('portrait-one',chosen[0]['channel_profile']['profile_version'])

    def test_profile_refresh_does_not_redraw_later_slot(self):
        self.add();key=self.freeze()
        self.freeze(snapshot({2:(100,100),3:(1,1),4:(1,1)},version='new-portrait'))
        self.assertEqual([],self.available(key,[2]))
        self.assertEqual(3,self.available(key,[3])[0]['candidate_account_id'])

    def test_missing_profile_uses_configured_order(self):
        self.add();key=self.freeze(({'accounts':{}},{'unavailable':True}))
        chosen=self.available(key)
        self.assertEqual(2,chosen[0]['candidate_account_id'])
        self.assertEqual('portrait_unavailable',chosen[0]['channel_profile']['fallback_reason'])

    def test_same_language_only_and_language_drift_is_not_reassigned(self):
        self.add();self.accounts[1]['drama_language']='ja'
        key=self.freeze()
        self.assertEqual(4,self.available(key,languages={2:'en',3:'ja',4:'en'})[0]['candidate_account_id'])
        self.assertEqual([],self.available(key,languages={2:'en',3:'ja',4:'ja'}))

    def test_existing_unfinished_binding_is_unchanged(self):
        pool=self.add()
        self.store.create_schedule_plan('drama','2026-07-27','09:00',2,[self.fixture.drama_candidate(pool,2,1)])
        key=self.freeze()
        # Already-reserved bound episodes are not offered again or reassigned.
        self.assertEqual([],self.available(key))
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(2,conn.execute('SELECT assigned_account_id FROM x_post_drama_pool WHERE id=?',(pool['id'],)).fetchone()[0])
            self.assertEqual(0,conn.execute('SELECT count(*) FROM x_post_drama_profile_assignment').fetchone()[0])

    def test_reservation_rejects_wrong_free_channel_then_binds_correct_one(self):
        pool=self.add();key=self.freeze()
        with self.assertRaises(service.XPostError) as caught:
            self.store.create_schedule_plan('drama','2026-07-27','09:00',2,[self.fixture.drama_candidate(pool,2,1)],drama_profile_scope_key=key)
        self.assertEqual('x_post_drama_assignment_conflict',caught.exception.code)
        result=self.store.create_schedule_plan('drama','2026-07-27','09:00',2,[self.fixture.drama_candidate(pool,3,1)],drama_profile_scope_key=key)
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            saved=conn.execute('SELECT account_id,queue_id FROM x_post_drama_profile_assignment').fetchone()
            self.assertEqual((3,result['queues'][0]['id']),saved)

    def test_scope_and_version_changes_have_distinct_freezes(self):
        self.add();key=self.freeze()
        self.assertNotEqual(key,drama_profile.scope_key('2026-07-27',2,[3,2,4]))
        with self.assertRaises(service.XPostError):self.store.drama_profile_scope('2026-07-27','09:00',3)
        self.assertEqual([],self.available(drama_profile.scope_key('2026-07-27',3,[2,3,4])))

    def test_priority_pool_order_preserved_while_channels_use_profile(self):
        first=self.add('D1');second=self.add('D2')
        self.store.set_drama_pool_priority(first['id'],True,{'user_id':'admin-1','name':'Admin'})
        key=self.freeze();found={v['candidate_account_id']:v['id'] for v in self.available(key)}
        self.assertEqual(first['id'],found[3]);self.assertEqual(second['id'],found[4])

    def test_new_upload_does_not_steal_frozen_channel(self):
        first=self.add('D1');key=self.freeze();self.add('D2')
        self.freeze();found={v['candidate_account_id']:v['id'] for v in self.available(key)}
        self.assertEqual(first['id'],found[3])
        self.assertEqual(2,len(found))

    def test_zero_queue_validation_failure_releases_capacity_but_keeps_audit(self):
        first=self.add('D1');key=self.freeze()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:conn.execute("UPDATE x_post_drama_pool SET last_error_code='metadata_unavailable' WHERE id=?",(first['id'],))
        second=self.add('D2');self.freeze()
        found=self.available(key)
        self.assertEqual([(second['id'],3)],[(v['id'],v['candidate_account_id']) for v in found])
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:conn.execute("UPDATE x_post_drama_pool SET last_error_code='' WHERE id=?",(first['id'],))
        self.freeze();self.assertEqual(1,len(self.available(key)))
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            old=conn.execute('SELECT released_at,release_reason FROM x_post_drama_profile_assignment WHERE pool_item_id=?',(first['id'],)).fetchone()
            self.assertTrue(old[0]);self.assertIn('metadata_unavailable',old[1])

    def test_deleted_pool_releases_capacity_without_deleting_audit(self):
        first=self.add('D1');key=self.freeze();self.store.delete_drama_pool_item(first['id'])
        second=self.add('D2');self.freeze()
        self.assertEqual((second['id'],3),(self.available(key)[0]['id'],self.available(key)[0]['candidate_account_id']))
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:self.assertEqual(2,conn.execute('SELECT count(*) FROM x_post_drama_profile_assignment').fetchone()[0])

    def test_legacy_unbound_pool_with_any_queue_history_is_never_released(self):
        first=self.add('D1');key=self.freeze();second=self.add('D2')
        # A minimal legacy copy represents queue evidence predating today's
        # binding guards. The publisher must conservatively retain its hold.
        with contextlib.closing(sqlite3.connect(':memory:')) as conn, conn:
            conn.row_factory=sqlite3.Row
            conn.execute('ATTACH DATABASE ? AS fixture',(str(self.path),))
            conn.execute('CREATE TABLE x_post_drama_pool AS SELECT * FROM fixture.x_post_drama_pool')
            conn.execute(drama_profile.DDL)
            conn.execute('INSERT INTO x_post_drama_profile_assignment SELECT * FROM fixture.x_post_drama_profile_assignment')
            conn.execute('CREATE TABLE x_post_queue(drama_pool_item_id INTEGER,drama_replay_generation INTEGER,episode_key TEXT,unknown_outcome INTEGER)')
            conn.execute("INSERT INTO x_post_queue VALUES(?,1,'D1:1',1)",(first['id'],))
            conn.execute("UPDATE x_post_drama_pool SET last_error_code='unknown_outcome' WHERE id=?",(first['id'],))
            drama_profile.freeze_assignments(conn,run_date='2026-07-27',config_version=2,
                configured_account_ids=[2,3,4],accounts=self.accounts,snapshot=snapshot(),blocked_accounts={},timestamp='2026-07-27T02:00:00Z')
            saved=drama_profile.load_assignments(conn,key)
            self.assertEqual({first['id']:3,second['id']:4},{v['pool_item_id']:v['account_id'] for v in saved})

    def test_target_random_slot_uses_full_frozen_day_scope(self):
        self.add()
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            target_random.ensure_schema(conn)
            target_random.ensure_day_plan(conn,'x:pool:drama','2026-07-27',2,
                {'source_type':'drama','account_ids':[2,3,4]},[2,3,4],lambda target:['09:00'])
            conn.execute("UPDATE x_post_schedule_run SET account_ids_json='[2]',expected_count=1,schedule_policy=? WHERE source_type='drama' AND publish_time='09:00'",(target_random.POLICY,))
        self.assertEqual([2,3,4],self.store.drama_profile_scope('2026-07-27','09:00',2))
        key=self.freeze();self.assertEqual([],self.available(key,[2]));self.assertEqual(3,self.available(key,[3])[0]['candidate_account_id'])

    def test_portrait_failure_is_visible_fallback_without_secret(self):
        from features.x_tag_publish.transport import Error
        with mock.patch.dict('os.environ',{'X_DRAMA_PROFILE_INTERNAL_TOKEN':'test-only'}),mock.patch('features.x_tag_publish.transport.rpc',side_effect=Error('unavailable')):
            self.assertTrue(drama_profile.portrait_snapshot(self.accounts)[1]['unavailable'])

    def test_available_api_freezes_full_scope_before_returning_single_slot(self):
        from features.x_accounts import oauth_service as api
        self.add()
        by_id={a['id']:a for a in self.accounts}
        with mock.patch.object(api,'POST_DB_PATH',self.path),mock.patch.object(api,'_manual_publish_accounts',return_value=[by_id[2]]),mock.patch.object(api,'find_account',side_effect=lambda value:by_id[value]),mock.patch('features.x_posts.drama_profile.portrait_snapshot',return_value=snapshot()) as read:
            response=api.available_post_drama_pool_request({'limit':1000,'account_ids':[2],
                'schedule_identity':{'run_date':'2026-07-27','publish_time':'09:00','version':2}})
        self.assertEqual([],response['items'])
        self.assertEqual([2,3,4],[a['id'] for a in read.call_args.args[0]])
        self.assertEqual(3,self.available(drama_profile.scope_key('2026-07-27',2,[2,3,4]),[3])[0]['candidate_account_id'])

    def test_other_scope_cannot_create_second_binding_after_first_reserves(self):
        pool=self.add();first=self.freeze()
        # A different day/config may prepare a different recommendation while
        # no queue exists. The canonical reservation still allows one owner.
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory=sqlite3.Row
            second=drama_profile.freeze_assignments(conn,run_date='2026-07-28',config_version=2,
                configured_account_ids=[2,3,4],accounts=self.accounts,snapshot=snapshot({2:(100,100),3:(1,1),4:(1,1)}),blocked_accounts={},timestamp='2026-07-28T00:00:00Z')
        self.store.create_schedule_plan('drama','2026-07-27','09:00',2,[self.fixture.drama_candidate(pool,3,1)],drama_profile_scope_key=first)
        self.fixture.claim_schedule_slot('2026-07-27','10:00')
        with self.assertRaises(service.XPostError):
            self.store.create_schedule_plan('drama','2026-07-27','10:00',2,[self.fixture.drama_candidate(pool,2,1)],drama_profile_scope_key=second)
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(1,conn.execute('SELECT count(*) FROM x_post_queue').fetchone()[0])
            self.assertEqual(3,conn.execute('SELECT assigned_account_id FROM x_post_drama_pool').fetchone()[0])


if __name__=='__main__':unittest.main()
