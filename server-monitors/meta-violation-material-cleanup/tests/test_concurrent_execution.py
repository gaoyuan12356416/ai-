import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_phased_execution as phased_fixtures
from test_runner import ad,legacy_lock,RuntimeLedgerError
import test_sql_source as sql_fixtures
from test_graph import Response,Session,ad as graph_ad
from meta_violation_cleanup.current_contexts import current_contexts
from meta_violation_cleanup.concurrent_execution import waves
from meta_violation_cleanup.concurrent_execution import ConcurrentExecutor
from meta_violation_cleanup.throttle import Governor,GovernedSession,RequestNotSent
from meta_violation_cleanup.graph import GraphClient
from meta_violation_cleanup.runtime import digest


class ParallelTests(phased_fixtures.PhasedExecutionTests):
    def prepare(self,ads=None):
        values=super().prepare(ads)
        values[0].config.update(execution_workers=4,execution_per_account=2,execution_batch_size=8)
        p=patch('meta_violation_cleanup.concurrent_execution.legacy_delete_lock',legacy_lock)
        p.start();self.addCleanup(p.stop)
        return values

    def test_real_overlap_and_global_phase_barrier(self):
        ads=[ad(str(10+i),str(20+i),str(100+i)) for i in range(4)]
        runner,source,graph,runtime,m=self.prepare(ads)
        lock=threading.Lock();active=0;peak=0;finished=[]
        original=graph.delete
        def delete(kind,*args):
            nonlocal active,peak
            with lock:
                if kind=='creative':self.assertEqual(len(finished),4)
                active+=1;peak=max(peak,active)
            time.sleep(.03)
            try:return original(kind,*args)
            finally:
                with lock:
                    active-=1
                    if kind=='ad':finished.append(args[0])
        graph.delete=delete
        owner=threading.get_ident();context=source.current_ad_context
        def checked_context(*args):
            self.assertEqual(threading.get_ident(),owner);return context(*args)
        source.current_ad_context=checked_context
        runner.execute('test-run',m)
        self.assertGreater(peak,1)
        self.assertEqual(runtime.queue,{})

    def test_last_changed_ad_does_not_replace_confirmed_creative_basis(self):
        runner,source,graph,runtime,m=self.prepare([ad(),ad('11','20')])
        graph.ads['11']['effective_status']='DISAPPROVED'
        runner.execute('test-run',m)
        self.assertIn(('delete','ad','10'),graph.calls)
        self.assertNotIn(('delete','ad','11'),graph.calls)
        self.assertIn(('delete','creative','20'),graph.calls)
        self.assertEqual(runtime.queue,{})

    def test_unknown_ad_is_not_retried_or_followed_by_creative(self):
        runner,source,graph,runtime,m=self.prepare()
        graph.results[('ad','10')]={'status':'unknown','request_sent':True}
        graph.verifications[('ad','10')]={'status':'unknown'}
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[('delete','ad','10')])

    def test_resumed_ad_is_blocked_by_final_full_policy_guard(self):
        runner,source,graph,runtime,m=self.prepare()
        m['evidence']['materials']['501']['count']=1
        m['evidence']['materials']['501']['violating_ad_ids']=['101']
        m['sha256']=digest({k:v for k,v in m.items() if k!='sha256'})
        graph.ads['10']=ad(status='ACTIVE')
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[])

    def test_accepted_write_with_failed_readback_stays_unknown(self):
        runner,source,graph,runtime,m=self.prepare()
        graph.verifications[('ad','10')]=ValueError('readback unavailable')
        runner.execute('test-run',m)
        row=next(o for o in self.store.get_job('policy-cleanup-test-run')['objects'] if o['key']=='ad:10')
        self.assertEqual(row['status'],'unknown');self.assertTrue(row['result']['api_accepted'])
        self.assertEqual(self.deletes(graph),[('delete','ad','10')])
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[('delete','ad','10')])

    def test_durable_barrier_failure_stops_all_new_writes(self):
        runner,source,graph,runtime,m=self.prepare([ad(),ad('11','21')])
        runtime.fail_barrier=1
        with self.assertRaises(RuntimeLedgerError):runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[])
        self.assertEqual(self.store.get_job('policy-cleanup-test-run')['objects'][0]['status'],'pending')

    def test_stop_drains_ad_requests_before_return_and_never_starts_creatives(self):
        runner,source,graph,runtime,m=self.prepare([ad(),ad('11','21')])
        stop=threading.Event();runner.stop=stop.is_set
        original=graph.delete
        def delete(kind,*args):
            stop.set();time.sleep(.03);return original(kind,*args)
        graph.delete=delete
        with self.assertRaises(InterruptedError):runner.execute('test-run',m)
        self.assertTrue(all(row[1]=='ad' for row in self.deletes(graph)))
        job=self.store.get_job('policy-cleanup-test-run')
        self.assertFalse(any(o['status']=='in_progress' for o in job['objects']))


