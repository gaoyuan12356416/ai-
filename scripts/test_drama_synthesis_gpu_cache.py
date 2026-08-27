"""Isolated GPU result cache tests; no video, cloud, database or ML calls."""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from features.drama_synthesis import gpu_cache
from features.drama_synthesis.core import DramaSynthesisError


ROOT = Path(__file__).resolve().parents[1]
JOB = 'synthetic-cache-job'
URL = 'https://media.example.test/canary/material.mp4'
SIZE = 691334


def recipe(source='concat_video'):
    value = {'source': source, 'version': 1, 'assets': {}}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {**value, 'recipe_sha256': hashlib.sha256(raw.encode()).hexdigest()}


def result_fixture():
    return {
        'job_id': JOB, 'output_video_url': URL, 'output_video_no_bgm_url': '',
        'output_random_template_url': '',
        gpu_cache.VERSION_KEY: 2,
        gpu_cache.ARTIFACTS_KEY: {'output_video_url': {'url': URL, 'size_bytes': SIZE}},
    }


def response(size=SIZE, status=200):
    return SimpleNamespace(status_code=status, headers={'Content-Length': str(size)}, close=mock.Mock())


def app_functions(root, head):
    names = {
        'gpu_video_result_path', 'gpu_video_result_satisfies_outputs',
        'read_gpu_video_result', 'write_gpu_video_result', '_handle_gpu_video_render_unlocked',
    }
    tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(selected) == len(names)
    env = {
        'os': os, 're': re, 'json': json, 'datetime': datetime, 'logging': logging,
        'DramaSynthesisError': DramaSynthesisError, 'drama_gpu_cache': gpu_cache,
        'GPU_VIDEO_RESULT_ROOT': str(root), 'GPU_VIDEO_WORKER_TOKEN': 'fake',
        'GPU_VIDEO_WORKER_TIMEOUT': 60, 'DRAMA_PUBLIC_ARTIFACT_CHECK_TIMEOUT': 5,
        'requests': SimpleNamespace(head=head),
        'ensure_dir': lambda path: Path(path).mkdir(parents=True, exist_ok=True),
        'public_artifact_ready': mock.Mock(return_value=False),
        'build_drama_public_url': mock.Mock(return_value=URL),
        'download_file': mock.Mock(side_effect=AssertionError('unexpected download')),
        'publish_asset': mock.Mock(side_effect=AssertionError('unexpected upload')),
        'concat_segments': mock.Mock(side_effect=AssertionError('unexpected render')),
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<isolated-gpu-cache>', 'exec'), env)
    return env


class ArtifactContractTests(unittest.TestCase):
    def check(self, value, head=None):
        head = head or mock.Mock(return_value=response())
        return gpu_cache.verify_artifacts(value, {'concat_video': True}, head=head, timeout=(5, 5))

    def test_small_verified_artifact_is_accepted_with_exact_size(self):
        head = mock.Mock(return_value=response())
        self.assertTrue(self.check(result_fixture(), head))
        head.assert_called_once_with(URL, allow_redirects=False, timeout=(5, 5))
        head.return_value.close.assert_called_once_with()

    def test_failed_head_never_becomes_a_cache_miss(self):
        for status in (206, 301, 302, 404, 500):
            with self.subTest(status=status), self.assertRaises(DramaSynthesisError):
                self.check(result_fixture(), mock.Mock(return_value=response(status=status)))
        with self.assertRaises(DramaSynthesisError):
            self.check(result_fixture(), mock.Mock(side_effect=TimeoutError('not exposed')))

    def test_truncated_or_larger_artifact_is_rejected(self):
        for size in (SIZE - 1, SIZE + 1, 0):
            with self.subTest(size=size), self.assertRaises(DramaSynthesisError):
                self.check(result_fixture(), mock.Mock(return_value=response(size)))

    def test_missing_or_malformed_length_is_rejected(self):
        for value in (None, '', 'abc', '-1', '1.0', SIZE):
            item = response()
            item.headers['Content-Length'] = value
            with self.subTest(value=value), self.assertRaises(DramaSynthesisError):
                self.check(result_fixture(), mock.Mock(return_value=item))

    def test_version_and_metadata_cannot_fall_back_to_legacy(self):
        values = []
        for key in (gpu_cache.VERSION_KEY, gpu_cache.ARTIFACTS_KEY):
            item = result_fixture()
            del item[key]
            values.append(item)
        for version in (1, 3, '2', 2.0, True):
            item = result_fixture()
            item[gpu_cache.VERSION_KEY] = version
            values.append(item)
        for item in values:
            with self.subTest(item=item), self.assertRaises(DramaSynthesisError):
                self.check(item)

    def test_malformed_artifact_entries_and_url_binding_are_rejected(self):
        for entry in ({}, {'url': URL}, {'url': URL, 'size_bytes': True},
                      {'url': URL, 'size_bytes': 0}, {'url': URL, 'size_bytes': '691334'},
                      {'url': URL + '?different', 'size_bytes': SIZE},
                      {'url': URL, 'size_bytes': SIZE, 'extra': 1}):
            item = result_fixture()
            item[gpu_cache.ARTIFACTS_KEY]['output_video_url'] = entry
            with self.subTest(entry=entry), self.assertRaises(DramaSynthesisError):
                self.check(item)

    def test_missing_requested_output_is_rejected(self):
        with self.assertRaises(DramaSynthesisError):
            gpu_cache.verify_artifacts(result_fixture(), {'no_bgm_video': True}, head=mock.Mock(), timeout=(5, 5))

    def test_random_profile_is_required_and_exact_before_any_head(self):
        for profile in (None, 'wrong-profile', gpu_cache.RECIPE_PROFILE):
            value = result_fixture()
            value['output_random_template_url'] = URL + '?random'
            value['random_template_output_sha256'] = 'a' * 64
            value['random_template_recipe_sha256'] = 'b' * 64
            if profile is not None:
                value['random_template_output_profile'] = profile
            value[gpu_cache.ARTIFACTS_KEY]['output_random_template_url'] = {'url': value['output_random_template_url'], 'size_bytes': SIZE}
            head = mock.Mock(return_value=response())
            with self.subTest(profile=profile):
                if profile == gpu_cache.RECIPE_PROFILE:
                    self.assertTrue(self.check(value, head))
                else:
                    with self.assertRaises(DramaSynthesisError):
                        self.check(value, head)
                    head.assert_not_called()

    def test_unsafe_url_is_not_requested(self):
        for url in ('file:///etc/passwd', 'https://user:pass@example.test/a', 'https://example.test/a#x'):
            item = result_fixture()
            item['output_video_url'] = item[gpu_cache.ARTIFACTS_KEY]['output_video_url']['url'] = url
            head = mock.Mock()
            with self.subTest(url=url), self.assertRaises(DramaSynthesisError):
                self.check(item, head)
            head.assert_not_called()

    def test_only_renderer_files_can_create_new_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'material.mp4'
            path.write_bytes(b'valid-render-fixture')
            result = {'output_video_url': URL}
            value = gpu_cache.artifact_metadata(result, {'output_video_url': path})
            self.assertEqual(value[gpu_cache.ARTIFACTS_KEY]['output_video_url']['size_bytes'], path.stat().st_size)
            self.assertNotIn('path', value[gpu_cache.ARTIFACTS_KEY]['output_video_url'])
            with self.assertRaises(DramaSynthesisError):
                gpu_cache.artifact_metadata(result, {})
            with mock.patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(DramaSynthesisError):
                gpu_cache.artifact_metadata(result, {'output_video_url': path})

    def test_same_recipe_is_recomputed_and_matches_completed_result(self):
        current = recipe()
        gpu_cache.verify_cached_recipe({'random_template_recipe_sha256': current['recipe_sha256']}, current)

    def test_tampered_recipe_cannot_reuse_self_reported_hash(self):
        current = recipe()
        saved = {'random_template_recipe_sha256': current['recipe_sha256']}
        current['source'] = 'no_bgm_video'
        with self.assertRaises(DramaSynthesisError) as caught:
            gpu_cache.verify_cached_recipe(saved, current)
        self.assertEqual(caught.exception.code, 'drama_recipe_hash_invalid')

    def test_different_valid_recipe_conflicts_without_regeneration(self):
        saved = {'random_template_recipe_sha256': recipe()['recipe_sha256']}
        with self.assertRaises(DramaSynthesisError) as caught:
            gpu_cache.verify_cached_recipe(saved, recipe('no_bgm_video'))
        self.assertEqual(caught.exception.code, 'drama_recipe_conflict')

    def test_response_comparison_ignores_only_internal_metadata(self):
        original = {'job_id': JOB, 'output_video_url': URL}
        cached = {**original, 'updated_at': '2026-08-27', **result_fixture()}
        self.assertEqual(gpu_cache.public_result(original),
                         gpu_cache.public_result({**original, 'updated_at': 'later', gpu_cache.VERSION_KEY: 2}))
        cached['output_video_url'] = URL + '?changed'
        self.assertNotEqual(gpu_cache.public_result(original), gpu_cache.public_result(cached))


class AppCacheIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setup_manifest(self, value):
        root = self.root / self._testMethodName
        root.mkdir()
        path = root / (JOB + '.json')
        path.write_text(json.dumps(value), encoding='utf-8')
        return root, path

    def test_cache_hit_and_fresh_process_replay_never_render(self):
        root, path = self.setup_manifest(result_fixture())
        before = path.read_bytes(), path.stat().st_mtime_ns
        for _ in range(2):
            env = app_functions(root, mock.Mock(return_value=response()))
            value = env['_handle_gpu_video_render_unlocked']({
                'job_id': JOB, 'episodes': [{'episode_url': 'http://127.0.0.1:1/not-running'}],
                'outputs': {'concat_video': True},
            })
            self.assertEqual(value['output_video_url'], URL)
            for name in ('download_file', 'publish_asset', 'concat_segments'):
                env[name].assert_not_called()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_head_failure_propagates_without_write_or_fallback(self):
        root, path = self.setup_manifest(result_fixture())
        before = path.read_bytes()
        env = app_functions(root, mock.Mock(side_effect=TimeoutError('fake')))
        with self.assertRaises(DramaSynthesisError):
            env['_handle_gpu_video_render_unlocked']({'job_id': JOB, 'episodes': [{}], 'outputs': {'concat_video': True}})
        self.assertEqual(path.read_bytes(), before)
        env['build_drama_public_url'].assert_not_called()
        env['public_artifact_ready'].assert_not_called()
        env['publish_asset'].assert_not_called()

    def test_corrupt_existing_manifest_is_not_overwritten(self):
        root, path = self.setup_manifest(result_fixture())
        path.write_text('invalid', encoding='utf-8')
        env = app_functions(root, mock.Mock())
        with self.assertRaises(DramaSynthesisError):
            env['read_gpu_video_result'](JOB, {'concat_video': True})
        self.assertEqual(path.read_text(), 'invalid')
        env['build_drama_public_url'].assert_not_called()

    def test_legacy_manifest_preserves_one_mib_threshold(self):
        root, path = self.setup_manifest({'job_id': JOB, 'output_video_url': URL})
        env = app_functions(root, mock.Mock())
        self.assertIsNone(env['read_gpu_video_result'](JOB, {'concat_video': True}))
        self.assertEqual(env['public_artifact_ready'].call_args_list, [mock.call(URL, 1024 * 1024)] * 2)
        self.assertNotIn(gpu_cache.VERSION_KEY, json.loads(path.read_text()))

    def test_legacy_url_inference_does_not_invent_new_metadata(self):
        root = self.root / self._testMethodName
        root.mkdir()
        env = app_functions(root, mock.Mock())
        env['public_artifact_ready'].return_value = True
        env['read_gpu_video_result'](JOB, {'concat_video': True})
        value = json.loads((root / (JOB + '.json')).read_text())
        self.assertNotIn(gpu_cache.VERSION_KEY, value)
        self.assertNotIn(gpu_cache.ARTIFACTS_KEY, value)

    def test_renderer_writer_records_actual_file_size_and_keeps_response_stable(self):
        root = self.root / self._testMethodName
        root.mkdir()
        path = root / 'material.mp4'
        path.write_bytes(b'small-already-verified-media')
        env = app_functions(root, mock.Mock(return_value=response(path.stat().st_size)))
        original = {'job_id': JOB, 'output_video_url': URL}
        env['write_gpu_video_result'](JOB, original, artifact_paths={'output_video_url': path})
        stored = json.loads((root / (JOB + '.json')).read_text())
        self.assertEqual(stored[gpu_cache.ARTIFACTS_KEY]['output_video_url']['size_bytes'], path.stat().st_size)
        self.assertEqual(env['read_gpu_video_result'](JOB, {'concat_video': True}), original)
        self.assertNotIn(gpu_cache.VERSION_KEY, original)

    def test_random_cache_recipe_conflict_is_not_caught_as_cache_miss(self):
        value = result_fixture()
        value['output_random_template_url'] = URL + '?random'
        value['random_template_output_sha256'] = 'a' * 64
        value['random_template_recipe_sha256'] = recipe()['recipe_sha256']
        value['random_template_output_profile'] = gpu_cache.RECIPE_PROFILE
        value[gpu_cache.ARTIFACTS_KEY]['output_random_template_url'] = {'url': value['output_random_template_url'], 'size_bytes': SIZE}
        root, path = self.setup_manifest(value)
        env = app_functions(root, mock.Mock(return_value=response()))
        before = path.read_bytes()
        with self.assertRaises(DramaSynthesisError) as caught:
            env['_handle_gpu_video_render_unlocked']({
                'job_id': JOB, 'episodes': [{}], 'outputs': {'random_template_video': True},
                'random_template_recipe': recipe('no_bgm_video'),
            })
        self.assertEqual(caught.exception.code, 'drama_recipe_conflict')
        self.assertEqual(path.read_bytes(), before)
        env['publish_asset'].assert_not_called()

    def test_worker_returns_safe_explicit_cache_error_status(self):
        from scripts.test_drama_synthesis_gpu_runtime import load_fake_worker
        module = load_fake_worker(SimpleNamespace(handle_gpu_video_render=mock.Mock(side_effect=gpu_cache.cache_error())))
        handler = module.Handler.__new__(module.Handler)
        body = json.dumps({'job_id': JOB}).encode()
        handler.headers = {'Content-Length': str(len(body))}
        handler.path = '/api/gpu-video/render'
        handler.rfile = io.BytesIO(body)
        handler._authorized = lambda: True
        handler._reply = mock.Mock()
        handler.do_POST()
        self.assertEqual(handler._reply.call_args.args[0], 503)
        self.assertEqual(handler._reply.call_args.args[1]['code'], 'gpu_result_cache_unverified')


if __name__ == '__main__':
    unittest.main(verbosity=2)
