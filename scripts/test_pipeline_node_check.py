"""Offline enrollment preflight configuration checks; no network or service mutation."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.pipeline_node_check import load_environment, validate_node, encode_sample, validate_outro_probe, encode_x_repair_sample
from types import SimpleNamespace
from scripts import test_tt_gpu_worker as tt_fixture
from features.pipeline_dispatch.tt_profiles import select_config


class NodeCheckTests(unittest.TestCase):
    def test_node_enrollment_checks_complete_x_repair_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            config = SimpleNamespace(ffmpeg_bin='ffmpeg', ffprobe_bin='ffprobe', profile='x-h264-libx264-720-duration-policy-v1')
            processor = SimpleNamespace(config=config)
            executor = SimpleNamespace(_processor=lambda kind: processor, capabilities=lambda: ['prepare:x'])
            env = {'PIPELINE_EXECUTORS': 'x_preflight,x_repair', 'PIPELINE_RENDER_PROFILES': config.profile}
            with patch.dict(os.environ, env), patch('scripts.pipeline_node_check.hardware_inventory', return_value={}), patch('scripts.pipeline_node_check.NodeExecutors', return_value=executor) as factory, patch('scripts.pipeline_node_check.encode_sample', return_value=directory / 'basic.mp4'), patch('scripts.pipeline_node_check.encode_x_repair_sample', return_value=directory / 'repair.mp4') as repair:
                factory.PREPARE = ('x_preflight', 'x_repair')
                factory.PUBLISH = ()
                report = validate_node({'state_root': temp, 'node_id': 'cpu-fixture', 'backend': 'cpu'}, encode=True)
            self.assertEqual(repair.call_count, 1)
            self.assertIs(repair.call_args.args[0], processor)
            self.assertTrue(repair.call_args.kwargs['cpu'])
            self.assertTrue(next(row for row in report['checks'] if row.get('encode_verified'))['repair_pipeline_verified'])

    def test_x_repair_diagnostic_executes_the_complete_repair_runner(self):
        from features.fb_gpu.prepare_worker import software_command
        import json
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            config = SimpleNamespace(ffmpeg_bin='ffmpeg', ffprobe_bin='ffprobe', max_output_bytes=500_000_000)
            calls = []
            def actual_cpu_runner(command, **kwargs):
                calls.append(software_command(command))
            processor = SimpleNamespace(config=config, runner=actual_cpu_runner)
            with patch('scripts.pipeline_node_check.encode_sample', return_value=directory / 'source.mp4'), patch('scripts.pipeline_node_check.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(self.outro_probe()))):
                encode_x_repair_sample(processor, directory, cpu=True)
            self.assertEqual(len(calls), 1)
            command = calls[0]
            self.assertIn('libx264', command)
            self.assertNotIn('hq', command)
            self.assertIn('yadif=', command[command.index('-vf') + 1])
            self.assertIn('anullsrc=channel_layout=stereo:sample_rate=48000', command)

    def outro_probe(self, codec='h264'):
        return {'streams': [
            {'codec_type': 'video', 'codec_name': codec, 'width': 720, 'height': 1280,
             'pix_fmt': 'yuv420p', 'r_frame_rate': '30/1', 'avg_frame_rate': '30/1'},
            {'codec_type': 'audio', 'codec_name': 'aac', 'profile': 'LC',
             'sample_rate': '48000', 'channels': 2, 'channel_layout': 'stereo'},
        ], 'format': {'duration': '1.100000'}}

    def test_outro_delivery_accepts_both_actual_encoder_contracts(self):
        for codec in ('h264', 'hevc'):
            with self.subTest(codec=codec):
                validate_outro_probe(self.outro_probe(codec), codec)

    def test_outro_delivery_rejects_reproduced_25fps_and_unknown_rates(self):
        for codec in ('h264', 'hevc'):
            for rate in ('25/1', '30000/1001', '0/0', '', None):
                with self.subTest(codec=codec, rate=rate):
                    value = self.outro_probe(codec)
                    value['streams'][0].update(r_frame_rate=rate, avg_frame_rate=rate)
                    with self.assertRaisesRegex(ValueError, 'outro_encode_probe_mismatch'):
                        validate_outro_probe(value, codec)
        value = self.outro_probe()
        value['streams'][0]['avg_frame_rate'] = '25/1'
        with self.assertRaisesRegex(ValueError, 'outro_encode_probe_mismatch'):
            validate_outro_probe(value, 'h264')

    def test_outro_delivery_requires_aac_lc_48k_stereo(self):
        for field, bad in (('codec_name', 'mp3'), ('profile', 'HE-AAC'),
                           ('sample_rate', '44100'), ('channels', 1), ('channel_layout', 'mono')):
            with self.subTest(field=field):
                value = self.outro_probe()
                value['streams'][1][field] = bad
                with self.assertRaisesRegex(ValueError, 'outro_encode_probe_mismatch'):
                    validate_outro_probe(value, 'h264')
        value = self.outro_probe()
        value['streams'].pop()
        with self.assertRaisesRegex(ValueError, 'outro_encode_probe_mismatch'):
            validate_outro_probe(value, 'h264')

    def test_outro_delivery_rejects_non_delivery_pixel_format(self):
        value = self.outro_probe()
        value['streams'][0]['pix_fmt'] = 'yuv444p'
        with self.assertRaisesRegex(ValueError, 'outro_encode_probe_mismatch'):
            validate_outro_probe(value, 'h264')

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
