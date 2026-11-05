import copy
import unittest
from scripts.promote_x_catchup_artifact import validate_candidate

class CandidateTests(unittest.TestCase):
    def record(self):
        old={'binding':{'request':'same'},'result':{'recipe':{'seed':1},'source_sha256':'original','job_key':'job','profile':'cpu',
            'output_sha256':'a'*64,'output_size':12,'output_url':'old','probe':{'size':12,'duration':733.52,'codec':'h264'}}}
        new=copy.deepcopy(old);new['result'].update(output_sha256='b'*64,output_size=13,output_url='new');new['result']['probe']['size']=13
        return {'queue_id':5925,'old_receipt':old,'candidate_receipt':new}
    def test_byte_different_same_source_and_recipe_is_eligible(self):
        record=self.record();before=copy.deepcopy(record);validate_candidate(record,5925,'b'*64);self.assertEqual(record,before)
    def test_source_recipe_profile_or_duration_changes_are_rejected(self):
        for field in ('recipe','source_sha256','job_key','profile','probe'):
            record=self.record();record['candidate_receipt']['result'][field]={'duration':732} if field=='probe' else 'changed'
            with self.assertRaises(RuntimeError):validate_candidate(record,5925,'b'*64)
    def test_unreviewed_file_and_out_of_scope_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'reviewed_bytes_changed'):validate_candidate(self.record(),5925,'c'*64)
        with self.assertRaisesRegex(RuntimeError,'outside_incident_scope'):validate_candidate(self.record(),6035,'b'*64)

if __name__=='__main__':unittest.main()
