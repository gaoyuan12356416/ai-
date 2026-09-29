import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from features.fb_gpu.render_process import RenderStalled, run_render


class RenderProgressTests(unittest.TestCase):
    def command(self, body, output):
        return [sys.executable, "-c", "import sys,time;from pathlib import Path;" + body, str(output)]

    def test_stalled_child_is_reaped_and_next_render_can_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'output.mp4'
            with self.assertRaises(RenderStalled):
                run_render(self.command("Path(sys.argv[1]).write_bytes(b'x');time.sleep(20)", output),
                           stall_seconds=.3, poll_seconds=.03)
            result = run_render(self.command("Path(sys.argv[1]).write_bytes(b'ok')", output),
                                stall_seconds=2, poll_seconds=.03)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b'ok')

    def test_growing_output_keeps_render_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'output.mp4'
            run_render(self.command("p=Path(sys.argv[1]);[(p.write_bytes(b'x'*(n+1)),time.sleep(.1)) for n in range(8)]", output),
                       stall_seconds=.4, poll_seconds=.03)
            self.assertEqual(output.stat().st_size, 8)

    def test_total_timeout_still_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.TimeoutExpired):
                run_render(self.command("time.sleep(20)", Path(tmp)/'o'), timeout=.2,
                           stall_seconds=2, poll_seconds=.03)
