"""Behavior tests use real SQLite claims and simulated Meta state only."""
import copy
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from features.fb_ad_asset_delete.store import Store
from meta_violation_cleanup.cover_store import CoverStore
from meta_violation_cleanup.cover_worker import CoverWorker
from meta_violation_cleanup.graph import GraphError


def ad(aid='10', cid='20', account='100', status='ACTIVE'):
    return {'id': aid, 'account_id': account, 'name': 'keep ad name',
            'status': status, 'configured_status': status, 'effective_status': status,
            'creative': {'id': cid}, 'campaign_id': '400', 'adset_id': '300',
            'campaign': {'id': '400', 'status': 'ACTIVE'},
            'adset': {'id': '300', 'status': 'ACTIVE'},
            'tracking_specs': [{'action.type': ['offsite_conversion'], 'fb_pixel': ['5']}],
            'conversion_specs': [], 'issues_info': [], 'ad_review_feedback': {}}


def creative(cid='20', account='100'):
    return {'id': cid, 'account_id': account, 'name': 'keep creative name',
            'status': 'ACTIVE', 'object_type': 'VIDEO',
            'object_story_spec': {'page_id': '7', 'video_data': {
                'video_id': '8', 'title': 'title', 'message': 'message',
                'call_to_action': {'type': 'LEARN_MORE', 'value': {'link': 'https://landing.example/?utm=x'}}}},
            'url_tags': 'source=original'}


class Source:
    def __init__(self):
        self.owner = threading.get_ident()
        self.calls = []
        self.errors = {}

    def prepare_candidates(self, tasks):
        assert threading.get_ident() == self.owner, 'SQL must run on owner'
        self.calls.append([r['id'] for r in tasks])
        return {str(row['id']): dict(row['candidate'], validated=row['ad_id'] not in self.errors,
            cover_url='https://cdn.example/drama.jpg', cover_version='',
            error=self.errors.get(row['ad_id'])) for row in tasks}


class Graph:
    def __init__(self, ads, creatives):
        self.ads = {x['id']: copy.deepcopy(x) for x in ads}
        self.creatives = {x['id']: copy.deepcopy(x) for x in creatives}
        self.calls = []
        self.create_result = None
        self.bind_results = {}
        self.delete_results = {}
        self.mutex = threading.Lock()
        self.next_id = 1000
        self.fail_get_ads = set()
        self.bind_unknown_applies = True
        self.bind_changes_name = False
        self.reference_error = False
        self.post_bind_get_error = False
        self.delete_read_error = False
        self.delete_identity_error = False
        self.new_creative_read_error = False
        self.bound = set()

    def close(self):
        pass

    def get_cover_ad(self, aid, user, account):
        with self.mutex:
            self.calls.append(('get_ad', aid))
            if aid in self.fail_get_ads or self.post_bind_get_error and aid in self.bound:
                raise GraphError('read_failed', 'read unavailable')
            return copy.deepcopy(self.ads[aid])

    def get_creative(self, cid, user, account):
        with self.mutex:
            self.calls.append(('get_creative', cid))
            if self.new_creative_read_error and int(cid)>=1000:
                raise GraphError('read_failed','readback temporarily unavailable')
            return copy.deepcopy(self.creatives[cid])

    def reconcile(self, kind, cid, user, account):
        if self.delete_read_error:
            return {'status':'unknown','error':{'code':100}}
        actual = self.get_creative(cid, user, account)
        return {'status':'deleted_verified' if actual['status']=='DELETED' else 'present', 'object':actual}

    def create_creative(self, account, user, payload, before):
        before()
        with self.mutex:
            self.calls.append(('create', account))
            if self.create_result:
                return copy.deepcopy(self.create_result)
            cid = str(self.next_id); self.next_id += 1
            self.creatives[cid] = dict(copy.deepcopy(payload), id=cid, account_id=account,
                                      status='ACTIVE', object_type='SHARE')
            return {'status': 'accepted', 'request_sent': True, 'new_creative_id': cid}

    def bind_creative(self, aid, cid, user, account, before):
        before()
        with self.mutex:
            self.calls.append(('bind', aid, cid))
            result = copy.deepcopy(self.bind_results.get(aid, {'status': 'accepted', 'request_sent': True}))
            if result['status'] == 'accepted' or result['status'] == 'unknown' and self.bind_unknown_applies:
                self.ads[aid]['creative'] = {'id': cid}
                self.bound.add(aid)
                if self.bind_changes_name:
                    self.ads[aid]['name'] = 'unexpected edit'
            return result

    def list_reference_ads(self, account, user):
        with self.mutex:
            self.calls.append(('references', account))
            if self.reference_error:
                raise GraphError('page_failed', 'inventory incomplete')
            return copy.deepcopy([a for a in self.ads.values() if a['account_id'] == account and a['status'] != 'DELETED'])

    def iter_target_ads(self, targets, stop):
        records = {x['ad_id']: copy.deepcopy(self.ads[x['ad_id']]) for x in targets}
        yield ('1', targets[0]['account_id'], tuple(records)), records, {}

    def get_reference_ads(self, identifiers, user, account):
        self.calls.append(('known_references', tuple(identifiers)))
        return {aid:copy.deepcopy(self.ads[aid]) for aid in identifiers}, {}

    def delete_creative(self, cid, user, account, before):
        if self.delete_identity_error:
            raise GraphError('read_failed','identity unavailable')
        if self.creatives[cid]['status'] == 'DELETED':
            return {'status': 'already_deleted', 'request_sent': False, 'confirmed_deleted': True,
                    'object': copy.deepcopy(self.creatives[cid])}
        before()
        with self.mutex:
            self.calls.append(('delete', cid))
            result = copy.deepcopy(self.delete_results.get(cid, {'status': 'accepted', 'request_sent': True,
                                                                'response': {'http_status': 200, 'success': True}}))
            if result['status'] == 'accepted':
                self.creatives[cid]['status'] = 'DELETED'
            return result