class ContextBatchTests(unittest.TestCase):
    setUp=sql_fixtures.SqlSourceTests.setUp
    tearDown=sql_fixtures.SqlSourceTests.tearDown
    make_source=sql_fixtures.SqlSourceTests.make_source
    run_source=sql_fixtures.SqlSourceTests.run_source
    credential_route=sql_fixtures.SqlSourceTests.credential_route
    context_row=sql_fixtures.SqlSourceTests.context_row
    context_responder=sql_fixtures.SqlSourceTests.context_responder

    def test_batch_exactly_matches_single_and_retains_other_creative_rows(self):
        rows=[self.context_row(),self.context_row(id='11',creative_id='6000',source_id='0')]
        one,*_=self.run_source(lambda db:db.current_ad_context('100','5000'),self.context_responder(rows))
        batch,db,process,popen=self.run_source(lambda db:current_contexts(db,[{'ad_id':'100','creative_id':'5000'}]),self.context_responder(rows))
        self.assertEqual(one,batch[('100','5000')]);self.assertEqual(popen.call_count,1)

    def test_invalid_context_does_not_contaminate_other_ad(self):
        rows=[self.context_row(),self.context_row(id='12',ad_id='101',available=False)]
        batch,*_=self.run_source(lambda db:current_contexts(db,[{'ad_id':a,'creative_id':'5000'} for a in ('100','101')]),self.context_responder(rows))
        self.assertNotIn('error',batch[('100','5000')])
        self.assertEqual(batch[('101','5000')]['error'],'publishing_credential_unavailable')

    def test_old_creative_mapping_not_used_for_new_binding(self):
        batch,*_=self.run_source(lambda db:current_contexts(db,[{'ad_id':'100','creative_id':'6000'}]),self.context_responder([self.context_row()],[]))
        self.assertEqual(batch[('100','6000')]['scope'],{})
        self.assertEqual(batch[('100','6000')]['credential_users'],[])


