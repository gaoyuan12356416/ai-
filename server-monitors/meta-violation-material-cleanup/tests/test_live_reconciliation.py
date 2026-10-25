import copy
import pathlib
import tempfile
import unittest

import test_runner
from features.fb_ad_asset_delete.store import Store, StoreError
from meta_violation_cleanup.runner import ACTOR
from meta_violation_cleanup.graph import GraphClient, GraphError
from meta_violation_cleanup.live_reconciliation import sync_observation, read_status_batch


class LiveReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(str(pathlib.Path(self.temp.name)/'ledger.sqlite3'))
        self.job='policy-cleanup-test'
        self.store.create_job({'job_id':self.job,'preview_id':'proof','actor':ACTOR,
            'objects':[{'kind':'ad','object_id':'10','account_ids':['100'],'user_ids':['1']}]})
        self.run=self.store.claim_run(self.job,'proof',ACTOR,['ad'],'req')['run_id']
        self.proof={'status':'deleted_verified','object':{'id':'10','account_id':'100','status':'DELETED'}}

    def tearDown(self):self.temp.cleanup()

    def row(self):return self.store.get_job(self.job)['objects'][0]

    def unknown(self):
        self.store.claim_object(self.job,'ad:10',self.run)
        self.store.finish_object(self.job,'ad:10',self.run,'unknown',
                                 {'credential_user_id':'1','api_accepted':True,'request_sent':True})

    def sync(self,outcome=None,**kw):
        return sync_observation(self.store,self.job,'ad:10',self.proof if outcome is None else outcome,'1',ACTOR,**kw)

    def test_unknown_reconciles_without_stopping_its_active_run(self):
        self.unknown();result=self.sync()
        self.assertEqual(result['status'],'deleted_verified')
        self.assertEqual(self.row()['status'],'deleted')
        self.assertEqual(self.store.get_job(self.job)['runs'][0]['status'],'running')
        with self.store._transaction(False) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM fb_asset_delete_v2_object_locks').fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM fb_asset_delete_v2_receipts').fetchone()[0],1)

    def test_legacy_pending_get_proof_prevents_a_future_delete_claim(self):
        self.assertEqual(self.sync(allow_pending=True)['ledger_status'],'already_deleted')
        self.assertFalse(self.store.claim_object(self.job,'ad:10',self.run)['claimed'])
        with self.store._transaction(False) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM fb_asset_delete_v2_attempts').fetchone()[0],0)

    def test_active_request_is_never_overwritten(self):
        self.store.claim_object(self.job,'ad:10',self.run)
        self.assertEqual(self.sync(allow_pending=True)['status'],'active_claim_untouched')
        self.assertEqual(self.row()['status'],'in_progress')

    def test_wrong_object_account_or_status_is_not_proof(self):
        self.unknown()
        for key,value in [('id','11'),('account_id','101'),('status','ACTIVE')]:
            bad=copy.deepcopy(self.proof);bad['object'][key]=value
            with self.subTest(key=key),self.assertRaises(StoreError):self.sync(bad)
        self.assertEqual(self.row()['status'],'unknown')

    def test_permission_error_keeps_unknown_fence(self):
        self.unknown();self.sync({'status':'unknown','error':{'code':100}})
        self.assertEqual(self.row()['status'],'unknown')
        with self.store._transaction(False) as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM fb_asset_delete_v2_object_locks').fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM fb_asset_delete_v2_receipts').fetchone()[0],0)

    def test_present_legacy_object_remains_pending(self):
        self.sync({'status':'present','object':{'id':'10','account_id':'100','status':'ACTIVE'}},allow_pending=True)
        self.assertEqual(self.row()['status'],'pending')

    def test_changed_snapshot_is_not_overwritten(self):
        self.unknown();self.assertEqual(self.sync(expected_updated_at='old')['status'],'changed_untouched')
        self.assertEqual(self.row()['status'],'unknown')

    def test_foreign_actor_is_rejected(self):
        with self.assertRaises(StoreError):
            sync_observation(self.store,self.job,'ad:10',self.proof,'1','foreign',allow_pending=True)

    def test_pending_requires_explicit_legacy_authorization(self):
        self.assertEqual(self.sync()['status'],'ineligible_untouched')
        self.assertEqual(self.row()['status'],'pending')


class BatchReadTests(unittest.TestCase):
    def graph(self,values):
        class Reader:
            _record=GraphClient._record
            _clean=GraphClient._clean
            _redact=GraphClient._redact
            def __init__(self):self.calls=[];self.tokens={}
            def _get(self,path,user,params):
                self.calls.append(params)
                value=values.pop(0)
                if isinstance(value,Exception):raise value
                return value
        return Reader()

    def test_mixed_deleted_present_and_permission_failure(self):
        g=self.graph([{'10':{'id':'10','account_id':'100','status':'DELETED'},
                      '11':{'id':'11','account_id':'100','status':'ACTIVE'},
                      '12':{'error':{'code':200}}}])
        out=read_status_batch(g,'creative',['10','11','12'],'1','100')
        self.assertEqual([out[x]['status'] for x in ['10','11','12']],['deleted_verified','present','unknown'])
        self.assertEqual(len(g.calls),1)

    def test_wrong_account_cannot_be_confirmed(self):
        g=self.graph([{'10':{'id':'10','account_id':'101','status':'DELETED'}}])
        self.assertEqual(read_status_batch(g,'ad',['10'],'1','100')['10']['status'],'unknown')

    def test_rate_limit_does_not_fan_out(self):
        g=self.graph([GraphError(4,'rate limited')])
        out=read_status_batch(g,'ad',['10','11'],'1','100')
        self.assertEqual(len(g.calls),1);self.assertTrue(all(v['status']=='unknown' for v in out.values()))


if __name__=='__main__':unittest.main()
