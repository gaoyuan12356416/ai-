import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

if os.name!='nt':
    from features.gpu_offload.campaign_sync import synchronize
    from features.gpu_offload.campaign_snapshot import index_snapshot,current,render
    from features.gpu_offload.campaign_reader import Server


@unittest.skipIf(os.name=='nt','production synchronization uses Linux flock')
class CampaignSynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.wire=dict(v=2,m={'read_only_verified':True,'generated_at':'2026-11-23 10:00:00'},
            cf=['platform','campaign_id','copy_date'],c=[[0,'7','2026-11-22']],
            df=['platform','campaign_id','dt'],d=[[0,'7','2026-11-23']],
            xf=['level','entity_key','copy_date'],x=[],xdf=['entity_key','dt'],xd=[])
        self.seed=self.root/'seed.json';self.seed.write_text(json.dumps(self.wire))
        index_snapshot(self.seed,self.root/'source')
        self.server=Server(('127.0.0.1',0),self.root/'source','t'*40,'company')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def sync(self):return synchronize(self.root/'destination',self.server.server_port,'t'*40,rate=10**9)

    def test_fallback_receives_same_complete_index_and_deduplicates_versions(self):
        self.assertEqual(self.sync()['status'],'copied')
        self.assertEqual(json.loads(render(self.root/'destination','')[1]),self.wire)
        self.assertEqual(self.sync()['status'],'unchanged')

    def test_corrupt_new_snapshot_keeps_last_successful_version(self):
        self.sync();before=(self.root/'destination/current.json').read_bytes()
        self.wire['m']['generated_at']='2026-11-23 10:15:00';self.seed.write_text(json.dumps(self.wire))
        manifest=index_snapshot(self.seed,self.root/'source');archive=self.root/'source'/manifest['archive']
        archive.chmod(0o600);raw=bytearray(archive.read_bytes());raw[15]^=1;archive.write_bytes(raw)
        with self.assertRaisesRegex(ValueError,'checksum'):self.sync()
        self.assertEqual((self.root/'destination/current.json').read_bytes(),before)
        self.assertEqual(json.loads(render(self.root/'destination','')[1])['m']['generated_at'],'2026-11-23 10:00:00')


if __name__=='__main__':unittest.main()
