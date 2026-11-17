"""Authorization is specific to a token/Page pair, with unknown scopes distinct."""
import hashlib
import unittest
from unittest.mock import patch

from features.meta_page_recovery.engine import Worker
from features.meta_page_recovery.meta import Graph,MetaError
from tests.test_meta_page_recovery import page,ad


class TokenGraph:
    def __init__(self,token,pages,complete=True):self.token,self.allowed,self.complete=token,pages,complete
    def ad(self,_):return ad()
    def account_pages(self):return set(self.allowed),self.complete


class PermissionTests(unittest.TestCase):
    def worker(self,graphs):
        source=type('Source',(),{'tokens':lambda _,data:list(graphs)})()
        return Worker(None,source,{},None,graphs.__getitem__)

    def select(self,worker,graph,proof=None):
        return worker.authorized_selection(dict(ad_id='100',ad_account_id='act_42',group_id=24,origin_page_id='10'),
            [page('10'),page('20'),page('30')],{},dict(language='en'),{'10'},graph,proof)

    def test_uses_token_owning_selected_page(self):
        first=TokenGraph('one',[]);second=TokenGraph('two',['20'])
        chosen,value=self.select(self.worker({'one':first,'two':second}),first,{})
        self.assertIs(chosen,second);self.assertEqual(value['page_id'],'20')

    def test_incomplete_scope_uses_platform_validation(self):
        graph=TokenGraph('one',[],False)
        chosen,value=self.select(self.worker({'one':graph}),graph,{})
        self.assertIs(chosen,graph);self.assertEqual(value['page_id'],'20')

    def test_complete_scope_without_eligible_page_waits(self):
        graph=TokenGraph('one',['10'])
        self.assertIsNone(self.select(self.worker({'one':graph}),graph,{})[1])

    def test_denied_pair_can_use_another_token_for_same_page(self):
        first=TokenGraph('one',['20']);second=TokenGraph('two',['20']);proof={}
        worker=self.worker({'one':first,'two':second})
        self.select(worker,first,proof)
        proof['permission_rejected_assignments']=[hashlib.sha256(b'one').hexdigest()+':20']
        chosen,value=self.select(worker,first,proof)
        self.assertIs(chosen,second);self.assertEqual(value['page_id'],'20')

    def test_changed_owner_refreshes_local_denials(self):
        graph=TokenGraph('two',['20']);proof={'authorization_scope_signature':'old',
            'permission_rejected_assignments':[hashlib.sha256(b'two').hexdigest()+':20']}
        chosen,value=self.select(self.worker({'two':graph}),graph,proof)
        self.assertIs(chosen,graph);self.assertEqual(value['page_id'],'20')
        self.assertEqual(proof['permission_rejected_assignments'],[])

    def test_receipted_creative_keeps_credential_after_restart(self):
        one=TokenGraph('one',[]);two=TokenGraph('two',['20']);worker=self.worker({'one':one,'two':two})
        chosen=worker.proof_graph({'ad_account_id':'act_42'},{'credential_fingerprint':hashlib.sha256(b'two').hexdigest()})
        self.assertIs(chosen,two)

    def test_task_enumeration_paginates_and_missing_tasks_is_unknown(self):
        graph=Graph('fixture')
        with patch.object(graph,'get',side_effect=[{'data':[{'id':'20','tasks':['ADVERTISE']}],
                'paging':{'next':'present','cursors':{'after':'next'}}},{'data':[{'id':'30'}]}]):
            self.assertEqual(graph.account_pages(),({'20'},False))
        with patch.object(graph,'get',side_effect=MetaError(10)):
            self.assertEqual(graph.account_pages(),(set(),False))


if __name__=='__main__':unittest.main()
