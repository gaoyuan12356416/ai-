import copy
import unittest
import meta_account_pool_denial_retry as r


class DenialRetryTests(unittest.TestCase):
    def sample(self):
        return [{'event':'response','key':'account:1','actor':{'credential_id':296},'acknowledged':False,
                 'response':{'http':400,'error':{'code':9,'error_subcode':None,'is_transient':True,'message':'(#9) Access denied'}}}]

    def test_explicit_denial_selects_exact_account_and_actor(self):
        self.assertEqual(r.denied_keys(self.sample(),{'account:1':{'status':'failed'}}),(['account:1'],[296]))

    def test_ambiguous_restricted_or_acknowledged_writes_never_retried(self):
        for status,field,value in [('unknown',None,None),('failed','code',368),('failed','code',100),
                                   ('failed','is_transient',False),('verified',None,None)]:
            events=self.sample()
            if field:events[0]['response']['error'][field]=value
            self.assertEqual(r.denied_keys(events,{'account:1':{'status':status}}),([],[]))
        events=self.sample();events[0]['acknowledged']=True
        self.assertEqual(r.denied_keys(events,{'account:1':{'status':'failed'}}),([],[]))

    def test_any_bm_pause_blocks_denial_retry(self):
        with self.assertRaisesRegex(ValueError,'restricted_parent'):
            r.denied_keys(self.sample()+[{'event':'bm_paused'}],{'account:1':{'status':'failed'}})


if __name__=='__main__':unittest.main()
