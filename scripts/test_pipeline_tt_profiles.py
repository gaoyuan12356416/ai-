"""Frozen TT routes execute locally with exact codec and asset capabilities."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.executors import NodeExecutors, ExecutionError
from features.pipeline_dispatch.tt_profiles import supported_profiles, select_config, asset_capabilities, TTProfileError
from features.tt_gpu import worker
from scripts import test_tt_gpu_worker as fixture
from scripts.test_pipeline_executors import Context


class TTProfiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = fixture.make_random_overlay_config(self.root)
        self.base = replace(self.base, fixed_outro_sha256=worker._file_sha256(self.base.fixed_outro_path)[0])

    def test_complete_cpu_gpu_profile_matrix_keeps_existing_delivery_gates(self):
        self.assertEqual(len(supported_profiles('cpu')), 5)
        self.assertEqual(len(supported_profiles('gpu')), 9)
        for backend in ('cpu', 'gpu'):
            for profile in supported_profiles(backend):
                config = select_config(self.base, profile, backend)
                self.assertEqual(config.profile, worker._selected_media_profile(config.video_encoder, config.media_mode))
                worker._delivery_video_contract(config)
                if backend == 'cpu':
                    self.assertEqual(config.video_encoder, 'libx264')
                    self.assertNotIn('nvenc', ' '.join(worker._encoder_arguments(config)))
                self.assertEqual(config.direct_post_eligible(), config.media_mode != 'branded_preview')
                if config.media_mode != 'random_overlay': self.assertEqual(config.compositor_backend, 'cpu_legacy')
        with self.assertRaisesRegex(TTProfileError, 'unsupported'):
            select_config(self.base, worker.DIRECT_OUTRO_PROFILE, 'cpu')

    def test_outro_processor_selected_by_frozen_profile_and_cached_separately(self):
        profiles = [worker.RANDOM_OVERLAY_CPU_PROFILE, worker.DIRECT_OUTRO_H264_PROFILE, worker.SOURCE_DIRECT_PROFILE]
        seen = []
        def construct(config, **kwargs):
            seen.append(config)
            return SimpleNamespace(config=config)
        env = {'PIPELINE_EXECUTORS': 'tt_prepare', 'PIPELINE_RENDER_PROFILES': ','.join(profiles)}
        with patch.dict(os.environ, env), patch.object(worker.WorkerConfig, 'from_env', return_value=self.base), patch.object(worker, 'TTPostGPUProcessor', side_effect=construct):
            node = NodeExecutors({'state_root': self.temp.name, 'backend': 'cpu'})
            outro = node._processor('tt_prepare', worker.DIRECT_OUTRO_H264_PROFILE)
            random = node._processor('tt_prepare', worker.RANDOM_OVERLAY_CPU_PROFILE)
            self.assertIs(outro, node._processor('tt_prepare', worker.DIRECT_OUTRO_H264_PROFILE))
            self.assertIsNot(outro, random)
            self.assertEqual(outro.config.media_mode, 'direct_outro')
            self.assertEqual(random.config.media_mode, 'random_overlay')
            caps = node.capabilities()
            self.assertTrue(asset_capabilities(outro.config).issubset(caps))
            self.assertNotIn('profile:' + worker.DIRECT_OUTRO_PROFILE, caps)
            from features.pipeline_dispatch import tt_profiles
            with patch.object(tt_profiles, 'file_sha256', wraps=tt_profiles.file_sha256) as fingerprint:
                node.capabilities()
                node.capabilities()
                fingerprint.assert_not_called()
            payload = {'request': {'expected_profile': worker.DIRECT_OUTRO_H264_PROFILE, 'source_trim_tail_seconds': 4.333333}, 'required_capabilities': sorted(asset_capabilities(outro.config))}
            node._validate_frozen_catalog('tt_prepare', payload)
            with patch.object(node, '_prepare_processor', return_value={'output_url': 'https://media.example/out.mp4', 'output_sha256': 'a'*64, 'output_size': 100, 'profile': worker.DIRECT_OUTRO_H264_PROFILE}) as prepare:
                node.prepare('tt_prepare', payload, Context())
            self.assertIs(prepare.call_args.args[1], outro)
            self.assertEqual(prepare.call_args.args[2]['source_trim_tail_seconds'], 4.333333)
            # Even a same-size modification with restored mtime is rehashed at
            # the actual execution boundary, rather than trusting idle cache.
            old_stat = self.base.font_file.stat()
            self.base.font_file.write_bytes(b'x' * old_stat.st_size)
            os.utime(self.base.font_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            with self.assertRaises(ExecutionError): node._validate_frozen_catalog('tt_prepare', payload)

    def test_approved_outro_mismatch_never_silently_switches_assets(self):
        self.base.fixed_outro_path.write_bytes(b'changed-outro')
        with self.assertRaisesRegex(TTProfileError, 'fingerprint'):
            select_config(self.base, worker.DIRECT_OUTRO_H264_PROFILE, 'cpu')


if __name__ == '__main__': unittest.main()
