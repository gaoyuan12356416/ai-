import copy
import unittest
import meta_asset_permission_followup as f


class FollowupTests(unittest.TestCase):
    def row(self):
        return {'kind':'page','asset_id':'1','business_id':'bm','source_live':{'is_published':True,'business':{'id':'bm'}},
                'backend':{'status':0,'forbidden_status':0}}

    def test_same_edge_matches_even_if_effective_list_has_other_tasks(self):
        row=self.row();row['source_list_tasks']=['MANAGE_LEADS']
        source={'tasks':['ADVERTISE','ANALYZE']}
        result=f.classify(row,source,copy.deepcopy(source),None,None,True,False,None,True)
        self.assertEqual(result['category'],'missing')

    def test_actual_assignment_disagreement_remains_unknown(self):
        result=f.classify(self.row(),{'tasks':['ADVERTISE']},{'tasks':['ANALYZE']},None,None,True,False,None,True)
        self.assertEqual(result['category'],'unknown')

    def test_abnormal_status_takes_precedence(self):
        row=self.row();row['backend']['status']=1
        result=f.classify(row,None,None,None,None,True,False,None,True)
        self.assertEqual(result['category'],'abnormal')

    def test_bm_share_ceiling_independent_of_individual_admin_visibility(self):
        row=self.row();row['source_live']['business']['id']='owner'
        result=f.classify(row,{'tasks':['ADVERTISE']},None,None,None,True,False,{'permitted_tasks':['PROFILE_PLUS_ADVERTISE']},False)
        self.assertEqual(result['category'],'not_delegable')
        self.assertEqual(result['difference'],'missing')

    def test_absent_share_grant_is_unknown_not_permission_denied(self):
        row=self.row();row['source_live']['business']['id']='owner'
        result=f.classify(row,{'tasks':['ADVERTISE']},{'tasks':['ADVERTISE']},None,None,True,False,None,False)
        self.assertEqual(result['category'],'unknown')

    def test_profile_task_bundle_proves_full_share_and_selects_control(self):
        row=self.row();row['source_live']['business']['id']='owner'
        result=f.classify(row,{'tasks':['ADVERTISE']},{'tasks':['ADVERTISE']},None,None,True,False,{'permitted_tasks':sorted(f.m.PAGE_PROFILE_TASKS)},False)
        self.assertEqual(result['category'],'missing')
        self.assertEqual(result['desired_tasks'],['PROFILE_PLUS_FULL_CONTROL'])


if __name__=='__main__':
    unittest.main()
