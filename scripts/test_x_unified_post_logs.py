"""Real two-ledger SQLite pagination and cross-lane identity regression."""
import contextlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace
from scripts import test_pipeline_x_native as fixture
from features.x_posts.log_query import query_logs
from features.x_posts.log_management import LogManager,dispatch

class UnifiedLogTests(unittest.TestCase):
    def setUp(self):
        self.old=fixture.NativeTests();self.old.setUp();self.addCleanup(self.old.doCleanups)
        self.profile=fixture.NativeTests();self.profile.setUp();self.addCleanup(self.profile.doCleanups)
        self.q1=self.old.queue(auto=True);self.q2=self.old.queue(identity=3,auto=False)
        self.p1=self.profile.queue(identity=4,auto=True);self.p2=self.profile.queue(identity=5,auto=False)
        self.main=self.old.runtime.POST_DB_PATH;self.tag=self.profile.runtime.POST_DB_PATH
        for path,dates in [(self.main,['2026-11-14T01:00:00Z','2026-11-14T03:00:00Z']),
                           (self.tag,['2026-11-14T02:00:00Z','2026-11-14T04:00:00Z'])]:
            with sqlite3.connect(path) as c:
                for qid,stamp in enumerate(dates,1):c.execute("update x_post_queue set created_at=? where id=?",(stamp,qid))
        # Deliberate same queue/log IDs across lanes, with different receipts.
        for store,q,code in [(self.old.bridge.store,self.q1,'ordinary-failure'),(self.profile.bridge.store,self.p1,'profile-failure')]:
            log=store.reserve_log(q['id']);store.mark_failed(log['id'],code,'known failure',False)

    def read(self,**params):
        return query_logs(self.main,params,profile_db=self.tag)

    def test_global_pages_are_interleaved_without_duplicates_or_gaps(self):
        first=self.read(page=1,page_size=2);second=self.read(page=2,page_size=2)
        self.assertEqual(first['pagination']['total'],4)
        self.assertEqual([(r['ledger_source'],r['queue_id']) for r in first['items']+second['items']],
                         [('profile',2),('ordinary',2),('profile',1),('ordinary',1)])

    def test_auto_filter_includes_both_lanes_with_separate_same_id_receipts(self):
        r=self.read(task_source='auto_publish');self.assertEqual(r['pagination']['total'],2)
        self.assertEqual([(x['ledger_source'],x['error_code']) for x in r['items']],
                         [('profile','profile-failure'),('ordinary','ordinary-failure')])

    def test_date_account_material_and_result_filters_apply_to_both_sources(self):
        self.assertEqual(self.read(run_date='2026-11-13')['pagination']['total'],0)
        self.assertEqual(self.read(run_date=self.q1['run_date'],status='failed')['pagination']['total'],2)
        r=self.read(account_id=4,material_id='501');self.assertEqual(r['pagination']['total'],1)
        self.assertEqual(r['items'][0]['ledger_source'],'profile')
        self.assertEqual(self.read(unknown_outcome=1)['pagination']['total'],0)

    def test_manual_filter_retains_both_lanes(self):
        self.assertEqual(self.read(task_source='manual_publish')['pagination']['total'],2)

    def test_missing_profile_source_fails_instead_of_false_complete_totals(self):
        with self.assertRaises(sqlite3.Error):query_logs(self.main,{},profile_db=self.tag.parent/'absent.sqlite')
        self.assertFalse((self.tag.parent/'absent.sqlite').exists())

    def test_profile_controls_are_disabled_even_when_ids_collide(self):
        m=LogManager(self.main,self.main.parent/'no-dispatch.sqlite')
        r=m.query_unified({},profile_db=self.tag)
        for item in r['items']:
            if item['ledger_source']=='profile':
                self.assertFalse(item['can_reset']);self.assertFalse(item['can_stop'])
                self.assertIn('画像',item['reset_reason'])

    def test_query_performs_zero_business_dml(self):
        def facts(p):
            with sqlite3.connect(p) as c:
                return {name:c.execute('select * from '+name+' order by rowid').fetchall() for name in
                        ['x_post_queue','x_post_publish_log','x_post_manual_run','x_post_repost_ledger']}
        before=[facts(self.main),facts(self.tag)];self.read();self.assertEqual(before,[facts(self.main),facts(self.tag)])

    def test_admin_gate_is_required_before_unified_query(self):
        handler=Mock();handler._require_cookie_admin.return_value=False
        app={'json_response':Mock()}
        with patch.object(LogManager,'query_unified') as query:
            dispatch(handler,SimpleNamespace(query=''),app);query.assert_not_called()

    def test_get_dispatch_uses_unified_query(self):
        h=Mock();h._require_cookie_admin.return_value=True;h.command='GET'
        app={'json_response':Mock(),'x_post_admin_query_params':lambda q:{'task_source':'auto_publish'}}
        with patch.object(LogManager,'query_unified',return_value={'items':[]}) as query:
            dispatch(h,SimpleNamespace(query='task_source=auto_publish'),app)
            query.assert_called_once_with({'task_source':'auto_publish'})
            self.assertEqual(app['json_response'].call_args.args[1],200)

if __name__=='__main__':unittest.main()
