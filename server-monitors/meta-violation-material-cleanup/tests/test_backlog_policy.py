import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from meta_violation_cleanup.backlog_policy import assess_backlog, validate_basis
from meta_violation_cleanup.policy import material_identity, product_scope
from meta_violation_cleanup.runtime import digest
import test_runner as fixtures
from test_runner import ad, lineage


def evidence(count=5):
    return {'materials': {'501': {'count': count,
        'violating_ad_ids': [str(1000+n) for n in range(count)],
        'policies': ['Spam'], 'root_ids': ['1479']}}}


def basis(snapshot=None):
    snapshot = copy.deepcopy(snapshot or ad())
    identity = material_identity([lineage('10','20')], {}, {'1479':'1479'}, '20')
    return {'ad_id':'10', 'creative_id':'20', 'account_id':'100', 'user_id':'1',
            'root_id':'1479', 'material_ids':['501'], 'identity':identity,
            'ad_snapshot':snapshot}


def context():
    return {'rows':[lineage('10','20')], 'sources':{}, 'scope':{'1479':'1479'}, 'credential_users':['1']}


class PureBacklogPolicyTests(unittest.TestCase):
    def test_live_basis_uses_fresh_five_and_pause(self):
        self.assertTrue(validate_basis(basis(),evidence(),context(),ad())[0])
        self.assertEqual(validate_basis(basis(),evidence(4),context(),ad()),(False,'violation_threshold_not_met'))
        self.assertEqual(validate_basis(basis(),evidence(),context(),ad(status='ACTIVE')),(False,'active_ad_protected'))

    def test_deleted_receipt_requires_matching_pause_snapshot_and_new_threshold(self):
        proof={'id':'10','account_id':'100','status':'DELETED'}
        self.assertEqual(validate_basis(basis(),evidence(),context(),deleted_proof=proof),
                         (True,'confirmed_deleted_paused_basis'))
        self.assertEqual(validate_basis(basis(ad(status='ACTIVE')),evidence(),context(),deleted_proof=proof),
                         (False,'pre_delete_pause_proof_unavailable'))
        self.assertFalse(validate_basis(basis(),evidence(4),context(),deleted_proof=proof)[0])

    def test_deleted_label_or_wrong_account_is_not_proof(self):
        for proof in (True, {'status':'DELETED'}, {'id':'10','account_id':'999','status':'DELETED'}):
            self.assertFalse(validate_basis(basis(),evidence(),context(),deleted_proof=proof)[0])

    def test_current_context_and_snapshot_binding_must_match(self):
        for field,value in [('credential_users',['2']),('scope',{'1479':'3302'}),('rows',[lineage('10','20',material='502')])]:
            current=context();current[field]=value
            self.assertFalse(validate_basis(basis(),evidence(),current,ad())[0])
        broken=basis();broken['ad_snapshot']['creative']['id']='99'
        self.assertFalse(validate_basis(broken,evidence(),context(),deleted_proof={'id':'10','account_id':'100','status':'DELETED'})[0])

    def test_current_sql_account_binding_is_required_and_normalized(self):
        for account in (None, '', '999', 'act_999'):
            current=context();current['rows'][0]['ad_account_id']=account
            self.assertFalse(validate_basis(basis(),evidence(),current,ad())[0])
        current=context();current['rows'][0].pop('ad_account_id')
        self.assertFalse(validate_basis(basis(),evidence(),current,ad())[0])
        current=context();current['rows'][0]['ad_account_id']='act_100'
        self.assertTrue(validate_basis(basis(),evidence(),current,ad())[0])

    def test_current_deleted_get_can_establish_exact_proof(self):
        current=ad();current.update(status='DELETED',effective_status='DELETED')
        self.assertTrue(validate_basis(basis(),evidence(),context(),ad=current)[0])

    def test_parent_paused_snapshot_is_valid(self):
        snapshot=ad(status='ACTIVE');snapshot['campaign']['status']='PAUSED';snapshot['effective_status']='CAMPAIGN_PAUSED'
        self.assertTrue(validate_basis(basis(snapshot),evidence(),context(),deleted_proof={'id':'10','account_id':'100','status':'DELETED'})[0])


