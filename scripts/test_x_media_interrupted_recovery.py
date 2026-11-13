import copy
import json
import unittest
from scripts.recover_x_media_interrupted import validate_nodes,check_current,HOLD


class RecoveryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.plan={'nodes':['cpu-main','hk-gpu'],'items':[{'proof':{
            'job':{'id':1,'generation':1,'claim_hash':'claim','node_id':'cpu-main','worker_id':'worker','error_code':'service_unavailable'},
            'deliveries':[{'delivery_id':'fail','request_sha256':'payload','response_json':json.dumps({'accepted':True,'status':'unknown'})}]}}]}
        self.evidence=[{'node_id':'cpu-main','receipts':[{'id':'fail','route':'jobs/fail','acked_at':'2026-11-13',
            'payload_sha256':'payload','payload':{'job_id':1,'generation':1,'claim_token_sha256':'claim',
            'worker_id':'worker','unknown_outcome':True,'error_code':'service_unavailable'}}]},
            {'node_id':'hk-gpu','receipts':[]}]

    def test_requires_acked_original_terminal_unknown_and_all_nodes(self):
        validate_nodes(self.plan,self.evidence)
        with self.assertRaises(ValueError):validate_nodes(self.plan,self.evidence[:1])
        self.evidence[0]['receipts'][0]['acked_at']=None
        with self.assertRaises(ValueError):validate_nodes(self.plan,self.evidence)

    def test_rejects_later_success_and_conflicting_claim(self):
        for field,value in [('route','jobs/complete'),('payload_sha256','different')]:
            evidence=copy.deepcopy(self.evidence);evidence[0]['receipts'][0][field]=value
            with self.assertRaises(ValueError):validate_nodes(self.plan,evidence)
        self.evidence[0]['receipts'][0]['payload']['claim_token_sha256']='other'
        with self.assertRaises(ValueError):validate_nodes(self.plan,self.evidence)

    def test_resume_permits_only_recorded_recovery_state_changes(self):
        old={'job':{'hold_reason':'','generation':1,'status':'unknown'},'prepare':{},
             'queue':{'status':'failed','updated_at':'old','id':7},
             'log':{'status':'failed','unknown_outcome':1,'updated_at':'old','attempt_count':1},'events':[{'unknown':True}]}
        new=copy.deepcopy(old);new['job']['hold_reason']=HOLD
        new['queue'].update(status='queued',updated_at='new')
        new['log'].update(status='reserved',unknown_outcome=0,updated_at='new')
        check_current(old,new,armed=True,held=True)
        self.assertEqual(old['log']['unknown_outcome'],1)
        new['log']['attempt_count']=0
        with self.assertRaises(ValueError):check_current(old,new,armed=True,held=True)


if __name__=='__main__':unittest.main()
