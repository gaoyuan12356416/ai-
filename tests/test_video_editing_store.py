import tempfile
import unittest
from pathlib import Path
from features.video_editing.store import Store,owner,can_manage


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Store(Path(self.tmp.name)/'db.sqlite3')
    def test_additive_restart_keeps_records(self):
        with self.store.transaction() as db:self.store.insert('source',{'id':'one','owner':'owner','kind':'asset','name':'video','status':'ready','data':{'sha':'x'}},db)
        again=Store(self.store.path);self.assertEqual(again.get('source','one')['data'],{'sha':'x'})
    def test_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                self.store.insert('source',{'id':'one','owner':'owner','kind':'asset','name':'video','status':'ready','data':{}},db)
                raise RuntimeError()
        self.assertIsNone(self.store.get('source','one'))
    def test_dispatcher_lease_shared_by_instances(self):
        again=Store(self.store.path)
        self.assertTrue(self.store.lease('worker1'));self.assertFalse(again.lease('worker2'));self.assertTrue(self.store.lease('worker1'))
    def test_owner_includes_tenant(self):
        a={'user_id':'same','tenant_key':'a'};b={'user_id':'same','tenant_key':'b'}
        self.assertNotEqual(owner(a),owner(b));self.assertFalse(can_manage({'owner':owner(a)},b))


if __name__=='__main__':unittest.main()
