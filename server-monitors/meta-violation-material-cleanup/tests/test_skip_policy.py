import copy
import threading
import unittest
from unittest.mock import patch

import test_phased_execution as phased
from test_runner import ad, legacy_lock
from meta_violation_cleanup.runtime import digest
from meta_violation_cleanup.skip_policy import SkipPolicy, failure_reason, ACTOR
from meta_violation_cleanup.throttle import Governor, GovernedSession, RequestNotSent


CONTENT = {'status':'failed','request_sent':True,'response':{'code':100,'error_subcode':2446289}}
ACCOUNT = {'status':'failed','request_sent':True,'response':{'code':200,'error_subcode':2490592}}
DEPENDENCY = {'status':'failed','request_sent':True,'response':{'code':100,'error_subcode':3858800}}


class SkipExecutionTests(unittest.TestCase):
    setUp=phased.PhasedExecutionTests.setUp
    tearDown=phased.PhasedExecutionTests.tearDown
    fixture=phased.PhasedExecutionTests.fixture
    deletes=phased.PhasedExecutionTests.deletes
    prepare=phased.PhasedExecutionTests.prepare
    next_manifest=phased.PhasedExecutionTests.next_manifest

    def seed(self, result, ad_id='10', creative='20', account='100', job_id='old',
             actor=ACTOR, source='scheduled_violation_cleanup', status='failed'):
        self.store.create_job({'job_id':job_id,'preview_id':job_id,'actor':actor,'status':'ready',
            'job_source':source,'objects':[{'kind':'ad','object_id':ad_id,
                'creative_ids':[creative],'account_ids':[account],'user_ids':['1']}]})
        run=self.store.claim_run(job_id,job_id,actor,['ad'],job_id+'-request')['run_id']
        claimed=self.store.claim_object(job_id,'ad:'+ad_id,run)
        self.store.finish_object(job_id,'ad:'+ad_id,run,status,copy.deepcopy(result))
        self.store.finish_run(run,'completed',{})
        return claimed

    def test_content_failure_is_not_repeated_in_shared_phase_or_next_run(self):
        runner,source,graph,runtime,m=self.prepare()
        graph.ads['12']=ad('12','20')
        graph.results[('ad','10')]=CONTENT
        graph.results[('creative','20')]=DEPENDENCY
        runner.execute('test-run',m)
        original=self.store.get_job('policy-cleanup-test-run')
        blocked=next(o for o in original['objects'] if o['key']=='ad:10')['result']
        self.assertEqual(blocked['status'],'blocked')
        self.assertFalse(blocked['request_sent'])
        self.assertTrue(blocked['policy_skip_source']['original_result']['request_sent'])
        self.assertEqual(blocked['policy_skip_reason'],'content_unavailable')
        with self.store._transaction(False) as conn:
            sent=conn.execute("SELECT result FROM fb_asset_delete_v2_attempts WHERE object_key='ad:10' AND status='failed'").fetchone()
        self.assertIn('"request_sent":true',sent['result'])
        second=self.next_manifest(m,runtime)
        runner.execute('second-run',second)
        self.assertEqual(self.deletes(graph).count(('delete','ad','10')),1)
        self.assertEqual(self.deletes(graph).count(('delete','ad','12')),1)
        self.assertEqual(self.deletes(graph).count(('delete','creative','20')),1)
        self.assertEqual(runtime.queue['20']['blocked_ads'][0]['result']['policy_skip_reason'],'content_unavailable')

    def test_account_failure_blocks_its_ads_and_creatives_but_other_account_completes(self):
        runner,source,graph,runtime,m=self.prepare([ad(),ad('11','21'),ad('12','22','200')])
        runner.config['execution_per_account']=1
        graph.results[('ad','10')]=ACCOUNT
        runner.execute('test-run',m)
        self.assertEqual(set(self.deletes(graph)),{('delete','ad','10'),('delete','ad','12'),('delete','creative','22')})
        blocked=[o for o in self.store.get_job('policy-cleanup-test-run')['objects'] if o['status']=='blocked']
        self.assertEqual({o['key'] for o in blocked},{'ad:11','creative:20','creative:21'})
        self.assertTrue(all(o['result']['policy_skip_reason']=='account_not_writable' for o in blocked))

    def test_historical_definite_failure_is_seeded_without_changing_history(self):
        self.seed(CONTENT)
        before=self.store.get_job('old')
        runner,source,graph,runtime,m=self.prepare()
        graph.results[('creative','20')]=DEPENDENCY
        runner.execute('test-run',m)
        self.assertNotIn(('delete','ad','10'),self.deletes(graph))
        self.assertEqual(self.store.get_job('old'),before)
        row=next(o for o in self.store.get_job('policy-cleanup-test-run')['objects'] if o['key']=='ad:10')
        self.assertEqual(row['result']['policy_skip_source']['job_id'],'old')

    def test_new_creative_binding_gets_full_preflight(self):
        self.seed(CONTENT)
        runner,source,graph,runtime,m=self.prepare([ad('10','21')])
        runner.execute('test-run',m)
        self.assertIn(('10','21'),source.context_calls)
        self.assertEqual(self.deletes(graph),[('delete','ad','10'),('delete','creative','21')])

    def test_foreign_actor_and_job_source_cannot_seed_policy(self):
        self.seed(CONTENT,actor='human:other')
        self.seed(ACCOUNT,ad_id='11',job_id='other',source='manual_delete')
        policy=SkipPolicy.from_store(self.store)
        self.assertIsNone(policy.result('ad',{'ad_id':'10','account_id':'100','creative_id':'20'}))

    def test_success_receipt_wins_over_a_previous_content_failure(self):
        self.seed(CONTENT)
        self.seed({'status':'deleted_verified','request_sent':True,'api_accepted':True,'confirmed_deleted':True},
                  job_id='later',status='deleted')
        runner,source,graph,runtime,m=self.prepare()
        runner.execute('test-run',m)
        row=next(o for o in self.store.get_job('policy-cleanup-test-run')['objects'] if o['key']=='ad:10')
        self.assertEqual(row['status'],'already_deleted')
        self.assertNotIn('policy_skip_reason',row['result'])
        self.assertNotIn(('delete','ad','10'),self.deletes(graph))

    def test_unknown_fence_wins_over_a_previous_content_failure(self):
        self.seed(CONTENT)
        self.seed({'status':'unknown','request_sent':True},job_id='later',status='unknown')
        runner,source,graph,runtime,m=self.prepare()
        runner.execute('test-run',m)
        row=next(o for o in self.store.get_job('policy-cleanup-test-run')['objects'] if o['key']=='ad:10')
        self.assertEqual(row['status'],'unknown')
        self.assertEqual(self.deletes(graph),[])

    def test_nested_unknown_or_unsent_error_does_not_seed_policy(self):
        for result in [dict(CONTENT,status='unknown'),dict(CONTENT,request_sent=False),
                       {'status':'failed','request_sent':True,'prior_result':CONTENT},
                       dict(CONTENT,api_accepted=True),dict(CONTENT,response={'code':200,'error_subcode':2446289})]:
            self.assertIsNone(failure_reason('ad',result))

    def test_later_account_write_success_invalidates_legacy_account_failure(self):
        self.seed(ACCOUNT)
        self.seed({'status':'deleted_verified','request_sent':True,'api_accepted':True,'confirmed_deleted':True},
                  ad_id='11',job_id='later',status='deleted')
        with self.store._transaction() as conn:
            conn.execute("UPDATE fb_asset_delete_v2_attempts SET finished_at='2026-10-25T00:00:00Z' WHERE job_id='old'")
            conn.execute("UPDATE fb_asset_delete_v2_attempts SET finished_at='2026-10-26T00:00:00Z' WHERE job_id='later'")
        policy=SkipPolicy.from_store(self.store)
        self.assertIsNone(policy.result('creative',{'ad_id':'10','account_id':'100','creative_id':'20'}))

    def test_explicit_account_skip_survives_origin_receipt_or_unknown_fence(self):
        self.seed(dict(ACCOUNT,policy_skip_reason='account_not_writable'))
        self.seed({'status':'unknown','request_sent':True},job_id='later',status='unknown')
        candidate={'ad_id':'11','account_id':'100','creative_id':'21'}
        self.assertEqual(SkipPolicy.from_store(self.store).result('ad',candidate)['policy_skip_reason'],'account_not_writable')
        self.store.reconcile_object('later','ad:10','deleted',{'confirmed_deleted':True,'proof':{'status':'DELETED'}})
        self.assertEqual(SkipPolicy.from_store(self.store).result('ad',candidate)['policy_skip_reason'],'account_not_writable')

    def test_imported_legacy_account_skip_survives_origin_receipt(self):
        self.seed(ACCOUNT)
        runner,source,graph,runtime,m=self.prepare([ad('11','21')])
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[])
        self.seed({'status':'unknown','request_sent':True},job_id='later',status='unknown')
        candidate={'ad_id':'12','account_id':'100','creative_id':'22'}
        self.assertEqual(SkipPolicy.from_store(self.store).result('ad',candidate)['policy_skip_reason'],'account_not_writable')
        self.store.reconcile_object('later','ad:10','deleted',{'confirmed_deleted':True,'proof':{'status':'DELETED'}})
        self.assertEqual(SkipPolicy.from_store(self.store).result('ad',candidate)['policy_skip_reason'],'account_not_writable')

    def test_real_graph_delete_boundary_blocks_after_identity_get(self):
        from types import SimpleNamespace
        import time
        from test_graph import Response,Session,ad as graph_ad
        from meta_violation_cleanup.graph import GraphClient
        from meta_violation_cleanup.concurrent_execution import ConcurrentExecutor
        policy=SkipPolicy();candidate={'ad_id':'10','account_id':'100','creative_id':'20','user_id':'1'}
        got_identity=threading.Event();resume=threading.Event();results=[]
        class PausingSession(Session):
            def request(self,method,*args,**kwargs):
                result=super().request(method,*args,**kwargs)
                if method=='GET':got_identity.set();self.assert_resumed=resume.wait(2)
                return result
        session=PausingSession([Response(graph_ad())])
        graph=GraphClient({'1':'synthetic'},session=session)
        graph.http=GovernedSession(session,Governor(rps=10000),lambda:False)
        executor=object.__new__(ConcurrentExecutor);executor.ttl=30;executor.stopped=lambda:False
        executor.manifest={};executor.claim_sources={('ad','10'):{'attempt_id':'claimed'}}
        executor.runner=SimpleNamespace(skip_policy=policy,preflight=lambda *args,**kwargs:{'eligible':True})
        def execution_graph(account,write_check):
            graph.http.account=account;graph.http.write_check=write_check;return graph
        executor.graph=execution_graph
        thread=threading.Thread(target=lambda:results.append(executor.write('ad',candidate,None,time.monotonic(),{})))
        thread.start();self.assertTrue(got_identity.wait(2))
        policy.observe('ad',candidate,ACCOUNT,{'job_id':'other','attempt_id':'failure'})
        resume.set();thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([call[0] for call in session.calls],['GET'])
        self.assertEqual(results[0]['status'],'blocked');self.assertFalse(results[0]['request_sent'])

    def test_request_waiting_on_governor_checks_skip_after_admission(self):
        policy=SkipPolicy();candidate={'ad_id':'10','account_id':'100','creative_id':'20'}
        governor=Governor(1,1,1000)
        sent=[];result=[];entered=threading.Event()
        class Session:
            def request(self,*args,**kwargs):sent.append(args)
        http=GovernedSession(Session(),governor,lambda:False)
        http.account='100';http.write_check=lambda:policy.check('ad',candidate)
        def write():
            entered.set()
            try:http.request('DELETE','unused')
            except RequestNotSent as exc:result.append(exc.code)
        with governor.slot('100',lambda:False):
            thread=threading.Thread(target=write);thread.start();self.assertTrue(entered.wait(2))
            policy.observe('ad',candidate,ACCOUNT,{'job_id':'old','attempt_id':'proof'})
        thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(sent,[]);self.assertEqual(result,['policy_skip'])


