"""Shared X account operations with real temporary ledgers; no platform calls."""
import copy
import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta
from unittest import mock

from scripts.test_x_video_publish_gateway import GatewayServiceTests, ACTOR
from scripts.test_x_video_publish_pool import PoolTests
from scripts.test_x_video_publish_language import fixture, OWNER
from features.x_accounts import oauth_service as oauth, client
from features.x_video_publish.core import assert_personal_run_publishable
from features.x_video_publish.scheduler import PoolScheduler
from features.x_posts.service import XPostError


class SharedGatewayTests(GatewayServiceTests):
    def test_other_account_manual_excel_and_real_operator(self):
        payload = {'material_ids':['101'], 'account_ids':['3'], 'pairs':[
            {'material_id':'101','account_id':'3','caption_template':'Other account'}]}
        response,_=self.dispatch('POST','/preflight',payload)
        self.assertEqual(response[0],200)
        response,_=self.dispatch('POST','/batches',{'preview_id':response[1]['preview_id'],'idempotency_key':'shared-manual-001'})
        self.assertEqual(response[0],200)
        with closing(self.service.store.connect()) as conn:
            row=conn.execute('SELECT b.tenant_key,b.user_id,t.account_id,t.manual_run_id FROM x_video_publish_task t JOIN x_video_publish_batch b ON b.id=t.batch_id').fetchone()
            self.assertEqual((row['tenant_key'],row['user_id'],row['account_id']),(ACTOR['tenant_key'],ACTOR['user_id'],3))
        assert_personal_run_publishable(self.oauth.POST_DB_PATH,row['manual_run_id'],self.oauth.rows['3'],platform_access=True)
        with self.assertRaises(XPostError):
            assert_personal_run_publishable(self.oauth.POST_DB_PATH,row['manual_run_id'],self.oauth.rows['3'])
        with mock.patch.object(oauth,'POST_DB_PATH',self.oauth.POST_DB_PATH), \
             mock.patch.object(oauth,'find_scoped_account_row',return_value=self.oauth.rows['3']) as lookup, \
             mock.patch.object(oauth,'row_to_item',side_effect=lambda value:value), \
             mock.patch('features.x_video_publish.transport.broker',return_value={'actor':{**ACTOR,'x_platform_access':True}}):
            self.assertIsNotNone(oauth._personal_run_guard(row['manual_run_id']))
            self.assertEqual(lookup.call_args.args[-1],'platform')
        response,_=self.dispatch('POST','/excel/preview',{'rows':[{'row_number':2,'x_user_id':self.oauth.rows['3']['x_user_id'],
            'material_id':'102','caption_template':'Excel other account','processing_mode':'original'}]})
        self.assertEqual(response[0],200)
        self.assertEqual(len(response[1]['items']),1)

    def test_shared_default_copy_preserves_existing_row_and_actual_version(self):
        a={**ACTOR,'x_platform_access':True}
        b={**a,'user_id':'other-operator'}
        account=self.oauth.rows['3']
        self.service.store.save_settings(a,3,{'default_copy':'First','version':0},account)
        self.assertEqual(self.service.store.get_settings(b,3)['default_copy'],'First')
        self.service.store.save_settings(b,3,{'default_copy':'Second','version':1},account)
        self.assertEqual(self.service.store.get_settings(a,3)['default_copy'],'Second')
        with self.assertRaises(XPostError):
            self.service.store.save_settings(a,3,{'default_copy':'Stale','version':1},account)
        with closing(self.service.store.connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM x_video_publish_account_config').fetchone()[0],1)

    def test_old_independent_permission_or_forged_capability_is_not_enough(self):
        with closing(sqlite3.connect(self.job_path)) as conn,conn:
            conn.execute('UPDATE drama_admin_user SET permissions_json=?', ('["x_video_publish"]',))
        payload=self.manual();payload.update(x_platform_access=True,role='admin',actor={**ACTOR,'role':'admin'})
        self.assertEqual(self.dispatch('POST','/preflight',payload)[0][0],403)

    def test_permission_revocation_after_preview_prevents_commit(self):
        response,_=self.dispatch('POST','/preflight',self.manual())
        with closing(sqlite3.connect(self.job_path)) as conn,conn:
            conn.execute("UPDATE drama_admin_user SET permissions_json='[]'")
        self.assertEqual(self.dispatch('POST','/batches',{'preview_id':response[1]['preview_id'],
            'idempotency_key':'revoked-operation'})[0][0],403)
        self.assertEqual(self.service.store.list_batches(ACTOR)['total'],0)


class SharedPoolTests(PoolTests):
    def test_second_operator_edits_existing_pool_and_scheduler_uses_actual_operator(self):
        # Start with an old private pool to prove no reset/migration of its items.
        self.settings(); self.add(['101','102'])
        operator={**self.actor,'user_id':'operator-b','name':'Operator B','x_platform_access':True}
        self.pool.identity_validator=lambda t,u: dict(operator)
        before=self.pool.listing(operator,'1')
        self.assertEqual([r['material_id'] for r in before['items']],['101','102'])
        self.pool.reorder_or_remove(operator,'1',{'expected_version':before['pool']['version'],
            'item_id':before['items'][1]['item_id'],'direction':'up'},False)
        current=self.pool.listing(operator,'1')
        self.pool.toggle(operator,'1',{'expected_version':current['pool']['version']},True)
        scheduler=PoolScheduler(self.pool,now_fn=lambda:self.now)
        scheduler.start('shared-worker')
        self.now += timedelta(minutes=1,seconds=1)
        self.assertEqual(scheduler.tick('shared-worker')['slots_created'],1)
        with closing(self.store.connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM x_video_publish_pool').fetchone()[0],1)
            row=conn.execute('SELECT b.user_id,t.material_id FROM x_video_publish_task t JOIN x_video_publish_batch b ON b.id=t.batch_id').fetchone()
            self.assertEqual((row['user_id'],row['material_id']),('operator-b','102'))
            initiator=conn.execute('SELECT user_id FROM x_video_publish_pool').fetchone()[0]
            self.assertEqual(initiator,'owner-a')

    def test_new_operator_permission_revoked_pauses_shared_pool(self):
        self.actor['x_platform_access']=True
        self.settings();self.add(['101'])
        self.pool.toggle(self.actor,'1',{'expected_version':self.version()},True)
        scheduler=PoolScheduler(self.pool,now_fn=lambda:self.now);scheduler.start('shared-revoked')
        self.identity_active=False;self.now+=timedelta(minutes=1,seconds=1)
        result=scheduler.tick('shared-revoked')
        self.assertEqual(result['slots_created'],0)
        self.assertEqual(self.pool.listing(self.actor,'1')['pool']['status'],'paused')


class AccountScopeTests(unittest.TestCase):
    def test_regular_platform_operator_can_set_all_but_owner_oauth_stays_private(self):
        actor={**OWNER,'user_id':'another-operator','x_platform_access':True}
        with fixture() as f:
            with self.assertRaises(oauth.ServiceError): oauth.find_scoped_account_row(1,actor,'mine')
            self.assertEqual(oauth.find_scoped_account_row(1,actor,'platform')['id'],1)
            oauth.set_account_drama_language(1,'de',actor)
            oauth.set_account_publish_approval(1,False,actor)
            self.assertEqual(oauth.find_account(1)['drama_language'],'de')
            self.assertFalse(oauth.find_account(1)['publish_approved'])
            with self.assertRaises(oauth.ServiceError): oauth.normalize_account_scope(OWNER,'platform')
            with self.assertRaises(oauth.ServiceError): oauth.normalize_account_scope(actor,'all')
            normalized=client.normalize_actor(actor)
            self.assertTrue(normalized['x_platform_access']);self.assertEqual(normalized['role'],'user')


if __name__=='__main__': unittest.main()
