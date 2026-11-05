import copy
import unittest
from scripts.promote_x5945_qualified_recovery import replacement,OLD_SHA,NEW_SHA

class QualificationTests(unittest.TestCase):
    def test_only_output_bytes_change_and_source_recipe_remain(self):
        old={'result':dict(output_sha256=OLD_SHA,output_size=12,output_url='old',probe={'size':12,'codec':'h264','duration':402.333},recipe={'seed':12},source_sha256='source',profile='frozen',job_key='same')}
        before=copy.deepcopy(old)
        result=replacement(old,dict(size=13,codec='h264',duration=402.333),NEW_SHA,13,'new')
        self.assertEqual(old,before)
        for k in ('recipe','source_sha256','profile','job_key'):self.assertEqual(result[k],old['result'][k])

    def test_changed_duration_or_unreviewed_file_rejected(self):
        old={'result':dict(output_sha256=OLD_SHA,probe={'size':12,'duration':402.333})}
        with self.assertRaisesRegex(RuntimeError,'media_contract_changed'):replacement(old,{'size':13,'duration':400},NEW_SHA,13,'new')
        with self.assertRaisesRegex(RuntimeError,'reviewed_bytes_changed'):replacement(old,{'size':13,'duration':402.333},'other',13,'new')

if __name__=='__main__':unittest.main()