class ParallelSkipExecutionTests(SkipExecutionTests):
    def prepare(self,ads=None):
        values=super().prepare(ads)
        values[0].config.update(execution_workers=4,execution_per_account=2,execution_batch_size=8)
        p=patch('meta_violation_cleanup.concurrent_execution.legacy_delete_lock',legacy_lock)
        p.start();self.addCleanup(p.stop)
        return values

    def test_account_rejection_drains_already_started_peer_and_skips_next_wave(self):
        runner,source,graph,runtime,m=self.prepare([ad(),ad('11','21'),ad('12','22')])
        graph.results[('ad','10')]=ACCOUNT
        both_started=threading.Barrier(2);original=graph.delete
        def delete(kind,identifier,*args):
            if kind=='ad' and identifier in ('10','11'):both_started.wait(timeout=2)
            return original(kind,identifier,*args)
        graph.delete=delete
        runner.execute('test-run',m)
        self.assertEqual(set(self.deletes(graph)),{('delete','ad','10'),('delete','ad','11')})
        rows={o['key']:o for o in self.store.get_job('policy-cleanup-test-run')['objects']}
        self.assertEqual(rows['ad:11']['status'],'deleted')
        self.assertTrue(rows['ad:11']['result']['request_sent'])
        self.assertEqual(rows['ad:12']['status'],'blocked')
        self.assertFalse(rows['ad:12']['result']['request_sent'])


if __name__=='__main__':unittest.main()
