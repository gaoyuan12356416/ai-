import copy
import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import test_runner as fixtures
from test_runner import ad,START,END
from meta_violation_cleanup.runtime import digest
from meta_violation_cleanup.verify_batches import verify


class VerificationTests(unittest.TestCase):
    setUp=fixtures.RunnerTests.setUp
    tearDown=fixtures.RunnerTests.tearDown
    fixture=fixtures.RunnerTests.fixture

    def prepare(self):
        runner,source,graph,runtime=self.fixture([ad(),ad('11','21')])
        baseline=runner.scan('baseline',START,END)
        base=copy.deepcopy(baseline)
        base.update(run_id='source',policy_version='20261024-v2',comparison_only=False,
            candidates=[],timings={'total_seconds':10.0},blocked_account_creatives=copy.deepcopy(baseline['candidates']))
        base['coverage']['accounts'][0]['read_user_id']='1'
        base['coverage']['errors']=[{'ad_id':aid,'account_id':'100','stage':'target_get','error':{'code':100}} for aid in ('10','11')]
        base['comparison']={'baseline_run_id':'baseline','baseline_sha256':baseline['sha256'],
            'current_candidates':0,'common_candidates':0,'new_candidate_count':0,
            'current_missing_old':[{'ad_id':aid,'reason':'meta_read_unavailable'} for aid in ('10','11')]}
        base['sha256']=digest({k:v for k,v in base.items() if k!='sha256'})
        for aid,rows in source.rows.items():
            for row in rows:row.update(id=aid,ad_account_id='100')
        source.selected_credentials=lambda rows:{r['id']:{'user_id':'1','error':''} for r in rows}
        runner.store=None
        graph.delete=lambda *args:self.fail('verification must never DELETE')
        return runner,source,graph,runtime,base,baseline

    def test_recovery_preserves_original_hash_deadline_and_separates_ineligible_ads(self):
        runner,source,graph,runtime,base,baseline=self.prepare()
        frozen=copy.deepcopy(base)
        runtime.get=lambda _: {'data':{'graph_metrics':{'get_requests':10},'sql_metrics':{'query_count':2}}}
        graph.safe_metrics={'get_requests':3};source.safe_metrics={'query_count':1}
        graph.iter_target_ads=lambda *args:iter([(('1','100',['10','11']),{'10':ad(),'11':ad('11','21',status='DELETED')},{})])
        result=verify(runner,'source','verified',base,baseline)
        self.assertEqual(base,frozen)
        self.assertEqual(result['frozen_at'],base['frozen_at'])
        self.assertEqual(result['verified_from_sha256'],base['sha256'])
        self.assertEqual([c['ad_id'] for c in result['candidates']],['10'])
        self.assertEqual(result['coverage']['errors'],[])
        self.assertEqual(result['blocked_account_creatives'],[])
        self.assertEqual(result['comparison']['common_candidates'],1)
        self.assertEqual(result['comparison']['current_missing_old'][0]['reason'],'current_status_not_eligible')
        self.assertEqual(result['batch_verification']['added_fresh_candidates'],1)
        self.assertEqual(result['scan_graph_metrics']['get_requests'],13)
        self.assertEqual(result['scan_sql_metrics']['query_count'],3)
        self.assertEqual(digest({k:v for k,v in result.items() if k!='sha256'}),result['sha256'])

    def test_persistently_unreadable_object_remains_an_explicit_gap(self):
        runner,source,graph,runtime,base,baseline=self.prepare()
        graph.iter_target_ads=lambda *args:iter([(('1','100',['10','11']),{'10':ad()},{'11':{'code':100}})])
        result=verify(runner,'source','verified',base,baseline)
        self.assertEqual([r['ad_id'] for r in result['coverage']['errors']],['11'])
        self.assertEqual(result['coverage']['accounts'][0]['status'],'partial')
        self.assertEqual(result['comparison']['current_missing_old'][0]['ad_id'],'11')
        self.assertEqual(result['batch_verification']['remaining_object_errors'],1)


if __name__=='__main__':unittest.main()
