import copy
import unittest
from scripts.recover_x_parent_admission import validate_parent, validate_reservation


class ParentAdmissionTests(unittest.TestCase):
    def parent(self):
        return {'queue': {'status':'queued','account_id':21}, 'parent': {
            'trigger_source':'auto_template','status':'queued','error_code':'',
            'unknown_count':0,'failed_count':0,'expected_count':1,'account_ids_json':'[21]'},
            'audited_recovery':True,'sibling_count':1,'logs':[{
                'status':'reserved','attempt_count':0,'unknown_outcome':0,
                'x_media_id':'','x_post_id':'','published_at':''}],
            'operations':[],'publications':[],'holds':[]}

    def reservation(self):
        return {'job':{'channel':'x','status':'failed','generation':1,
                'error_code':'x_manual_parent_not_publishable','external_intent':0,
                'canonical_receipt':'','hold_reason':'','claim_hash':'abc'},
                **{k:[] for k in ('operations','checkpoints','receipts','resources',
                                  'legacy_resources','current_native_claims')},
                'deliveries':[{'generation':1,'claim_hash':'abc',
                               'response_json':'{"accepted":true,"status":"failed"}'}]}

    def test_valid_parent_and_acknowledged_unbound_failure(self):
        validate_parent(self.parent()); validate_reservation(self.reservation())

    def test_unrelated_recovery_is_rejected(self):
        p=self.parent();p['audited_recovery']=False
        with self.assertRaises(ValueError):validate_parent(p)

    def test_stopped_parent_cannot_be_reopened(self):
        p=self.parent();p['parent']['status']='stopped'
        with self.assertRaises(ValueError):validate_parent(p)

    def test_account_and_unknown_protections_remain(self):
        for field,value in [('attempt_count',1),('unknown_outcome',1),('x_post_id','published')]:
            p=self.parent();p['logs'][0][field]=value
            with self.assertRaises(ValueError):validate_parent(p)
        p=self.parent();p['queue']['account_id']=22
        with self.assertRaises(ValueError):validate_parent(p)

    def test_failed_reservation_requires_matching_terminal_ack(self):
        for change in ({'deliveries':[]}, {'deliveries':[{'generation':2,'claim_hash':'abc','response_json':'{"accepted":true,"status":"failed"}'}]}):
            p=self.reservation();p.update(change)
            with self.assertRaises(ValueError):validate_reservation(p)

    def test_native_binding_or_external_operation_is_not_rearmed(self):
        for field in ('current_native_claims','operations','resources'):
            p=self.reservation();p[field]=[{'present':True}]
            with self.assertRaises(ValueError):validate_reservation(p)

    def test_other_errors_are_not_retried(self):
        p=self.reservation();p['job']['error_code']='x_account_suspended'
        with self.assertRaises(ValueError):validate_reservation(p)

if __name__=='__main__':unittest.main()