class BacklogAssessmentTests(unittest.TestCase):
    setUp=fixtures.RunnerTests.setUp
    tearDown=fixtures.RunnerTests.tearDown
    fixture=fixtures.RunnerTests.fixture

    def entry(self):
        return {'basis':basis(), 'origin_run_id':'old-run', 'origin_manifest_sha256':'old-hash',
                'evidence':evidence(4),'last_result':{'status':'failed'}}

    def test_preserves_origin_and_old_evidence_without_queue_mutation(self):
        runner,source,graph,runtime=self.fixture()
        original=self.entry();before=copy.deepcopy(original)
        runtime.queue_creative=lambda *args:self.fail('preview cannot mutate backlog')
        eligible,assessment=assess_backlog(runner,[original],evidence())
        self.assertEqual(original,before)
        self.assertEqual(eligible[0]['evidence'],evidence(4))
        self.assertEqual(eligible[0]['execution_evidence'],evidence())
        self.assertEqual(eligible[0]['origin_run_id'],'old-run')
        self.assertEqual(assessment['eligible_ids'],['20'])
        self.assertEqual(assessment['total'],1)

    def test_expired_threshold_defers_without_sql_or_graph(self):
        runner,source,graph,runtime=self.fixture()
        source.current_ad_context=lambda *args:self.fail('threshold gate precedes current SQL')
        graph.get_ad=lambda *args:self.fail('threshold gate precedes Graph')
        eligible,assessment=assess_backlog(runner,[self.entry()],evidence(4))
        self.assertEqual(eligible,[])
        self.assertEqual(assessment['deferred_by_reason'],{'violation_threshold_not_met':1})

    def test_missing_readback_is_never_deletion_proof(self):
        runner,source,graph,runtime=self.fixture()
        graph.ads.clear()
        eligible,assessment=assess_backlog(runner,[self.entry()],evidence())
        self.assertEqual(eligible,[])
        self.assertEqual(assessment['deferred_ids'],{'20':'deleted_ad_proof_unavailable'})

    def test_unknown_fence_stays_fenced(self):
        runner,source,graph,runtime=self.fixture()
        with runner.store._transaction() as conn:
            conn.execute('INSERT INTO fb_asset_delete_v2_object_locks VALUES(?,?,?,?,?,?)',
                         ('creative:20','old','run','attempt','unknown','2026-10-26',))
        eligible,assessment=assess_backlog(runner,[self.entry()],evidence())
        self.assertEqual(eligible,[])
        self.assertEqual(assessment['deferred_ids'],{'20':'unknown_fence'})
        self.assertEqual(graph.calls,[])

    def test_execution_preserves_historical_evidence_and_origin_when_new_candidate_overlaps(self):
        runner,source,graph,runtime=self.fixture()
        manifest=runner.scan('history-overlap',fixtures.START,fixtures.END)
        original=self.entry()
        manifest['recovery_creatives']=[copy.deepcopy(original)]
        manifest['sha256']=digest({k:v for k,v in manifest.items() if k!='sha256'})
        frozen=copy.deepcopy(manifest)
        graph.ads['12']=ad('12','20',status='ACTIVE')
        source.rows['12']=[lineage('12','20')]
        runtime.queue_creative(original)
        self.assertEqual(runner.execute('history-overlap',manifest),'partial')
        retained=runtime.queue['20']
        self.assertEqual(retained['evidence'],original['evidence'])
        self.assertEqual(retained['origin_run_id'],original['origin_run_id'])
        self.assertEqual(retained['origin_manifest_sha256'],original['origin_manifest_sha256'])
        self.assertEqual(retained['execution_evidence']['materials']['501']['count'],5)
        self.assertEqual(manifest,frozen)
        self.assertEqual([call for call in graph.calls if call[0]=='delete'],[('delete','ad','10')])

    def test_execute_annotates_deferred_history_without_changing_evidence_or_deleting(self):
        runner,source,graph,runtime=self.fixture()
        source.vios.clear()
        manifest=runner.scan('all-deferred',fixtures.START,fixtures.END)
        original=self.entry();runtime.queue_creative(original)
        eligible,assessment=assess_backlog(runner,runtime.cleanup_backlog(),manifest['evidence'])
        self.assertEqual(runtime.queue['20'],original)
        manifest.update(recovery_creatives=eligible,recovery_assessment=assessment)
        manifest['sha256']=digest({k:v for k,v in manifest.items() if k!='sha256'})
        runner.execute('all-deferred',manifest)
        retained=runtime.queue['20']
        self.assertEqual(retained['evidence'],original['evidence'])
        self.assertEqual(retained['origin_run_id'],original['origin_run_id'])
        self.assertEqual(retained['basis'],original['basis'])
        self.assertEqual(retained['requalification'],{
            'policy_version':'20261027-v3','run_id':'all-deferred',
            'checked_at':manifest['frozen_at'],'eligible':False,'reason':'violation_threshold_not_met'})
        self.assertFalse(any(call[0]=='delete' for call in graph.calls))


if __name__=='__main__':unittest.main()
