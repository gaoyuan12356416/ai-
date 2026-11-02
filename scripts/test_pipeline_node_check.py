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
from scripts import test_tt_gpu_worker as tt_fixture
from features.pipeline_dispatch.tt_profiles import select_config


class NodeCheckTests(unittest.TestCase):
    def test_new_outro_profile_has_targeted_real_pipeline_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = tt_fixture.make_random_overlay_config(root)
            from dataclasses import replace
            base = replace(base, fixed_outro_sha256=tt_fixture.worker._file_sha256(base.fixed_outro_path)[0])
            base = select_config(base, tt_fixture.worker.RANDOM_OVERLAY_CPU_PROFILE, 'cpu')
            profile = tt_fixture.worker.DIRECT_OUTRO_H264_PROFILE
            env = {'PIPELINE_EXECUTORS': 'tt_prepare', 'PIPELINE_RENDER_PROFILES': base.profile + ',' + profile}
            with patch.dict(os.environ, env), patch('scripts.pipeline_node_check.hardware_inventory', return_value={}), patch.object(tt_fixture.worker.WorkerConfig, 'from_env', return_value=base), patch.object(tt_fixture.worker, 'TTPostGPUProcessor', side_effect=lambda config, **kw: SimpleNamespace(config=config)), patch('scripts.pipeline_node_check.encode_sample', return_value=root / 'sample.mp4'), patch('scripts.pipeline_node_check.encode_outro_sample', return_value=root / 'outro.mp4') as outro:
                report = validate_node({'state_root': temp, 'node_id': 'cpu-fixture', 'backend': 'cpu'}, encode=True, encode_profiles=[profile])
            self.assertEqual(outro.call_count, 1)
            self.assertEqual(outro.call_args.args[0].profile, profile)
            self.assertTrue(outro.call_args.kwargs['cpu'])
            encoded = [row for row in report['checks'] if row.get('encode_verified')]
            self.assertEqual([row['profile'] for row in encoded], [profile])
            self.assertTrue(encoded[0]['outro_transition_verified'])

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
