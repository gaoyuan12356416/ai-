import copy
import unittest
from scripts.recover_x_unstarted_media import validate


class UnstartedMediaRecoveryTests(unittest.TestCase):
    def proof(self):
        return dict(job=dict(channel='x',family='queue',stage='prepare',status='failed',generation=0,external_intent=0,hold_reason=''),
                    queue={'status':'failed'},logs=[],operations=[],native_operations=[],publications=[],holds=[],resources=[],legacy_resources=[],reposts=[],
                    selected=[dict(status='skipped',kind='deadline_original',reason='invalid_media_codec')])

    def test_repairable_failure_with_no_attempt_is_eligible(self):
        self.assertEqual(validate(self.proof()),'native_preparation')

    def test_external_evidence_and_unrepairable_sources_are_rejected(self):
        for key in ('operations','native_operations','publications','holds','resources','legacy_resources','reposts','logs'):
            p=self.proof();p[key]=[{'evidence':True}]
            with self.subTest(key=key),self.assertRaises(ValueError):validate(p)
        p=self.proof();p['selected'][0]['reason']='media_object_missing'
        with self.assertRaises(ValueError):validate(p)

    def test_publish_attempt_or_unknown_cannot_use_zero_attempt_recovery(self):
        p=self.proof();p['job'].update(stage='publish',generation=1,error_code='coordinator_operation_failed')
        p['logs']=[dict(status='failed',attempt_count=0,unknown_outcome=0,x_media_id='',x_post_id='',published_at='')]
        self.assertEqual(validate(p),'first_publish')
        for key,value in [('attempt_count',1),('unknown_outcome',1),('x_media_id','media'),('x_post_id','post')]:
            changed=copy.deepcopy(p);changed['logs'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):validate(changed)
