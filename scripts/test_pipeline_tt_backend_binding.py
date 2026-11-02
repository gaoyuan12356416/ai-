"""Real native ledgers bind shared TT H264 profiles to the selected host."""
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from features.pipeline_dispatch.adapters import AdapterError
from features.pipeline_dispatch.tt_native import preparation_target
from scripts import test_pipeline_tt_bridge as fixtures


class TTBackendBindingTests(unittest.TestCase):
    def setUp(self):
        approved = patch.dict(os.environ, {'PIPELINE_TT_APPROVED_OUTRO_SHA256': 'd' * 64,
                                          'PIPELINE_TT_APPROVED_FONT_SHA256': 'e' * 64})
        approved.start(); self.addCleanup(approved.stop)
        self.assets = ['asset:tt-outro:' + 'd' * 64, 'asset:tt-font:' + 'e' * 64]

    def pool_case(self, profile, family, backend):
        fixture = fixtures.PoolBridgeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.service.media_profile_version = profile
        fixture.service.source_trim_tail_seconds = 0 if profile == 'tt-post-source-direct-v1' else 4.333333
        if family == 'intake':
            item = fixture.service.material_pool_add(fixture.fixture.recurring_material_payload())['item']
            job = {'source_key': 'tt:intake:' + str(item['id']), 'source_id': str(item['id']),
                   'channel': 'tt', 'family': family, 'stage': 'prepare', 'generation': 1}
        else:
            item, job = fixture.direct_task()
        job.update(executor_backend=backend, executor_capabilities=self.assets)
        self.assertEqual(item['preparation_profile'], profile)
        return fixture, job

    def test_gpu_auto_h264_routes_bind_gpu_and_preserve_frozen_generation(self):
        for profile in ('tt-post-h264-720x1280-v2', 'tt-post-direct-clean-h264-720x1280-v1', 'tt-post-direct-outro-h264-720x1280-v2'):
            with self.subTest(profile=profile):
                fixture = fixtures.RoutedTTBridgeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
                fixture.executor.test_routes[(fixture.task.template_id, fixture.task.template_version)] = replace(fixture.route, media_profile_version=profile)
                job = dict(fixture.job, executor_backend='gpu')
                descriptor = fixture.bridge.reserve(job, fixture.claim)
                self.assertEqual(descriptor['request']['expected_profile'], profile)
                self.assertIn('backend:gpu', descriptor['required_capabilities'])
                self.assertNotIn('backend:cpu', descriptor['required_capabilities'])
                self.assertEqual(fixture.bridge.reserve(dict(job, generation=2, executor_backend='cpu'), fixture.claim), descriptor)
                self.assertEqual(fixture.route_gpu.prepare_calls, [])

    def test_gpu_intake_and_direct_h264_keep_profile_and_actual_backend(self):
        for family in ('intake', 'direct_test'):
            for profile in ('tt-post-h264-720x1280-v2', 'tt-post-direct-clean-h264-720x1280-v1', 'tt-post-direct-outro-h264-720x1280-v2'):
                with self.subTest(family=family, profile=profile):
                    fixture, job = self.pool_case(profile, family, 'gpu')
                    descriptor = fixture.bridge.reserve(job, fixture.claim)
                    self.assertEqual(descriptor['request']['expected_profile'], profile)
                    self.assertIn('backend:gpu', descriptor['required_capabilities'])
                    self.assertEqual(fixture.bridge.reserve(dict(job, generation=2, executor_backend='cpu'), fixture.claim), descriptor)
                    self.assertEqual(fixture.fixture.gpu.prepare_jobs, [])

    def test_new_cpu_automatic_random_variants_freeze_explicit_cpu_profile(self):
        for profile in ('tt-post-random-overlay-hevc-720x1280-v3', 'tt-post-random-overlay-h264-720x1280-v3'):
            with self.subTest(profile=profile):
                fixture = fixtures.RoutedTTBridgeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
                fixture.executor.test_routes[(fixture.task.template_id, fixture.task.template_version)] = replace(fixture.route, media_profile_version=profile, source_trim_tail_seconds=0)
                descriptor = fixture.bridge.reserve(fixture.job, fixture.claim)
                self.assertEqual(descriptor['request']['expected_profile'], 'tt-post-random-overlay-libx264-720x1280-v1')
                self.assertEqual(descriptor['native_route']['profile'], profile)
                self.assertIn('backend:cpu', descriptor['required_capabilities'])
                self.assertEqual(fixture.bridge.reserve(dict(fixture.job, generation=2, executor_backend='gpu'), fixture.claim), descriptor)
                self.assertEqual(fixture.route_gpu.prepare_calls, [])
                # A saved intake/direct profile is never converted this way.
                with self.assertRaisesRegex(AdapterError, 'canonical_render_profile_backend_mismatch'):
                    preparation_target({'executor_backend': 'cpu'}, profile)

    def test_source_direct_supports_both_backends_without_transforming_profile(self):
        for family in ('intake', 'direct_test'):
            for backend in ('cpu', 'gpu'):
                with self.subTest(family=family, backend=backend):
                    fixture, job = self.pool_case('tt-post-source-direct-v1', family, backend)
                    descriptor = fixture.bridge.reserve(job, fixture.claim)
                    self.assertEqual(descriptor['request']['expected_profile'], 'tt-post-source-direct-v1')
                    self.assertIn('backend:' + backend, descriptor['required_capabilities'])
                    self.assertEqual(fixture.fixture.gpu.prepare_jobs, [])

    def test_frozen_hevc_rejects_cpu_before_any_native_claim(self):
        for family in ('intake', 'direct_test'):
            fixture, job = self.pool_case('tt-post-direct-outro-hevc-720x1280-v2', family, 'cpu')
            before = fixture.bridge._native_dict(job)
            with self.subTest(family=family), self.assertRaisesRegex(AdapterError, 'canonical_render_profile_backend_mismatch'):
                fixture.bridge.reserve(job, fixture.claim)
            self.assertEqual(fixture.bridge._native_dict(job), before)
            with fixture.bridge.connect() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_tt_claim').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_tt_operation').fetchone()[0], 0)
            self.assertEqual(fixture.fixture.gpu.prepare_jobs, [])

    def test_legacy_backend_inference_and_new_auto_cpu_mapping_remain_compatible(self):
        self.assertEqual(preparation_target({}, 'tt-post-h264-720x1280-v2'), ('tt-post-h264-720x1280-v2', 'cpu'))
        self.assertEqual(preparation_target({}, 'tt-post-hevc-720x1280-v2'), ('tt-post-hevc-720x1280-v2', 'gpu'))
        self.assertEqual(preparation_target({}, 'tt-post-source-direct-v1'), ('tt-post-source-direct-v1', 'gpu'))
        self.assertEqual(preparation_target({'executor_backend': 'cpu'}, 'tt-post-direct-outro-hevc-720x1280-v2', automatic=True),
                         ('tt-post-direct-outro-h264-720x1280-v2', 'cpu'))


if __name__ == '__main__': unittest.main()
