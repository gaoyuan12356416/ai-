from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from meta_violation_cleanup.cover_store import CoverStore, CoverStoreError
from features.fb_ad_asset_delete.store import Store, StoreError


class CoverStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CoverStore(Path(self.tmp.name)/'cover.sqlite3')
        self.store.begin_batch('b1', {'window_start':'2026-10-25','window_end':'2026-10-28'})
        self.candidate = {'account_id':'100','ad_id':'200','creative_id':'300','root_id':'1479','material_ids':['400'],'user_id':'803'}

    def task(self, candidate=None):
        self.store.enqueue('b1',candidate or self.candidate)
        self.store.seal_batch('b1')
        return self.store.ready_tasks()[0]

    def succeeded(self):
        task = self.task()
        self.store.claim_task(task['id'])
        self.store.mark_task_write(task['id'],intent={'new_creative_id':'301','expected_ad':{'id':'200'}})
        self.store.finish_task(task['id'],'succeeded',{'new_creative_id':'301','confirmed_binding':True})
        return task

    def test_trigger_distinct_boundary_and_permanent_evidence(self):
        evidence = {'window_start':'2026-10-25','materials':{
            '400':{'root_ids':['1479'],'count':3,'violating_ad_ids':['1','2','3']},
            '401':{'root_ids':['1479'],'count':2,'violating_ad_ids':['4','5']},
            '402':{'root_ids':['3302'],'count':3,'violating_ad_ids':['6','6','7']}}}
        self.assertEqual(self.store.add_triggers(evidence),1)
        self.assertEqual(self.store.add_triggers(evidence),0)
        self.store.add_triggers({'materials':{}})
        self.assertEqual(set(self.store.triggered_materials()),{'400'})
        self.assertEqual(self.store.triggered_materials()['400']['window_start'],'2026-10-25')

    def test_cleanup_waits_for_same_creative_replacements_without_mutating_queue(self):
        self.succeeded()
        self.store.begin_batch('b2', {})
        self.store.enqueue('b2', dict(self.candidate, ad_id='201'))
        self.store.seal_batch('b2')
        other = self.store.ready_tasks()[0]
        self.assertEqual(self.store.pending_deletions(), [])
        self.assertEqual(self.store.get_delete('100', '300')['status'], 'pending')
        self.store.claim_task(other['id'])
        self.assertEqual(self.store.pending_deletions(), [])
        self.store.mark_task_write(other['id'], intent={'new_creative_id': '302'})
        self.store.finish_task(other['id'], 'unknown', {'new_creative_id': '302'})
        self.assertEqual(self.store.pending_deletions(), [])
        self.store.finish_task(other['id'], 'succeeded',
                               {'new_creative_id': '302', 'confirmed_binding': True})
        self.assertEqual(len(self.store.pending_deletions()), 1)

    def test_failed_scan_or_other_account_does_not_hold_cleanup(self):
        self.succeeded()
        self.store.begin_batch('b2', {})
        self.store.enqueue('b2', dict(self.candidate, ad_id='201'))
        self.store.fail_batch('b2', {'reason': 'scan_interrupted'})
        self.store.begin_batch('b3', {})
        self.store.enqueue('b3', dict(self.candidate, ad_id='202', account_id='101'))
        self.store.seal_batch('b3')
        self.assertEqual(len(self.store.pending_deletions()), 1)

    def test_concurrent_enqueue_and_claim_are_unique(self):
        with ThreadPoolExecutor(max_workers=32) as pool:
            answers = list(pool.map(lambda _:self.store.enqueue('b1',self.candidate),range(64)))
        self.assertEqual(sum(answers),1)
        self.assertEqual(self.store.ready_tasks(),[])
        self.store.seal_batch('b1')
        task = self.store.ready_tasks()[0]
        with ThreadPoolExecutor(max_workers=32) as pool:
            claims = list(pool.map(lambda _:self.store.claim_task(task['id']),range(64)))
        self.assertEqual(sum(claims),1)

    def test_failed_and_success_never_reenqueue(self):
        task = self.task()
        self.store.claim_task(task['id'])
        self.store.finish_task(task['id'],'failed',{'reason':'definite_error'})
        self.store.begin_batch('b2',{})
        self.assertFalse(self.store.enqueue('b2',self.candidate))
        self.assertTrue(self.store.enqueue('b2',dict(self.candidate,creative_id='302')))
        self.assertEqual(len(self.store.existing_pairs()),2)

    def test_failed_partial_scan_can_rehome_only_unstarted_tasks(self):
        self.store.enqueue('b1',self.candidate)
        original_id = next(iter(self.store.known_keys()))
        self.assertEqual(original_id,('100','200','300'))
        self.store.fail_batch('b1',{'reason':'association_read_failed'})
        self.assertEqual(self.store.ready_tasks(),[])
        self.assertEqual(self.store.known_keys(),set())
        self.assertEqual(self.store.notification_candidates()[0]['status'],'failed')
        self.store.begin_batch('b2',{})
        self.assertTrue(self.store.enqueue('b2',dict(self.candidate,user_id='804')))
        self.store.seal_batch('b2')
        task = self.store.ready_tasks()[0]
        self.assertEqual(task['candidate']['user_id'],'804')
        self.assertEqual(task['batch_id'],'b2')
        self.assertEqual(self.store.stats()['tasks'],{'pending':1})
        self.assertEqual(self.store.known_keys(),{('100','200','300')})

    def test_sealed_batch_cannot_be_failed_to_replay_a_write(self):
        task = self.task()
        self.store.claim_task(task['id'])
        self.store.mark_task_write(task['id'],intent={'new_creative_id':'301'})
        with self.assertRaises(CoverStoreError):
            self.store.fail_batch('b1',{'reason':'late_failure'})
        self.store.begin_batch('b2',{})
        self.assertFalse(self.store.enqueue('b2',self.candidate))

    def test_waiting_survives_window_and_returns_in_new_batch(self):
        task = self.task()
        self.store.finish_task(task['id'],'waiting_review',{'reason':'disapproved'})
        self.assertEqual(len(self.store.finishable_batches()),1)
        self.assertEqual(len(self.store.due_waiting(now='2099-01-01')),1)
        self.store.begin_batch('b2',{})
        self.assertTrue(self.store.requeue_waiting(task['id'],'b2'))
        self.store.seal_batch('b2')
        self.assertEqual(self.store.ready_tasks()[0]['batch_id'],'b2')

    def test_terminal_success_requires_binding_proof_and_creative_change(self):
        task = self.task()
        with self.assertRaises(CoverStoreError):
            self.store.finish_task(task['id'],'succeeded',{'new_creative_id':'301'})
        with self.assertRaises(CoverStoreError):
            self.store.enqueue_delete(task['id'])

    def test_write_intent_survives_crash_and_only_readonly_success_resolves(self):
        task = self.task()
        self.store.claim_task(task['id'])
        self.store.mark_task_write(task['id'],intent={'new_creative_id':'301','expected_ad':{'id':'200'}})
        self.store.recover_inflight()
        saved = self.store.get_task(task['id'])
        self.assertEqual(saved['status'],'unknown')
        self.assertEqual(saved['result']['new_creative_id'],'301')
        self.assertFalse(self.store.claim_task(task['id']))
        with self.assertRaises(CoverStoreError):
            self.store.finish_task(task['id'],'failed',{'reason':'no_proof'})
        self.store.finish_task(task['id'],'succeeded',{'confirmed_binding':True,'new_creative_id':'301'})
        self.assertIsNotNone(self.store.get_delete('100','300'))
        self.assertFalse(self.store.enqueue_delete(task['id']))

    def test_no_network_intent_can_reprepare_but_not_created_resource(self):
        task=self.task();self.store.claim_task(task['id'])
        self.store.claim_resource('r','100','b1',{'picture':'https://example.test/cover.jpg'})
        self.store.mark_resource_write('r')
        self.store.recover_inflight()
        self.assertEqual(self.store.get_task(task['id'])['status'],'pending')
        self.assertEqual(self.store.get_resource('r')['status'],'unknown')
        self.assertFalse(self.store.claim_resource('r','100','b1',{'picture':'https://example.test/cover.jpg'})['claimed'])

    def test_only_provably_unsent_interrupted_resource_can_prepare_again(self):
        self.store.claim_resource('unsent','100','b1',{'picture':'url'})
        self.store.claim_resource('failed','100','b1',{'picture':'url'})
        self.store.finish_resource('failed','failed',{'reason':'explicit_failure'})
        recovered=self.store.recover_inflight()
        self.assertEqual(recovered['resources_unsent'],1)
        self.assertEqual(self.store.get_resource('unsent')['status'],'pending')
        self.assertTrue(self.store.claim_resource('unsent','100','b1',{'picture':'url'})['claimed'])
        self.assertFalse(self.store.claim_resource('failed','100','b1',{'picture':'url'})['claimed'])

    def test_resource_singleflight_payload_conflict_and_unsent_defer(self):
        with ThreadPoolExecutor(max_workers=32) as pool:
            answers=list(pool.map(lambda _:self.store.claim_resource('r','100','b1',{'picture':'url'}),range(32)))
        self.assertEqual(sum(x['claimed'] for x in answers),1)
        self.store.defer_resource('r',{'reason':'unsent_stop'})
        self.assertTrue(self.store.claim_resource('r','100','b1',{'picture':'url'})['claimed'])
        self.store.mark_resource_write('r')
        with self.assertRaises(CoverStoreError):self.store.defer_resource('r',{})
        with self.assertRaises(CoverStoreError):self.store.claim_resource('r','100','b1',{'picture':'changed'})

    def test_readonly_resource_proof_resumes_only_unsent_dependency(self):
        self.store.enqueue('b1',self.candidate)
        self.store.enqueue('b1',dict(self.candidate,ad_id='201'))
        self.store.seal_batch('b1')
        tasks=self.store.ready_tasks()
        self.store.claim_resource('r','100','b1',{'picture':'url'})
        self.store.mark_resource_write('r')
        self.store.finish_resource('r','unknown',{'new_creative_id':'301'})
        for task in tasks:
            self.store.claim_task(task['id'])
        self.store.finish_task(tasks[0]['id'],'unknown',{
            'reason':'creative_dependency_unknown','resource':{'resource_key':'r'},'request_sent':False})
        self.store.mark_task_write(tasks[1]['id'],intent={'new_creative_id':'301'})
        self.store.finish_task(tasks[1]['id'],'unknown',{
            'reason':'creative_dependency_unknown','resource':{'resource_key':'r'},'request_sent':True})
        with self.assertRaises(CoverStoreError):
            self.store.resolve_resource('r',{'new_creative_id':'301','verified':True,'readback':{'id':'301','account_id':'101'}})
        resumed=self.store.resolve_resource('r',{
            'new_creative_id':'301','verified':True,'readback':{'id':'301','account_id':'100'}})
        self.assertEqual(resumed,1)
        self.assertEqual(self.store.get_task(tasks[0]['id'])['status'],'pending')
        self.assertEqual(self.store.get_task(tasks[1]['id'])['status'],'unknown')
        self.assertFalse(self.store.claim_resource('r','100','b1',{'picture':'url'})['claimed'])

    def test_unknown_resource_without_saved_id_cannot_guess_recovery(self):
        self.store.claim_resource('r','100','b1',{'picture':'url'})
        self.store.mark_resource_write('r')
        self.store.finish_resource('r','unknown',{})
        with self.assertRaises(CoverStoreError):
            self.store.resolve_resource('r',{'new_creative_id':'301','verified':True,'readback':{'id':'301','account_id':'100'}})

    def test_delete_once_and_failed_result_does_not_revert_replacement(self):
        task=self.succeeded();self.assertIsNotNone(self.store.get_delete('100','300'))
        self.assertFalse(self.store.enqueue_delete(task['id']))
        with ThreadPoolExecutor(max_workers=32) as pool:
            claims=list(pool.map(lambda _:self.store.claim_delete_once('100','300'),range(32)))
        self.assertEqual(sum(claims),1)
        self.store.finish_delete('100','300','failed',{'request_sent':True,'reason':'meta_rejected'})
        self.assertFalse(self.store.claim_delete_once('100','300'))
        self.assertEqual(self.store.get_task(task['id'])['status'],'succeeded')

    def test_reference_wait_does_not_consume_attempt(self):
        task=self.succeeded();self.store.enqueue_delete(task['id'])
        self.store.set_delete_waiting('100','300',{'blockers':['201']})
        self.assertEqual(self.store.get_delete('100','300')['request_sent'],0)
        self.assertTrue(self.store.claim_delete_once('100','300'))
        self.store.recover_inflight()
        self.assertEqual(self.store.get_delete('100','300')['status'],'unknown')
        self.assertFalse(self.store.claim_delete_once('100','300'))
        with self.assertRaises(CoverStoreError):self.store.finish_delete('100','300','deleted_verified',{})
        self.store.finish_delete('100','300','deleted_verified',{'confirmed_deleted':True})

    def test_shared_creative_enqueue_delete_is_unique(self):
        self.store.enqueue('b1',self.candidate)
        self.store.enqueue('b1',dict(self.candidate,ad_id='201'))
        self.store.seal_batch('b1')
        for task in self.store.ready_tasks():
            self.store.finish_task(task['id'],'succeeded',{'confirmed_binding':True,'new_creative_id':'301'})
            self.store.enqueue_delete(task['id'])
        self.assertEqual(len(self.store.pending_deletions()),1)

    def test_success_and_deletion_queue_commit_atomically_with_current_user(self):
        task=self.task();self.store.claim_task(task['id'])
        self.store.finish_task(task['id'],'succeeded',{
            'confirmed_binding':True,'new_creative_id':'301','user_id':'804'})
        reopened=CoverStore(self.store.path)
        self.assertEqual(reopened.get_task(task['id'])['status'],'succeeded')
        self.assertEqual(reopened.get_delete('100','300')['user_id'],'804')
        self.assertFalse(reopened.enqueue_delete(task['id']))

    def test_known_cover_index_survives_reopen_and_prevents_hourly_reconversion(self):
        self.assertFalse(self.store.is_cover_creative('100','301'))
        self.store.claim_resource('r','100','b1',{'picture':'url'})
        self.store.finish_resource('r','accepted',{'new_creative_id':'301'})
        self.assertTrue(self.store.is_cover_creative('100','301'))
        self.assertFalse(self.store.is_cover_creative('101','301'))
        self.succeeded()
        reopened=CoverStore(self.store.path)
        self.assertTrue(reopened.is_cover_creative('100','301'))
        # Upgrade an existing cover ledger without the derived lookup table.
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute('DROP TABLE cover_creatives')
            db.commit()
        self.assertTrue(CoverStore(self.store.path).is_cover_creative('100','301'))

    def test_unchanged_creative_rolls_back_success_and_cleanup_together(self):
        task=self.task();self.store.claim_task(task['id'])
        with self.assertRaises(CoverStoreError):
            self.store.finish_task(task['id'],'succeeded',{
                'confirmed_binding':True,'new_creative_id':'300'})
        self.assertEqual(self.store.get_task(task['id'])['status'],'in_progress')
        self.assertIsNone(self.store.get_delete('100','300'))

    def test_completed_first_cohort_can_notify_with_pending_remainder(self):
        for n in range(257):
            self.store.enqueue('b1',dict(self.candidate,ad_id=str(200+n)))
        self.store.seal_batch('b1')
        for task in self.store.ready_tasks(limit=256):
            self.store.finish_task(task['id'],'failed',{'reason':'definite_failure'})
        self.assertEqual(self.store.notification_candidates(),[])
        partial=self.store.notification_candidates(include_pending=True)[0]
        self.assertEqual(partial['counts']['tasks'],{'failed':256,'pending':1})
        self.store.mark_batch_notified('b1',{
            'counts':partial['counts'],'notification_attempt':partial['notification_attempt']})
        self.assertEqual(self.store.notification_candidates(include_pending=True),[])
        last=self.store.ready_tasks()[0]
        self.store.finish_task(last['id'],'failed',{'reason':'definite_failure'})
        final=self.store.notification_candidates()[0]
        self.assertEqual(final['notification_attempt'],2)
        self.assertEqual(final['counts']['tasks'],{'failed':257})

    def test_late_deletion_change_has_one_new_notification_attempt(self):
        self.succeeded()
        self.assertEqual(self.store.notification_candidates(),[])
        self.store.set_delete_waiting('100','300',{'blockers':['201']})
        first=self.store.notification_candidates()[0]
        self.assertEqual(first['notification_attempt'],1)
        self.store.mark_batch_notified('b1',{
            'counts':first['counts'],'notification_attempt':first['notification_attempt']})
        self.assertEqual(self.store.notification_candidates(),[])
        self.store.claim_delete_once('100','300')
        self.store.finish_delete('100','300','deleted_verified',{'confirmed_deleted':True})
        second=self.store.notification_candidates()[0]
        self.assertEqual(second['notification_attempt'],2)
        self.assertEqual(second['counts']['deletions'],{'deleted_verified':1})
        self.store.mark_batch_notified('b1',{
            'counts':second['counts'],'notification_attempt':second['notification_attempt']})
        self.assertEqual(self.store.notification_candidates(),[])

    def test_secrets_rejected_and_notice_waits_for_actual_work(self):
        with self.assertRaises(CoverStoreError):self.store.enqueue('b1',dict(self.candidate,access_token='SECRET'))
        task=self.task();self.assertEqual(self.store.finishable_batches(),[])
        self.store.finish_task(task['id'],'failed',{'reason':'failure'})
        self.assertEqual(len(self.store.finishable_batches()),1)
        self.store.mark_batch_notified('b1',self.store.stats('b1'))
        self.assertEqual(self.store.finishable_batches(),[])

    def test_review_rehome_and_resource_only_changes_do_not_repeat_notice(self):
        task=self.task()
        self.store.finish_task(task['id'],'waiting_review',{'reason':'disapproved'})
        first=self.store.notification_candidates()[0]
        self.store.mark_batch_notified('b1',{'counts':first['counts'],'notification_attempt':1})
        self.store.begin_batch('b2',{})
        self.store.requeue_waiting(task['id'],'b2')
        self.assertEqual(self.store.notification_candidates(include_pending=True),[])
        self.store.claim_resource('r','100','b1',{'picture':'url'})
        self.store.finish_resource('r','failed',{'reason':'preflight_failed'})
        self.assertEqual(self.store.notification_candidates(include_pending=True),[])


class CoverSharedStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'shared.sqlite3')

    def legacy(self,kind='creative',oid='300',job='j1'):
        self.store.create_job({'job_id':job,'preview_id':'p'+job,'actor':'a','objects':[{'kind':kind,'object_id':oid,'status':'pending'}]})
        return self.store.claim_run(job,'p'+job,'a',[kind],'req'+job)['run_id']

    def test_atomic_reservations_refuse_legacy_and_other_owner(self):
        run=self.legacy()
        self.assertTrue(self.store.reserve_cover_resources('cover:1','100','200',['300'])['claimed'])
        self.assertFalse(self.store.claim_object('j1','creative:300',run)['claimed'])
        self.assertFalse(self.store.reserve_cover_resources('cover:2','100','201',['300'])['claimed'])
        self.assertEqual({r['object_key'] for r in self.store.cover_reservations()},{'ad:200','creative:300'})
        self.store.release_cover_resources('cover:1')
        self.assertTrue(self.store.claim_object('j1','creative:300',run)['claimed'])
        self.assertFalse(self.store.reserve_cover_resources('cover:2','100','201',['300'])['claimed'])

    def test_unknown_reservation_requires_verified_release(self):
        self.store.reserve_cover_resources('cover:1','100','200',['300'])
        self.store.release_cover_resources('cover:1',unknown=True)
        with self.assertRaises(StoreError):self.store.release_cover_resources('cover:1')
        self.assertFalse(self.store.reserve_cover_resources('cover:1','100','200',['300'])['claimed'])
        self.store.release_cover_resources('cover:1',verified=True)
        self.assertEqual(self.store.cover_reservations(),[])

    def test_delete_once_guard_is_visible_to_legacy_and_survives_failure(self):
        run=self.legacy()
        self.store.reserve_cover_resources('cover:delete:300','100',creative_ids=['300'])
        self.assertTrue(self.store.claim_cover_delete_once('cover:delete:300','100','300')['claimed'])
        self.store.finish_cover_delete('cover:delete:300','100','300','failed',{'request_sent':True,'error':'definite'})
        self.assertFalse(self.store.claim_cover_delete_once('cover:again','100','300')['claimed'])
        self.assertFalse(self.store.claim_object('j1','creative:300',run)['claimed'])
        # A later discovered reference can still receive its cover, even though
        # this Creative has already consumed its one cleanup DELETE.
        self.assertTrue(self.store.reserve_cover_resources('cover:later','100','201',['300'])['claimed'])
        self.assertFalse(self.store.claim_cover_delete_once('cover:later','100','300')['claimed'])

    def test_delete_once_global_concurrency(self):
        with ThreadPoolExecutor(max_workers=32) as pool:
            values=list(pool.map(lambda n:self.store.claim_cover_delete_once('cover:'+str(n),'100','300'),range(32)))
        self.assertEqual(sum(v['claimed'] for v in values),1)

    def test_shared_intent_is_readable_after_local_commit_gap(self):
        self.store.claim_cover_delete_once('cover:delete:300','100','300')
        reopened=Store(Path(self.tmp.name)/'shared.sqlite3')
        intent=reopened.get_cover_delete_once('100','300')
        self.assertEqual(intent['owner'],'cover:delete:300')
        self.assertEqual(intent['status'],'in_progress')
        self.assertTrue(intent['result']['request_sent'])
        self.assertIsNone(reopened.get_cover_delete_once('101','300'))
        self.assertFalse(reopened.claim_cover_delete_once('cover:again','100','300')['claimed'])

    def test_historical_definite_failure_does_not_consume_new_authorization(self):
        run=self.legacy();self.store.claim_object('j1','creative:300',run)
        self.store.finish_object('j1','creative:300',run,'failed',{'request_sent':True,'error':'old_reference_exists'})
        self.assertTrue(self.store.claim_cover_delete_once('cover:1','100','300')['claimed'])

    def test_success_receipt_prevents_all_future_writes(self):
        self.assertTrue(self.store.claim_cover_delete_once('cover:1','100','300')['claimed'])
        with self.assertRaises(StoreError):self.store.finish_cover_delete('cover:1','100','300','deleted_verified',{})
        self.store.finish_cover_delete('cover:1','100','300','deleted_verified',{'confirmed_deleted':True,'request_sent':True,'object':{'id':'300','status':'DELETED'}})
        self.assertEqual(self.store.claim_cover_delete_once('cover:2','100','300')['status'],'previous_deleted')
        run=self.legacy()
        self.assertFalse(self.store.claim_object('j1','creative:300',run)['claimed'])
        self.assertEqual(self.store.get_job('j1')['objects'][0]['status'],'already_deleted')

    def test_unknown_delete_only_readonly_deleted_proof_resolves(self):
        self.store.claim_cover_delete_once('cover:1','100','300')
        self.store.finish_cover_delete('cover:1','100','300','unknown',{'request_sent':True})
        with self.assertRaises(StoreError):self.store.finish_cover_delete('cover:1','100','300','failed',{'request_sent':True})
        self.store.finish_cover_delete('cover:1','100','300','deleted_verified',{'confirmed_deleted':True})
        self.assertEqual(self.store.cover_reservations(),[])


if __name__=='__main__':unittest.main()
