"""Runner integration tests: real deletion ledger, fake SQL/Graph/notifications."""
import copy
from contextlib import contextmanager
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE=pathlib.Path(__file__).resolve()
sys.path.insert(0,str(HERE.parents[1]))
sys.path.insert(0,str(HERE.parents[3]))
from features.fb_ad_asset_delete.store import Store, StoreError
from meta_violation_cleanup.runner import Runner
from meta_violation_cleanup.runtime import digest


START='2026-09-23T12:00:00+08:00'
END='2026-10-23T12:00:00+08:00'


def ad(identifier='10',creative='20',account='100',status='PAUSED'):
    return {'id':identifier,'account_id':account,'status':status,'effective_status':status,
        'creative':{'id':creative},'adset':{'id':'30','status':'ACTIVE'},
        'campaign':{'id':'40','status':'ACTIVE'},'issues_info':[],'ad_review_feedback':{}}


def lineage(identifier,creative,product='1479',material='501'):
    return {'ad_id':identifier,'creative_id':creative,'product':product,
        'source_id':'0','original_source_id':material,'user_id':'1'}


class Source:
    def __init__(self,ads):
        self.rows={a['id']:[lineage(a['id'],a['creative']['id'])] for a in ads}
        self.vios=[];self.source_rows={};self.credential_fail=set();self.credential_values={};self.queried_sources=[]
        for i in range(4):
            aid=str(101+i)
            self.rows[aid]=[lineage(aid,str(201+i))]
            self.vios.append({'id':str(i+1),'ad_id':aid,'created_at':'2026-10-22T10:00:00+08:00',
                'issues_info':{'global':{'Spam':'review explanation'}}})

    def products(self):return [{'id':'1479','ads_app_id':'0'},{'id':'3302','ads_app_id':'0'},{'id':'14790','ads_app_id':'1479'},{'id':'9999','ads_app_id':'0'}]
    def violations(self,start,end):return copy.deepcopy(self.vios)
    def violations_by_ids(self,ids):return copy.deepcopy([v for v in self.vios if v['id'] in ids])
    def lineages(self,ids):return copy.deepcopy({aid:self.rows[aid] for aid in ids if aid in self.rows})
    def sources(self,ids):
        self.queried_sources.extend(ids)
        return copy.deepcopy({key:self.source_rows[key] for key in ids if key in self.source_rows})
    def selected_credential(self,row):
        if row['ad_id'] in self.credential_fail:raise ValueError('DO-NOT-LOG-SECRET')
        return self.credential_values.get(row['ad_id'],'1')


class Graph:
    def __init__(self,ads):
        self.tokens={'1':'DO-NOT-LOG-SECRET'}
        self.ads={a['id']:copy.deepcopy(a) for a in ads}
        self.calls=[];self.results={};self.verifications={};self.on_delete=None;self.bad_accounts=set()
    def list_accounts(self):
        return {'accounts':{a['account_id']:{'user_ids':['1']} for a in self.ads.values()},'errors':[]}
    def list_ads(self,account,user):
        if account in self.bad_accounts:raise ValueError('incomplete pages')
        return copy.deepcopy([a for a in self.ads.values() if a['account_id']==account])
    def get_ad(self,identifier,user):
        self.calls.append(('get',identifier))
        return copy.deepcopy(self.ads[identifier])
    def delete(self,kind,identifier,user,account):
        self.calls.append(('delete',kind,identifier))
        if self.on_delete:self.on_delete(kind,identifier)
        result=self.results.get((kind,identifier),{'status':'api_accepted','request_sent':True})
        if isinstance(result,BaseException):raise result
        if result['status']=='api_accepted':
            if kind=='creative':
                for row in self.ads.values():
                    if row['creative']['id']==identifier:row['effective_status']='WITH_ISSUES'
            else:self.ads[identifier]['status']='DELETED'
        return copy.deepcopy(result)
    def reconcile(self,kind,identifier,user,account):
        value=self.verifications.get((kind,identifier),{'status':'deleted_verified'})
        if isinstance(value,BaseException):raise value
        return copy.deepcopy(value)


class RuntimeLedgerError(RuntimeError):
    code='runtime_ledger_unavailable'