class ThrottleTests(unittest.TestCase):
    def test_stop_still_drains_exact_readback_of_an_already_sent_delete(self):
        from types import SimpleNamespace
        stop=threading.Event()
        class StoppingSession(Session):
            def request(self,method,*args,**kwargs):
                result=super().request(method,*args,**kwargs)
                if method=='DELETE':stop.set()
                return result
        session=StoppingSession([Response(graph_ad()),Response({'success':True}),Response(graph_ad(status='DELETED'))])
        graph=GraphClient({'1':'synthetic'},session=session);graph.stop=stop.is_set;graph.delete_stop=stop.is_set
        graph.http=GovernedSession(session,Governor(rps=10000),stop.is_set);graph.http.account='100'
        executor=object.__new__(ConcurrentExecutor);executor.ttl=30;executor.stopped=stop.is_set
        executor.graph=lambda *args:graph;executor.manifest={}
        executor.runner=SimpleNamespace(preflight=lambda *args,**kwargs:{'eligible':True})
        candidate={'ad_id':'10','creative_id':'20','account_id':'100','user_id':'1'}
        result=executor.write('ad',candidate,None,time.monotonic(),{})
        self.assertTrue(result['confirmed_deleted']);self.assertTrue(result['api_accepted'])
        self.assertEqual([c[0] for c in session.calls],['GET','DELETE','GET'])
        self.assertTrue(graph.stop())

    def test_start_rate_waits_for_exact_budget_instead_of_polling_at_ten_per_second(self):
        clock=[0.];g=Governor(rps=24,clock=lambda:clock[0])
        def advance(stop,delay):clock[0]+=delay
        g.stopped_wait=advance
        for _ in range(10):
            with g.slot('100',lambda:False):pass
        self.assertAlmostEqual(clock[0],9/24)

    def test_wave_width_adapts_between_waves(self):
        size=[2];items=[{'account_id':str(i)} for i in range(10)]
        iterator=waves(items,lambda:size[0],2)
        self.assertEqual(len(next(iterator)),2);size[0]=6
        self.assertEqual(len(next(iterator)),6)
        self.assertEqual(len(next(iterator)),2)

    def test_global_and_per_account_limits_apply_to_real_concurrent_slots(self):
        from concurrent.futures import ThreadPoolExecutor
        from collections import Counter
        g=Governor(maximum=4,per_account=2,rps=10000)
        lock=threading.Lock();active=Counter();peak=Counter();total=0;global_peak=0
        def work(account):
            nonlocal total,global_peak
            with g.slot(account,lambda:False):
                with lock:
                    active[account]+=1;total+=1;peak[account]=max(peak[account],active[account]);global_peak=max(global_peak,total)
                time.sleep(.03)
                with lock:active[account]-=1;total-=1
        with ThreadPoolExecutor(max_workers=12) as pool:list(pool.map(work,['100','200']*6))
        self.assertLessEqual(global_peak,4);self.assertLessEqual(max(peak.values()),2)
        self.assertEqual(g.snapshot()['active'],0)

    def test_stop_interrupts_cooldown_without_sending(self):
        g=Governor();g.global_until=time.monotonic()+600
        session=Session([]);wrapped=GovernedSession(session,g,lambda:True)
        with self.assertRaises(RequestNotSent):wrapped.request('DELETE','unused')
        self.assertEqual(session.calls,[])

    def test_usage_reduces_global_limit_and_honors_retry_after(self):
        clock=[0.];g=Governor(clock=lambda:clock[0]);g.limit=8
        g.observe(Response({'error':{'code':80004}},400,{'Retry-After':'180'}),'100')
        self.assertEqual(g.limit,4);self.assertEqual(g.global_until,180)
        self.assertEqual(g.snapshot()['rate_limit_responses'],1)

    def test_business_cooldown_is_minutes_and_account_specific(self):
        g=Governor(clock=lambda:0)
        g.observe(Response({},headers={'x-business-use-case-usage':json.dumps({'b':[{'call_count':90,'estimated_time_to_regain_access':3}]})}),'100')
        self.assertEqual(g.account_until['100'],180);self.assertEqual(g.global_until,0)

    def test_missing_headers_never_raise_limit(self):
        g=Governor(clock=lambda:100);g.last_change=0
        g.observe(Response({}),'100');self.assertEqual(g.limit,4)

    def test_expired_or_missing_usage_never_recovers_concurrency(self):
        clock=[0.];g=Governor(clock=lambda:clock[0])
        g.observe(Response({},headers={'x-app-usage':'{"call_count":20}'}),'100')
        clock[0]=180;g.limit=2
        g.observe(Response({}),'100');self.assertEqual(g.limit,2)

    def test_low_usage_gradually_restores_limit(self):
        g=Governor(clock=lambda:100);g.last_change=0
        g.observe(Response({},headers={'x-app-usage':'{"call_count":20}'}),'100')
        self.assertEqual(g.limit,6)

    def test_guard_failure_before_send_is_not_an_unknown_delete(self):
        session=Session([Response(graph_ad()),Response({'success':True})])
        graph=GraphClient({'1':'synthetic'},session=session)
        governed=GovernedSession(session,Governor(rps=1000),lambda:False);governed.account='100'
        governed.write_check=lambda:(_ for _ in ()).throw(RequestNotSent('execution_context_expired'))
        graph.http=governed
        result=graph.delete('ad','10','1','100',expected_creative_id='20')
        self.assertFalse(result['request_sent']);self.assertEqual(result['status'],'failed')
        self.assertEqual([c[0] for c in session.calls],['GET'])

    def test_one_current_get_checks_full_policy_before_delete(self):
        session=Session([Response(graph_ad()),Response({'success':True})])
        graph=GraphClient({'1':'synthetic'},session=session)
        result=graph.delete('ad','10','1','100',expected_creative_id='20',ad_guard=lambda ad:{'eligible':False,'reason':'rule_not_matched'})
        self.assertEqual(result['code'],'current_policy_changed');self.assertEqual(len(session.calls),1)

    def test_round_robin_waves_do_not_concentrate_on_one_account(self):
        items=[{'account_id':str(a),'id':str(i)} for a in range(4) for i in range(5)]
        batches=list(waves(items,8,2))
        self.assertEqual(sum(map(len,batches)),20)
        for batch in batches:
            for account in range(4):self.assertLessEqual(sum(c['account_id']==str(account) for c in batch),2)


if __name__=='__main__':unittest.main()
