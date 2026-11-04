"""Watchdog acceptance for a child that emits output only at final mux."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from features.fb_gpu.render_process import RenderStalled,run_render
from features.random_gpu import native_pipeline
from features.random_gpu.gpu_compute import file_digest


class NativeControlTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output=Path(self.directory.name)/'output.mp4'

    def child(self, code):
        # Python ignores our marker/work-dir arguments after -c; the helper
        # behaves exactly like a native subprocess from the watchdog's view.
        return [sys.executable,'-c',code,'features.random_gpu.native_pipeline',str(self.output)]

    def test_real_progress_keeps_non_growing_mp4_alive(self):
        code="""import sys,time
from pathlib import Path
p=Path(sys.argv[-1]);progress=Path(str(p)+'.progress')
for k in range(10):
 progress.write_text(str(k));time.sleep(.07)
p.write_bytes(b'finished')
"""
        run_render(self.child(code),timeout=5,stall_seconds=.4,poll_seconds=.02)
        self.assertEqual(self.output.read_bytes(),b'finished')
        self.assertFalse(native_pipeline.progress_path(self.output).exists())
        self.assertEqual(list(self.output.parent.glob('.native-*')),[])

    def test_constant_or_stale_progress_does_not_hide_stall(self):
        native_pipeline.progress_path(self.output).write_text('previous-job')
        code="import time; time.sleep(5)"
        with self.assertRaises(RenderStalled):
            run_render(self.child(code),timeout=4,stall_seconds=.3,poll_seconds=.02)
        self.assertFalse(self.output.exists())
        self.assertFalse(native_pipeline.progress_path(self.output).exists())
        self.assertEqual(list(self.output.parent.glob('.native-*')),[])

    def test_total_timeout_overrides_ongoing_progress(self):
        code="""import sys,time
from pathlib import Path
root=Path(sys.argv[sys.argv.index('--work-dir')+1]);(root/'partial').write_bytes(b'temp')
p=Path(str(sys.argv[-1])+'.progress')
for k in range(100):
 p.write_text(str(k));time.sleep(.03)
"""
        with self.assertRaises(subprocess.TimeoutExpired):
            run_render(self.child(code),timeout=.4,stall_seconds=3,poll_seconds=.02)
        self.assertEqual(list(self.output.parent.iterdir()),[])

    @unittest.skipUnless(os.name=='posix','Linux production process-group contract')
    def test_timeout_terminates_native_helper_grandchild(self):
        pidfile=self.output.parent/'helper.pid'
        code="""import sys,subprocess,time
from pathlib import Path
helper=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])
Path(sys.argv[-1]).with_name('helper.pid').write_text(str(helper.pid))
time.sleep(20)
"""
        with self.assertRaises(RenderStalled):
            run_render(self.child(code),timeout=5,stall_seconds=.6,poll_seconds=.03)
        pid=int(pidfile.read_text())
        state=Path('/proc')/str(pid)/'stat'
        for _ in range(20):
            if not state.exists() or state.read_text().split()[2]=='Z':
                break
            time.sleep(.05)
        else:
            self.fail('native helper survived process-group timeout')
        self.assertEqual(list(self.output.parent.glob('.native-*')),[])

    def test_cpu_node_never_creates_cuda_for_file_hash(self):
        with mock.patch.dict(os.environ,{'RANDOM_GPU_COMPUTE':'1','PIPELINE_RENDER_BACKEND':'cpu'}):
            digest=file_digest();digest.update(b'abc')
            self.assertEqual(digest.hexdigest(),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')

    def test_unchecked_failure_retains_bounded_diagnostic_tail(self):
        result=run_render(self.child("import sys;sys.stderr.write('x'*9000+'root-cause');sys.exit(2)"),
                          check=False,timeout=5,poll_seconds=.02)
        self.assertEqual(result.returncode,2)
        self.assertEqual(len(result.stderr),8192)
        self.assertTrue(result.stderr.endswith('root-cause'))
        self.assertEqual(list(self.output.parent.glob('.native-*')),[])

    def test_native_command_contains_only_render_spec(self):
        with mock.patch.dict(os.environ,{'RANDOM_GPU_PYTHON':sys.executable}):
            value=native_pipeline.command('/source',self.output,{'duration':1},
                {'version':1},{'border':'/assets/border'},cache_root='/cache')
        self.assertEqual(value[-1],str(self.output))
        self.assertIn('features.random_gpu.native_pipeline',value)
        self.assertNotIn('TOKEN',' '.join(value))


if __name__=='__main__':unittest.main(verbosity=2)
