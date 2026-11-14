"""Cancellation, history preservation and healthy-target replay contracts."""
import contextlib
import sqlite3
import unittest
from unittest.mock import patch
from scripts.test_x_video_publish_core import PersonalPublishTests, ACTOR, NOW
from features.x_video_publish.account_recovery import reconcile, CODE, locked_accounts
from features.x_posts.service import XPostError

# Reuse the real schema and public batch/queue fixtures, not a mirrored schema.
class AccountRecoveryTests(PersonalPublishTests):
    def setUp(self):
        super().setUp()
        self.single = [{'account_id':'1','material_id':'501','caption':'First'}]

    def apply(self, locked=True):
        with contextlib.closing(self.store.connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            evidence={1:dict(code='x_post_account_locked',queue_id=99,log_id=98)} if locked else {}
            with patch('features.x_video_publish.account_recovery.locked_accounts',return_value=evidence):
                result=reconcile(conn,apply=True,now='2030-01-02T01:00:00Z')
            conn.commit();return result

    def excel(self, rows=None):
        return self.commit(self.preview(rows or self.single,source='excel'))

    def test_locked_pending_excel_is_stopped_and_new_healthy_target_can_commit(self):
        batch=self.excel();r=self.apply();self.assertEqual(r['cancelled_tasks'],1);self.assertEqual(r['released_reservations'],1)
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('select status,error_code from x_post_manual_run').fetchone()[:],('stopped',CODE))
            self.assertEqual(c.execute('select count(*) from x_video_account_cancellation_audit').fetchone()[0],1)
        p=self.preview([{'account_id':'2','material_id':'501','caption':'New target'}],source='excel')
        self.assertEqual(self.commit(p,key='new-healthy-target')['tasks'][0]['account_id'],'2')
        self.assertEqual(self.apply()['cancelled_tasks'],0)

    def test_known_unpublished_queue_cancel_preserves_log_and_ids(self):
        b=self.excel();self.freeze(b['tasks'][0]);r=self.apply();self.assertEqual(r['cancelled_queues'],1)
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('select status from x_post_queue').fetchone()[0],'cancelled')
            self.assertEqual(c.execute('select count(*) from x_post_queue').fetchone()[0],1)

    def test_unknown_and_inflight_are_not_cancelled_or_released(self):
        for log_status,unknown in [('post_creating',1),('media_uploading',0),('reserved',0)]:
            with self.subTest(log_status=log_status):
                b=self.excel();self.freeze(b['tasks'][0])
                with contextlib.closing(self.store.connect()) as c,c:
                    q=c.execute('select id from x_post_queue').fetchone()[0]
                    c.execute("insert into x_post_publish_log(queue_id,account_id,status,unknown_outcome,created_at,updated_at) values(?,1,?,?,?,?)",(q,log_status,unknown,'a','a'))
                r=self.apply();self.assertEqual(r['cancelled_tasks'],0);self.assertEqual(r['retained_inflight_or_unknown'],1)
                self.tearDown();self.setUp()

    def test_published_history_survives_and_normal_target_repeat_still_fenced(self):
        b=self.excel();self.freeze(b['tasks'][0])
        with contextlib.closing(self.store.connect()) as c,c:
            q=c.execute('select id from x_post_queue').fetchone()[0]
            c.execute("update x_post_queue set status='published'")
            c.execute("update x_post_manual_run set status='completed'")
            c.execute("update x_post_manual_material_reservation set state='consumed' where state='active'")
        self.assertEqual(self.apply()['cancelled_tasks'],0)
        p=self.preview(self.single,source='excel');self.assertEqual(p['rows'][0]['error_code'],'x_video_repeat_required')
        new=self.preview([{'account_id':'2','material_id':'501','caption':'New'}],source='excel')
        self.assertEqual(new['valid_count'],1)
        self.assertTrue(self.commit(new,key='different-account')['created'])
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('select status from x_post_queue where id=?',(q,)).fetchone()[0],'published')

    def test_healthy_work_and_non_excel_work_unchanged(self):
        self.excel();self.assertEqual(self.apply(locked=False)['cancelled_tasks'],0)
        self.tearDown();self.setUp()
        self.commit(self.preview(self.single,source='manual'))
        self.assertEqual(self.apply()['cancelled_tasks'],0)

    def test_shared_material_keeps_healthy_pending_reservation(self):
        b=self.excel([{'account_id':'1','material_id':'501','caption':'One'}, {'account_id':'2','material_id':'501','caption':'Two'}])
        r=self.apply();self.assertEqual(r['cancelled_tasks'],1);self.assertEqual(r['released_reservations'],0)
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute("select count(*) from x_post_manual_material_reservation where state='active'").fetchone()[0],1)

    def test_run_unknown_without_queue_retains_reservation(self):
        self.excel()
        with contextlib.closing(self.store.connect()) as c,c:
            c.execute('update x_post_manual_run set unknown_count=1')
        self.assertEqual(self.apply()['cancelled_tasks'],0)

    def test_only_explicit_platform_lock_or_suspension_qualifies(self):
        with contextlib.closing(self.store.connect()) as c,c:
            c.execute('create table x_authorized_account(id integer primary key,last_error text)')
            c.execute("insert into x_authorized_account values(1,'Token失效，请重新登陆')")
            with patch('features.x_video_publish.account_recovery.read_account_publish_blockers',
                       return_value={1:dict(code='x_account_not_publishable')}):
                self.assertEqual(locked_accounts(c,[1]),{})
            c.execute("update x_authorized_account set last_error='X账号已被平台暂停，请登录X处理申诉后重新校验'")
            with patch('features.x_video_publish.account_recovery.read_account_publish_blockers',return_value={}):
                self.assertEqual(locked_accounts(c,[1])[1]['code'],'x_account_suspended')

    def test_mutations_require_transaction_and_dry_run_is_non_mutating(self):
        self.excel()
        with contextlib.closing(self.store.connect()) as c:
            with self.assertRaisesRegex(RuntimeError,'transaction'):reconcile(c,apply=True)
            c.execute('begin immediate')
            with patch('features.x_video_publish.account_recovery.locked_accounts',return_value={1:{}}):r=reconcile(c)
            self.assertEqual(r['would_cancel_tasks'],1)
            self.assertEqual(c.execute('select status from x_post_manual_run').fetchone()[0],'queued');c.rollback()

if __name__=='__main__': unittest.main()