class WorkerTests(unittest.TestCase):
    def test_accepted_bind_stale_readback_is_isolated_and_never_replayed(self):
        original = ad()
        self.enqueue([original])

        class StaleBindingGraph(Graph):
            stale = True

            def get_cover_ad(self, aid, user, account):
                value = super().get_cover_ad(aid, user, account)
                if self.stale and aid in self.bound:
                    value['creative'] = copy.deepcopy(original['creative'])
                return value

        graph = StaleBindingGraph([original], [creative()])
        worker = self.worker(graph)
        result = worker.run_once(max_ads=1, process_deletes=False)
        self.assertFalse(result['halted'])
        task = self.store.get_task(1)
        self.assertEqual(task['status'], 'unknown')
        self.assertTrue(task['request_sent'])
        self.assertEqual(task['result']['reason'], 'binding_result_unknown')
        self.assertEqual(self.store.stats()['deletions'], {})
        self.assertTrue(any(r['status'] == 'unknown' for r in self.shared.cover_reservations()))
        worker.run_once(max_ads=1, process_deletes=False)
        self.assertEqual(worker.reconcile_unknown()['unresolved_tasks'], 1)
        self.assertEqual(sum(c[0] == 'bind' for c in graph.calls), 1)
        graph.stale = False
        self.assertEqual(worker.reconcile_unknown()['tasks_confirmed'], 1)
        self.assertEqual(self.store.get_task(1)['status'], 'succeeded')
        self.assertEqual(sum(c[0] == 'bind' for c in graph.calls), 1)
        self.assertEqual(self.store.get_delete('100', '20')['status'], 'pending')

    def test_missing_derived_tracking_is_unknown_then_reconciled_without_second_bind(self):
        original=ad();original['tracking_specs'].append(
            {'action.type':['post_engagement'],'page':['7'],'post':['9']})
        old=creative();old['effective_object_story_id']='7_9';self.enqueue([original])
        class DelayedTrackingGraph(Graph):
            def bind_creative(self,aid,cid,user,account,before):
                result=super().bind_creative(aid,cid,user,account,before)
                self.creatives[cid]['effective_object_story_id']='7_10'
                self.ads[aid]['tracking_specs']=[r for r in self.ads[aid]['tracking_specs'] if 'post' not in r]
                return result
        graph=DelayedTrackingGraph([original],[old]);worker=self.worker(graph)
        worker.run_once(max_ads=1,process_deletes=False)
        task=self.store.get_task(1)
        self.assertEqual(task['status'],'unknown');self.assertFalse(worker.halted.is_set())
        self.assertEqual(self.store.stats()['deletions'],{})
        graph.ads['10']['tracking_specs'].append(
            {'action.type':['post_engagement'],'page':['7'],'post':['10']})
        self.assertEqual(worker.reconcile_unknown()['tasks_confirmed'],1)
        self.assertEqual(self.store.get_task(1)['status'],'succeeded')
        self.assertEqual(sum(c[0]=='bind' for c in graph.calls),1)

    def test_post_identity_materializes_only_after_bind(self):
        for available in (True, False):
            with self.subTest(available=available):
                aid = '10' if available else '11'
                original = ad(aid); original['tracking_specs'].append(
                    {'action.type':['post_engagement'],'page':['7'],'post':['9']})
                old = creative(); old['effective_object_story_id'] = '7_9'
                self.enqueue([original],batch='materialized-'+aid)
                class MaterializingGraph(Graph):
                    def bind_creative(self, aid, cid, user, account, before):
                        result = super().bind_creative(aid,cid,user,account,before)
                        self.ads[aid]['tracking_specs'][-1]['post'] = ['10']
                        if available: self.creatives[cid]['effective_object_story_id'] = '7_10'
                        return result
                graph = MaterializingGraph([original],[old]); worker = self.worker(graph)
                worker.run_once(max_ads=1,process_deletes=False)
                task = self.store.get_task(1 if available else 2)
                self.assertFalse(worker.halted.is_set())
                self.assertEqual(task['status'],'succeeded' if available else 'unknown')
                if not available:
                    graph.creatives[task['result']['new_creative_id']]['effective_object_story_id'] = '7_10'
                    self.assertEqual(worker.reconcile_unknown()['tasks_confirmed'],1)
                    self.assertEqual(self.store.get_task(task['id'])['status'],'succeeded')
                self.assertEqual(sum(call[0]=='bind' for call in graph.calls),1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CoverStore(pathlib.Path(self.temp.name) / 'cover.sqlite3')
        self.shared = Store(pathlib.Path(self.temp.name) / 'shared.sqlite3')
        self.source = Source()
        self.config = {'cover_live_enabled': True, 'execution_workers': 32,
                       'execution_batch_size': 64, 'execution_context_ttl_seconds': 30}

    def enqueue(self, ads, batch='batch-1'):
        self.store.begin_batch(batch, {})
        for item in ads:
            self.store.enqueue(batch, {'account_id': item['account_id'], 'ad_id': item['id'],
                'old_creative_id': item['creative']['id'], 'user_id': '1', 'root_id': '1479', 'material_ids': ['501']})
        self.store.seal_batch(batch)

    def worker(self, graph, **kwargs):
        value = CoverWorker(self.config, self.store, self.source, lambda: graph, self.shared, **kwargs)
        self.addCleanup(value.close)
        return value

    def test_success_keeps_ad_id_and_only_then_deletes_old_once(self):
        original = ad(); self.enqueue([original]); graph = Graph([original], [creative()])
        worker = self.worker(graph); report = worker.run_once()
        self.assertEqual(report['tasks'], {'succeeded': 1})
        self.assertEqual(graph.ads['10']['name'], original['name'])
        self.assertEqual(graph.ads['10']['tracking_specs'], original['tracking_specs'])
        self.assertNotEqual(graph.ads['10']['creative']['id'], '20')
        self.assertEqual(self.store.get_delete('100', '20')['status'], 'deleted_verified')
        self.assertLess(next(i for i,c in enumerate(graph.calls) if c[0]=='bind'), next(i for i,c in enumerate(graph.calls) if c[0]=='delete'))
        worker.run_once()
        self.assertEqual(len([c for c in graph.calls if c[0]=='delete']), 1)

    def test_clear_active_and_paused_allowed_reviewing_ad_waits(self):
        ads = [ad('10','20'), ad('11','21',status='PAUSED'), ad('12','22')]
        ads[2]['effective_status'] = 'DISAPPROVED'
        self.enqueue(ads); graph = Graph(ads, [creative('20'),creative('21'),creative('22')])
        report = self.worker(graph).run_once(process_deletes=False)
        # Shared new Creative reservations may defer one valid task rather than
        # send competing writes. A subsequent wave safely completes it.
        self.worker(graph).run_once(process_deletes=False)
        statuses = self.store.stats()['tasks']
        self.assertEqual(statuses.get('succeeded'), 2)
        self.assertEqual(statuses.get('waiting_review'), 1)
        self.assertFalse(any(c[0]=='bind' and c[1]=='12' for c in graph.calls))

    def test_failure_continues_and_does_not_delete_failed_ad_creative(self):
        ads = [ad('10','20'),ad('11','21')]; self.enqueue(ads)
        graph = Graph(ads,[creative('20'),creative('21')]); graph.bind_results['10'] = {'status':'failed','request_sent':True,'code':613}
        worker = self.worker(graph); worker.run_once(); worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'], 'failed')
        self.assertEqual(self.store.get_task(2)['status'], 'succeeded')
        self.assertIsNone(self.store.get_delete('100','20'))
        self.assertEqual(len([c for c in graph.calls if c[0]=='bind' and c[1]=='10']),1)

    def test_shared_archived_reference_preserves_old_creative(self):
        target=ad(); archived=ad('90','20',status='ARCHIVED'); self.enqueue([target])
        graph=Graph([target,archived],[creative()]); self.worker(graph).run_once()
        row=self.store.get_delete('100','20')
        self.assertEqual(row['status'],'waiting_references')
        self.assertEqual(row['result']['blocker_ad_ids'],['90'])
        self.assertFalse(any(c[0]=='delete' for c in graph.calls))

    def test_unreadable_known_reference_survives_hourly_rechecks(self):
        target=ad(); archived=ad('90','20',status='ARCHIVED'); self.enqueue([target])
        graph=Graph([target,archived],[creative()]); worker=self.worker(graph); worker.run_once()
        graph.get_reference_ads=lambda ids,user,account: ({},{aid:{'code':100} for aid in ids})
        for _ in range(2):
            ready, outputs=worker._scan_references([self.store.get_delete('100','20')])
            self.assertEqual(ready,[])
            row=self.store.get_delete('100','20')
            self.assertEqual(row['result']['blocker_ad_ids'],['90'])
            self.assertEqual(row['result']['unresolved_ad_ids'],['90'])
        self.assertEqual(len([c for c in graph.calls if c[0]=='references']),1)
        self.assertFalse(any(c[0]=='delete' for c in graph.calls))

    def test_shared_creative_created_and_deleted_once_for_multiple_ads(self):
        ads=[ad('10','20'),ad('11','20')]; self.enqueue(ads)
        graph=Graph(ads,[creative()]); worker=self.worker(graph)
        worker.run_once(process_deletes=False); worker.run_once()
        self.assertEqual(self.store.stats()['tasks'].get('succeeded'),2)
        self.assertEqual(len([c for c in graph.calls if c[0]=='create']),1)
        self.assertEqual(len([c for c in graph.calls if c[0]=='delete']),1)

    def test_delete_failure_does_not_undo_success_or_repeat(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.delete_results['20']={'status':'failed','request_sent':True,'code':100}
        worker=self.worker(graph); worker.run_once(); worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'succeeded')
        self.assertEqual(self.store.get_delete('100','20')['status'],'failed')
        self.assertEqual(len([c for c in graph.calls if c[0]=='delete']),1)

    def test_incomplete_inventory_never_deletes(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()]); graph.reference_error=True
        self.worker(graph).run_once()
        self.assertEqual(self.store.get_delete('100','20')['status'],'reference_scan_incomplete')
        self.assertFalse(any(c[0]=='delete' for c in graph.calls))

    def test_unknown_bind_resolves_readonly_and_enqueues_cleanup(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.post_bind_get_error=True; graph.bind_results['10']={'status':'unknown','request_sent':True}
        worker=self.worker(graph); worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'unknown')
        self.assertIsNone(self.store.get_delete('100','20'))
        writes=[c for c in graph.calls if c[0] in ('create','bind','delete')]
        graph.post_bind_get_error=False
        result=worker.reconcile_unknown()
        self.assertEqual(result['tasks_confirmed'],1)
        self.assertEqual(writes,[c for c in graph.calls if c[0] in ('create','bind','delete')])
        self.assertEqual(self.shared.cover_reservations(),[])
        self.assertIsNotNone(self.store.get_delete('100','20'))

    def test_unknown_bind_not_applied_is_never_retried(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.bind_unknown_applies=False; graph.bind_results['10']={'status':'unknown','request_sent':True}
        worker=self.worker(graph); worker.run_once(); worker.run_once(); worker.reconcile_unknown()
        self.assertEqual(self.store.get_task(1)['status'],'unknown')
        self.assertEqual(len([c for c in graph.calls if c[0]=='bind']),1)
        self.assertFalse(any(c[0]=='delete' for c in graph.calls))

    def test_ad_parameter_drift_after_bind_halts_without_cleanup(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()]); graph.bind_changes_name=True
        result=self.worker(graph).run_once()
        self.assertTrue(result['halted'])
        self.assertEqual(self.store.get_task(1)['status'],'unknown')
        self.assertFalse(any(c[0]=='delete' for c in graph.calls))

    def test_unsupported_creative_and_cover_error_have_no_write(self):
        original=ad(); self.enqueue([original]); old=creative(); old['asset_feed_spec']={'images':[{'hash':'old'}]}
        graph=Graph([original],[old]); self.worker(graph).run_once()
        self.assertEqual(self.store.get_task(1)['status'],'failed')
        self.assertFalse(any(c[0] in ('create','bind','delete') for c in graph.calls))

    def test_first_batch_cap_preserves_untouched_queue(self):
        ads=[ad(str(10+i),str(20+i),str(100+i)) for i in range(4)]; self.enqueue(ads)
        graph=Graph(ads,[creative(str(20+i),str(100+i)) for i in range(4)])
        self.worker(graph).run_once(max_ads=2,process_deletes=False)
        self.assertEqual(self.store.stats()['tasks'],{'pending':2,'succeeded':2})
        self.assertEqual(sum(map(len,self.source.calls)),2)

    def test_unknown_delete_only_readback_can_confirm(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.delete_results['20']={'status':'unknown','request_sent':True}
        worker=self.worker(graph); worker.run_once(); worker.run_once()
        self.assertEqual(self.store.get_delete('100','20')['status'],'unknown')
        graph.creatives['20']['status']='DELETED'
        self.assertEqual(worker.reconcile_unknown()['deletions_confirmed'],1)
        self.assertEqual(len([c for c in graph.calls if c[0]=='delete']),1)

    def test_successful_delete_receipt_survives_inaccessible_readback(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.delete_read_error=True
        self.worker(graph).run_once()
        row=self.store.get_delete('100','20')
        self.assertEqual(row['status'],'deleted_verified')
        self.assertEqual(row['result']['proof_source'],'delete_api_receipt')

    def test_historical_shared_receipt_is_reused_without_delete(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        worker=self.worker(graph); worker.run_once(process_deletes=False)
        owner='earlier-cover-flow'
        self.shared.claim_cover_delete_once(owner,'100','20')
        self.shared.finish_cover_delete(owner,'100','20','deleted_verified',{'confirmed_deleted':True})
        worker.process_deletions()
        self.assertEqual(self.store.get_delete('100','20')['status'],'previous_deleted')
        self.assertFalse(any(c[0]=='delete' for c in graph.calls))

    def test_shared_intent_before_local_intent_crash_never_sends_delete(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        worker=self.worker(graph); worker.run_once(process_deletes=False)
        owner='cover-delete:100:20'
        self.shared.claim_cover_delete_once(owner,'100','20')
        worker.process_deletions()
        self.assertEqual(self.store.get_delete('100','20')['status'],'unknown')
        worker.run_once(); worker.reconcile_unknown()
        self.assertFalse(any(c[0]=='delete' for c in graph.calls))
        graph.creatives['20']['status']='DELETED'
        self.assertEqual(worker.reconcile_unknown()['deletions_confirmed'],1)
        self.assertEqual(self.shared.get_cover_delete_once('100','20')['status'],'deleted_verified')

    def test_prior_shared_delete_failure_consumes_attempt_without_retry(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        worker=self.worker(graph); worker.run_once(process_deletes=False)
        owner='cover-delete:100:20'
        self.shared.claim_cover_delete_once(owner,'100','20')
        self.shared.finish_cover_delete(owner,'100','20','failed',{'reason':'explicit_failure'})
        worker.process_deletions(); worker.run_once()
        self.assertEqual(self.store.get_delete('100','20')['status'],'failed')
        self.assertFalse(any(c[0]=='delete' for c in graph.calls))

    def test_crash_after_delete_intent_is_reconciled_without_repeating(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        worker=self.worker(graph); worker.run_once(process_deletes=False)
        delete=graph.delete_creative
        def crash(*args):
            delete(*args)
            raise SystemExit('simulated worker loss')
        graph.delete_creative=crash
        with self.assertRaises(SystemExit):
            worker.process_deletions()
        self.store.recover_inflight()
        graph.delete_creative=delete
        restarted=self.worker(graph)
        self.assertEqual(restarted.reconcile_unknown()['deletions_confirmed'],1)
        restarted.run_once()
        self.assertEqual(len([c for c in graph.calls if c[0]=='delete']),1)

    def test_identity_read_failure_defers_only_cleanup(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.delete_identity_error=True
        report=self.worker(graph).run_once()
        self.assertFalse(report['halted'])
        self.assertEqual(self.store.get_task(1)['status'],'succeeded')
        self.assertEqual(self.store.get_delete('100','20')['request_sent'],0)
        self.assertEqual(self.store.get_delete('100','20')['status'],'reference_scan_incomplete')

    def test_context_expiry_preserves_created_resource_and_refreshes_before_bind(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        tick=[0.0]; create=graph.create_creative
        def slow_create(*args):
            result=create(*args); tick[0]+=31; return result
        graph.create_creative=slow_create
        worker=self.worker(graph,clock=lambda:tick[0]); worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'pending')
        self.assertFalse(any(c[0]=='bind' for c in graph.calls))
        worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'succeeded')
        self.assertEqual(len([c for c in graph.calls if c[0]=='create']),1)

    def test_cover_fetch_rejection_and_unknown_create_are_not_retried(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.create_result={'status':'failed','request_sent':True,'code':'image_fetch_failed'}
        worker=self.worker(graph); worker.run_once(); worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'failed')
        self.assertEqual(len([c for c in graph.calls if c[0]=='create']),1)
        self.assertFalse(any(c[0] in ('bind','delete') for c in graph.calls))

    def test_unknown_create_without_id_never_repeats(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.create_result={'status':'unknown','request_sent':True}
        worker=self.worker(graph); worker.run_once(); worker.reconcile_unknown(); worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'unknown')
        self.assertEqual(len([c for c in graph.calls if c[0]=='create']),1)
        self.assertFalse(any(c[0] in ('bind','delete') for c in graph.calls))

    def test_known_created_id_is_verified_readonly_then_bound_without_recreate(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.new_creative_read_error=True
        worker=self.worker(graph); worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'unknown')
        self.assertFalse(any(c[0]=='bind' for c in graph.calls))
        graph.new_creative_read_error=False
        writes=[c for c in graph.calls if c[0] in ('create','bind','delete')]
        report=worker.reconcile_unknown()
        self.assertEqual(report['resources_confirmed'],1)
        self.assertEqual(report['tasks_resumed'],1)
        self.assertEqual(writes,[c for c in graph.calls if c[0] in ('create','bind','delete')])
        worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'succeeded')
        self.assertEqual(len([c for c in graph.calls if c[0]=='create']),1)

    def test_reference_checks_are_merged_by_account(self):
        ads=[ad('10','20'),ad('11','21')]; self.enqueue(ads)
        graph=Graph(ads,[creative('20'),creative('21')]); worker=self.worker(graph)
        worker.run_once(process_deletes=False); worker.run_once(process_deletes=False)
        worker.process_deletions()
        self.assertEqual(len([c for c in graph.calls if c[0]=='references']),1)
        self.assertEqual(len([c for c in graph.calls if c[0]=='delete']),2)

    def test_stopped_worker_sends_no_mutation(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        self.worker(graph,stop=lambda:True).run_once()
        self.assertEqual(self.store.get_task(1)['status'],'pending')
        self.assertEqual(graph.calls,[])

    def test_shared_unknown_fence_blocks_before_any_create(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        self.shared.reserve_cover_resources('legacy-unknown','100',creative_ids=('20',))
        self.shared.release_cover_resources('legacy-unknown',unknown=True)
        worker=self.worker(graph); worker.run_once(); worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'blocked')
        self.assertFalse(any(c[0] in ('create','bind','delete') for c in graph.calls))

    def test_recover_completed_binding_releases_stale_reservations(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        worker=self.worker(graph); worker.run_once(process_deletes=False)
        new_id=self.store.get_task(1)['result']['new_creative_id']
        self.shared.reserve_cover_resources('cover-task:1','100',ad_id='10',creative_ids=('20',new_id))
        self.assertEqual(worker.recover_reservations()['task_reservations_released'],1)
        self.assertEqual(self.shared.cover_reservations(),[])
        worker.process_deletions()
        self.assertEqual(self.store.get_delete('100','20')['status'],'deleted_verified')

    def test_recover_retains_unknown_binding_fence(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        graph.bind_unknown_applies=False; graph.bind_results['10']={'status':'unknown','request_sent':True}
        worker=self.worker(graph); worker.run_once()
        before=self.shared.cover_reservations()
        self.assertEqual(worker.recover_reservations()['retained'],1)
        self.assertEqual(self.shared.cover_reservations(),before)

    def test_recover_completes_durable_delete_receipt_without_delete(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        worker=self.worker(graph); worker.run_once(process_deletes=False)
        owner='cover-delete:100:20'; self.shared.claim_cover_delete_once(owner,'100','20')
        self.store.claim_delete_once('100','20')
        receipt={'confirmed_deleted':True,'response':{'success':True,'http_status':200},'proof_source':'delete_api_receipt'}
        self.store.finish_delete('100','20','deleted_verified',receipt)
        self.assertEqual(worker.recover_reservations()['delete_receipts_completed'],1)
        self.assertEqual(self.shared.cover_reservations(),[])
        self.assertEqual(self.shared.get_cover_delete_once('100','20')['status'],'deleted_verified')
        self.assertFalse(any(c[0]=='delete' for c in graph.calls))

    def test_recover_releases_unsent_task_reservation_after_requeue(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        self.store.claim_task(1)
        self.shared.reserve_cover_resources('cover-task:1','100',ad_id='10',creative_ids=('20',))
        self.store.recover_inflight()
        worker=self.worker(graph)
        self.assertEqual(worker.recover_reservations()['task_reservations_released'],1)
        worker.run_once()
        self.assertEqual(self.store.get_task(1)['status'],'succeeded')

    def test_hourly_scan_of_updated_binding_never_replaces_cover_again(self):
        original=ad(); self.enqueue([original]); graph=Graph([original],[creative()])
        worker=self.worker(graph); worker.run_once()
        writes=[c for c in graph.calls if c[0] in ('create','bind','delete')]
        self.enqueue([graph.ads['10']],batch='next-hour')
        worker.run_once()
        self.assertEqual(self.store.get_task(2)['status'],'obsolete')
        self.assertEqual(self.store.get_task(2)['result']['reason'],'cover_already_applied')
        self.assertEqual(writes,[c for c in graph.calls if c[0] in ('create','bind','delete')])


if __name__ == '__main__':
    unittest.main()
