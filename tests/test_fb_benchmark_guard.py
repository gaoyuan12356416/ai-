import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.benchmark_fb_efficiency import check_permit, ProductionNeedsResources


class FakeProcess:
    name='123'
    def __init__(self, output):
        self.output=output
    def __truediv__(self,name):
        return self
    def read_bytes(self):
        return b'python\0-m\0features.random_gpu.native_pipeline\0'+self.output.encode()+b'\0'


class BenchmarkGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.permit=self.root/'permit.json'

    def test_expired_or_withdrawn_permit_stops_test(self):
        for data in [dict(allowed=True,expires_at=time.time()-1),dict(allowed=False,expires_at=time.time()+10)]:
            self.permit.write_text(json.dumps(data))
            with self.assertRaises(ProductionNeedsResources):
                check_permit(self.permit,self.root/'benchmark')

    def test_new_production_render_stops_test(self):
        self.permit.write_text(json.dumps(dict(allowed=True,expires_at=time.time()+10)))
        with patch.object(Path,'iterdir',return_value=[FakeProcess('/production/output.mp4')]):
            with self.assertRaisesRegex(ProductionNeedsResources,'production_media_process_started'):
                check_permit(self.permit,self.root/'benchmark')

    def test_own_test_render_can_continue(self):
        self.permit.write_text(json.dumps(dict(allowed=True,expires_at=time.time()+10)))
        with patch.object(Path,'iterdir',return_value=[FakeProcess(str(self.root/'benchmark'/'output.mp4'))]):
            check_permit(self.permit,self.root/'benchmark')


if __name__=='__main__':
    unittest.main()
