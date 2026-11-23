import copy
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from features.gpu_offload.media_rollback import rollback,NODE


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'rollback.json'
        self.current=dict(config={'version':20,'hosts':{'cpu-main':{'prepare':{'target':4,'ceiling':4,'scaling_mode':'manual'}},NODE:{'prepare':{'target':1,'ceiling':1,'scaling_mode':'manual'},'publish':{'target':0}}}},held=[],last_seen_at=dt.datetime.now(dt.timezone.utc).isoformat())
        self.changes=[]
    def tearDown(self):self.temp.cleanup()
    def invoke(self,action):
        def change(config,host,slots):
            self.changes.append((host,slots));self.current['config']['hosts'][host]['prepare'].update(target=slots,ceiling=slots)
            self.current['config']['version']+=1
        return rollback(action,self.path,read=lambda:copy.deepcopy(self.current),change=change)
    def test_only_returns_released_slots_after_real_or_unknown_work_settles(self):
        self.current['held']=[{'id':1,'status':'running'}]
        self.invoke('drain');self.assertEqual(self.changes,[(NODE,0)])
        self.assertIn('waiting_for',self.invoke('restore'))
        self.current['held'][0]['status']='unknown'
        self.assertIn('waiting_for',self.invoke('restore'))
        self.current['held']=[];self.invoke('restore');self.invoke('restore')
        self.assertEqual(self.changes,[(NODE,0),('cpu-main',5)])
    def test_does_not_override_concurrent_cpu_maintenance(self):
        self.invoke('drain');self.current['config']['hosts']['cpu-main']['prepare']['target']=3
        with self.assertRaises(RuntimeError):self.invoke('restore')
        self.assertEqual(self.changes,[(NODE,0)])
    def test_status_never_changes_capacity_or_creates_a_rollback(self):
        self.invoke('status');self.assertEqual(self.changes,[]);self.assertFalse(self.path.exists())
    def test_later_transfer_has_a_fresh_rollback_record(self):
        self.invoke('drain');self.invoke('restore')
        self.current['config']['hosts']['cpu-main']['prepare'].update(target=3,ceiling=3)
        self.current['config']['hosts'][NODE]['prepare'].update(target=2,ceiling=2)
        self.invoke('drain');self.invoke('restore')
        self.assertEqual(self.changes,[(NODE,0),('cpu-main',5),(NODE,0),('cpu-main',5)])
        self.assertEqual(len(list(self.path.parent.glob('*.completed-*.json'))),1)


if __name__=='__main__':unittest.main()
