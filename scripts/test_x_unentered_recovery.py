import copy
import unittest
from scripts.resume_x_unentered_recovery import validate,matches


def proof():
    return dict(job=dict(channel='x',stage='publish',status='failed',generation=2,error_code='x_tag_dispatch_failed',external_intent=0,hold_reason='',canonical_receipt=''),
        prepare={'status':'completed'},queue={'status':'queued'},log=dict(status='reserved',unknown_outcome=0,x_media_id='',x_post_id='',published_at='',attempt_count=1),
        authorizations=[dict(state='armed',from_generation=1,to_generation=2,prior_attempts=1,artifact_sha256='a'*64)],
        **{k:[] for k in ('publications','reposts','holds','resources','legacy_resources','operations','checkpoints','deliveries','receipts','native_operations','native_checkpoints','native_claims')})


class Tests(unittest.TestCase):
    def test_unentered_only(self):self.assertEqual(validate(proof())['to_generation'],2)
    def test_every_new_execution_fact_blocks(self):
        for name in ('operations','checkpoints','receipts','native_operations','native_checkpoints'):
            p=proof();p[name]=[{'generation':2}]
            with self.subTest(name=name),self.assertRaises(ValueError):validate(p)
    def test_only_exact_failed_delivery_can_continue(self):
        p=proof();p['job']['claim_hash']='same'
        p['deliveries']=[dict(generation=2,claim_hash='same',response_json='{"accepted":true,"status":"failed"}')]
        validate(p)
        p['deliveries'][0]['response_json']='{"accepted":true,"status":"published"}'
        with self.assertRaises(ValueError):validate(p)
    def test_claim_or_fence_blocks(self):
        for name,record in [('native_claims',dict(stage='publish',generation=2)),('resources',{'job_id':1}),('holds',{})]:
            p=proof();p[name]=[record]
            with self.subTest(name=name),self.assertRaises(ValueError):validate(p)
    def test_no_unknown_or_consumed_retry(self):
        for update in ({'status':'unknown'},{'external_intent':1},{'hold_reason':'other'}):
            p=proof();p['job'].update(update)
            with self.assertRaises(ValueError):validate(p)
        p=proof();p['authorizations'][0]['state']='consumed'
        with self.assertRaises(ValueError):validate(p)
    def test_attempt_is_never_reset(self):
        p=proof();p['log']['attempt_count']=2
        with self.assertRaises(ValueError):validate(p)
    def test_old_post_intent_blocks(self):
        p=proof();p['native_operations']=[dict(generation=1,operation='x:POST:/2/tweets')]
        with self.assertRaises(ValueError):validate(p)
    def test_arm_preserves_old_record_and_generation(self):
        saved=proof();current=copy.deepcopy(saved)
        current['authorizations'][0]['state']='superseded_unentered'
        current['authorizations'].append(dict(saved['authorizations'][0],from_generation=2,to_generation=3))
        matches(saved,current,armed=True)
        current['authorizations'][1]['prior_attempts']=0
        with self.assertRaises(ValueError):matches(saved,current,armed=True)

if __name__=='__main__':unittest.main()
