import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name=='posix','Linux service locking')
class ScheduleTests(unittest.TestCase):
    def setUp(self):
        from features.gpu_offload import campaign_schedule
        self.module=campaign_schedule
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'schedule.json').write_text(json.dumps({'enabled':True,'first_due':0}))
        (self.root/'seed.json').write_text('{}');(self.root/'index').mkdir()

    def tearDown(self):self.temp.cleanup()

    def test_failed_index_retries_local_work_without_querying_database_again(self):
        with patch.object(self.module.subprocess,'run',side_effect=[SimpleNamespace(returncode=0),SimpleNamespace(returncode=1)]) as run:
            first=self.module.tick(self.root);self.assertEqual(run.call_count,2)
        self.assertTrue(first['pending_index']);first['next_due']=0
        (self.root/'refresh-state.json').write_text(json.dumps(first))
        with patch.object(self.module.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            result=self.module.tick(self.root)
        self.assertEqual(run.call_count,1);self.assertIn('-c',run.call_args.args[0]);self.assertEqual(result['status'],'complete')

    def test_disabled_and_not_due_schedules_open_no_database_connection(self):
        with patch.object(self.module.subprocess,'run') as run:
            (self.root/'schedule.json').write_text('{"enabled":false}')
            self.assertEqual(self.module.tick(self.root)['status'],'disabled')
            (self.root/'schedule.json').write_text(json.dumps({'enabled':True,'first_due':time.time()+300}))
            self.assertEqual(self.module.tick(self.root)['status'],'not_due');run.assert_not_called()

    def test_crash_after_index_publication_waits_original_interval(self):
        now=time.time();(self.root/'index/current.json').write_text('{}')
        os.utime(self.root/'seed.json',(now-20,now-20));os.utime(self.root/'index/current.json',(now-10,now-10))
        (self.root/'refresh-state.json').write_text(json.dumps({'status':'running','started_at':now-60,'next_due':0,'failures':0}))
        with patch.object(self.module.subprocess,'run') as run:
            self.assertEqual(self.module.tick(self.root)['status'],'not_due');run.assert_not_called()


if __name__=='__main__':unittest.main()
