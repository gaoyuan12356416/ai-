"""Offline enrollment preflight configuration checks; no network or service mutation."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.pipeline_node_check import load_environment, validate_node, encode_sample
from types import SimpleNamespace


class NodeCheckTests(unittest.TestCase):
    def test_nvenc_diagnostic_uses_supported_production_frame_size(self):
        with tempfile.TemporaryDirectory() as temp, patch('scripts.pipeline_node_check.subprocess.run', side_effect=[SimpleNamespace(), SimpleNamespace(stdout='{"streams":[{"codec_name":"h264"}]}')]) as run:
            encode_sample('ffmpeg', 'ffprobe', 'h264_nvenc', Path(temp), cpu=False)
            self.assertIn('testsrc2=size=720x1280:rate=10:duration=1', run.call_args_list[0].args[0])
    def test_environment_loader_never_executes_shell_text(self):
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / "private.env"
            env.write_text('PIPELINE_TEST_LITERAL=$(touch bad)\nPIPELINE_TEST_JSON=\'{"value":"safe"}\'\n')
            env.chmod(0o600)
            with patch.dict(os.environ, {}, clear=False):
                load_environment(env)
                self.assertEqual(os.environ["PIPELINE_TEST_LITERAL"], "$(touch bad)")
                self.assertEqual(os.environ["PIPELINE_TEST_JSON"], '{"value":"safe"}')
                self.assertFalse((Path(temp) / "bad").exists())

    def test_unknown_executor_is_rejected_before_enrollment(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PIPELINE_EXECUTORS": "unknown_adapter"}), patch("scripts.pipeline_node_check.hardware_inventory", return_value={}):
            with self.assertRaisesRegex(ValueError, "executor_configuration_invalid"):
                validate_node({"state_root": temp, "node_id": "arbitrary-new-server", "backend": "cpu"})

    def test_publish_only_node_reports_actual_supported_capabilities(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PIPELINE_EXECUTORS": "x_share_publish", "PIPELINE_RENDER_PROFILES": ""}), patch("scripts.pipeline_node_check.hardware_inventory", return_value={"cpu_count": 6}):
            result = validate_node({"state_root": temp, "node_id": "arbitrary-new-server", "backend": "cpu"})
            self.assertEqual(result["node_id"], "arbitrary-new-server")
            self.assertIn("publish:x", result["capabilities"])
            self.assertNotIn("prepare:x", result["capabilities"])


if __name__ == "__main__": unittest.main()
