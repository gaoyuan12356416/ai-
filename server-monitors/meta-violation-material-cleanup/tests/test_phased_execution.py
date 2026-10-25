import copy
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import test_runner as fixtures
from test_runner import ad, START, END
from meta_violation_cleanup.runtime import digest
from meta_violation_cleanup.graph import GraphError
from meta_violation_cleanup.phased_execution import shared_state


class PhasedExecutionTests(unittest.TestCase):
    setUp=fixtures.RunnerTests.setUp
    tearDown=fixtures.RunnerTests.tearDown
    fixture=fixtures.RunnerTests.fixture
    deletes=fixtures.RunnerTests.deletes

    def prepare(self,ads=None):
        runner,source,graph,runtime=self.fixture(ads)
        manifest=runner.scan('test-run',START,END)
        manifest['policy_version']='20261024-v2'
        manifest['sha256']=digest({k:v for k,v in manifest.items() if k!='sha256'})
        runner.config['execution_order']='ads_first'
        runtime.queue={}
        runtime.queue_creative=lambda entry:runtime.queue.update({entry['basis']['creative_id']:copy.deepcopy(entry)})
        runtime.cleanup_backlog=lambda:list(copy.deepcopy(runtime.queue).values())
        runtime.complete_creative=lambda cid:runtime.queue.pop(cid,None)
        self.lock=patch('meta_violation_cleanup.phased_execution.legacy_delete_lock',fixtures.legacy_lock)
        self.lock.start();self.addCleanup(self.lock.stop)
        return runner,source,graph,runtime,manifest

    def next_manifest(self,manifest,runtime):
        result=copy.deepcopy(manifest);result['run_id']='second-run';result['candidates']=[]
        result['recovery_creatives']=runtime.cleanup_backlog()
        result['sha256']=digest({k:v for k,v in result.items() if k!='sha256'})
        return result

    def test_all_ads_precede_every_creative(self):
        runner,source,graph,runtime,m=self.prepare([ad(),ad('11','21')])
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[('delete','ad','10'),('delete','ad','11'),
            ('delete','creative','20'),('delete','creative','21')])
        self.assertEqual(runtime.queue,{})

    def test_skipped_first_member_does_not_strand_a_later_deleted_ads_creative(self):
        runner,source,graph,runtime,m=self.prepare([ad(),ad('11','20')])
        graph.ads['10']['effective_status']='DISAPPROVED'
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[('delete','ad','11'),('delete','creative','20')])
        self.assertEqual(runtime.queue,{})

    def test_definite_dependency_rejection_collects_shared_ads_then_retries(self):
        runner,source,graph,runtime,m=self.prepare()
        graph.ads['12']=ad('12','20',status='ACTIVE')
        tries=[]
        def on_delete(kind,identifier):
            if kind=='creative':
                tries.append(identifier)
                graph.results[(kind,identifier)]=({'status':'failed','request_sent':True,
                    'response':{'code':100,'error_subcode':3858800}} if len(tries)==1 else
                    {'status':'api_accepted','request_sent':True})
        graph.on_delete=on_delete
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[('delete','ad','10'),('delete','creative','20'),
            ('delete','ad','12'),('delete','creative','20')])
        self.assertEqual(runtime.queue,{})
        job=self.store.get_job('policy-cleanup-test-run')
        dependency=next(o for o in job['objects'] if o['key']=='ad:12')
        self.assertEqual(dependency['scope_rule'],'shared_reference_cleanup_20261024')

    def test_violating_shared_ad_waits_and_is_rechecked_in_next_run(self):
        runner,source,graph,runtime,m=self.prepare()
        graph.ads['12']=ad('12','20');graph.ads['12']['effective_status']='DISAPPROVED'
        graph.results[('creative','20')]={'status':'failed','request_sent':True,
            'response':{'code':100,'error_subcode':3858800}}
        runner.execute('test-run',m)
        self.assertNotIn(('delete','ad','12'),self.deletes(graph))
        self.assertEqual(runtime.queue['20']['blocked_ads'][0]['result']['reason'],'violating')
        second=self.next_manifest(m,runtime);graph.ads['12']['effective_status']='PAUSED'
        def on_delete(kind,identifier):
            if kind=='ad' and identifier=='12':graph.results[('creative','20')]={'status':'api_accepted','request_sent':True}
        graph.on_delete=on_delete
        runner.execute('second-run',second)
        self.assertEqual(self.deletes(graph).count(('delete','ad','10')),1)
        self.assertEqual(self.deletes(graph).count(('delete','ad','12')),1)
        self.assertEqual(runtime.queue,{})

    def test_all_shared_ads_precede_every_creative_retry(self):
        runner,source,graph,runtime,m=self.prepare([ad(),ad('11','21')])
        graph.ads['12']=ad('12','20');graph.ads['13']=ad('13','21')
        tries={}
        def on_delete(kind,identifier):
            if kind=='creative':
                tries[identifier]=tries.get(identifier,0)+1
                graph.results[(kind,identifier)]=({'status':'failed','request_sent':True,
                    'response':{'error_subcode':3858800}} if tries[identifier]==1 else
                    {'status':'api_accepted','request_sent':True})
        graph.on_delete=on_delete
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[('delete','ad','10'),('delete','ad','11'),
            ('delete','creative','20'),('delete','creative','21'),
            ('delete','ad','12'),('delete','ad','13'),
            ('delete','creative','20'),('delete','creative','21')])
        self.assertEqual(runtime.queue,{})

    def test_unknown_creative_does_not_trigger_reference_deletes_or_retry(self):
        runner,source,graph,runtime,m=self.prepare()
        graph.results[('creative','20')]={'status':'unknown','request_sent':True}
        graph.verifications[('creative','20')]={'status':'unknown'}
        graph.list_ads=lambda *args:self.fail('unknown is not definite dependency failure')
        runner.execute('test-run',m)
        second=self.next_manifest(m,runtime)
        runner.execute('second-run',second)
        self.assertEqual(self.deletes(graph).count(('delete','creative','20')),1)

    def test_disabled_account_can_probe_creative_without_an_ad_delete(self):
        runner,source,graph,runtime,m=self.prepare()
        basis=m['candidates'][0];m['candidates']=[]
        m['blocked_account_creatives']=[basis]
        m['sha256']=digest({k:v for k,v in m.items() if k!='sha256'})
        graph.account_diagnostic=lambda *args:{'account_status':2}
        graph.get_ad=lambda *args:(_ for _ in ()).throw(GraphError(200,'account disabled'))
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[('delete','creative','20')])

    def test_changed_material_blocks_ad_and_creative(self):
        runner,source,graph,runtime,m=self.prepare()
        source.rows['10'][0]['original_source_id']='777'
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[])

    def test_shared_ad_rebinding_is_not_deleted(self):
        runner,source,graph,runtime,m=self.prepare()
        graph.ads['12']=ad('12','21')
        graph.list_ads=lambda *args:[ad('12','20')]
        graph.results[('creative','20')]={'status':'failed','request_sent':True,
            'response':{'code':100,'error_subcode':3858800}}
        runner.execute('test-run',m)
        self.assertNotIn(('delete','ad','12'),self.deletes(graph))

    def test_pending_review_is_deferred_even_with_empty_feedback(self):
        value=ad();value['effective_status']='PENDING_REVIEW'
        self.assertEqual(shared_state(value),'state_not_eligible')

    def test_1487235_uses_same_shared_ad_before_creative_order(self):
        runner,source,graph,runtime,m=self.prepare()
        graph.ads['12']=ad('12','20',status='ACTIVE')
        calls=[]
        def on_delete(kind,identifier):
            if kind=='creative':
                calls.append(identifier)
                graph.results[(kind,identifier)]=({'status':'failed','request_sent':True,
                    'response':{'code':200,'error_subcode':1487235}} if len(calls)==1 else
                    {'status':'api_accepted','request_sent':True})
        graph.on_delete=on_delete
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[('delete','ad','10'),('delete','creative','20'),
                                            ('delete','ad','12'),('delete','creative','20')])

    def test_known_dependency_backlog_skips_creative_probe_until_shared_ads_finish(self):
        runner,source,graph,runtime,m=self.prepare([ad(),ad('11','21')])
        graph.ads['12']=ad('12','20');graph.ads['13']=ad('13','21')
        m['recovery_creatives']=[{'basis':c,'origin_run_id':'old','origin_manifest_sha256':'proof',
            'evidence':m['evidence'],'last_result':{'status':'failed','response':{'error_subcode':1487235}}}
            for c in m['candidates']]
        m['sha256']=digest({k:v for k,v in m.items() if k!='sha256'})
        runner.execute('test-run',m)
        self.assertEqual(self.deletes(graph),[('delete','ad','10'),('delete','ad','11'),
            ('delete','ad','12'),('delete','ad','13'),('delete','creative','20'),('delete','creative','21')])

    def test_stale_dependency_backlog_cannot_override_global_unknown(self):
        runner,source,graph,runtime,m=self.prepare()
        graph.results[('creative','20')]={'status':'unknown','request_sent':True}
        graph.verifications[('creative','20')]={'status':'unknown'}
        runner.execute('test-run',m)
        runtime.queue['20']['last_result']={'status':'failed','response':{'error_subcode':1487235}}
        second=self.next_manifest(m,runtime)
        graph.list_ads=lambda *args:self.fail('Global unknown must prevent shared Ad deletion')
        runner.execute('second-run',second)
        self.assertEqual(self.deletes(graph).count(('delete','creative','20')),1)

    def test_global_success_prevents_shared_deletion_from_stale_backlog(self):
        runner,source,graph,runtime,m=self.prepare()
        entry={'basis':copy.deepcopy(m['candidates'][0]),'origin_run_id':'test-run',
               'origin_manifest_sha256':m['sha256'],'evidence':copy.deepcopy(m['evidence']),
               'last_result':{'status':'failed','response':{'error_subcode':1487235}}}
        runner.execute('test-run',m);runtime.queue['20']=entry
        second=self.next_manifest(m,runtime)
        graph.list_ads=lambda *args:self.fail('Already deleted Creative must not delete shared Ads')
        runner.execute('second-run',second)
        self.assertEqual(self.deletes(graph).count(('delete','creative','20')),1)


if __name__=='__main__':unittest.main()
