"""Recovery preserves frozen targets and only consumes durable account evidence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from features.fb_ad_asset_delete.core import AssetError
from features.fb_ad_asset_delete.graph import GraphClient, GraphError
from features.fb_ad_asset_delete.service import Service
from features.fb_ad_asset_delete.store import Store
from test_fb_ad_asset_delete_service import FakeSource, GraphState, QueuedWorkers, SESSION
from test_fb_ad_asset_delete_video_account_service import AccountGraph


def inspector(actual, receipts, account='444'):
    graph = GraphClient(Mock())
    graph.read_node = Mock(return_value=dict(id='201', account_id=account,
        asset_feed_spec={'videos': [{'video_id': v} for v in actual]}))
    graph.credential_context = Mock(return_value={})
    graph.check_references = Mock()
    graph.deleted_video_accounts = receipts
    return graph


class ReceiptGraphTests(unittest.TestCase):
    def test_only_missing_videos_with_matching_receipts_are_allowed(self):
        obj = dict(kind='creative', object_id='201', account_ids=['444'], verified_video_ids=['301', '302'])
        for actual in (['301'], []):
            graph = inspector(actual, {'444': {'301', '302'}})
            state, proof = graph.inspect(obj, {'101'})
            self.assertEqual(state, 'pending')
            self.assertEqual(proof['missing_video_ids_confirmed_deleted'], sorted({'301', '302'}-set(actual)))
            self.assertEqual(obj['verified_video_ids'], ['301', '302'])
            graph.check_references.assert_called_once_with(obj, {'101'})
        for actual, receipts in [(['301'], {}), (['301'], {'555': {'302'}}), (['301','999'], {'444': {'302','999'}})]:
            with self.subTest(actual=actual, receipts=receipts):
                with self.assertRaises(GraphError) as caught:
                    inspector(actual, receipts).inspect(obj)
                self.assertEqual(caught.exception.code, 'creative_video_changed')

    def test_receipts_never_override_ownership_or_live_references(self):
        obj = dict(kind='creative', object_id='201', account_ids=['444'], verified_video_ids=['301'])
        with self.assertRaises(GraphError) as caught:
            inspector([], {'555': {'301'}}, account='555').inspect(obj)
        self.assertEqual(caught.exception.code, 'account_mismatch')
        graph = inspector([], {'444': {'301'}})
        graph.check_references.side_effect = GraphError('shared_outside_scope', 'new outside Ad')
        with self.assertRaises(GraphError) as caught:
            graph.inspect(obj)
        self.assertEqual(caught.exception.code, 'shared_outside_scope')

    def test_unknown_failed_and_global_receipts_do_not_prove_account_removal(self):
        row = dict(account_id='444', status='deleted', result=dict(success=True,
            delete_scope='ad_account_video', account_id='444', video_id='301'))
        video = dict(kind='video', object_id='301', account_ids=['444'], video_account_results=[row])
        self.assertEqual(Service._deleted_video_accounts({'objects':[video]}), {'444': {'301'}})
        for status in ('unknown', 'failed', 'pending', 'already_deleted'):
            changed = deepcopy(video); changed['video_account_results'][0]['status'] = status
            self.assertEqual(Service._deleted_video_accounts({'objects':[changed]}), {})
        for field, value in [('success',False), ('delete_scope','global'), ('account_id','555'), ('video_id','999')]:
            changed = deepcopy(video); changed['video_account_results'][0]['result'][field] = value
            self.assertEqual(Service._deleted_video_accounts({'objects':[changed]}), {})


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'ledger.sqlite3')
        self.source, self.state, self.workers = FakeSource(), GraphState(), QueuedWorkers()
        self.state.account_outcomes = {}
        self.real_inspection = False
        test = self
        class Graph(AccountGraph):
            def inspect(self, obj, allowed_ads=None, deleted_creatives=None, check_references=True):
                if test.real_inspection and obj['kind']=='creative':
                    graph = inspector(test.state.creative_videos[obj['object_id']], self.deleted_video_accounts)
                    return graph.inspect(obj, allowed_ads, deleted_creatives, check_references)
                return super().inspect(obj, allowed_ads, deleted_creatives, check_references)
        self.service = Service(self.store, self.source, lambda: Graph(self.state,self.store), spawn=self.workers)

    def preview(self, reason='creative_video_changed'):
        self.source.reference_error = AssetError(reason, 'prior interrupted check')
        result = self.service.preview(SESSION, dict(input_type='content_id',ids=['100'],product_ids=['1']))
        self.workers.run_next();self.job = self.store.get_job(result['job_id']);self.state.job_id=self.job['job_id']
        self.source.reference_error = None;self.state.events.clear()

    def execute(self, phase, request):
        self.service.execute(SESSION,self.job['job_id'],dict(preview_id=self.job['preview_id'],request_id=request,phases=[phase],only_pending=True))
        self.workers.run_next()

    def recheck(self):
        self.service.recheck(SESSION,self.job['job_id'],dict(preview_id=self.job['preview_id'],request_id='recovery-recheck-123456'))
        self.workers.run_next()
        return self.store.get_job(self.job['job_id'])

    def test_account_receipt_releases_original_creative_and_survives_fresh_execution_check(self):
        self.preview();self.execute('video','delete-video-123456')
        self.state.creative_videos['201']=[];self.real_inspection=True
        fresh=self.recheck();creative=next(o for o in fresh['objects'] if o['kind']=='creative')
        self.assertEqual(creative['status'],'pending')
        self.assertEqual(creative['verified_video_ids'],['301'])
        self.assertEqual(creative['result']['missing_video_ids_confirmed_deleted'],['301'])
        self.execute('creative','delete-creative-123456')
        final=self.store.get_job(self.job['job_id'])
        self.assertEqual(next(o for o in final['objects'] if o['kind']=='creative')['status'],'deleted')
        self.assertEqual([e[1] for e in self.state.events if e[0]=='DELETE'],['creative:201'])

    def test_stopped_check_releases_only_after_fresh_inspection(self):
        self.preview('execution_stopped')
        fresh=self.recheck()
        creative=next(o for o in fresh['objects'] if o['kind']=='creative')
        self.assertEqual(creative['status'],'pending')
        self.assertIn(('GET','creative:201'),self.state.events)
        with self.store._transaction(False) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fb_asset_delete_v2_attempts').fetchone()[0],0)

    def test_stopped_checks_can_resume_without_clearing_a_new_read_failure(self):
        self.preview('execution_stopped')
        self.state.inspect_errors['creative:201']=GraphError('read_unavailable','still unavailable')
        fresh=self.recheck()
        creative=next(o for o in fresh['objects'] if o['kind']=='creative')
        self.assertEqual(creative['status'],'blocked')
        self.assertEqual(creative['result']['code'],'read_unavailable')
        self.assertEqual(self.state.deleted_keys(),[])

    def test_cancelled_read_does_not_overwrite_original_blocker(self):
        self.preview('source_unavailable')
        self.state.inspect_errors['creative:201']=GraphError('execution_stopped','another worker stopped')
        fresh=self.recheck()
        self.assertEqual(fresh['recheck']['status'],'interrupted')
        creative=next(o for o in fresh['objects'] if o['kind']=='creative')
        self.assertEqual(creative['result']['code'],'source_unavailable')
        self.assertEqual(self.state.deleted_keys(),[])


if __name__ == '__main__':
    unittest.main()