class Runtime:
    def __init__(self):
        self.data={};self.artifacts={};self.events=[];self.ads={};self.barriers=0;self.fail_barrier=None
    def artifact(self,run,name,data):self.artifacts[(run,name)]=copy.deepcopy(data)
    def update(self,run,**data):self.data.update(copy.deepcopy(data))
    def event(self,run,kind,data):self.events.append((kind,copy.deepcopy(data)))
    def inventory(self,run,account,ads):self.ads.update({a['id']:copy.deepcopy(a) for a in ads})
    def references(self,run,creative):return [copy.deepcopy(a) for a in self.ads.values() if a['creative']['id']==creative]
    def notice(self,run,phase):return {'status':'sent'}
    def write_barrier(self,run):
        self.barriers+=1
        if self.barriers==self.fail_barrier:raise RuntimeLedgerError('disk unavailable')


class Lease:
    def check(self):pass


@contextmanager
def legacy_lock(path,stop):
    yield Lease()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(str(pathlib.Path(self.temp.name)/'ledger.sqlite'))
        self.patches=[patch('meta_violation_cleanup.runner.legacy_delete_lock',legacy_lock),
            patch('features.fb_ad_asset_delete.store._process_start',return_value=(True,'test-owner'))]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def fixture(self,ads=None):
        ads=ads or [ad()]
        source=Source(ads);graph=Graph(ads);runtime=Runtime()
        runner=Runner({'legacy_lock':'unused'},source,graph,runtime,self.store,None)
        return runner,source,graph,runtime

    def manifest(self,runner):return runner.scan('test-run',START,END)
    def deletes(self,graph):return [c for c in graph.calls if c[0]=='delete']

    def test_credential_failure_is_per_ad_and_is_coverage_gap(self):
        runner,source,graph,runtime=self.fixture([ad(),ad('11','21')])
        source.credential_fail.add('10')
        manifest=self.manifest(runner)
        self.assertEqual([c['ad_id'] for c in manifest['candidates']],['11'])
        self.assertEqual(manifest['coverage']['accounts'][0]['status'],'complete')
        self.assertEqual(manifest['mapping_gap_counts']['credential_lineage_unavailable'],1)
        self.assertNotIn('DO-NOT-LOG-SECRET',str(runtime.events)+str(manifest))

    def test_secret_returned_as_credential_is_rejected_without_serializing(self):
        runner,source,graph,runtime=self.fixture()
        source.credential_values['10']='DO-NOT-LOG-SECRET'
        manifest=self.manifest(runner)
        self.assertEqual(manifest['candidates'],[])
        self.assertNotIn('DO-NOT-LOG-SECRET',str(runtime.events)+str(manifest))

    def test_explicit_violation_source_loaded_in_scan_and_preflight(self):
        runner,source,_,_=self.fixture()
        source.vios[0]['source_id']='900'
        source.source_rows['900']={'id':'900','source_type':'3','source_id':'501'}
        manifest=self.manifest(runner)
        self.assertEqual(manifest['evidence']['materials']['501']['count'],4)
        source.queried_sources=[]
        self.assertTrue(runner.preflight(manifest['candidates'][0],manifest)['eligible'])
        self.assertIn('900',source.queried_sources)

    def test_multiple_violation_rows_for_one_ad_retained_for_preflight(self):
        runner,source,_,_=self.fixture()
        source.vios.append(dict(source.vios[0],id='88'))
        manifest=self.manifest(runner)
        self.assertEqual(manifest['violation_ids_by_ad']['101'],['1','88'])
        self.assertEqual(manifest['evidence']['materials']['501']['count'],4)
        self.assertTrue(runner.preflight(manifest['candidates'][0],manifest)['eligible'])

    def test_material_product_and_ad_state_changes_cancel_preflight(self):
        for change in ('material','product','review','creative','proof'):
            with self.subTest(change=change):
                runner,source,graph,_=self.fixture()
                manifest=self.manifest(runner)
                if change=='material':source.rows['10'][0]['original_source_id']='502'
                if change=='product':source.rows['10'][0]['product']='3302'
                if change=='review':graph.ads['10']['effective_status']='PENDING_REVIEW'
                if change=='creative':graph.ads['10']['creative']['id']='999'
                if change=='proof':source.vios=[]
                self.assertFalse(runner.preflight(manifest['candidates'][0],manifest)['eligible'])

    def test_shared_creative_all_targets_preflight_before_mutation_other_product_not_deleted(self):
        runner,source,graph,runtime=self.fixture([ad(),ad('11','20'),ad('99','20')])
        source.rows['99'][0]['product']='9999'
        manifest=self.manifest(runner)
        self.assertEqual(manifest['shared_creative_impacts']['20'][0]['ad_id'],'99')
        self.assertEqual(runner.execute('test-run',manifest),'completed')
        self.assertEqual(self.deletes(graph),[('delete','creative','20'),('delete','ad','10'),('delete','ad','11')])
        first_delete=graph.calls.index(('delete','creative','20'))
        self.assertIn(('get','10'),graph.calls[:first_delete])
        self.assertIn(('get','11'),graph.calls[:first_delete])
        self.assertEqual(runtime.barriers,3)

    def test_external_creative_change_after_group_preflight_cancels_ad(self):
        runner,_,graph,runtime=self.fixture()
        manifest=self.manifest(runner)
        def change(kind,identifier):
            if kind=='creative':graph.ads['10']['creative']['id']='999'
        graph.on_delete=change
        self.assertEqual(runner.execute('test-run',manifest),'partial')
        self.assertEqual(self.deletes(graph),[('delete','creative','20')])
        self.assertEqual(runtime.data['remaining_ads'],1)

    def test_rule1_resumed_ad_is_not_deleted_after_creative(self):
        runner,source,graph,_=self.fixture()
        source.vios=source.vios[:1]
        manifest=self.manifest(runner)
        def change(kind,identifier):
            if kind=='creative':graph.ads['10']['status']='ACTIVE'
        graph.on_delete=change
        self.assertEqual(runner.execute('test-run',manifest),'partial')
        self.assertEqual(self.deletes(graph),[('delete','creative','20')])

    def test_canary_then_same_manifest_continuation_only_pending_groups(self):
        runner,_,graph,runtime=self.fixture([ad(),ad('11','21')])
        manifest=self.manifest(runner)
        self.assertEqual(runner.execute('test-run',manifest,max_ads=1),'partial')
        self.assertEqual(runtime.data['remaining_ads'],1)
        self.assertEqual(runner.execute('test-run',manifest,start_phase='start-2'),'completed')
        self.assertEqual(self.deletes(graph),[
            ('delete','creative','20'),('delete','ad','10'),('delete','creative','21'),('delete','ad','11')])
        job=self.store.get_job('policy-cleanup-test-run')
        self.assertEqual(len(job['runs']),2)
        self.assertEqual(len({r['request_id'] for r in job['runs']}),2)
        self.assertEqual(runtime.data['remaining_ads'],0)

    def test_canary_never_splits_shared_creative_group(self):
        runner,_,graph,runtime=self.fixture([ad(),ad('11','20')])
        manifest=self.manifest(runner)
        self.assertEqual(runner.execute('test-run',manifest,max_ads=1),'partial')
        self.assertEqual(self.deletes(graph),[])
        self.assertEqual(runtime.data['remaining_ads'],2)
        self.assertEqual(runner.execute('test-run',manifest,max_ads=2,start_phase='start-2'),'completed')

    def test_api_accepted_is_preserved_while_unknown_blocks_replay(self):
        runner,_,graph,_=self.fixture()
        manifest=self.manifest(runner)
        graph.verifications={('creative','20'):{'status':'unknown'},('ad','10'):{'status':'unknown'}}
        self.assertEqual(runner.execute('test-run',manifest),'partial')
        before=len(self.deletes(graph))
        self.assertEqual(runner.execute('test-run',manifest,start_phase='start-2'),'partial')
        self.assertEqual(len(self.deletes(graph)),before)
        objects=self.store.get_job('policy-cleanup-test-run')['objects']
        for obj in objects:
            self.assertEqual(obj['status'],'unknown')
            self.assertTrue(obj['result']['api_accepted'])
            self.assertEqual(obj['result']['delete_status'],'api_accepted')
            self.assertEqual(obj['result']['credential_user_id'],'1')

    def test_failed_objects_are_not_automatically_retried_same_run(self):
        runner,_,graph,_=self.fixture()
        manifest=self.manifest(runner)
        graph.results={('creative','20'):{'status':'failed'},('ad','10'):{'status':'failed'}}
        runner.execute('test-run',manifest)
        runner.execute('test-run',manifest,start_phase='start-2')
        self.assertEqual(len(self.deletes(graph)),2)

    def test_runtime_ledger_failure_stops_before_any_delete(self):
        runner,_,graph,runtime=self.fixture()
        manifest=self.manifest(runner);runtime.fail_barrier=1
        with self.assertRaises(RuntimeLedgerError):runner.execute('test-run',manifest)
        self.assertEqual(self.deletes(graph),[])
        creative=self.store.get_job('policy-cleanup-test-run')['objects'][0]
        self.assertEqual(creative['status'],'unknown')

    def test_shared_ledger_failure_stops_all_later_deletes(self):
        runner,_,graph,_=self.fixture()
        manifest=self.manifest(runner)
        with patch.object(self.store,'finish_object',side_effect=StoreError('disk stopped')):
            with self.assertRaises(StoreError):runner.execute('test-run',manifest)
        self.assertEqual(self.deletes(graph),[('delete','creative','20')])
        creative=self.store.get_job('policy-cleanup-test-run')['objects'][0]
        self.assertEqual(creative['status'],'unknown')

    def test_interruption_fences_touched_object_and_continuation_does_not_replay(self):
        runner,_,graph,_=self.fixture([ad(),ad('11','21')])
        manifest=self.manifest(runner)
        graph.results[('ad','10')]=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):runner.execute('test-run',manifest)
        graph.results.clear()
        runner.execute('test-run',manifest,start_phase='start-2')
        self.assertEqual(self.deletes(graph).count(('delete','ad','10')),1)
        self.assertIn(('delete','ad','11'),self.deletes(graph))
        statuses={o['key']:o['status'] for o in self.store.get_job('policy-cleanup-test-run')['objects']}
        self.assertEqual(statuses['ad:10'],'unknown')
        self.assertEqual(statuses['ad:11'],'deleted')

    def test_legacy_priority_releases_then_rechecks_and_continues_without_fence(self):
        runner,_,graph,_=self.fixture()
        manifest=self.manifest(runner)
        acquisitions=[];releases=[]
        class StopLease:
            def check(self):raise InterruptedError('legacy_priority_window')
        @contextmanager
        def lock(path,stop):
            number=len(acquisitions)+1;acquisitions.append(number)
            try:yield StopLease() if number==1 else Lease()
            finally:releases.append(number)
        with patch('meta_violation_cleanup.runner.legacy_delete_lock',lock):
            self.assertEqual(runner.execute('test-run',manifest),'completed')
        self.assertEqual(acquisitions,[1,2,3])
        self.assertEqual(releases,[1,2,3])
        self.assertEqual(self.deletes(graph),[('delete','creative','20'),('delete','ad','10')])
        self.assertTrue(all(o['status']=='deleted' for o in self.store.get_job('policy-cleanup-test-run')['objects']))

    def test_legacy_priority_between_creative_and_ad_does_not_replay_creative(self):
        runner,_,graph,_=self.fixture()
        manifest=self.manifest(runner);acquisitions=[]
        class StopLease:
            def check(self):raise InterruptedError('legacy_priority_window')
        @contextmanager
        def lock(path,stop):
            number=len(acquisitions)+1;acquisitions.append(number)
            yield StopLease() if number==2 else Lease()
        with patch('meta_violation_cleanup.runner.legacy_delete_lock',lock):
            self.assertEqual(runner.execute('test-run',manifest),'completed')
        self.assertEqual(self.deletes(graph),[('delete','creative','20'),('delete','ad','10')])
        self.assertEqual(acquisitions,[1,2,3])

    def test_stop_after_claim_before_graph_call_records_known_not_sent(self):
        runner,_,graph,runtime=self.fixture()
        manifest=self.manifest(runner)
        runner.stop=lambda:runtime.barriers>0
        with self.assertRaises(InterruptedError):runner.execute('test-run',manifest)
        self.assertEqual(self.deletes(graph),[])
        creative=self.store.get_job('policy-cleanup-test-run')['objects'][0]
        self.assertEqual(creative['status'],'failed')
        self.assertFalse(creative['result']['request_sent'])

    def test_incomplete_account_cannot_contribute_candidates_or_counts(self):
        runner,_,graph,_=self.fixture([ad(),ad('11','21',account='200')])
        graph.bad_accounts.add('100')
        manifest=self.manifest(runner)
        self.assertEqual([c['ad_id'] for c in manifest['candidates']],['11'])
        self.assertEqual(manifest['product_scanned_ads'],{'1479':1})
        self.assertEqual(manifest['shared_reference_coverage'],'partial')

    def test_parallel_inventory_contract_keeps_serial_source_processing(self):
        runner,_,graph,_=self.fixture([ad(),ad('11','21',account='200')])
        def parallel(accounts,stop):
            yield '200',graph.list_ads('200','1'),'1',[]
            yield '100',None,None,[{'code':'page_failure'}]
        graph.iter_account_ads=parallel
        manifest=self.manifest(runner)
        self.assertEqual([c['ad_id'] for c in manifest['candidates']],['11'])
        self.assertEqual(manifest['product_scanned_ads'],{'1479':1})
        self.assertEqual(len(manifest['coverage']['accounts']),2)

    def test_parallel_inventory_missing_account_cannot_freeze_manifest(self):
        runner,_,graph,runtime=self.fixture([ad(),ad('11','21',account='200')])
        graph.iter_account_ads=lambda accounts,stop:iter([('100',graph.list_ads('100','1'),'1',[])])
        with self.assertRaisesRegex(ValueError,'account_inventory_incomplete'):self.manifest(runner)
        self.assertNotIn(('test-run','manifest.json'),runtime.artifacts)

    def test_account_failure_discards_counts_staged_before_failure(self):
        runner,_,graph,_=self.fixture([ad(),ad('11','21')])
        graph.ads['11']['creative']=[]
        # An invalid current object after an earlier eligible ad must discard
        # the entire account's staged candidate and product count.
        graph.ads['11']['creative']=['invalid']
        manifest=self.manifest(runner)
        self.assertEqual(manifest['candidates'],[])
        self.assertEqual(manifest['product_scanned_ads'],{})
        self.assertEqual(manifest['coverage']['accounts'][0]['status'],'failed')

    def test_global_unknown_creative_fence_is_not_replayed(self):
        runner,_,graph,_=self.fixture()
        manifest=self.manifest(runner)
        self.store.create_job({'job_id':'other','preview_id':'other-hash','actor':'other',
            'objects':[{'kind':'creative','object_id':'20'}]})
        oldrun=self.store.claim_run('other','other-hash','other',['creative'],'other-request')['run_id']
        self.store.claim_object('other','creative:20',oldrun)
        self.store.finish_object('other','creative:20',oldrun,'unknown',{'reason':'timeout'})
        self.store.finish_run(oldrun,'partial')
        self.assertEqual(runner.execute('test-run',manifest),'partial')
        self.assertNotIn(('delete','creative','20'),self.deletes(graph))
        self.assertIn(('delete','ad','10'),self.deletes(graph))

    def test_missing_lineage_is_gap_but_other_product_is_regular_exclusion(self):
        runner,source,_,_=self.fixture([ad(),ad('11','21'),ad('99','99')])
        del source.rows['11'];source.rows['99'][0]['product']='9999'
        manifest=self.manifest(runner)
        self.assertEqual(sum(manifest['mapping_gap_counts'].values()),1)
        self.assertNotIn('product_out_of_scope',manifest['mapping_gap_counts'])
        self.assertEqual(manifest['skip_counts']['product_out_of_scope'],1)

    def test_unknown_or_empty_product_is_coverage_gap(self):
        for product in ('','7654321'):
            with self.subTest(product=product):
                runner,source,_,_=self.fixture()
                source.rows['10'][0]['product']=product
                manifest=self.manifest(runner)
                self.assertEqual(sum(manifest['mapping_gap_counts'].values()),1)
                self.assertTrue(manifest['coverage']['errors'])
                self.assertIn('frozen_at',manifest)

    def test_hash_notice_run_and_account_boundaries_fail_without_delete(self):
        for fault in ('hash','notice','run','account','uid'):
            with self.subTest(fault=fault):
                runner,_,graph,runtime=self.fixture()
                manifest=self.manifest(runner)
                if fault=='hash':manifest['window_end']='changed'
                if fault=='notice':runtime.notice=lambda run,phase:None
                if fault=='run':manifest['run_id']='different'
                if fault=='account':manifest['coverage']['accounts'][0]['status']='failed'
                if fault=='uid':manifest['candidates'][0]['user_id']='secret'
                if fault in ('run','account','uid'):manifest['sha256']=digest({k:v for k,v in manifest.items() if k!='sha256'})
                with self.assertRaises((ValueError,RuntimeError)):runner.execute('test-run',manifest)
                self.assertEqual(self.deletes(graph),[])


if __name__=='__main__':unittest.main()
