import copy
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from meta_violation_cleanup.graph import GraphClient
from meta_violation_cleanup.material_scan import scan
from meta_violation_cleanup.indexed_source import complex_value
import test_runner as fixtures
from test_runner import ad, lineage, START, END


class MaterialScanTests(unittest.TestCase):
    setUp=fixtures.RunnerTests.setUp
    tearDown=fixtures.RunnerTests.tearDown
    fixture=fixtures.RunnerTests.fixture
    def test_reference_comparison_keeps_old_threshold_separate_from_fresh_candidates(self):
        runner,source,graph,runtime=self.fixture([ad(status='ACTIVE')])
        old=runner.scan('baseline',START,END)
        self.assertEqual(len(old['candidates']),1)
        runtime.load_artifact=lambda run,name:copy.deepcopy(old)
        runner.config['comparison_reference_run_id']='baseline'
        source.vios.pop() # Fresh evidence has 3 violations; old control still has 4.
        for aid,rows in source.rows.items():
            for row in rows:row.update(id=aid,ad_account_id='100')
        source.selected_credentials=lambda rows:{r['id']:{'user_id':'1','error':''} for r in rows}
        graph.safe_metrics={};source.safe_metrics={}
        graph.iter_target_ads=lambda *args:iter([(('1','100',['10']),{'10':ad(status='ACTIVE')},{})])
        raw=[dict(source.rows['10'][0])]
        with patch('meta_violation_cleanup.material_scan.material_rows',return_value=(raw,{})):
            result=scan(runner,'fresh',START,END)
        self.assertFalse(result['comparison_only'])
        self.assertEqual(result['candidates'],[])
        self.assertEqual(result['comparison']['common_candidates'],1)
        self.assertEqual(result['comparison']['fresh_window_candidates'],0)
        self.assertEqual(result['comparison']['unexplained_retrieval_missing_ids'],[])

    def test_comparison_stops_before_meta_when_still_eligible_old_ad_is_missing(self):
        runner,source,graph,runtime=self.fixture()
        old=runner.scan('baseline',START,END)
        runtime.load_artifact=lambda run,name:copy.deepcopy(old)
        runner.config['comparison_baseline_run_id']='baseline'
        graph.iter_target_ads=lambda *args:self.fail('missing retrieval must block verification')
        with patch('meta_violation_cleanup.material_scan.material_rows',return_value=([],{})):
            with self.assertRaisesRegex(ValueError,'material_lookup_lost_baseline_candidates'):
                scan(runner,'comparison',START,END)
        report=runtime.artifacts[('comparison','comparison-retrieval.json')]
        self.assertEqual(report['unexplained_retrieval_missing_ids'],['10'])

    def test_material_path_never_enumerates_accounts_and_current_creative_wins(self):
        runner,source,graph,runtime=self.fixture([ad(),ad('11','21')])
        for aid,rows in source.rows.items():
            for row in rows:
                row.update(id=aid,ad_account_id='100')
        source.selected_credentials=lambda rows:{r['id']:{'user_id':'1','error':''} for r in rows}
        graph.list_accounts=lambda:self.fail('must not enumerate all accounts')
        graph.list_ads=lambda *args:self.fail('must not enumerate account inventory')
        graph.safe_metrics={};source.safe_metrics={}
        def targeted(targets,stop):
            self.assertEqual({x['ad_id'] for x in targets},{'10','11'})
            # SQL says creative 21; Meta changed to 22, so this ad is excluded.
            current=ad('11','22')
            yield ('1','100',['10','11']),{'10':ad(),'11':current},{}
        graph.iter_target_ads=targeted
        raw=[dict(r) for aid in ('10','11') for r in source.rows[aid]]
        with patch('meta_violation_cleanup.material_scan.material_rows',return_value=(raw,{})):
            result=scan(runner,'test-run',START,END)
        self.assertEqual([c['ad_id'] for c in result['candidates']],['10'])
        self.assertEqual(result['skip_counts']['current_creative_lineage_missing'],1)
        self.assertEqual(result['scanner'],'material_indexed')

    def test_batch_read_preserves_per_object_errors_and_account_identity(self):
        class Response:
            status_code=200;headers={}
            def json(self):return {'10':ad(),'11':{'error':{'code':100,'message':'denied'}},
                                   '12':ad('12','23',account='999')}
        class Session:
            def mount(self,*args):pass
            def request(self,method,url,**kw):
                self.method=method;self.params=kw['params'];return Response()
            def close(self):pass
        session=Session();graph=GraphClient({'1':'secret'},session=session)
        # The thread-local reader must remain an isolated read-only client.
        with patch.object(graph,'_reader',return_value=graph),patch.object(graph,'_release_reader'):
            item,records,errors=graph._target_ads(('1','100',['10','11','12']))
        self.assertEqual(session.method,'GET')
        self.assertEqual(set(records),{'10'})
        self.assertEqual(errors['11']['code'],100)
        self.assertEqual(errors['12']['code'],'account_mismatch')

    def test_global_bad_id_error_isolated_without_losing_good_ids(self):
        from test_graph import Response,Session
        session=Session([Response({'error':{'code':100}},400),Response({'10':ad()}),
                         Response({'error':{'code':100}},400)])
        graph=GraphClient({'1':'secret'},session=session)
        with patch.object(graph,'_reader',return_value=graph),patch.object(graph,'_release_reader'):
            _,records,errors=graph._target_ads(('1','100',['10','11']))
        self.assertEqual(set(records),{'10'});self.assertEqual(set(errors),{'11'})
        self.assertEqual([call[0] for call in session.calls],['GET']*3)

    def test_invalid_token_does_not_fan_out_into_individual_reads(self):
        from test_graph import Response,Session
        session=Session([Response({'error':{'code':190}},400)])
        graph=GraphClient({'1':'secret'},session=session)
        with patch.object(graph,'_reader',return_value=graph),patch.object(graph,'_release_reader'):
            _,records,errors=graph._target_ads(('1','100',['10','11']))
        self.assertEqual(records,{});self.assertEqual(set(errors),{'10','11'})
        self.assertEqual(len(session.calls),1)

    def test_composite_predicate_includes_csv_json_and_padding(self):
        predicate=complex_value('source_id')
        self.assertIn("LOCATE(',',source_id)",predicate)
        self.assertIn("LEFT(TRIM(source_id),1)='['",predicate)
        self.assertIn("source_id REGEXP '[[:space:]]'",predicate)


if __name__=='__main__':unittest.main()
