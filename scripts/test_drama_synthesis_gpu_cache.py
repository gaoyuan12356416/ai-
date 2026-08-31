"""Isolated GPU result cache tests; no video, cloud, database or ML calls."""
from __future__ import annotations

import ast
import contextlib
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from features.drama_synthesis import cos_upload, gpu_cache
from features.drama_synthesis.core import DramaSynthesisError
from features.drama_synthesis.local_checkpoint import (
    atomic_write_record, checkpoint_error, durable_ensure_directory,
    file_fingerprint, load_completed, read_record, save_completed,
)
from scripts import verify_drama_cos_upload as cos_verifier


ROOT = Path(__file__).resolve().parents[1]
JOB = 'synthetic-cache-job'
BUCKET = 'media-test-12345'
KEY = 'drama/canary/material.mp4'
URL = 'https://media.example.test/' + KEY
SIZE = 691334
SHA = 'a' * 64
BINDING = 'b' * 32
ETAG = '"' + 'c' * 32 + '"'

# The verifier intentionally imports no checkout modules before its real
# cleanliness gate. Unit tests inject only the already imported exact checkout
# fixtures and never enter the real apply path.
cos_verifier.cos_upload = cos_upload
cos_verifier.DramaSynthesisError = DramaSynthesisError
cos_verifier.atomic_write_record = atomic_write_record
cos_verifier.file_fingerprint = file_fingerprint
cos_verifier.read_record = read_record


def install_offline_sdk_runtime():
    from qcloud_cos import CosConfig, CosS3Client
    from qcloud_cos.cos_auth import CosS3Auth
    import requests
    from requests.adapters import HTTPAdapter
    cos_verifier._VERIFIED_SDK_RUNTIME = SimpleNamespace(
        CosConfig=CosConfig, CosS3Client=CosS3Client, CosS3Auth=CosS3Auth,
        requests=requests, HTTPAdapter=HTTPAdapter,
    )


def recipe(source='concat_video'):
    value = {'source': source, 'version': 1, 'assets': {}}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {**value, 'recipe_sha256': hashlib.sha256(raw.encode()).hexdigest()}


def artifact_url(key):
    return 'https://media.example.test/' + key


def receipt(*, key=KEY, sha256=SHA, size_bytes=SIZE, etag=ETAG, binding=BINDING, bucket=BUCKET):
    return {
        'bucket': bucket, 'key': key, 'sha256': sha256, 'size_bytes': size_bytes,
        'etag': etag, 'binding': binding,
    }


def head_response(value=None):
    value = receipt() if value is None else value
    return {
        'Content-Length': str(value['size_bytes']), 'ETag': value['etag'],
        gpu_cache.SHA_HEADER: value['sha256'], gpu_cache.SIZE_HEADER: str(value['size_bytes']),
        gpu_cache.BINDING_HEADER: value['binding'],
    }


class FakeHeadCos:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {(BUCKET, KEY): head_response()}
        self.error = error
        self.calls = []

    def head_object(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if self.error is not None:
            raise self.error
        try:
            return deepcopy(self.responses[(kwargs['Bucket'], kwargs['Key'])])
        except KeyError:
            raise FakeCosError(404) from None


def result_fixture():
    return {
        'job_id': JOB, 'output_video_url': URL, 'output_video_no_bgm_url': '',
        'output_random_template_url': '', 'input_fingerprint': 'f' * 64,
        gpu_cache.VERSION_KEY: gpu_cache.VERSION,
        gpu_cache.ARTIFACTS_KEY: {'output_video_url': {'url': URL, **receipt()}},
    }


def app_functions(root, client=None):
    client = client or FakeHeadCos()
    names = {
        'gpu_video_result_path', 'gpu_video_result_satisfies_outputs',
        'read_gpu_video_result', 'write_gpu_video_result', 'verify_gpu_artifact_uploads',
        'cleanup_gpu_video_job_files', 'cached_gpu_video_result', 'strict_cached_gpu_video_result',
        'gpu_video_local_artifact_identity', 'gpu_video_local_checkpoint_path',
        'load_gpu_video_local_artifact', 'save_gpu_video_local_artifact',
        'restore_gpu_video_public_artifact', 'gpu_video_no_bgm_profile',
        'handle_gpu_video_render',
        '_handle_gpu_video_render_unlocked',
    }
    tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(selected) == len(names)
    env = {
        'os': os, 're': re, 'json': json, 'datetime': datetime, 'timezone': timezone, 'logging': logging,
        'secrets': secrets,
        'DramaSynthesisError': DramaSynthesisError, 'drama_gpu_cache': gpu_cache,
        'atomic_write_record': atomic_write_record, 'checkpoint_error': checkpoint_error,
        'durable_ensure_directory': durable_ensure_directory, 'file_fingerprint': file_fingerprint,
        'load_completed': load_completed, 'read_record': read_record, 'save_completed': save_completed,
        'GPU_VIDEO_RESULT_ROOT': str(root), 'GPU_VIDEO_WORKER_TOKEN': 'fake',
        'GPU_VIDEO_WORKER_TIMEOUT': 60, 'DRAMA_PUBLIC_ARTIFACT_CHECK_TIMEOUT': 5,
        'COS_BUCKET': BUCKET, 'get_cos_client': mock.Mock(return_value=client),
        'build_cos_url': artifact_url,
        'drama_async_runtime': SimpleNamespace(
            capture_context=lambda: None, render_fingerprint=lambda _payload: 'f' * 64,
            emit_progress=mock.Mock(),
        ),
        'WORK_ROOT': str(root / 'work'), 'PUBLIC_ROOT': str(root / 'public'),
        'NORMALIZATION_PROFILE': 'unit-normalization-v1',
        'DEMUCS_DEVICE': 'cpu', 'DEMUCS_CHUNK_SECONDS': 90,
        'DEMUCS_FALLBACK_CHUNK_SECONDS': 45,
        'demucs_profiles': lambda: [
            {'model': 'unit-demucs', 'segment': 8, 'shifts': 1, 'jobs': 0, 'label': 'unit-demucs/s8'},
        ],
        'GPU_VIDEO_RENDER_LOCKS': {}, 'GPU_VIDEO_RENDER_LOCKS_LOCK': threading.Lock(),
        'get_named_runtime_lock': lambda *_args: threading.Lock(),
        'ensure_dir': lambda path: Path(path).mkdir(parents=True, exist_ok=True),
        'public_artifact_ready': mock.Mock(return_value=False),
        'build_drama_public_url': mock.Mock(return_value=URL),
        'download_file': mock.Mock(side_effect=AssertionError('unexpected download')),
        'publish_asset': mock.Mock(side_effect=AssertionError('unexpected upload')),
        'concat_segments': mock.Mock(side_effect=AssertionError('unexpected render')),
        'shutil': shutil, 'tempfile': tempfile,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<isolated-gpu-cache>', 'exec'), env)
    return env


class ArtifactContractTests(unittest.TestCase):
    def check(self, value, client=None):
        client = client or FakeHeadCos()
        return gpu_cache.verify_artifacts(
            value, {'concat_video': True}, client=client, bucket=BUCKET, url_for_key=artifact_url,
        )

    def test_authenticated_head_accepts_all_exact_v3_identity_fields(self):
        client = FakeHeadCos()
        self.assertTrue(self.check(result_fixture(), client))
        self.assertEqual(client.calls, [{'Bucket': BUCKET, 'Key': KEY}])

    def test_failed_head_never_becomes_a_cache_miss(self):
        for error in (FakeCosError(404), FakeCosError(500), TimeoutError('not exposed')):
            with self.subTest(error=type(error).__name__), self.assertRaises(DramaSynthesisError):
                self.check(result_fixture(), FakeHeadCos(error=error))

    def test_same_length_replacement_and_each_remote_identity_mismatch_are_rejected(self):
        changes = {
            'content-length': lambda headers: headers.update({'Content-Length': str(SIZE + 1)}),
            'metadata-size': lambda headers: headers.update({gpu_cache.SIZE_HEADER: str(SIZE + 1)}),
            'sha': lambda headers: headers.update({gpu_cache.SHA_HEADER: 'd' * 64}),
            'binding': lambda headers: headers.update({gpu_cache.BINDING_HEADER: 'e' * 32}),
            'etag': lambda headers: headers.update({'ETag': '"' + 'f' * 32 + '"'}),
        }
        for name, mutate in changes.items():
            headers = head_response()
            mutate(headers)
            client = FakeHeadCos({(BUCKET, KEY): headers})
            with self.subTest(name=name), self.assertRaises(DramaSynthesisError):
                self.check(result_fixture(), client)
            self.assertEqual(client.calls, [{'Bucket': BUCKET, 'Key': KEY}])

    def test_missing_or_malformed_authenticated_head_fields_are_rejected(self):
        for field in ('Content-Length', 'ETag', gpu_cache.SHA_HEADER, gpu_cache.SIZE_HEADER,
                      gpu_cache.BINDING_HEADER):
            headers = head_response()
            del headers[field]
            with self.subTest(field=field), self.assertRaises(DramaSynthesisError):
                self.check(result_fixture(), FakeHeadCos({(BUCKET, KEY): headers}))
        for value in ('', 'abc', '-1', '1.0', True):
            headers = head_response()
            headers['Content-Length'] = value
            with self.subTest(value=value), self.assertRaises(DramaSynthesisError):
                self.check(result_fixture(), FakeHeadCos({(BUCKET, KEY): headers}))

    def test_version_and_metadata_cannot_fall_back_to_legacy(self):
        values = []
        for key in (gpu_cache.VERSION_KEY, gpu_cache.ARTIFACTS_KEY):
            item = result_fixture()
            del item[key]
            values.append(item)
        for version in (1, 2, 4, '3', 3.0, True):
            item = result_fixture()
            item[gpu_cache.VERSION_KEY] = version
            values.append(item)
        for item in values:
            with self.subTest(item=item), self.assertRaises(DramaSynthesisError):
                self.check(item)

    def test_malformed_artifact_entries_and_url_binding_are_rejected(self):
        valid = {'url': URL, **receipt()}
        entries = [{}, {'url': URL}, {**valid, 'size_bytes': True},
                   {**valid, 'size_bytes': 0}, {**valid, 'sha256': '0' * 64},
                   {**valid, 'binding': '0' * 31}, {**valid, 'etag': ''},
                   {**valid, 'url': URL + '?different'}, {**valid, 'extra': 1}]
        for entry in entries:
            item = result_fixture()
            item[gpu_cache.ARTIFACTS_KEY]['output_video_url'] = entry
            with self.subTest(entry=entry), self.assertRaises(DramaSynthesisError):
                self.check(item)

    def test_missing_requested_output_is_rejected(self):
        with self.assertRaises(DramaSynthesisError):
            gpu_cache.verify_artifacts(
                result_fixture(), {'no_bgm_video': True}, client=FakeHeadCos(),
                bucket=BUCKET, url_for_key=artifact_url,
            )

    def test_random_profile_is_required_and_exact_before_any_head(self):
        for profile in (None, 'wrong-profile', gpu_cache.RECIPE_PROFILE):
            value = result_fixture()
            random_key = 'drama/canary/material_random_template.mp4'
            random_url = artifact_url(random_key)
            value['output_random_template_url'] = random_url
            value['random_template_output_sha256'] = SHA
            value['random_template_recipe_sha256'] = 'b' * 64
            if profile is not None:
                value['random_template_output_profile'] = profile
            value[gpu_cache.ARTIFACTS_KEY]['output_random_template_url'] = {
                'url': random_url, **receipt(key=random_key),
            }
            client = FakeHeadCos({
                (BUCKET, KEY): head_response(),
                (BUCKET, random_key): head_response(receipt(key=random_key)),
            })
            with self.subTest(profile=profile):
                if profile == gpu_cache.RECIPE_PROFILE:
                    self.assertTrue(self.check(value, client))
                    self.assertEqual(len(client.calls), 2)
                else:
                    with self.assertRaises(DramaSynthesisError):
                        self.check(value, client)
                    self.assertEqual(client.calls, [])

    def test_unsafe_url_is_not_requested(self):
        for url in ('file:///etc/passwd', 'https://user:pass@example.test/a', 'https://example.test/a#x'):
            item = result_fixture()
            item['output_video_url'] = item[gpu_cache.ARTIFACTS_KEY]['output_video_url']['url'] = url
            client = FakeHeadCos()
            with self.subTest(url=url), self.assertRaises(DramaSynthesisError):
                self.check(item, client)
            self.assertEqual(client.calls, [])

    def test_manifest_bucket_key_and_url_must_match_fixed_configuration_before_head(self):
        for field, value in (('bucket', 'other-test-12345'), ('key', 'other/object.mp4')):
            item = result_fixture()
            item[gpu_cache.ARTIFACTS_KEY]['output_video_url'][field] = value
            client = FakeHeadCos()
            with self.subTest(field=field), self.assertRaises(DramaSynthesisError):
                self.check(item, client)
            self.assertEqual(client.calls, [])

    def test_renderer_recomputes_local_sha_and_requires_exact_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'material.mp4'
            body = b'valid-render-fixture'
            path.write_bytes(body)
            result = {'output_video_url': URL, 'input_fingerprint': 'f' * 64}
            local_receipt = receipt(
                sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body),
            )
            value = gpu_cache.artifact_metadata(
                result, {'output_video_url': path}, {'output_video_url': local_receipt},
            )
            self.assertEqual(value[gpu_cache.ARTIFACTS_KEY]['output_video_url']['size_bytes'], path.stat().st_size)
            self.assertEqual(value[gpu_cache.ARTIFACTS_KEY]['output_video_url']['sha256'], local_receipt['sha256'])
            self.assertNotIn('path', value[gpu_cache.ARTIFACTS_KEY]['output_video_url'])
            with self.assertRaises(DramaSynthesisError):
                gpu_cache.artifact_metadata(result, {}, {})
            wrong = {**local_receipt, 'sha256': '0' * 64}
            with self.assertRaises(DramaSynthesisError):
                gpu_cache.artifact_metadata(
                    result, {'output_video_url': path}, {'output_video_url': wrong},
                )
            with mock.patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(DramaSynthesisError):
                gpu_cache.artifact_metadata(
                    result, {'output_video_url': path}, {'output_video_url': local_receipt},
                )

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
                         gpu_cache.public_result({**original, 'updated_at': 'later', gpu_cache.VERSION_KEY: 3}))
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

    @staticmethod
    def local_render_payload(outputs):
        return {
            'job_id': JOB, 'content_id': '1', 'episode_start': 1, 'episode_end': 1,
            'episodes': [{'episode_number': 1, 'episode_url': 'https://source.test/1.mp4'}],
            'outputs': dict(outputs),
        }

    @staticmethod
    def local_render_paths(root):
        workdir = root / 'work' / JOB
        public_dir = root / 'public' / JOB
        return SimpleNamespace(
            workdir=workdir,
            public_dir=public_dir,
            concat=workdir / ('1_%s_eps_1_1.mp4' % JOB[:8]),
            concat_public=public_dir / 'material.mp4',
            no_bgm=workdir / 'material_no_bgm.mp4',
            no_bgm_public=public_dir / 'material_no_bgm.mp4',
        )

    @staticmethod
    def configure_local_render(env, segment, *, concat_render, no_bgm_render, publish):
        env.update({
            'read_gpu_video_result': lambda *_args, **_kwargs: None,
            'download_and_prepare_segments': lambda *_args, **_kwargs: [str(segment)],
            'probe_media_stream_info': mock.Mock(), 'normalize_concat_segment': mock.Mock(),
            'valid_video_file': lambda path: Path(path).is_file() and Path(path).stat().st_size > 0,
            'valid_av_duration_alignment': lambda path: Path(path).is_file(),
            'file_ready': lambda path: Path(path).is_file() and Path(path).stat().st_size > 0,
            'concat_segments': concat_render, 'run_no_bgm_pipeline': no_bgm_render,
            'publish_asset': publish,
            'update_no_bgm_stage': mock.Mock(),
            'verify_gpu_artifact_uploads': mock.Mock(return_value=None),
            'write_gpu_video_result': mock.Mock(), 'cos_enabled': lambda: False,
            'cleanup_gpu_video_job_files': mock.Mock(
                side_effect=AssertionError('no-COS cleanup forbidden'),
            ),
        })

    @staticmethod
    def save_concat_checkpoint(env, paths, segment, *, input_fingerprint='f' * 64):
        identity = env['gpu_video_local_artifact_identity'](
            input_fingerprint, 'concat', [str(segment)], {
                'version': 1, 'pipeline': 'drama-concat-copy-v1',
                'normalization_profile': 'unit-normalization-v1',
            },
        )
        env['save_gpu_video_local_artifact'](str(paths.concat), identity)
        return identity

    @staticmethod
    def save_no_bgm_checkpoint(env, paths, *, input_fingerprint='f' * 64):
        identity = env['gpu_video_local_artifact_identity'](
            input_fingerprint, 'no_bgm', [str(paths.concat)], env['gpu_video_no_bgm_profile'](),
        )
        env['save_gpu_video_local_artifact'](str(paths.no_bgm), identity)
        return identity

    def test_cache_hit_and_fresh_process_replay_never_render(self):
        root, path = self.setup_manifest(result_fixture())
        before = path.read_bytes(), path.stat().st_mtime_ns
        for _ in range(2):
            env = app_functions(root)
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
        same_length_replacement = head_response()
        same_length_replacement[gpu_cache.SHA_HEADER] = '0' * 64
        clients = (
            FakeHeadCos(error=TimeoutError('fake')),
            FakeHeadCos({(BUCKET, KEY): same_length_replacement}),
        )
        for client in clients:
            env = app_functions(root, client)
            with self.subTest(client=type(client.error).__name__ if client.error else 'same-length'), \
                    self.assertRaises(DramaSynthesisError):
                env['_handle_gpu_video_render_unlocked']({
                    'job_id': JOB, 'episodes': [{}], 'outputs': {'concat_video': True},
                })
            self.assertEqual(path.read_bytes(), before)
            env['build_drama_public_url'].assert_not_called()
            env['public_artifact_ready'].assert_not_called()
            env['publish_asset'].assert_not_called()
            env['concat_segments'].assert_not_called()

    def test_corrupt_existing_manifest_is_not_overwritten(self):
        root, path = self.setup_manifest(result_fixture())
        path.write_text('invalid', encoding='utf-8')
        env = app_functions(root)
        with self.assertRaises(DramaSynthesisError):
            env['read_gpu_video_result'](JOB, {'concat_video': True}, input_fingerprint='f' * 64)
        self.assertEqual(path.read_text(), 'invalid')
        env['build_drama_public_url'].assert_not_called()

    def test_legacy_manifest_preserves_one_mib_threshold(self):
        root, path = self.setup_manifest({'job_id': JOB, 'output_video_url': URL})
        env = app_functions(root)
        self.assertIsNone(env['read_gpu_video_result'](JOB, {'concat_video': True}))
        self.assertEqual(env['public_artifact_ready'].call_args_list, [mock.call(URL, 1024 * 1024)] * 2)
        self.assertNotIn(gpu_cache.VERSION_KEY, json.loads(path.read_text()))

    def test_async_strict_cache_rejects_existing_unversioned_manifest_but_legacy_accepts(self):
        legacy = {'job_id': JOB, 'output_video_url': URL}
        root, path = self.setup_manifest(legacy)
        env = app_functions(root)
        env['public_artifact_ready'].return_value = True
        payload = {'job_id': JOB, 'outputs': {'concat_video': True}}
        self.assertEqual(env['cached_gpu_video_result'](payload), legacy)
        with self.assertRaises(DramaSynthesisError):
            env['strict_cached_gpu_video_result'](payload)
        env['drama_async_runtime'] = SimpleNamespace(
            capture_context=lambda: object(), render_fingerprint=lambda _payload: 'f' * 64,
            emit_progress=mock.Mock(),
        )
        with self.assertRaises(DramaSynthesisError):
            env['read_gpu_video_result'](
                JOB, {'concat_video': True}, input_fingerprint='f' * 64,
            )
        self.assertEqual(json.loads(path.read_text()), legacy)
        env['concat_segments'].assert_not_called()

    def test_legacy_url_inference_does_not_invent_new_metadata(self):
        root = self.root / self._testMethodName
        root.mkdir()
        env = app_functions(root)
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
        local = receipt(
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size_bytes=path.stat().st_size,
        )
        env = app_functions(root, FakeHeadCos({(BUCKET, KEY): head_response(local)}))
        original = {'job_id': JOB, 'output_video_url': URL, 'input_fingerprint': 'f' * 64}
        env['write_gpu_video_result'](
            JOB, original, artifact_paths={'output_video_url': path},
            artifact_receipts={'output_video_url': local},
        )
        stored = json.loads((root / (JOB + '.json')).read_text())
        self.assertRegex(stored['updated_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertEqual(stored[gpu_cache.ARTIFACTS_KEY]['output_video_url']['size_bytes'], path.stat().st_size)
        self.assertEqual(
            env['read_gpu_video_result'](
                JOB, {'concat_video': True}, input_fingerprint='f' * 64,
            ), gpu_cache.public_result(original),
        )
        self.assertNotIn(gpu_cache.VERSION_KEY, original)

    def test_random_cache_recipe_conflict_is_not_caught_as_cache_miss(self):
        value = result_fixture()
        random_key = 'drama/canary/material_random_template.mp4'
        value['output_random_template_url'] = artifact_url(random_key)
        value['random_template_output_sha256'] = SHA
        value['random_template_recipe_sha256'] = recipe()['recipe_sha256']
        value['random_template_output_profile'] = gpu_cache.RECIPE_PROFILE
        value[gpu_cache.ARTIFACTS_KEY]['output_random_template_url'] = {
            'url': value['output_random_template_url'], **receipt(key=random_key),
        }
        root, path = self.setup_manifest(value)
        env = app_functions(root, FakeHeadCos({
            (BUCKET, KEY): head_response(),
            (BUCKET, random_key): head_response(receipt(key=random_key)),
        }))
        before = path.read_bytes()
        with self.assertRaises(DramaSynthesisError) as caught:
            env['_handle_gpu_video_render_unlocked']({
                'job_id': JOB, 'episodes': [{}], 'outputs': {'random_template_video': True},
                'random_template_recipe': recipe('no_bgm_video'),
            })
        self.assertEqual(caught.exception.code, 'drama_recipe_conflict')
        self.assertEqual(path.read_bytes(), before)
        env['publish_asset'].assert_not_called()

    def test_v2_manifest_and_v3_fingerprint_errors_fail_closed_without_render(self):
        values = []
        old = result_fixture()
        old[gpu_cache.VERSION_KEY] = 2
        values.append(old)
        missing = result_fixture()
        del missing['input_fingerprint']
        values.append(missing)
        mismatch = result_fixture()
        mismatch['input_fingerprint'] = '0' * 64
        values.append(mismatch)
        root = self.root / self._testMethodName
        root.mkdir()
        path = root / (JOB + '.json')
        for index, value in enumerate(values):
            with self.subTest(index=index):
                path.write_text(json.dumps(value), encoding='utf-8')
                env = app_functions(root)
                with self.assertRaises(DramaSynthesisError):
                    env['_handle_gpu_video_render_unlocked']({
                        'job_id': JOB, 'episodes': [{}], 'outputs': {'concat_video': True},
                    })
                self.assertEqual(path.read_bytes(), json.dumps(value).encode())
                env['publish_asset'].assert_not_called()
                env['concat_segments'].assert_not_called()

    def test_cached_v3_manifest_always_compares_current_render_fingerprint(self):
        root, _path = self.setup_manifest(result_fixture())
        env = app_functions(root)
        payload = {'job_id': JOB, 'outputs': {'concat_video': True}}
        self.assertEqual(env['cached_gpu_video_result'](payload)['output_video_url'], URL)
        env['drama_async_runtime'] = SimpleNamespace(
            capture_context=lambda: None, render_fingerprint=lambda _payload: '0' * 64,
        )
        with self.assertRaises(DramaSynthesisError):
            env['cached_gpu_video_result'](payload)

    def test_async_missing_manifest_never_infers_predictable_public_url(self):
        root = self.root / self._testMethodName
        root.mkdir()
        env = app_functions(root)
        env['drama_async_runtime'] = SimpleNamespace(
            capture_context=lambda: object(), render_fingerprint=lambda _payload: 'f' * 64,
        )
        env['public_artifact_ready'].return_value = True
        self.assertIsNone(env['read_gpu_video_result'](
            JOB, {'concat_video': True}, input_fingerprint='f' * 64,
        ))
        self.assertFalse((root / (JOB + '.json')).exists())
        env['public_artifact_ready'].assert_not_called()
        env['build_drama_public_url'].assert_not_called()

    def test_manifest_writer_orders_directory_atomic_write_and_durable_readback(self):
        root = self.root / self._testMethodName
        root.mkdir()
        env = app_functions(root)
        events, saved = [], []
        env['durable_ensure_directory'] = lambda path: events.append(('directory', str(path))) or Path(path)
        env['atomic_write_record'] = lambda path, value: (
            events.append(('atomic', str(path))), saved.append(deepcopy(value))
        )
        env['read_record'] = lambda path: events.append(('readback', str(path))) or deepcopy(saved[-1])
        env['write_gpu_video_result'](JOB, {'job_id': JOB, 'output_video_url': ''})
        self.assertEqual([item[0] for item in events], ['directory', 'atomic', 'readback'])

    def test_manifest_writer_replace_fsync_and_readback_faults_require_recovery(self):
        failures = ('replace', 'fsync', 'readback_exception', 'readback_mismatch', 'directory')
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'results'
                root.mkdir()
                artifact = Path(directory) / 'material.mp4'
                artifact.write_bytes(b'retained-local-artifact')
                result_path = root / (JOB + '.json')
                result_path.write_text('{"old":"retained"}', encoding='utf-8')
                before = result_path.read_bytes()
                env = app_functions(root)
                context = contextlib.ExitStack()
                self.addCleanup(context.close)
                if failure == 'replace':
                    context.enter_context(mock.patch(
                        'features.drama_synthesis.local_checkpoint.os.replace',
                        side_effect=OSError('replace fault'),
                    ))
                elif failure == 'fsync':
                    context.enter_context(mock.patch(
                        'features.drama_synthesis.local_checkpoint.os.fsync',
                        side_effect=OSError('fsync fault'),
                    ))
                elif failure == 'readback_exception':
                    env['read_record'] = mock.Mock(side_effect=OSError('readback fault'))
                elif failure == 'readback_mismatch':
                    env['read_record'] = mock.Mock(return_value={'different': True})
                elif failure == 'directory':
                    env['durable_ensure_directory'] = mock.Mock(side_effect=OSError('directory fsync fault'))
                with self.assertRaises(DramaSynthesisError) as caught:
                    env['write_gpu_video_result'](JOB, {'job_id': JOB, 'output_video_url': ''})
                self.assertEqual(caught.exception.code, 'gpu_result_cache_unverified')
                self.assertTrue(artifact.is_file())
                if failure in {'replace', 'fsync', 'directory'}:
                    self.assertEqual(result_path.read_bytes(), before)
                context.close()

    def test_render_tail_blocks_cleanup_when_manifest_write_fails(self):
        root = self.root / self._testMethodName
        root.mkdir()
        segment = root / 'segment.mp4'
        segment.write_bytes(b'verified-segment')
        env = app_functions(root)
        events = []
        env.update({
            'read_gpu_video_result': lambda *_args, **_kwargs: None,
            'download_and_prepare_segments': lambda *_args, **_kwargs: [str(segment)],
            'probe_media_stream_info': mock.Mock(), 'normalize_concat_segment': mock.Mock(),
            'remove_invalid_video_file': mock.Mock(), 'valid_video_file': lambda _path: True,
            'file_ready': lambda path: Path(path).is_file() and Path(path).stat().st_size > 0,
            'concat_segments': lambda _segments, path: Path(path).write_bytes(b'rendered'),
            'shutil': shutil, 'publish_asset': mock.Mock(return_value=URL),
            'verify_gpu_artifact_uploads': mock.Mock(side_effect=lambda *_args: events.append('verified') or {}),
            'write_gpu_video_result': mock.Mock(
                side_effect=lambda *_args, **_kwargs: events.append('write') or (_ for _ in ()).throw(gpu_cache.cache_error())
            ),
            'cleanup_gpu_video_job_files': mock.Mock(side_effect=lambda *_args: events.append('cleanup')),
        })
        with self.assertRaises(DramaSynthesisError):
            env['_handle_gpu_video_render_unlocked']({
                'job_id': JOB, 'content_id': '1', 'episode_start': 1, 'episode_end': 1,
                'episodes': [{'episode_number': 1, 'episode_url': 'https://source.test/1.mp4'}],
                'outputs': {'concat_video': True},
            })
        self.assertEqual(events, ['verified', 'write'])
        env['cleanup_gpu_video_job_files'].assert_not_called()

    def test_no_cos_tail_writes_no_v3_and_keeps_local_directories(self):
        root = self.root / self._testMethodName
        root.mkdir()
        segment = root / 'segment.mp4'
        segment.write_bytes(b'verified-segment')
        env = app_functions(root)
        writes = []
        env.update({
            'read_gpu_video_result': lambda *_args, **_kwargs: None,
            'download_and_prepare_segments': lambda *_args, **_kwargs: [str(segment)],
            'probe_media_stream_info': mock.Mock(), 'normalize_concat_segment': mock.Mock(),
            'remove_invalid_video_file': mock.Mock(), 'valid_video_file': lambda _path: True,
            'file_ready': lambda path: Path(path).is_file() and Path(path).stat().st_size > 0,
            'concat_segments': lambda _segments, path: Path(path).write_bytes(b'rendered'),
            'shutil': shutil, 'publish_asset': mock.Mock(return_value=URL),
            'verify_gpu_artifact_uploads': mock.Mock(return_value=None),
            'write_gpu_video_result': mock.Mock(side_effect=lambda *args, **kwargs: writes.append((args, kwargs))),
            'cos_enabled': lambda: False,
            'cleanup_gpu_video_job_files': mock.Mock(side_effect=AssertionError('no-COS cleanup forbidden')),
        })
        result = env['_handle_gpu_video_render_unlocked']({
            'job_id': JOB, 'content_id': '1', 'episode_start': 1, 'episode_end': 1,
            'episodes': [{'episode_number': 1, 'episode_url': 'https://source.test/1.mp4'}],
            'outputs': {'concat_video': True},
        })
        self.assertEqual(result['output_video_url'], URL)
        self.assertEqual(writes[0][1], {})
        self.assertNotIn(gpu_cache.VERSION_KEY, writes[0][0][1])
        self.assertTrue((root / 'work' / JOB).is_dir())
        self.assertTrue((root / 'public' / JOB).is_dir())
        env['cleanup_gpu_video_job_files'].assert_not_called()

    def test_local_artifact_identity_binds_input_kind_ordered_sources_and_profile(self):
        root = self.root / self._testMethodName
        root.mkdir()
        first, second = root / 'first.mp4', root / 'second.mp4'
        first.write_bytes(b'first-source')
        second.write_bytes(b'second-source')
        env = app_functions(root)
        profile = {'pipeline': 'unit', 'version': 1}
        baseline = env['gpu_video_local_artifact_identity'](
            'f' * 64, 'concat', [first, second], profile,
        )
        self.assertNotEqual(baseline, env['gpu_video_local_artifact_identity'](
            'f' * 64, 'concat', [second, first], profile,
        ))
        self.assertNotEqual(baseline, env['gpu_video_local_artifact_identity'](
            '0' * 64, 'concat', [first, second], profile,
        ))
        self.assertNotEqual(baseline, env['gpu_video_local_artifact_identity'](
            'f' * 64, 'no_bgm', [first, second], profile,
        ))
        self.assertNotEqual(baseline, env['gpu_video_local_artifact_identity'](
            'f' * 64, 'concat', [first, second], {**profile, 'version': 2},
        ))
        second.write_bytes(b'second-source-changed')
        self.assertNotEqual(baseline, env['gpu_video_local_artifact_identity'](
            'f' * 64, 'concat', [first, second], profile,
        ))

    def test_no_bgm_pipeline_has_no_publish_side_effect_when_checkpointing(self):
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'run_no_bgm_pipeline'
        )
        self.assertEqual(
            [argument.arg for argument in function.args.kwonlyargs], ['publish_result'],
        )
        self.assertEqual(
            [value.value for value in function.args.kw_defaults if isinstance(value, ast.Constant)],
            [True],
        )
        guard = next(
            node for node in ast.walk(function)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
            and node.test.id == 'publish_result'
        )
        guarded_nodes = set(ast.walk(guard))
        publish_calls = [
            node for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'publish_asset'
        ]
        self.assertEqual(len(publish_calls), 1)
        self.assertTrue(all(node in guarded_nodes for node in publish_calls))
        guarded_copy_calls = [
            node for node in ast.walk(guard)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'shutil'
            and node.func.attr == 'copy2'
        ]
        self.assertEqual(len(guarded_copy_calls), 1)

    def test_public_restore_rechecks_checkpointed_source_before_copy(self):
        root = self.root / self._testMethodName
        root.mkdir()
        source = root / 'workspace.mp4'
        target = root / 'public' / 'material.mp4'
        source.write_bytes(b'checkpointed-bytes')
        expected = file_fingerprint(source)
        source.write_bytes(b'changed-after-checkpoint')
        env = app_functions(root)
        with self.assertRaises(DramaSynthesisError) as caught:
            env['restore_gpu_video_public_artifact'](
                source, target, expected_fingerprint=expected,
            )
        self.assertEqual(caught.exception.code, 'drama_media_checkpoint_unverified')
        self.assertFalse(target.exists())

    def test_completed_no_bgm_checkpoint_restores_and_retries_upload_without_demucs(self):
        root = self.root / self._testMethodName
        root.mkdir()
        env = app_functions(root)
        paths = self.local_render_paths(root)
        segment = root / 'segment.mp4'
        segment.write_bytes(b'verified-segment')
        paths.workdir.mkdir(parents=True)
        paths.concat.write_bytes(b'completed-concat')
        self.save_concat_checkpoint(env, paths, segment)
        paths.no_bgm.write_bytes(b'completed-no-bgm')
        no_bgm_identity = self.save_no_bgm_checkpoint(env, paths)
        concat_render = mock.Mock(side_effect=AssertionError('concat rerender forbidden'))
        no_bgm_render = mock.Mock(side_effect=AssertionError('Demucs rerender forbidden'))
        publish_events = []

        def publish(path):
            self.assertEqual(Path(path), paths.no_bgm_public)
            self.assertEqual(paths.no_bgm_public.read_bytes(), paths.no_bgm.read_bytes())
            self.assertEqual(
                env['load_gpu_video_local_artifact'](
                    str(paths.no_bgm), no_bgm_identity,
                    related_paths=(str(paths.no_bgm_public),),
                ),
                file_fingerprint(paths.no_bgm),
            )
            publish_events.append('checkpointed-copy')
            if len(publish_events) == 1:
                raise TimeoutError('simulated upload failure')
            return URL

        publish_mock = mock.Mock(side_effect=publish)
        self.configure_local_render(
            env, segment, concat_render=concat_render,
            no_bgm_render=no_bgm_render, publish=publish_mock,
        )
        env['update_no_bgm_stage'].side_effect = (
            lambda *_args: publish_events.append('progress-after-publish')
        )
        payload = self.local_render_payload({'no_bgm_video': True})
        with self.assertRaises(TimeoutError):
            env['_handle_gpu_video_render_unlocked'](payload)
        self.assertEqual(paths.no_bgm_public.read_bytes(), b'completed-no-bgm')
        env['write_gpu_video_result'].assert_not_called()

        result = env['_handle_gpu_video_render_unlocked'](payload)
        self.assertEqual(result['output_video_no_bgm_url'], URL)
        self.assertEqual(
            publish_events,
            ['checkpointed-copy', 'checkpointed-copy', 'progress-after-publish'],
        )
        self.assertEqual(paths.no_bgm_public.read_bytes(), b'completed-no-bgm')
        concat_render.assert_not_called()
        no_bgm_render.assert_not_called()
        self.assertEqual(publish_mock.call_count, 2)
        env['write_gpu_video_result'].assert_called_once()
        env['cleanup_gpu_video_job_files'].assert_not_called()

    def test_missing_corrupt_and_conflicting_local_checkpoints_fail_closed(self):
        cases = (
            ('missing-no-bgm', 'drama_media_checkpoint_unverified'),
            ('corrupt-concat', 'drama_media_checkpoint_unverified'),
            ('identity-conflict', 'drama_media_checkpoint_conflict'),
            ('public-conflict', 'drama_media_checkpoint_conflict'),
        )
        for case, expected_code in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = app_functions(root)
                paths = self.local_render_paths(root)
                segment = root / 'segment.mp4'
                segment.write_bytes(b'verified-segment')
                paths.workdir.mkdir(parents=True)
                paths.concat.write_bytes(b'completed-concat')
                outputs = {'concat_video': True}
                if case == 'missing-no-bgm':
                    self.save_concat_checkpoint(env, paths, segment)
                    paths.no_bgm.write_bytes(b'untracked-no-bgm')
                    outputs = {'no_bgm_video': True}
                elif case == 'corrupt-concat':
                    Path(env['gpu_video_local_checkpoint_path'](paths.concat)).write_text(
                        '{corrupt', encoding='utf-8',
                    )
                elif case == 'identity-conflict':
                    self.save_concat_checkpoint(
                        env, paths, segment, input_fingerprint='0' * 64,
                    )
                else:
                    self.save_concat_checkpoint(env, paths, segment)
                    paths.no_bgm.write_bytes(b'completed-no-bgm')
                    self.save_no_bgm_checkpoint(env, paths)
                    paths.no_bgm_public.parent.mkdir(parents=True)
                    paths.no_bgm_public.write_bytes(b'conflicting-public-copy')
                    outputs = {'no_bgm_video': True}
                retained = {
                    path: path.read_bytes()
                    for path in (paths.concat, paths.no_bgm, paths.no_bgm_public)
                    if path.is_file()
                }
                concat_render = mock.Mock(side_effect=AssertionError('concat rerender forbidden'))
                no_bgm_render = mock.Mock(side_effect=AssertionError('Demucs rerender forbidden'))
                publish = mock.Mock(side_effect=AssertionError('upload forbidden'))
                self.configure_local_render(
                    env, segment, concat_render=concat_render,
                    no_bgm_render=no_bgm_render, publish=publish,
                )
                with self.assertRaises(DramaSynthesisError) as caught:
                    env['_handle_gpu_video_render_unlocked'](
                        self.local_render_payload(outputs),
                    )
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(
                    {path: path.read_bytes() for path in retained}, retained,
                )
                concat_render.assert_not_called()
                no_bgm_render.assert_not_called()
                publish.assert_not_called()
                env['verify_gpu_artifact_uploads'].assert_not_called()
                env['write_gpu_video_result'].assert_not_called()

    def test_checkpoint_persistence_failure_retains_output_and_blocks_retry_publish(self):
        root = self.root / self._testMethodName
        root.mkdir()
        env = app_functions(root)
        paths = self.local_render_paths(root)
        segment = root / 'segment.mp4'
        segment.write_bytes(b'verified-segment')
        concat_render = mock.Mock(
            side_effect=lambda _segments, path: Path(path).write_bytes(b'rendered-once'),
        )
        no_bgm_render = mock.Mock(side_effect=AssertionError('Demucs forbidden'))
        publish = mock.Mock(side_effect=AssertionError('upload forbidden'))
        self.configure_local_render(
            env, segment, concat_render=concat_render,
            no_bgm_render=no_bgm_render, publish=publish,
        )
        env['save_completed'] = mock.Mock(side_effect=OSError('checkpoint fsync fault'))
        payload = self.local_render_payload({'concat_video': True})
        with self.assertRaises(DramaSynthesisError) as first:
            env['_handle_gpu_video_render_unlocked'](payload)
        self.assertEqual(first.exception.code, 'drama_media_checkpoint_unverified')
        self.assertEqual(paths.concat.read_bytes(), b'rendered-once')
        self.assertFalse(Path(env['gpu_video_local_checkpoint_path'](paths.concat)).exists())
        env['save_completed'].assert_called_once()
        publish.assert_not_called()

        # A retry sees the untracked artifact and stops before rendering.  The
        # only completed bytes remain available for operator recovery.
        with self.assertRaises(DramaSynthesisError) as retry:
            env['_handle_gpu_video_render_unlocked'](payload)
        self.assertEqual(retry.exception.code, 'drama_media_checkpoint_unverified')
        self.assertEqual(paths.concat.read_bytes(), b'rendered-once')
        concat_render.assert_called_once()
        env['save_completed'].assert_called_once()
        publish.assert_not_called()
        env['verify_gpu_artifact_uploads'].assert_not_called()
        env['write_gpu_video_result'].assert_not_called()

    def test_final_receipt_recheck_uses_same_job_checkpoint_arguments(self):
        root = self.root / self._testMethodName
        root.mkdir()
        path = root / 'material.mp4'
        path.write_bytes(b'local')
        env = app_functions(root)
        local = receipt(sha256=hashlib.sha256(b'local').hexdigest(), size_bytes=5)
        env['cos_enabled'] = lambda: True
        env['publish_asset'] = mock.Mock(return_value=(URL, local))
        result = {'job_id': JOB, 'output_video_url': URL, 'input_fingerprint': 'f' * 64}
        self.assertEqual(
            env['verify_gpu_artifact_uploads'](
                JOB, result, {'output_video_url': str(path)},
            ), {'output_video_url': local},
        )
        env['publish_asset'].assert_called_once_with(
            str(path), return_receipt=True, checkpoint_job_id=JOB,
        )

    def test_no_cos_receipt_recheck_never_publishes_or_creates_remote_metadata(self):
        root = self.root / self._testMethodName
        root.mkdir()
        env = app_functions(root)
        env['cos_enabled'] = lambda: False
        env['publish_asset'] = mock.Mock(side_effect=AssertionError('no remote publish'))
        self.assertIsNone(env['verify_gpu_artifact_uploads'](
            JOB, {'output_video_url': URL}, {'output_video_url': root / 'material.mp4'},
        ))
        env['publish_asset'].assert_not_called()

    def test_worker_returns_safe_explicit_cache_error_status(self):
        from scripts.test_drama_synthesis_gpu_runtime import load_fake_worker
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        module = load_fake_worker(SimpleNamespace(
            WORK_ROOT=directory.name, cached_gpu_video_result=mock.Mock(side_effect=gpu_cache.cache_error()),
            strict_cached_gpu_video_result=mock.Mock(side_effect=gpu_cache.cache_error()),
            handle_gpu_video_render=mock.Mock(side_effect=AssertionError('unexpected render')),
        ))
        handler = module.Handler.__new__(module.Handler)
        body = json.dumps({'job_id': JOB}).encode()
        handler.headers = {'Content-Length': str(len(body))}
        handler.path = '/api/gpu-video/render'
        handler.rfile = io.BytesIO(body)
        handler._authorized = lambda: True
        handler._reply = mock.Mock()
        handler.do_POST()
        if module.RUNTIME is not None:
            self.addCleanup(module.RUNTIME.close, 3)
        self.assertEqual(handler._reply.call_args.args[0], 503)
        self.assertEqual(handler._reply.call_args.args[1]['code'], 'gpu_result_cache_unverified')


class CompatibilityOwnerLockTests(unittest.TestCase):
    def environment(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        env = app_functions(root)
        env['get_named_runtime_lock'] = lambda *_args: contextlib.nullcontext()
        env['_handle_gpu_video_render_unlocked'] = mock.Mock(return_value={'job_id': JOB})
        return env

    @staticmethod
    def runtime(context, lock_factory):
        return SimpleNamespace(
            capture_context=lambda: context, _FileLock=lock_factory,
            runtime_error=lambda code: DramaSynthesisError(code, code, 503),
        )

    def test_async_runtime_context_does_not_reacquire_owner_lock(self):
        env = self.environment()
        lock_factory = mock.Mock(side_effect=AssertionError('owner lock must not be added twice'))
        env['drama_async_runtime'] = self.runtime(object(), lock_factory)
        self.assertEqual(env['handle_gpu_video_render']({'job_id': JOB}), {'job_id': JOB})
        lock_factory.assert_not_called()
        env['_handle_gpu_video_render_unlocked'].assert_called_once_with({'job_id': JOB})

    def test_monolith_rejects_while_worker_owner_exists(self):
        env = self.environment()
        occupied = SimpleNamespace(acquire=mock.Mock(return_value=False), release=mock.Mock())
        env['drama_async_runtime'] = self.runtime(None, mock.Mock(return_value=occupied))
        with self.assertRaises(DramaSynthesisError) as caught:
            env['handle_gpu_video_render']({'job_id': JOB})
        self.assertEqual(caught.exception.code, 'gpu_runtime_unavailable')
        occupied.release.assert_not_called()
        env['_handle_gpu_video_render_unlocked'].assert_not_called()

    def test_worker_absent_allows_at_most_one_monolith_while_owner_is_held(self):
        env = self.environment()

        class GateFileLock:
            gate = threading.Lock()

            def __init__(self, _path):
                self.held = False

            def acquire(self):
                self.held = self.gate.acquire(blocking=False)
                return self.held

            def release(self):
                if self.held:
                    self.held = False
                    self.gate.release()

        entered, finish, first = threading.Event(), threading.Event(), []

        def render(_payload):
            entered.set()
            self.assertTrue(finish.wait(3))
            return {'job_id': JOB}

        env['_handle_gpu_video_render_unlocked'] = mock.Mock(side_effect=render)
        env['drama_async_runtime'] = self.runtime(None, GateFileLock)
        thread = threading.Thread(
            target=lambda: first.append(env['handle_gpu_video_render']({'job_id': JOB})), daemon=True,
        )
        thread.start()
        self.assertTrue(entered.wait(3))
        with self.assertRaises(DramaSynthesisError) as caught:
            env['handle_gpu_video_render']({'job_id': JOB})
        self.assertEqual(caught.exception.code, 'gpu_runtime_unavailable')
        finish.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first, [{'job_id': JOB}])
        self.assertEqual(env['_handle_gpu_video_render_unlocked'].call_count, 1)


class FakeCosError(Exception):
    def __init__(self, status):
        super().__init__("private https://source.test/?token=do-not-expose")
        self.status_code = status

    def get_status_code(self):
        return self.status_code


class FakeMultipartCos:
    """In-memory SDK-shaped fake; deliberately no HTTP or cloud credentials."""

    def __init__(self):
        self.calls = {name: [] for name in ("head", "create", "list", "part", "complete", "versioning")}
        self.uploads = {}
        self.object_headers = None
        self.fail_before_parts = set()
        self.lose_part_responses = set()
        self.lose_create_response = False
        self.lose_complete_response = False
        self.fail_complete_before_commit = False
        self.fail_heads_after_complete = 0
        self.fail_all_heads = False
        self.page_size = 1000
        self.transform_list = None
        self.versioning = {}
        self.fail_versioning = False
        self.racing_object_headers = None
        self.aborts = 0

    def head_object(self, **kwargs):
        self.calls["head"].append(kwargs)
        if self.fail_all_heads:
            raise FakeCosError(503)
        if self.object_headers is not None:
            if self.fail_heads_after_complete:
                self.fail_heads_after_complete -= 1
                raise FakeCosError(503)
            return deepcopy(self.object_headers)
        raise FakeCosError(404)

    def create_multipart_upload(self, **kwargs):
        self.calls["create"].append(deepcopy(kwargs))
        upload_id = "upload-" + str(len(self.calls["create"]))
        self.uploads[upload_id] = {"parts": {}, "settings": deepcopy(kwargs)}
        if self.lose_create_response:
            self.lose_create_response = False
            raise TimeoutError("private create response")
        return {"UploadId": upload_id}

    def get_bucket_versioning(self, **kwargs):
        self.calls["versioning"].append(kwargs)
        if self.fail_versioning:
            raise FakeCosError(503)
        return deepcopy(self.versioning)

    def list_parts(self, **kwargs):
        self.calls["list"].append(kwargs)
        value = self.uploads.get(kwargs["UploadId"])
        if value is None:
            raise FakeCosError(404)
        numbers = sorted(number for number in value["parts"] if number > kwargs["PartNumberMarker"])
        selected = numbers[:min(self.page_size, kwargs["MaxParts"])]
        entries = [{"PartNumber": str(number), "ETag": value["parts"][number]["ETag"],
                    "Size": str(len(value["parts"][number]["Body"]))} for number in selected]
        response = {"Part": entries, "IsTruncated": "true" if len(selected) < len(numbers) else "false",
                    "NextPartNumberMarker": str(selected[-1] if selected else kwargs["PartNumberMarker"])}
        return self.transform_list(response) if self.transform_list else response

    def upload_part(self, **kwargs):
        self.calls["part"].append({key: value for key, value in kwargs.items() if key != "Body"})
        number = kwargs["PartNumber"]
        if number in self.fail_before_parts:
            self.fail_before_parts.remove(number)
            raise FakeCosError(503)
        body = kwargs["Body"].read() if hasattr(kwargs["Body"], "read") else bytes(kwargs["Body"])
        etag = '"' + hashlib.md5(body).hexdigest() + '"'
        self.uploads[kwargs["UploadId"]]["parts"][number] = {"Body": body, "ETag": etag}
        if number in self.lose_part_responses:
            self.lose_part_responses.remove(number)
            raise TimeoutError("private part response")
        return {"ETag": etag}

    def complete_multipart_upload(self, **kwargs):
        self.calls["complete"].append(deepcopy(kwargs))
        if self.racing_object_headers is not None:
            self.object_headers = deepcopy(self.racing_object_headers)
            self.racing_object_headers = None
        if (self.object_headers is not None
                and kwargs.get("Metadata", {}).get(cos_upload.FORBID_OVERWRITE_HEADER) == "true"):
            raise FakeCosError(409)
        if self.fail_complete_before_commit:
            self.fail_complete_before_commit = False
            raise FakeCosError(503)
        value = self.uploads[kwargs["UploadId"]]
        receipts = kwargs["MultipartUpload"]["Part"]
        for entry in receipts:
            if entry["ETag"] != value["parts"][entry["PartNumber"]]["ETag"]:
                raise AssertionError("incorrect completion receipt")
        body = b"".join(value["parts"][entry["PartNumber"]]["Body"] for entry in receipts)
        etag = '"' + hashlib.md5(body).hexdigest() + "-" + str(len(receipts)) + '"'
        self.object_headers = {
            **{name.upper(): item for name, item in value["settings"]["Metadata"].items()},
            "Content-Length": str(len(body)), "ETag": etag,
            "Content-Type": value["settings"]["ContentType"],
        }
        del self.uploads[kwargs["UploadId"]]
        if self.lose_complete_response:
            self.lose_complete_response = False
            raise TimeoutError("private complete response")
        return {"ETag": etag}

    def abort_multipart_upload(self, **_kwargs):
        self.aborts += 1
        raise AssertionError("must not abort retained parts")


class ResumableCosUploadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "rendered.mp4"
        self.body = b"a" * cos_upload.MIB + b"b" * cos_upload.MIB + b"ending"
        self.path.write_bytes(self.body)
        self.checkpoint = self.root / ".runtime" / "uploads" / "object.json"
        self.client = FakeMultipartCos()
        patcher = mock.patch.object(cos_upload, "DEFAULT_PART_SIZE", cos_upload.MIB)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.assertEqual(self.client.aborts, 0)

    def upload(self, **updates):
        args = {"bucket": "private-test-12345", "key": "drama/test/material.mp4", "path": self.path,
                "checkpoint_path": self.checkpoint, "content_type": "video/mp4"}
        args.update(updates)
        return cos_upload.resume_upload(self.client, **args)

    def interrupted(self, part=2):
        self.client.fail_before_parts.add(part)
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_failed")
        return json.loads(self.checkpoint.read_text())

    def test_completed_upload_verifies_metadata_and_replay_sends_no_new_parts(self):
        progress = []
        result = self.upload(progress_callback=lambda done, total: progress.append((done, total)))
        self.assertEqual(set(result), {'bucket', 'key', 'sha256', 'size_bytes', 'etag', 'binding'})
        self.assertEqual(result["sha256"], hashlib.sha256(self.body).hexdigest())
        self.assertEqual(result["size_bytes"], len(self.body))
        self.assertEqual(self.path.read_bytes(), self.body)
        self.assertEqual([item["PartNumber"] for item in self.client.calls["part"]], [1, 2, 3])
        self.assertTrue(all(item["EnableMD5"] is True for item in self.client.calls["part"]))
        self.assertEqual(progress[-1], (len(self.body), len(self.body)))
        self.assertEqual([item[0] for item in progress], sorted(item[0] for item in progress))
        create = self.client.calls["create"][0]
        self.assertEqual((create["ACL"], create["ContentType"]), ("public-read", "video/mp4"))
        self.assertIn(cos_upload.SHA_HEADER, create["Metadata"])
        self.assertEqual(result['binding'], create['Metadata'][cos_upload.BINDING_HEADER])
        self.assertEqual(json.loads(self.checkpoint.read_text())['result'], result)
        before = self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns
        versioning_checks = len(self.client.calls["versioning"])
        authenticated_heads = len(self.client.calls['head'])
        self.client.versioning = {"Status": "Enabled"}
        self.assertEqual(self.upload(), result)
        self.assertEqual(len(self.client.calls['head']), authenticated_heads + 1)
        self.assertEqual(len(self.client.calls["versioning"]), versioning_checks)
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"]),
                          len(self.client.calls["complete"])), (1, 3, 1))
        self.assertEqual((self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns), before)
        self.assertLess(len(before[0]), 4096)
        self.assertNotIn("parts", json.loads(before[0]))
        if os.name == "posix":
            self.assertEqual(self.checkpoint.stat().st_mode & 0o777, 0o600)

    def test_completed_checkpoint_receipt_missing_binding_fails_before_remote_reuse(self):
        self.upload()
        value = json.loads(self.checkpoint.read_text())
        del value['result']['binding']
        self.checkpoint.write_text(json.dumps(value), encoding='utf-8')
        heads = len(self.client.calls['head'])
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, 'drama_upload_checkpoint_unverified')
        self.assertEqual(len(self.client.calls['head']), heads)

    def test_part_response_loss_reuses_server_receipt_and_upload_id(self):
        self.client.lose_part_responses.add(2)
        with self.assertRaises(DramaSynthesisError):
            self.upload()
        first = json.loads(self.checkpoint.read_text())
        self.assertEqual(first["phase"], "uploading")
        self.upload()
        self.assertEqual([item["PartNumber"] for item in self.client.calls["part"]], [1, 2, 3])
        self.assertEqual({item["UploadId"] for item in self.client.calls["part"]}, {first["upload_id"]})
        self.assertEqual(len(self.client.calls["create"]), 1)

    def test_paginated_server_parts_are_reused_before_missing_part(self):
        self.interrupted(part=3)
        start = len(self.client.calls["list"])
        self.client.page_size = 1
        self.upload()
        self.assertEqual([item["PartNumberMarker"] for item in self.client.calls["list"][start:]], [0, 1])
        self.assertEqual([item["PartNumber"] for item in self.client.calls["part"]], [1, 2, 3, 3])
        self.assertEqual(len(self.client.calls["create"]), 1)

    def test_lost_create_response_never_creates_another_upload(self):
        self.client.lose_create_response = True
        for _attempt in range(2):
            with self.assertRaises(DramaSynthesisError) as caught:
                self.upload()
            self.assertEqual(caught.exception.code, "drama_upload_recovery_required")
        self.assertEqual(json.loads(self.checkpoint.read_text())["phase"], "creating")
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"])), (1, 0))

    def test_create_id_persist_failure_keeps_unknown_creation_fenced(self):
        real_save = cos_upload.atomic_write_record

        def fail_id_write(path, record):
            if record["phase"] == "uploading":
                raise OSError("private disk failure")
            real_save(path, record)

        with mock.patch.object(cos_upload, "atomic_write_record", side_effect=fail_id_write):
            with self.assertRaises(DramaSynthesisError):
                self.upload()
        self.assertEqual(json.loads(self.checkpoint.read_text())["phase"], "creating")
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_recovery_required")
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"])), (1, 0))

    def test_complete_response_loss_is_settled_by_authenticated_head(self):
        self.client.lose_complete_response = True
        result = self.upload()
        self.assertEqual(result["size_bytes"], len(self.body))
        self.assertEqual(json.loads(self.checkpoint.read_text())["phase"], "completed")
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["complete"])), (1, 1))

    def test_complete_loss_and_head_outage_recover_without_resending_parts(self):
        self.client.lose_complete_response = True
        self.client.fail_heads_after_complete = 1
        with self.assertRaises(DramaSynthesisError):
            self.upload()
        self.assertEqual(json.loads(self.checkpoint.read_text())["phase"], "completing")
        result = self.upload()
        self.assertEqual(result["sha256"], hashlib.sha256(self.body).hexdigest())
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"]),
                          len(self.client.calls["complete"])), (1, 3, 1))

    def test_failed_complete_retries_same_upload_without_new_parts(self):
        self.client.fail_complete_before_commit = True
        with self.assertRaises(DramaSynthesisError):
            self.upload()
        self.upload()
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"]),
                          len(self.client.calls["complete"])), (1, 3, 2))
        self.assertEqual({item["UploadId"] for item in self.client.calls["complete"]}, {"upload-1"})

    def test_completed_checkpoint_write_failure_is_recovered_from_head(self):
        real_save = cos_upload.atomic_write_record

        def fail_completed_write(path, record):
            if record["phase"] == "completed":
                raise OSError("private disk failure")
            real_save(path, record)

        with mock.patch.object(cos_upload, "atomic_write_record", side_effect=fail_completed_write):
            with self.assertRaises(DramaSynthesisError):
                self.upload()
        self.assertEqual(json.loads(self.checkpoint.read_text())["phase"], "completing")
        self.upload()
        self.assertEqual((len(self.client.calls["part"]), len(self.client.calls["complete"])), (3, 1))

    def test_unknown_existing_object_and_head_failure_never_start_upload(self):
        self.client.object_headers = {"Content-Length": str(len(self.body)),
                                      cos_upload.SHA_HEADER: hashlib.sha256(self.body).hexdigest()}
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_object_conflict")
        self.client.object_headers = None
        self.client.fail_all_heads = True
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_failed")
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(self.client.calls["create"]), 0)
        self.assertFalse(self.checkpoint.exists())

    def test_corrupt_checkpoint_and_write_failure_do_not_create_session(self):
        self.checkpoint.parent.mkdir(parents=True)
        self.checkpoint.write_text("private invalid JSON", encoding="utf-8")
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_checkpoint_unverified")
        self.assertEqual(self.checkpoint.read_text(), "private invalid JSON")
        self.checkpoint.unlink()
        with mock.patch.object(cos_upload, "atomic_write_record", side_effect=OSError("private")):
            with self.assertRaises(DramaSynthesisError):
                self.upload()
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"])), (0, 0))

    def test_checkpoint_directory_is_durable_before_create_and_failure_stops_all_writes(self):
        real_directory = cos_upload.durable_ensure_directory
        real_create = self.client.create_multipart_upload
        events = []

        def directory(path):
            events.append('directory')
            return real_directory(path)

        def create(**kwargs):
            events.append('create')
            return real_create(**kwargs)

        with mock.patch.object(cos_upload, 'durable_ensure_directory', side_effect=directory), \
                mock.patch.object(self.client, 'create_multipart_upload', side_effect=create):
            self.upload()
        self.assertLess(events.index('directory'), events.index('create'))

        other = self.root / '.runtime' / 'uploads' / 'other.json'
        with mock.patch.object(cos_upload, 'durable_ensure_directory', side_effect=OSError('directory fsync fault')):
            with self.assertRaises(DramaSynthesisError) as caught:
                cos_upload.resume_upload(
                    self.client, bucket='private-test-12345', key='drama/test/other.mp4',
                    path=self.path, checkpoint_path=other, content_type='video/mp4',
                )
        self.assertEqual(caught.exception.code, 'drama_upload_checkpoint_unverified')
        self.assertEqual(len(self.client.calls['create']), 1)
        self.assertFalse(other.exists())

    def test_target_or_local_file_change_cannot_reuse_or_replace_session(self):
        self.interrupted()
        before = self.checkpoint.read_bytes()
        count = len(self.client.calls["part"])
        for change in ({"key": "other.mp4"}, {"bucket": "other-12345"},
                       {"content_type": "application/octet-stream"}, {"acl": "private"}):
            with self.subTest(change=change), self.assertRaises(DramaSynthesisError) as caught:
                self.upload(**change)
            self.assertEqual(caught.exception.code, "drama_upload_checkpoint_conflict")
        self.path.write_bytes(self.body + b"changed")
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_checkpoint_conflict")
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"])), (1, count))

    def test_invalid_server_receipts_or_pagination_do_not_overwrite_parts(self):
        self.interrupted()
        count = len(self.client.calls["part"])
        transforms = [
            lambda data: {**data, "Part": [{**data["Part"][0], "Size": "1"}]},
            lambda data: {**data, "Part": [{**data["Part"][0], "ETag": '"' + "0" * 32 + '"'}]},
            lambda data: {**data, "Part": [{**data["Part"][0], "PartNumber": "99999"}]},
            lambda data: {**data, "Part": data["Part"] * 2},
            lambda data: {**data, "IsTruncated": "true", "NextPartNumberMarker": "0"},
            lambda data: {**data, "IsTruncated": 1},
        ]
        for index, transform in enumerate(transforms):
            self.client.transform_list = transform
            with self.subTest(index=index), self.assertRaises(DramaSynthesisError):
                self.upload()
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"])), (1, count))
        self.assertEqual(len(self.client.calls["complete"]), 0)

    def test_missing_upload_id_or_deleted_completed_object_is_not_recreated(self):
        self.interrupted()
        self.client.uploads.clear()
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_recovery_required")
        self.assertEqual(len(self.client.calls["create"]), 1)

    def test_completed_object_changes_or_disappears_fail_closed(self):
        self.upload()
        headers = deepcopy(self.client.object_headers)
        for changed in ({**headers, "Content-Length": "1"},
                        {**headers, cos_upload.SHA_HEADER.upper(): "0" * 64},
                        {**headers, cos_upload.BINDING_HEADER.upper(): "0" * 32}, None):
            self.client.object_headers = changed
            with self.subTest(changed=changed is None), self.assertRaises(DramaSynthesisError):
                self.upload()
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"])), (1, 3))

    def test_source_mutation_during_upload_stops_before_completion(self):
        def progress(done, _total):
            if done:
                self.path.write_bytes(self.body + b"changed")

        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload(progress_callback=progress)
        self.assertEqual(caught.exception.code, "drama_upload_source_changed")
        self.assertEqual((len(self.client.calls["part"]), len(self.client.calls["complete"])), (1, 0))

    def test_same_checkpoint_lock_rejects_a_competing_uploader(self):
        self.checkpoint.parent.mkdir(parents=True)
        lock = cos_upload._FileLock(self.checkpoint.with_name(self.checkpoint.name + ".lock"))
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_busy")
        self.assertEqual((len(self.client.calls["head"]), len(self.client.calls["create"])), (0, 0))

    def test_invalid_record_schema_fails_before_additional_cos_requests(self):
        record = self.interrupted()
        before = {key: len(value) for key, value in self.client.calls.items()}
        for changes in ({"version": True}, {"phase": []}, {"part_size": True},
                        {"part_size": cos_upload.MAX_BUFFER_SIZE + 1}, {"upload_id": ""},
                        {"result": {"unverified": True}}):
            self.checkpoint.write_text(json.dumps({**record, **changes}), encoding="utf-8")
            with self.subTest(changes=changes), self.assertRaises(DramaSynthesisError) as caught:
                self.upload()
            self.assertEqual(caught.exception.code, "drama_upload_checkpoint_unverified")
        self.assertEqual({key: len(value) for key, value in self.client.calls.items()}, before)

    def test_part_memory_limit_is_checked_before_creation(self):
        with mock.patch.object(cos_upload, "DEFAULT_PART_SIZE", cos_upload.MAX_BUFFER_SIZE + 1):
            with self.assertRaises(DramaSynthesisError) as caught:
                self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_configuration_invalid")
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(len(self.client.calls["create"]), 0)

    def test_checkpoint_cannot_replace_the_source_file(self):
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload(checkpoint_path=self.path)
        self.assertEqual(caught.exception.code, "drama_upload_checkpoint_conflict")
        self.assertEqual(self.path.read_bytes(), self.body)
        self.assertEqual(len(self.client.calls["create"]), 0)

    def test_progress_callback_failure_preserves_the_confirmed_part(self):
        def progress(done, _total):
            if done:
                raise RuntimeError("private callback https://source.test/token")

        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload(progress_callback=progress)
        self.assertEqual(caught.exception.code, "drama_upload_failed")
        self.assertNotIn("private", str(caught.exception))
        self.upload()
        self.assertEqual([item["PartNumber"] for item in self.client.calls["part"]], [1, 2, 3])
        self.assertEqual(len(self.client.calls["create"]), 1)

    def test_racing_foreign_object_is_not_overwritten_by_completion(self):
        foreign = {"Content-Length": "7", "ETag": '"foreign"', "x-cos-meta-owner": "another-writer"}
        self.client.racing_object_headers = deepcopy(foreign)
        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload()
        self.assertEqual(caught.exception.code, "drama_upload_object_conflict")
        self.assertEqual(self.client.object_headers, foreign)
        self.assertEqual(self.client.calls["complete"][0]["Metadata"],
                         {cos_upload.FORBID_OVERWRITE_HEADER: "true"})
        self.assertEqual(json.loads(self.checkpoint.read_text())["phase"], "completing")
        self.assertIn("upload-1", self.client.uploads)

    def test_versioning_must_be_unconfigured_before_create_and_complete(self):
        for status in ("Enabled", "Suspended", "unexpected"):
            self.client.versioning = {"Status": status}
            with self.subTest(status=status), self.assertRaises(DramaSynthesisError) as caught:
                self.upload()
            self.assertEqual(caught.exception.code, "drama_upload_bucket_state_unverified")
        self.client.versioning = {}
        self.client.fail_versioning = True
        with self.assertRaises(DramaSynthesisError):
            self.upload()
        self.assertEqual(len(self.client.calls["create"]), 0)
        self.client.fail_versioning = False

        def enable_during_upload(done, _total):
            if done:
                self.client.versioning = {"Status": "Enabled"}

        with self.assertRaises(DramaSynthesisError) as caught:
            self.upload(progress_callback=enable_during_upload)
        self.assertEqual(caught.exception.code, "drama_upload_bucket_state_unverified")
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["complete"])), (1, 0))

    def test_real_sdk_lost_create_transport_does_not_retry_post(self):
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError:
            self.skipTest("COS SDK absent locally; this test must run in the Linux SDK environment")
        import requests

        class NoNetworkTransport:
            def __init__(self):
                self.posts = []
                self.heads = 0

            def head(self, _url, **_kwargs):
                self.heads += 1
                response = requests.Response()
                response.status_code = 404
                response._content = b"<Error><Code>NoSuchKey</Code><Message>fixture</Message></Error>"
                return response

            def get(self, _url, **kwargs):
                if "versioning" not in kwargs.get("params", {}):
                    raise AssertionError("unexpected offline SDK GET")
                response = requests.Response()
                response.status_code = 200
                response._content = b"<VersioningConfiguration/>"
                return response

            def post(self, _url, **kwargs):
                self.posts.append(dict(kwargs.get("params", {})))
                # Model a server that allocated its upload ID but whose
                # response never reached the client. There is no real socket.
                raise requests.ConnectionError("private lost create response")

        transport = NoNetworkTransport()
        client = CosS3Client(CosConfig(Region="ap-hongkong", SecretId="unit-test-id",
                                      SecretKey="unit-test-key"), retry=0, session=transport)
        for _attempt in range(2):
            with self.assertRaises(DramaSynthesisError) as caught:
                cos_upload.resume_upload(client, bucket="test-12345", key="drama/fixture.mp4",
                                         path=self.path, checkpoint_path=self.checkpoint,
                                         content_type="video/mp4")
            self.assertEqual(caught.exception.code, "drama_upload_recovery_required")
        self.assertEqual((len(transport.posts), transport.heads), (1, 1))
        self.assertEqual(json.loads(self.checkpoint.read_text())["phase"], "creating")

    def test_real_sdk_complete_transmits_conditional_write_header(self):
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError:
            self.skipTest("COS SDK absent locally; this test must run in the Linux SDK environment")
        import requests

        class NoNetworkTransport:
            def __init__(self):
                self.headers = None

            def post(self, _url, **kwargs):
                self.headers = {name.lower(): value for name, value in kwargs["headers"].items()}
                response = requests.Response()
                response.status_code = 200
                response._content = b'<CompleteMultipartUploadResult><ETag>"verified"</ETag></CompleteMultipartUploadResult>'
                return response

        transport = NoNetworkTransport()
        client = CosS3Client(CosConfig(Region="ap-hongkong", SecretId="unit-test-id",
                                      SecretKey="unit-test-key"), retry=0, session=transport)
        client.complete_multipart_upload(
            Bucket="test-12345", Key="drama/fixture.mp4", UploadId="unit-upload",
            MultipartUpload={"Part": [{"PartNumber": 1, "ETag": '"' + "a" * 32 + '"'}]},
            Metadata={cos_upload.FORBID_OVERWRITE_HEADER: "true"})
        # SDK 1.9.44 normalizes HTTP header values to bytes before passing
        # them to requests; both forms encode the same exact wire value.
        self.assertIn(transport.headers[cos_upload.FORBID_OVERWRITE_HEADER], ("true", b"true"))


class AcceptanceMultipartCos(FakeMultipartCos):
    """Cloud-free protocol fixture; its synthetic bytes are not media acceptance."""

    def __init__(self):
        super().__init__()
        self.object_body = None
        self.existing_key = False
        self.existing_upload = False
        self.corrupt_download = False
        self.acceptance_calls = []
        self.notification_documents = [b"<NotificationConfiguration/>", b"<NotificationConfiguration/>"]
        self.notification_reads = 0
        self.anonymous_statuses = [403, 403]
        self.anonymous_reads = 0
        self.object_acl = {
            "Owner": {"ID": "fixture-owner"},
            "AccessControlList": {"Grant": [{
                "Grantee": {"Type": "CanonicalUser", "ID": "fixture-owner"},
                "Permission": "FULL_CONTROL",
            }]},
            "CannedACL": "private",
        }

    def list_objects(self, **kwargs):
        self.acceptance_calls.append(("list_objects", kwargs))
        return {"Prefix": kwargs["Prefix"], "IsTruncated": "false",
                "Contents": [{"Key": kwargs["Prefix"] + "existing.mp4"}] if self.existing_key else []}

    def list_multipart_uploads(self, **kwargs):
        self.acceptance_calls.append(("list_multipart_uploads", kwargs))
        return {"Prefix": kwargs["Prefix"], "IsTruncated": "false",
                "Upload": [{"Key": kwargs["Prefix"] + "pending.mp4"}] if self.existing_upload else []}

    def complete_multipart_upload(self, **kwargs):
        value = self.uploads[kwargs["UploadId"]]
        self.object_body = b"".join(value["parts"][item["PartNumber"]]["Body"]
                                    for item in kwargs["MultipartUpload"]["Part"])
        return super().complete_multipart_upload(**kwargs)

    def get_object(self, **kwargs):
        self.acceptance_calls.append(("get_object", kwargs))
        body = self.object_body
        if self.corrupt_download:
            body = bytes([body[0] ^ 1]) + body[1:]
        return {**deepcopy(self.object_headers), "Body": io.BytesIO(body)}

    def read_notification_configuration(self, _client, *, bucket, deadline):
        deadline.check()
        self.acceptance_calls.append(("get_bucket_notification_v2", {"Bucket": bucket}))
        if self.notification_reads >= len(self.notification_documents):
            raise AssertionError("unexpected notification configuration read")
        body = self.notification_documents[self.notification_reads]
        self.notification_reads += 1
        return cos_verifier.verify_empty_notification_configuration(body)

    def read_anonymous_head(self):
        self.acceptance_calls.append(("anonymous_head", {}))
        if self.anonymous_reads >= len(self.anonymous_statuses):
            raise AssertionError("unexpected anonymous HEAD")
        status = self.anonymous_statuses[self.anonymous_reads]
        self.anonymous_reads += 1
        return status

    def get_object_acl(self, **kwargs):
        self.acceptance_calls.append(("get_object_acl", kwargs))
        return deepcopy(self.object_acl)


class CosAcceptanceDriverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "synthetic-protocol-fixture.mp4"
        self.evidence = self.root / "private-evidence"
        self.prefix = cos_verifier.acceptance_prefix("a" * 40, "cos-unit-fixture-20260828")
        self.client = AcceptanceMultipartCos()
        try:
            install_offline_sdk_runtime()
        except ImportError:
            cos_verifier._VERIFIED_SDK_RUNTIME = None

    def scenario(self):
        # Never patch multipart size in the verifier tests. These fake bytes
        # exercise the protocol only; the real CLI separately requires ffprobe.
        if not self.source.exists():
            self.source.write_bytes(b"a" * cos_verifier.MIN_BYTES + b"unit-test-tail")
        if not self.evidence.exists():
            cos_verifier.fresh_evidence_directory(self.evidence)
        snapshots = []
        self.snapshots = snapshots
        result = cos_verifier.verify_upload(
            self.client, bucket="test-12345", prefix=self.prefix, source=self.source,
            evidence_dir=self.evidence, artifact=cos_verifier.file_fingerprint(self.source),
            persist=lambda item: snapshots.append(deepcopy(item)),
            notification_reader=self.client.read_notification_configuration,
            anonymous_head=self.client.read_anonymous_head,
        )
        return result, snapshots

    @staticmethod
    def git_result(stdout=b"", returncode=0, stderr=b""):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    @staticmethod
    def candidate_tracked():
        return [
            "scripts/verify_drama_cos_upload.py", "features/__init__.py",
            "features/drama_synthesis/__init__.py", "features/drama_synthesis/core.py",
            "features/drama_synthesis/local_checkpoint.py", "features/drama_synthesis/gpu_cache.py",
            "features/drama_synthesis/async_runtime.py", "features/drama_synthesis/cos_upload.py",
        ]

    def clean_git_results(self, *, replacement=b"",
                          config=b"command\tcommand line:\t\n",
                          version=b"git version 2.27.0\n", object_format=b"sha1\n",
                          candidate_tree=None, index_entries=None,
                          index_flags=None, fsmonitor_flags=None):
        commit = b"a" * 40 + b"\n"
        tree = b"b" * 40 + b"\n"
        tracked = self.candidate_tracked()
        blobs = {item: ("{:040x}".format(index + 1)).encode()
                 for index, item in enumerate(tracked)}
        tree_entries = b"".join(
            b"100644 blob " + blobs[item] + b"\t" + item.encode() + b"\x00"
            for item in tracked)
        index_entries = index_entries if index_entries is not None else b"".join(
            b"100644 " + blobs[item] + b" 0\t" + item.encode() + b"\x00"
            for item in tracked)
        index_flags = index_flags if index_flags is not None else b"".join(
            b"H " + item.encode() + b"\x00" for item in tracked)
        fsmonitor_flags = fsmonitor_flags if fsmonitor_flags is not None else b"".join(
            b"H " + item.encode() + b"\x00" for item in tracked)
        return [
            self.git_result(version), self.git_result(config),
            self.git_result(replacement), self.git_result(object_format),
            self.git_result((str(ROOT.resolve()) + "\n").encode()),
            self.git_result(commit), self.git_result(commit), self.git_result(tree),
            self.git_result(candidate_tree if candidate_tree is not None else tree),
            self.git_result(tree_entries), self.git_result(index_entries),
            self.git_result(index_flags), self.git_result(fsmonitor_flags),
        ]

    def test_default_preview_never_reads_credentials_media_or_network(self):
        output = io.StringIO()
        with mock.patch.object(cos_verifier, "apply") as apply, \
                mock.patch.object(cos_verifier, "load_credentials") as credentials, \
                mock.patch.object(cos_verifier, "probe_source") as probe, \
                mock.patch.object(cos_verifier, "build_real_client") as client, \
                mock.patch("sys.stdout", output):
            self.assertEqual(cos_verifier.main([]), 0)
        preview = json.loads(output.getvalue())
        self.assertEqual(preview["status"], "plan_only")
        self.assertEqual(preview["deadline_seconds"], 3600)
        self.assertEqual(preview["required_invocation"],
                         "/data/drama-synthesis-gpu/runtime/bin/python -I -B -S")
        self.assertNotIn("deadline", vars(cos_verifier.parser().parse_args([])))
        for function in (apply, credentials, probe, client):
            function.assert_not_called()
        self.assertFalse(self.evidence.exists())

    def test_real_apply_bootstrap_rejects_pythonpath_shadow_without_importing_it(self):
        shadow = self.root / "shadow"
        marker = self.root / "shadow-imported.txt"
        package = shadow / "features"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "from pathlib import Path\nPath({!r}).write_text('imported')\n".format(str(marker)),
            encoding="utf-8",
        )
        (shadow / "shadow_only_fixture.py").write_text("VALUE = 'shadow'\n", encoding="utf-8")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(shadow)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify_drama_cos_upload.py"), "--apply"],
            capture_output=True, text=True, env=environment, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["code"], "runtime_unverified")
        self.assertFalse(marker.exists())

        isolated = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c",
             "import importlib.util,sys;print(sys.flags.isolated,sys.flags.ignore_environment,"
             "sys.flags.no_user_site,sys.flags.no_site,sys.flags.dont_write_bytecode,"
             "importlib.util.find_spec('shadow_only_fixture'))"],
            capture_output=True, text=True, env={**environment, "PYTHONPATH": str(shadow)}, timeout=10,
        )
        self.assertEqual(isolated.returncode, 0)
        self.assertEqual(isolated.stdout.strip(), "1 1 1 1 1 None")

    def test_real_apply_bootstrap_rejects_explicit_pycache_prefix(self):
        pycache_prefix = self.root / "attacker-controlled-pycache"
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-X",
             "pycache_prefix={}".format(pycache_prefix),
             str(ROOT / "scripts" / "verify_drama_cos_upload.py"), "--apply"],
            capture_output=True, text=True, env={}, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["code"], "runtime_unverified")
        self.assertFalse(pycache_prefix.exists())

    def test_candidate_gate_rejects_untracked_without_status_or_filter_commands(self):
        results = self.clean_git_results()
        with mock.patch.dict(cos_verifier.os.environ, {}, clear=True), \
                mock.patch.object(cos_verifier, "verify_git_binary", return_value=Path("/usr/bin/git")), \
                mock.patch.object(cos_verifier, "_run_git", side_effect=results) as run, \
                mock.patch.object(
                    cos_verifier, "_verify_candidate_worktree",
                    side_effect=cos_verifier.VerificationError("candidate_unverified")), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.verify_candidate("a" * 40)
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertEqual(run.call_count, 13)
        commands = [call.args[1] for call in run.call_args_list]
        self.assertFalse(any(command and command[0] == "status" for command in commands))
        self.assertFalse(any("--others" in command or "--error-unmatch" in command
                             for command in commands))

    def test_candidate_filesystem_limit_is_enforced_while_scandir_streams(self):
        root = (self.root / "streamed-candidate").resolve()
        root.mkdir()
        entries = [
            SimpleNamespace(path=str(root / ".git"), name=".git"),
            SimpleNamespace(path=str(root / "tracked.py"), name="tracked.py"),
            SimpleNamespace(path=str(root / "overflow.py"), name="overflow.py"),
        ]

        class StreamingEntries:
            def __enter__(self):
                return iter(entries)

            def __exit__(self, *_args):
                return False

        directory_stat = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)
        file_stat = SimpleNamespace(st_mode=stat.S_IFREG | 0o644)

        def lstat(path):
            return directory_stat if Path(path).name == ".git" else file_stat

        deadline = SimpleNamespace(check=mock.Mock())
        with mock.patch.object(cos_verifier, "ROOT", root), \
                mock.patch.object(cos_verifier, "MAX_CANDIDATE_FILESYSTEM_ENTRIES", 2), \
                mock.patch.object(cos_verifier.os, "scandir", return_value=StreamingEntries()), \
                mock.patch.object(cos_verifier.os, "lstat", side_effect=lstat) as inspected, \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier._verify_candidate_worktree(
                {"tracked.py": ("100644", "a" * 40)}, ["tracked.py"], deadline)
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertEqual(inspected.call_count, 2)
        self.assertEqual(deadline.check.call_count, 2)

    def test_candidate_gate_rejects_ignored_replace_fsmonitor_and_git_environment(self):
        cases = (
            (self.clean_git_results(replacement=b"refs/replace/" + b"a" * 40 + b"\n"), 3),
            (self.clean_git_results(config=(
                b"local\t.git/config\t/tmp/never-execute\n"
                b"command\tcommand line:\t\n")), 2),
            (self.clean_git_results(index_entries=(
                b"100644 " + b"f" * 40 + b" 0\t" +
                self.candidate_tracked()[0].encode() + b"\x00")), 11),
            (self.clean_git_results(index_flags=(
                b"S " + self.candidate_tracked()[0].encode() + b"\x00")), 12),
            (self.clean_git_results(fsmonitor_flags=(
                b"h " + self.candidate_tracked()[0].encode() + b"\x00")), 13),
        )
        for results, calls in cases:
            with self.subTest(calls=calls), mock.patch.dict(cos_verifier.os.environ, {}, clear=True), \
                    mock.patch.object(cos_verifier, "verify_git_binary", return_value=Path("/usr/bin/git")), \
                    mock.patch.object(cos_verifier, "_run_git", side_effect=results) as run, \
                    self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier.verify_candidate("a" * 40)
            self.assertEqual(caught.exception.code, "candidate_unverified")
            self.assertEqual(run.call_count, calls)

        with mock.patch.dict(cos_verifier.os.environ, {"GIT_CONFIG_COUNT": "1"}, clear=True), \
                mock.patch.object(cos_verifier, "verify_git_binary") as verify_git, \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.verify_candidate("a" * 40)
        self.assertEqual(caught.exception.code, "candidate_unverified")
        verify_git.assert_not_called()

        old_git = self.clean_git_results(version=b"git version 2.26.3\n")
        with mock.patch.dict(cos_verifier.os.environ, {}, clear=True), \
                mock.patch.object(cos_verifier, "verify_git_binary", return_value=Path("/usr/bin/git")), \
                mock.patch.object(cos_verifier, "_run_git", side_effect=old_git) as run, \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.verify_candidate("a" * 40)
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertEqual(run.call_count, 1)

        bad_format = self.clean_git_results(object_format=b"sha256\n")
        with mock.patch.dict(cos_verifier.os.environ, {}, clear=True), \
                mock.patch.object(cos_verifier, "verify_git_binary", return_value=Path("/usr/bin/git")), \
                mock.patch.object(cos_verifier, "_run_git", side_effect=bad_format) as run, \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.verify_candidate("a" * 40)
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertEqual(run.call_count, 4)

        ambiguous_tree = self.clean_git_results(
            candidate_tree=b"b" * 40 + b"\n" + b"c" * 40 + b"\n")
        with mock.patch.dict(cos_verifier.os.environ, {}, clear=True), \
                mock.patch.object(cos_verifier, "verify_git_binary", return_value=Path("/usr/bin/git")), \
                mock.patch.object(cos_verifier, "_run_git", side_effect=ambiguous_tree) as run, \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.verify_candidate("a" * 40)
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertEqual(run.call_count, 9)

    def test_candidate_gate_clean_success_checks_every_tracked_file(self):
        tracked = self.candidate_tracked()
        results = self.clean_git_results()
        for item in tracked:
            results.append(self.git_result((ROOT / item).read_bytes()))
        with mock.patch.dict(cos_verifier.os.environ, {}, clear=True), \
                mock.patch.object(cos_verifier, "verify_git_binary", return_value=Path("/usr/bin/git")), \
                mock.patch.object(cos_verifier, "_run_git", side_effect=results) as run, \
                mock.patch.object(cos_verifier, "_verify_candidate_worktree") as worktree:
            cos_verifier.verify_candidate("a" * 40)
        self.assertEqual(run.call_count, 13 + len(tracked))
        worktree.assert_called_once()
        self.assertEqual(
            [call.args[1][-1] for call in run.call_args_list[-len(tracked):]],
            ["{}:{}".format("a" * 40, item) for item in tracked])

    def test_git_runner_forces_clean_namespace_and_never_accepts_ambiguous_sha(self):
        response = self.git_result(b"sha1\n")
        git_path = Path(tempfile.gettempdir()).resolve() / "fixed-git"
        with mock.patch.object(cos_verifier, "run_bounded_process", return_value=response) as bounded:
            self.assertIs(cos_verifier._run_git(
                git_path, ["rev-parse", "--show-object-format"],
                cos_verifier.AcceptanceDeadline()), response)
        command = bounded.call_args.args[0]
        self.assertEqual(command[0], str(git_path))
        self.assertIn("--no-pager", command)
        self.assertIn("core.fsmonitor=", command)
        self.assertNotIn("core.fsmonitor=false", command)
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertEqual(bounded.call_args.kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(bounded.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertNotIn("HOME", bounded.call_args.kwargs["env"])
        self.assertNotIn("XDG_CONFIG_HOME", bounded.call_args.kwargs["env"])
        for candidate in ("a" * 39, "A" * 40, "HEAD", "a" * 41):
            with self.subTest(candidate=candidate), \
                    self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier.verify_candidate(candidate)
            self.assertEqual(caught.exception.code, "candidate_unverified")

    def test_real_local_git_227_or_newer_never_executes_fsmonitor_or_filters_and_checks_exact_tree(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("local Git unavailable")
        version = subprocess.run(
            [git, "--version"], capture_output=True, text=True, check=True, timeout=10).stdout
        found = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if found is None or tuple(map(int, found.groups())) < (2, 27, 0):
            self.skipTest("test requires Git >=2.27")

        repository = self.root / "git-gate-fixture"
        repository.mkdir()

        def git_setup(*arguments):
            subprocess.run(
                [git, "-C", str(repository), *arguments], check=True, capture_output=True,
                stdin=subprocess.DEVNULL, timeout=10,
                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

        git_setup("init")
        git_setup("config", "user.name", "Verifier Fixture")
        git_setup("config", "user.email", "verifier@example.invalid")
        (repository / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (repository / ".gitattributes").write_text("*.py filter=hostile\n", encoding="utf-8")
        originals = {}
        for item in self.candidate_tracked():
            path = repository / item
            path.parent.mkdir(parents=True, exist_ok=True)
            content = ("fixture for {}\n".format(item)).encode()
            path.write_bytes(content)
            originals[item] = content
        git_setup("add", ".")
        git_setup("commit", "-m", "fixture")
        candidate = subprocess.run(
            [git, "-C", str(repository), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, timeout=10).stdout.strip()

        marker = repository / "external-command-executed.txt"
        hook = repository / ".git" / "external-marker.sh"
        hook.write_text(
            "#!/bin/sh\nprintf executed > '{}'\nexit 1\n".format(marker.as_posix()),
            encoding="utf-8")
        hook.chmod(0o755)
        git_setup("config", "core.fsmonitor", hook.as_posix())

        def verify():
            with mock.patch.dict(cos_verifier.os.environ, {}, clear=True), \
                    mock.patch.object(cos_verifier, "ROOT", repository.resolve()), \
                    mock.patch.object(
                        cos_verifier, "verify_git_binary", return_value=Path(git).resolve()):
                return cos_verifier.verify_candidate(candidate)

        with self.assertRaises(cos_verifier.VerificationError) as caught:
            verify()
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertFalse(marker.exists())

        git_setup("config", "--unset-all", "core.fsmonitor")
        git_setup("config", "filter.hostile.clean", hook.as_posix())
        git_setup("config", "filter.hostile.process", hook.as_posix())

        # A clean tree with hostile clean/process filters is accepted without
        # executing either filter; worktree blobs are hashed directly in Python.
        self.assertIsNone(verify())
        self.assertFalse(marker.exists())

        dirty_path = repository / self.candidate_tracked()[0]
        dirty_path.write_bytes(b"dirty tracked content\n")
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            verify()
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertFalse(marker.exists())
        dirty_path.write_bytes(originals[self.candidate_tracked()[0]])

        ignored = repository / "ignored" / "shadow.py"
        ignored.parent.mkdir()
        ignored.write_text("must be rejected\n", encoding="utf-8")
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            verify()
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertFalse(marker.exists())
        ignored.unlink()
        ignored.parent.rmdir()

        # A staged change is rejected by exact HEAD/index comparison. Configure
        # it before restoring the hostile filters so the fixture setup itself
        # cannot create a false-positive marker.
        git_setup("config", "--unset-all", "filter.hostile.clean")
        git_setup("config", "--unset-all", "filter.hostile.process")
        dirty_path.write_bytes(b"staged content\n")
        git_setup("add", self.candidate_tracked()[0])
        git_setup("config", "filter.hostile.clean", hook.as_posix())
        git_setup("config", "filter.hostile.process", hook.as_posix())
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            verify()
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertFalse(marker.exists())

        git_setup("update-ref", "refs/replace/" + candidate, candidate)
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            verify()
        self.assertEqual(caught.exception.code, "candidate_unverified")
        self.assertFalse(marker.exists())

    def test_candidate_origin_gate_rejects_real_shadow_file(self):
        shadow = self.root / "shadow" / "features" / "drama_synthesis" / "cos_upload.py"
        shadow.parent.mkdir(parents=True)
        shadow.write_text("raise AssertionError('must never import')\n", encoding="utf-8")
        modules = {name: sys.modules[name] for name in cos_verifier.CANDIDATE_MODULE_PATHS}
        modules["features.drama_synthesis.cos_upload"] = SimpleNamespace(__file__=str(shadow))
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.validate_candidate_module_origins(modules)
        self.assertEqual(caught.exception.code, "candidate_unverified")

    def test_verified_candidate_loader_executes_frozen_blob_not_disk_or_bytecode_shadow(self):
        module_path = self.root / "verified_loader_fixture.py"
        module_path.write_text("VALUE = 'disk-shadow'\n", encoding="utf-8")
        name = "verified_loader_fixture"
        try:
            with mock.patch.object(sys, "dont_write_bytecode", True):
                module = cos_verifier._load_candidate_source(
                    name, module_path, b"VALUE = 'frozen-git-blob'\n")
            self.assertEqual(module.VALUE, "frozen-git-blob")
            self.assertIsInstance(module.__loader__, cos_verifier.VerifiedCandidateSourceLoader)
        finally:
            sys.modules.pop(name, None)

    def test_sdk_runtime_gate_rejects_any_preloaded_qcloud_module(self):
        try:
            import qcloud_cos  # noqa: F401
        except ImportError:
            self.skipTest("COS SDK absent locally; exact runtime gate test requires SDK fixture")
        previous = cos_verifier._VERIFIED_SDK_RUNTIME
        cos_verifier._VERIFIED_SDK_RUNTIME = None
        try:
            with self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier.load_verified_sdk_runtime(cos_verifier.AcceptanceDeadline())
            self.assertEqual(caught.exception.code, "sdk_unverified")
        finally:
            cos_verifier._VERIFIED_SDK_RUNTIME = previous

    def test_sdk_dependency_gate_rejects_pth_duplicate_site_enable_and_system_site(self):
        prefix = self.root / "fixed-runtime"
        dependency = prefix / "lib" / "python{}.{}".format(
            sys.version_info[0], sys.version_info[1]) / "site-packages"
        dependency.mkdir(parents=True)
        config = prefix / "pyvenv.cfg"
        config.write_text("include-system-site-packages = false\n", encoding="utf-8")

        def secure_directory(path, _code):
            return Path(path).resolve(strict=True)

        def secure_file(path, _code, **_kwargs):
            return Path(path).resolve(strict=True)

        injected = dependency / "execute-before-verifier.pth"
        injected.write_text("import never_execute\n", encoding="utf-8")
        with mock.patch.object(cos_verifier, "RUNTIME_PREFIX", prefix), \
                mock.patch.object(cos_verifier, "_secure_directory", side_effect=secure_directory), \
                mock.patch.object(cos_verifier, "_secure_regular_file", side_effect=secure_file), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier._verified_dependency_roots(
                prefix.resolve(), cos_verifier.AcceptanceDeadline())
        self.assertEqual(caught.exception.code, "sdk_unverified")

        tree = self.root / "dependency-tree"
        package = tree / "transitive"
        package.mkdir(parents=True)
        (package / "module.py").write_text("raise AssertionError('must never import')\n", encoding="utf-8")
        cache = package / "__pycache__"
        cache.mkdir()
        (cache / "module.cpython-39.pyc").write_bytes(b"fake-pyc")
        import_spy = mock.Mock(side_effect=AssertionError("dependency import must not run"))
        with mock.patch.object(cos_verifier, "_secure_directory", side_effect=secure_directory), \
                mock.patch.object(cos_verifier, "_secure_regular_file", side_effect=secure_file), \
                mock.patch.object(cos_verifier.importlib, "import_module", import_spy), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier._verified_dependency_tree(
                tree.resolve(), cos_verifier.AcceptanceDeadline())
        self.assertEqual(caught.exception.code, "sdk_unverified")
        import_spy.assert_not_called()

        shutil.rmtree(cache)

        def reject_untrusted_transitive(path, _code, **_kwargs):
            path = Path(path).resolve(strict=True)
            if path.name == "module.py":
                raise cos_verifier.VerificationError("sdk_unverified")
            return path

        with mock.patch.object(cos_verifier, "_secure_directory", side_effect=secure_directory), \
                mock.patch.object(cos_verifier, "_secure_regular_file",
                                  side_effect=reject_untrusted_transitive), \
                mock.patch.object(cos_verifier.importlib, "import_module", import_spy), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier._verified_dependency_tree(
                tree.resolve(), cos_verifier.AcceptanceDeadline())
        self.assertEqual(caught.exception.code, "sdk_unverified")
        import_spy.assert_not_called()

        injected.unlink()
        config.write_text(
            "include-system-site-packages = false\ninclude-system-site-packages = true\n",
            encoding="utf-8")
        with mock.patch.object(cos_verifier, "RUNTIME_PREFIX", prefix), \
                mock.patch.object(cos_verifier, "_secure_directory", side_effect=secure_directory), \
                mock.patch.object(cos_verifier, "_secure_regular_file", side_effect=secure_file), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier._verified_dependency_roots(
                prefix.resolve(), cos_verifier.AcceptanceDeadline())
        self.assertEqual(caught.exception.code, "sdk_unverified")

        stdlib = self.root / "stdlib" / "python"
        system_site = stdlib / "site-packages"
        system_site.mkdir(parents=True)
        with mock.patch.object(cos_verifier.sysconfig, "get_paths", return_value={
                "stdlib": str(stdlib), "platstdlib": str(stdlib)}), \
                mock.patch.object(cos_verifier.sys, "path", [str(system_site)]), \
                mock.patch.object(cos_verifier, "_secure_directory", side_effect=secure_directory), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier._verified_runtime_import_paths(
                prefix.resolve(), frozenset({dependency.resolve()}),
                cos_verifier.AcceptanceDeadline())
        self.assertEqual(caught.exception.code, "sdk_unverified")

    def test_sdk_transport_module_origin_rejects_shadow_outside_dependency_prefix(self):
        dependency = self.root / "runtime" / "site-packages"
        dependency.mkdir(parents=True)
        shadow = self.root / "shadow" / "requests" / "adapters.py"
        shadow.parent.mkdir(parents=True)
        shadow.write_text("class HTTPAdapter: pass\n", encoding="utf-8")

        def secure_file(path, _code, **_kwargs):
            return Path(path).resolve(strict=True)

        with mock.patch.object(cos_verifier, "_secure_regular_file", side_effect=secure_file), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier._verified_module_file(
                SimpleNamespace(__file__=str(shadow)), frozenset({dependency.resolve()}),
                cos_verifier.AcceptanceDeadline())
        self.assertEqual(caught.exception.code, "sdk_unverified")

        previous_modules = frozenset(sys.modules)
        injected_name = "fake_transport_origin_fixture"
        sys.modules[injected_name] = SimpleNamespace(__file__=str(shadow))
        try:
            with mock.patch.object(cos_verifier, "_secure_regular_file", side_effect=secure_file), \
                    self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier._verify_new_import_origins(
                    previous_modules, frozenset({dependency.resolve()}), frozenset(),
                    cos_verifier.AcceptanceDeadline())
            self.assertEqual(caught.exception.code, "sdk_unverified")
        finally:
            sys.modules.pop(injected_name, None)

        spec = SimpleNamespace(origin=str(shadow), submodule_search_locations=[str(shadow.parent)])
        with mock.patch.object(cos_verifier.importlib_util, "find_spec", return_value=spec), \
                mock.patch.object(cos_verifier, "_secure_regular_file", side_effect=secure_file), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier._verified_module_spec(
                "requests", frozenset({dependency.resolve()}),
                cos_verifier.AcceptanceDeadline())
        self.assertEqual(caught.exception.code, "sdk_unverified")

    def test_ffprobe_has_no_override_and_uses_fixed_verified_binary_with_clean_env(self):
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr), self.assertRaises(SystemExit):
            cos_verifier.parser().parse_args(["--ffprobe", "/tmp/operator-selected-ffprobe"])
        self.source.write_bytes(b"a" * cos_verifier.MIN_BYTES + b"unit-test-tail")
        size = self.source.stat().st_size
        binary = Path("/verified/fixed/ffprobe")
        proof = {
            "path": str(cos_verifier.FFPROBE_PATH), "realpath": str(binary), "sha256": "f" * 64,
            "root_owned": True, "group_other_writable": False,
        }
        response = SimpleNamespace(returncode=0, stdout=json.dumps({
            "format": {"format_name": "mp4", "duration": "1.25", "size": str(size)},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 16, "height": 16}],
        }).encode(), stderr=b"")
        with mock.patch.object(cos_verifier, "verify_ffprobe_binary", return_value=(binary, proof)), \
                mock.patch.object(cos_verifier, "run_bounded_process", return_value=response) as run:
            _artifact, media = cos_verifier.probe_source(self.source)
        self.assertEqual(run.call_args.args[0][0], str(binary))
        self.assertEqual(run.call_args.kwargs["env"], cos_verifier.clean_subprocess_environment())
        self.assertEqual(run.call_args.kwargs["cwd"], "/")
        self.assertEqual(run.call_args.kwargs["output_limit"], cos_verifier.FFPROBE_OUTPUT_MAX_BYTES)
        self.assertEqual(media["binary"]["sha256"], "f" * 64)

    def test_bounded_subprocess_caps_both_ffprobe_streams_and_timeout_without_real_process(self):
        class FakeProcess:
            def __init__(self, stdout, stderr, *, times_out=False, wait_error=None,
                         wait_for_kill=False):
                self.stdout = io.BytesIO(stdout) if isinstance(stdout, bytes) else stdout
                self.stderr = io.BytesIO(stderr) if isinstance(stderr, bytes) else stderr
                self.returncode = 0
                self.pid = None
                self.times_out = times_out
                self.wait_error = wait_error
                self.wait_for_kill = wait_for_kill
                self.killed = False
                self.completed = False
                self.wait_calls = 0
                self.kill_event = threading.Event()

            def wait(self, timeout):
                self.wait_calls += 1
                if self.wait_error is not None and self.wait_calls == 1 and not self.killed:
                    raise self.wait_error
                if self.times_out and not self.killed:
                    raise subprocess.TimeoutExpired(["fake-ffprobe"], timeout)
                if self.wait_for_kill and not self.killed:
                    self.kill_event.wait(timeout=min(1, timeout))
                self.completed = True
                return self.returncode

            def poll(self):
                return self.returncode if self.completed or self.killed else None

            def kill(self):
                self.killed = True
                self.returncode = -9
                self.kill_event.set()

        class FailingStream:
            def read(self, _size):
                raise OSError("fixture read failure")

            def close(self):
                pass

        limit = 64
        for stdout, stderr in ((b"x" * (limit + 1), b""), (b"", b"y" * (limit + 1))):
            process = FakeProcess(stdout, stderr)
            with self.subTest(stream="stdout" if stdout else "stderr"), \
                    mock.patch.object(cos_verifier.subprocess, "Popen", return_value=process) as popen, \
                    self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier.run_bounded_process(
                    ["/verified/fake-ffprobe"], deadline=cos_verifier.AcceptanceDeadline(),
                    timeout=60, output_limit=limit, cwd="/",
                    env=cos_verifier.clean_subprocess_environment(), code="ffprobe_failed")
            self.assertEqual(caught.exception.code, "ffprobe_failed")
            self.assertTrue(process.killed)
            self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.PIPE)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

        process = FakeProcess(b"z" * limit, b"")
        with mock.patch.object(cos_verifier.subprocess, "Popen", return_value=process):
            result = cos_verifier.run_bounded_process(
                ["/verified/fake-ffprobe"], deadline=cos_verifier.AcceptanceDeadline(),
                timeout=60, output_limit=limit, cwd="/",
                env=cos_verifier.clean_subprocess_environment(), code="ffprobe_failed")
        self.assertEqual(result.stdout, b"z" * limit)
        self.assertFalse(process.killed)
        self.assertEqual(process.wait_calls, 1)

        process = FakeProcess(b"", b"", times_out=True)
        with mock.patch.object(cos_verifier.subprocess, "Popen", return_value=process), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.run_bounded_process(
                ["/verified/fake-ffprobe"], deadline=cos_verifier.AcceptanceDeadline(),
                timeout=60, output_limit=limit, cwd="/",
                env=cos_verifier.clean_subprocess_environment(), code="ffprobe_failed")
        self.assertEqual(caught.exception.code, "ffprobe_failed")
        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 2)

        process = FakeProcess(b"", b"", wait_error=KeyboardInterrupt())
        with mock.patch.object(cos_verifier.subprocess, "Popen", return_value=process), \
                self.assertRaises(KeyboardInterrupt):
            cos_verifier.run_bounded_process(
                ["/verified/fake-ffprobe"], deadline=cos_verifier.AcceptanceDeadline(),
                timeout=60, output_limit=limit, cwd="/",
                env=cos_verifier.clean_subprocess_environment(), code="ffprobe_failed")
        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 2)

        process = FakeProcess(b"", b"")
        with mock.patch.object(cos_verifier.subprocess, "Popen", return_value=process), \
                mock.patch.object(cos_verifier.threading.Thread, "start",
                                  side_effect=RuntimeError("fixture start failure")), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.run_bounded_process(
                ["/verified/fake-ffprobe"], deadline=cos_verifier.AcceptanceDeadline(),
                timeout=60, output_limit=limit, cwd="/",
                env=cos_verifier.clean_subprocess_environment(), code="ffprobe_failed")
        self.assertEqual(caught.exception.code, "ffprobe_failed")
        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 1)

        process = FakeProcess(FailingStream(), b"", wait_for_kill=True)
        with mock.patch.object(cos_verifier.subprocess, "Popen", return_value=process), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.run_bounded_process(
                ["/verified/fake-ffprobe"], deadline=cos_verifier.AcceptanceDeadline(),
                timeout=60, output_limit=limit, cwd="/",
                env=cos_verifier.clean_subprocess_environment(), code="ffprobe_failed")
        self.assertEqual(caught.exception.code, "ffprobe_failed")
        self.assertTrue(process.killed)
        self.assertGreaterEqual(process.wait_calls, 1)

    def test_ffprobe_failure_precedes_credential_read_in_real_apply_order(self):
        args = SimpleNamespace(
            candidate_sha="a" * 40, run_id="cos-unit-fixture-20260828",
            source=str(self.root / "fixture.mp4"), env_file=str(self.root / "private.env"),
            evidence_dir=str(self.root / "evidence"),
        )
        credentials = mock.Mock(side_effect=AssertionError("credentials must not be read"))
        with mock.patch.object(cos_verifier, "verify_candidate"), \
                mock.patch.object(cos_verifier, "load_verified_sdk_runtime"), \
                mock.patch.object(cos_verifier, "load_verified_candidate_modules"), \
                mock.patch.object(cos_verifier, "probe_source",
                                  side_effect=cos_verifier.VerificationError("ffprobe_failed")), \
                mock.patch.object(cos_verifier, "load_credentials", credentials), \
                self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier._apply_with_deadline(
                args, cos_verifier.AcceptanceDeadline(), "2026-08-31T00:00:00+00:00")
        self.assertEqual(caught.exception.code, "ffprobe_failed")
        credentials.assert_not_called()

    def test_cleanup_deadline_preserves_original_safety_error_without_sleep(self):
        now = [0.0]
        deadline = cos_verifier.AcceptanceDeadline(clock=lambda: now[0])
        deadline.exceeded = True
        primary = cos_verifier.VerificationError("scope_violation")

        def blocked_cleanup():
            now[0] += cos_verifier.CLEANUP_DEADLINE_SECONDS + 1

        retained, failed = cos_verifier.preserve_bounded_action(deadline, blocked_cleanup, primary)
        self.assertIs(retained, primary)
        self.assertTrue(failed)
        self.assertEqual(cos_verifier.safe_error(retained), "scope_violation")
        self.assertEqual(deadline._cleanup_started, 0.0)

    def test_fixed_deadline_caps_request_timeout_and_fails_without_sleep(self):
        now = [100.0]
        deadline = cos_verifier.AcceptanceDeadline(clock=lambda: now[0])
        now[0] = 3650.0
        self.assertEqual(deadline.request_timeout(60), 50.0)
        self.assertEqual(deadline.elapsed(), 3550.0)
        now[0] = 3700.0
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            deadline.check()
        self.assertEqual(caught.exception.code, "acceptance_deadline_exceeded")
        self.assertTrue(deadline.exceeded)

    def test_deadline_blocks_candidate_probe_credentials_and_evidence_before_io(self):
        now = [0.0]
        deadline = cos_verifier.AcceptanceDeadline(clock=lambda: now[0])
        now[0] = cos_verifier.ACCEPTANCE_DEADLINE_SECONDS
        candidate_run = mock.Mock(side_effect=AssertionError("candidate subprocess must not run"))
        source = mock.Mock(spec=Path)
        credential_open = mock.Mock(side_effect=AssertionError("credential file must not open"))
        evidence = self.root / "deadline-must-not-create-evidence"
        operations = (
            lambda: cos_verifier.verify_candidate("a" * 40, deadline=deadline),
            lambda: cos_verifier.probe_source(source, deadline=deadline),
            lambda: cos_verifier.load_credentials(self.root / "credentials.env", deadline=deadline),
            lambda: cos_verifier.fresh_evidence_directory(evidence, deadline=deadline),
        )
        with mock.patch.object(cos_verifier.subprocess, "Popen", candidate_run), \
                mock.patch.object(cos_verifier.os, "open", credential_open):
            for operation in operations:
                with self.subTest(operation=operation), \
                        self.assertRaises(cos_verifier.VerificationError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, "acceptance_deadline_exceeded")
        candidate_run.assert_not_called()
        credential_open.assert_not_called()
        self.assertFalse(evidence.exists())

    def test_deadline_blocks_real_sdk_and_anonymous_requests_before_transport(self):
        try:
            import qcloud_cos  # noqa: F401
        except ImportError:
            self.skipTest("COS SDK absent locally; deadline transport test runs in the Linux SDK environment")
        import requests

        now = [0.0]
        deadline = cos_verifier.AcceptanceDeadline(clock=lambda: now[0])
        client, _http, session = cos_verifier.build_real_client(
            {"COS_BUCKET": "test-12345", "COS_REGION": "ap-hongkong",
             "COS_SECRET_ID": "fixture-id", "COS_SECRET_KEY": "fixture-key"},
            self.prefix, deadline=deadline)
        anonymous_head, anonymous_session = cos_verifier.build_anonymous_head_gate(
            bucket="test-12345", region="ap-hongkong", key=self.prefix + "material.mp4",
            deadline=deadline)
        now[0] = cos_verifier.ACCEPTANCE_DEADLINE_SECONDS
        transport = mock.Mock(side_effect=AssertionError("transport must not run"))
        try:
            with mock.patch.object(requests.sessions.Session, "send", transport):
                for operation in (
                    lambda: cos_verifier.get_bucket_notification_v2(
                        client, bucket="test-12345", deadline=deadline),
                    anonymous_head,
                ):
                    with self.subTest(operation=operation), \
                            self.assertRaises(cos_verifier.VerificationError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "acceptance_deadline_exceeded")
            transport.assert_not_called()
        finally:
            anonymous_session.close()
            session.close()

    def test_download_stream_rechecks_deadline_and_retains_partial(self):
        now = [0.0]
        deadline = cos_verifier.AcceptanceDeadline(clock=lambda: now[0])
        body_bytes = b"deadline-fixture"

        class ExpiringBody(io.BytesIO):
            def read(self, size=-1):
                value = super().read(size)
                now[0] = cos_verifier.ACCEPTANCE_DEADLINE_SECONDS
                return value

        artifact = {"sha256": hashlib.sha256(body_bytes).hexdigest(), "size_bytes": len(body_bytes)}
        response = {
            "content-length": str(len(body_bytes)), "etag": "fixture-etag",
            cos_upload.SHA_HEADER: artifact["sha256"], cos_upload.SIZE_HEADER: str(len(body_bytes)),
            cos_upload.BINDING_HEADER: "fixture-binding", "Body": ExpiringBody(body_bytes),
        }
        client = SimpleNamespace(get_object=lambda **_kwargs: response)
        destination = self.root / "deadline-download.mp4"
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            cos_verifier.download_and_verify(
                client, bucket="test-12345", key=self.prefix + "material.mp4", artifact=artifact,
                result={"etag": "fixture-etag"}, record={"binding": "fixture-binding"},
                destination=destination, deadline=deadline)
        self.assertEqual(caught.exception.code, "acceptance_deadline_exceeded")
        self.assertFalse(destination.exists())
        self.assertTrue(destination.with_suffix(".mp4.part").is_file())

    def test_only_derived_acceptance_scope_is_allowed_at_sdk_and_transport(self):
        for sha, run in (("a" * 40, JOB), ("a" * 40, "cos-../../production"),
                         ("main", "cos-unit-fixture-20260828")):
            with self.subTest(sha=sha, run=run), self.assertRaises(cos_verifier.VerificationError):
                cos_verifier.acceptance_prefix(sha, run)
        audit = cos_verifier.HttpAudit("test-12345", "ap-hongkong", self.prefix)
        base = "https://test-12345.cos.ap-hongkong.myqcloud.com/"
        self.assertEqual(audit.classify("GET", base, {"params": {
            "prefix": self.prefix.encode(), "delimiter": b"", "marker": b"", "max-keys": b"1",
            "encoding-type": b"url",
        }}), "list_objects")
        self.assertEqual(audit.classify("GET", base, {"params": {
            "notification": b"", "notify-type": b"2"
        }}), "get_bucket_notification_v2")
        self.assertEqual(audit.classify("POST", base + self.prefix + "material.mp4",
                                       {"params": {"uploadId": b"fixture"}}), "complete_multipart_upload")
        for method, url, kwargs in (
            ("GET", base, {"params": {"prefix": "drama-materials/"}}),
            ("DELETE", base + self.prefix + "material.mp4", {}),
            ("GET", base + "drama-materials/production.mp4", {}),
            ("GET", "https://other.example/", {"params": {"prefix": self.prefix}}),
            ("GET", base, {"params": {"notification": "", "notify-type": "1"}}),
            ("GET", base, {"params": {"notification": "", "notify-type": "2", "prefix": self.prefix}}),
            ("HEAD", base + self.prefix + "material.mp4", {"params": {"versionId": "1"}}),
            ("GET", base + self.prefix + "material.mp4", {"params": {"acl": "", "extra": "1"}}),
            ("GET", base + self.prefix + "material.mp4", {"params": {
                "uploadId": "fixture", "part-number-marker": "0", "max-parts": "1000",
                "encoding-type": "url", "extra": "1",
            }}),
            ("POST", base + self.prefix + "material.mp4", {"params": {"uploads": "", "extra": "1"}}),
            ("POST", base + self.prefix + "material.mp4", {"params": {"uploadId": "fixture", "extra": "1"}}),
            ("PUT", base + self.prefix + "material.mp4", {"params": {
                "uploadId": "fixture", "partNumber": "1", "extra": "1",
            }}),
        ):
            with self.subTest(method=method, url=url), self.assertRaises(cos_verifier.VerificationError):
                audit.classify(method, url, kwargs)
        wrapped = cos_verifier.AuditedCos(self.client, bucket="test-12345", prefix=self.prefix,
                                         checkpoint=self.root / "none.json", evidence_dir=self.root,
                                         persist=lambda _item: None,
                                         notification_reader=self.client.read_notification_configuration,
                                         anonymous_head=self.client.read_anonymous_head,
                                         deadline=cos_verifier.AcceptanceDeadline())
        with self.assertRaises(cos_verifier.VerificationError):
            wrapped.head_object(Bucket="test-12345", Key="drama-materials/production.mp4")
        with self.assertRaises(cos_verifier.VerificationError):
            wrapped.head_object(Bucket="test-12345", Key=self.prefix + "material.mp4", VersionId="1")
        self.assertEqual(self.client.calls["head"], [])

    def test_credentials_are_literal_whitelisted_and_private(self):
        raw = ("# dedicated verifier credentials\nCOS_SECRET_ID=fixture-id\nCOS_SECRET_KEY='fixture-key'\n"
               "COS_BUCKET=test-12345\nCOS_REGION=ap-hongkong\n\n")
        self.assertEqual(set(cos_verifier.parse_credentials(raw)), cos_verifier.ENV_KEYS)
        for changed in (raw + "COS_SECRET_ID=duplicate\n", raw.replace("fixture-key", "$(shell-command)"),
                        raw.replace("fixture-key", "`shell-command`"), raw.replace("ap-hongkong", "https://other.example/"),
                        raw + "COS_PREFIX=drama-materials/production\n", raw + "UNRELATED_TOKEN=not-selected\n",
                        raw + "set -a\n"):
            with self.subTest(), self.assertRaises(cos_verifier.VerificationError):
                cos_verifier.parse_credentials(changed)
        path = self.root / "restricted.env"
        path.write_text(raw)
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=17, st_nlink=1, st_size=path.stat().st_size)
        platform = SimpleNamespace(name="posix", open=os.open, fdopen=os.fdopen, O_RDONLY=os.O_RDONLY,
                                   O_NOFOLLOW=getattr(os, "O_NOFOLLOW", 0), fstat=lambda _fd: info, geteuid=lambda: 17)
        with mock.patch.object(cos_verifier, "os", platform):
            self.assertEqual(cos_verifier.load_credentials(path)["COS_BUCKET"], "test-12345")
            info.st_mode = stat.S_IFREG | 0o644
            with self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier.load_credentials(path)
            self.assertEqual(caught.exception.code, "credential_file_unverified")

    def test_source_size_gate_runs_before_probe_and_does_not_change_part_size(self):
        for size in (0, cos_verifier.MIN_BYTES, cos_verifier.MAX_BYTES + 1):
            path = mock.Mock(spec=Path, parents=(), suffix=".mp4")
            path.is_symlink.return_value = False
            path.is_file.return_value = True
            path.stat.return_value = SimpleNamespace(st_size=size)
            with self.subTest(size=size), mock.patch.object(cos_verifier.subprocess, "Popen") as run, \
                    self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier.probe_source(path)
            self.assertEqual(caught.exception.code, "source_size_outside_acceptance_limit")
            run.assert_not_called()
        self.assertEqual(cos_upload.DEFAULT_PART_SIZE, 16 * 1024 * 1024)

    def test_probe_rejects_non_media_before_any_cos_client_is_created(self):
        self.source.write_bytes(b"a" * cos_verifier.MIN_BYTES + b"not-media")
        ffprobe_proof = (Path("/verified/fixed/ffprobe"), {
            "path": str(cos_verifier.FFPROBE_PATH), "realpath": "/verified/fixed/ffprobe",
            "sha256": "f" * 64, "root_owned": True, "group_other_writable": False,
        })
        with mock.patch.object(cos_verifier, "verify_ffprobe_binary", return_value=ffprobe_proof), \
                mock.patch.object(cos_verifier, "run_bounded_process", return_value=SimpleNamespace(
                    returncode=0, stderr=b"", stdout=b'{"format":{"format_name":"mp4","duration":"NaN","size":"16777225"},"streams":[]}')):
            with self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier.probe_source(self.source)
        self.assertEqual(caught.exception.code, "ffprobe_failed")

    def test_fixed_fault_sequence_reuses_parts_and_downloads_identical_bytes(self):
        result, snapshots = self.scenario()
        self.assertEqual(result["audit"]["injections"], {"part_response_loss": 1, "complete_response_loss": 1})
        self.assertEqual(result["audit"]["part_calls"], {"1": 1, "2": 1})
        self.assertEqual(result["audit"]["listed_parts_on_resume"], [1])
        self.assertEqual([item["PartNumber"] for item in self.client.calls["part"]], [1, 2])
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["complete"])), (1, 1))
        self.assertEqual(result["completed_replay_write_delta"], {name: 0 for name in cos_verifier.WRITE_NAMES})
        self.assertEqual(cos_verifier.file_fingerprint(self.evidence / "downloaded.mp4"),
                         cos_verifier.file_fingerprint(self.source))
        first = json.loads((self.evidence / "checkpoint_after_part_response_loss.json").read_text())
        completing = json.loads((self.evidence / "checkpoint_after_complete_response_loss.json").read_text())
        done = json.loads((self.evidence / "upload-checkpoint.json").read_text())
        self.assertEqual((first["phase"], completing["phase"], done["phase"]), ("uploading", "completing", "completed"))
        self.assertEqual(len({item["upload_id"] for item in (first, completing, done)}), 1)
        self.assertEqual(self.client.calls["create"][0]["ACL"], "private")
        self.assertEqual(self.client.notification_reads, 2)
        self.assertEqual(result["audit"]["sdk_calls"]["get_bucket_notification_v2"], 2)
        self.assertEqual(result["audit"]["successful_sdk_responses"]["get_bucket_notification_v2"], 2)
        self.assertEqual(result["audit"]["notification_configuration"], {
            "hash": [hashlib.sha256(b"<NotificationConfiguration/>").hexdigest()] * 2,
            "count": 2, "verified": True,
        })
        self.assertEqual(result["audit"]["anonymous_head"], {
            "count": 2, "status": [403, 403], "verified": True,
        })
        self.assertEqual(result["audit"]["object_acl"], {
            "grant_count": 1, "public_grants": 0, "owner_only": True, "verified": True,
        })
        safe_report = json.dumps(result, sort_keys=True)
        for forbidden in ("fixture-owner", "Authorization", "COS_SECRET", "https://",
                          "CloudFunction", "Ckafka", "AuthenticatedUsers", "AllUsers"):
            self.assertNotIn(forbidden, safe_report)
        self.assertTrue(any(item["injections"]["part_response_loss"] == 1
                            and item["successful_sdk_responses"]["upload_part"] == 1 for item in snapshots))
        self.assertEqual(self.client.aborts, 0)

    def test_notification_xml_is_strictly_empty_and_evidence_is_non_sensitive(self):
        body = b'<?xml version="1.0"?><NotificationConfiguration>\n</NotificationConfiguration>'
        proof = cos_verifier.verify_empty_notification_configuration(body)
        self.assertEqual(proof, {"configuration_sha256": hashlib.sha256(body).hexdigest(),
                                 "rule_count": 0, "verified": True})
        rejected = (
            b"<NotificationConfiguration><CloudFunctionConfiguration/></NotificationConfiguration>",
            b"<NotificationConfiguration><Unknown/></NotificationConfiguration>",
            b"<NotificationConfiguration enabled='false'/>",
            b"<NotificationConfiguration xmlns='urn:unknown'/>",
            b"<NotificationConfiguration><!--unknown--></NotificationConfiguration>",
            b"<?unknown value?><NotificationConfiguration/>",
            b"<!--unknown--><NotificationConfiguration/>",
            b"<!DOCTYPE NotificationConfiguration><NotificationConfiguration/>",
            b"<!DOCTYPE NotificationConfiguration [<!ENTITY value 'x'>]><NotificationConfiguration>&value;</NotificationConfiguration>",
            b"<NotificationConfiguration>",
            b"x" * (cos_verifier.MAX_NOTIFICATION_BYTES + 1),
        )
        for document in rejected:
            with self.subTest(document_sha=hashlib.sha256(document).hexdigest()), \
                    self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier.verify_empty_notification_configuration(document)
            self.assertEqual(caught.exception.code,
                             "notification_configuration_not_empty_or_unverified")

    def test_existing_notification_rule_stops_before_any_cos_write(self):
        self.client.notification_documents = [
            b"<NotificationConfiguration><CloudFunctionConfiguration/></NotificationConfiguration>"
        ]
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            self.scenario()
        self.assertEqual(caught.exception.code,
                         "notification_configuration_not_empty_or_unverified")
        fixture_names = {"create_multipart_upload": "create", "upload_part": "part",
                         "complete_multipart_upload": "complete"}
        self.assertEqual({name: len(self.client.calls[fixture_names[name]]) for name in cos_verifier.WRITE_NAMES},
                         {name: 0 for name in cos_verifier.WRITE_NAMES})
        checkpoint = self.evidence / "upload-checkpoint.json"
        self.assertTrue(checkpoint.is_file())
        self.assertEqual(json.loads(checkpoint.read_text())["phase"], "creating")
        self.assertEqual(self.snapshots[-1]["notification_configuration"], {
            "hash": [], "count": 0, "verified": False,
        })
        self.assertNotIn("CloudFunction", json.dumps(self.snapshots))
        self.assertEqual(self.client.aborts, 0)

    def test_notification_rule_appearing_before_complete_preserves_upload_and_checkpoint(self):
        self.client.notification_documents = [
            b"<NotificationConfiguration/>",
            b"<NotificationConfiguration><CkafkaConfiguration/></NotificationConfiguration>",
        ]
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            self.scenario()
        self.assertEqual(caught.exception.code,
                         "notification_configuration_not_empty_or_unverified")
        checkpoint = json.loads((self.evidence / "upload-checkpoint.json").read_text())
        after_part = json.loads((self.evidence / "checkpoint_after_part_response_loss.json").read_text())
        self.assertEqual(checkpoint["phase"], "completing")
        self.assertEqual(checkpoint["upload_id"], after_part["upload_id"])
        self.assertEqual(len(self.client.calls["create"]), 1)
        self.assertEqual([item["PartNumber"] for item in self.client.calls["part"]], [1, 2])
        self.assertEqual(self.client.calls["complete"], [])
        self.assertEqual(self.snapshots[-1]["notification_configuration"], {
            "hash": [hashlib.sha256(b"<NotificationConfiguration/>").hexdigest()],
            "count": 1, "verified": False,
        })
        self.assertNotIn("Ckafka", json.dumps(self.snapshots))
        self.assertEqual(self.client.aborts, 0)

    def test_anonymous_404_or_200_stops_before_create_with_checkpoint_retained(self):
        fixture_names = {"create_multipart_upload": "create", "upload_part": "part",
                         "complete_multipart_upload": "complete"}
        for status in (404, 200):
            with self.subTest(status=status):
                self.client = AcceptanceMultipartCos()
                self.client.anonymous_statuses = [status]
                self.evidence = self.root / ("private-evidence-" + str(status))
                with self.assertRaises(cos_verifier.VerificationError) as caught:
                    self.scenario()
                self.assertEqual(caught.exception.code,
                                 "anonymous_access_not_private_or_unverified")
                self.assertEqual(
                    {name: len(self.client.calls[fixture_names[name]]) for name in cos_verifier.WRITE_NAMES},
                    {name: 0 for name in cos_verifier.WRITE_NAMES},
                )
                self.assertEqual(json.loads((self.evidence / "upload-checkpoint.json").read_text())["phase"],
                                 "creating")
                self.assertEqual(self.snapshots[-1]["anonymous_head"], {
                    "count": 1, "status": [status], "verified": False,
                })

    def test_public_after_complete_preserves_object_upload_and_checkpoint(self):
        self.client.anonymous_statuses = [403, 200]
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            self.scenario()
        self.assertEqual(caught.exception.code, "anonymous_access_not_private_or_unverified")
        checkpoint = json.loads((self.evidence / "upload-checkpoint.json").read_text())
        self.assertEqual(checkpoint["phase"], "completing")
        self.assertEqual(len(self.client.calls["create"]), 1)
        self.assertEqual(len(self.client.calls["complete"]), 1)
        self.assertIsNotNone(self.client.object_body)
        self.assertEqual(self.client.aborts, 0)
        self.assertEqual(self.snapshots[-1]["anonymous_head"], {
            "count": 2, "status": [403, 200], "verified": False,
        })

    def test_acl_proof_rejects_public_or_unknown_grantees_and_preserves_object(self):
        self.assertEqual(cos_verifier.verify_private_object_acl(deepcopy(self.client.object_acl)), {
            "grant_count": 1, "public_grants": 0, "owner_only": True, "verified": True,
        })
        rejected = []
        for canned, grantee in (
            ("public-read", {"Type": "Group", "URI": "http://cam.qcloud.com/groups/global/AllUsers"}),
            ("private", {"Type": "Group", "URI": "http://cam.qcloud.com/groups/global/AuthenticatedUsers"}),
            ("private", {"Type": "Unknown", "ID": "fixture-owner"}),
        ):
            value = deepcopy(self.client.object_acl)
            value["CannedACL"] = canned
            value["AccessControlList"]["Grant"][0]["Grantee"] = grantee
            rejected.append(value)
        extra_named = deepcopy(self.client.object_acl)
        extra_named["AccessControlList"]["Grant"].append({
            "Grantee": {"Type": "CanonicalUser", "ID": "another-named-user"},
            "Permission": "READ",
        })
        rejected.append(extra_named)
        wrong_owner = deepcopy(self.client.object_acl)
        wrong_owner["AccessControlList"]["Grant"][0]["Grantee"]["ID"] = "another-named-user"
        rejected.append(wrong_owner)
        read_only = deepcopy(self.client.object_acl)
        read_only["AccessControlList"]["Grant"][0]["Permission"] = "READ"
        rejected.append(read_only)
        missing_owner = deepcopy(self.client.object_acl)
        missing_owner["Owner"] = {"ID": ""}
        rejected.append(missing_owner)
        for response in rejected:
            with self.subTest(canned=response["CannedACL"]), \
                    self.assertRaises(cos_verifier.VerificationError) as caught:
                cos_verifier.verify_private_object_acl(response)
            self.assertEqual(caught.exception.code, "object_acl_not_private_or_unverified")

        self.client.object_acl = rejected[0]
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            self.scenario()
        self.assertEqual(caught.exception.code, "object_acl_not_private_or_unverified")
        self.assertEqual(json.loads((self.evidence / "upload-checkpoint.json").read_text())["phase"],
                         "completing")
        self.assertIsNotNone(self.client.object_body)
        self.assertEqual(self.client.aborts, 0)
        self.assertEqual(self.snapshots[-1]["object_acl"], {
            "grant_count": None, "public_grants": None, "owner_only": False, "verified": False,
        })

    def test_nonempty_object_or_multipart_prefix_stops_before_create(self):
        for field in ("existing_key", "existing_upload"):
            setattr(self.client, field, True)
            with self.subTest(field=field), self.assertRaises(cos_verifier.VerificationError) as caught:
                self.scenario()
            self.assertEqual(caught.exception.code, "prefix_not_empty_or_unverified")
            setattr(self.client, field, False)
        self.assertEqual(self.client.calls["create"], [])
        self.assertFalse((self.evidence / "upload-checkpoint.json").exists())

    def test_missing_part_loss_injection_cannot_report_pass(self):
        with mock.patch.object(cos_verifier.AuditedCos, "upload_part",
                               lambda instance, **kwargs: instance._call("upload_part", kwargs)):
            with self.assertRaises(cos_verifier.VerificationError) as caught:
                self.scenario()
        self.assertEqual(caught.exception.code, "part_loss_not_verified")
        self.assertIsNotNone(self.client.object_headers)
        self.assertEqual(self.client.aborts, 0)

    def test_missing_complete_loss_injection_cannot_report_pass(self):
        with mock.patch.object(cos_verifier.AuditedCos, "complete_multipart_upload",
                               lambda instance, **kwargs: instance._call("complete_multipart_upload", kwargs)):
            with self.assertRaises(cos_verifier.VerificationError) as caught:
                self.scenario()
        self.assertEqual(caught.exception.code, "completion_loss_not_verified")
        self.assertEqual(json.loads((self.evidence / "upload-checkpoint.json").read_text())["phase"], "completed")
        self.assertEqual(self.client.aborts, 0)

    def test_download_sha_mismatch_keeps_completed_checkpoint_and_evidence(self):
        self.client.corrupt_download = True
        with self.assertRaises(cos_verifier.VerificationError) as caught:
            self.scenario()
        self.assertEqual(caught.exception.code, "download_not_verified")
        self.assertTrue((self.evidence / "downloaded.mp4.part").is_file())
        self.assertFalse((self.evidence / "downloaded.mp4").exists())
        self.assertEqual(json.loads((self.evidence / "upload-checkpoint.json").read_text())["phase"], "completed")
        self.assertEqual(self.client.aborts, 0)

    def test_reusing_evidence_directory_is_rejected_without_deletion(self):
        cos_verifier.fresh_evidence_directory(self.evidence)
        marker = self.evidence / "retained.json"
        marker.write_text("retained")
        with self.assertRaises(cos_verifier.VerificationError):
            cos_verifier.fresh_evidence_directory(self.evidence)
        self.assertEqual(marker.read_text(), "retained")

    def test_sdk_factory_disables_both_retries_and_ambient_proxy_credentials(self):
        import requests
        from requests.adapters import HTTPAdapter
        fake = SimpleNamespace(CosConfig=mock.Mock(), CosS3Client=mock.Mock())
        previous = cos_verifier._VERIFIED_SDK_RUNTIME
        self.addCleanup(setattr, cos_verifier, "_VERIFIED_SDK_RUNTIME", previous)
        cos_verifier._VERIFIED_SDK_RUNTIME = SimpleNamespace(
            CosConfig=fake.CosConfig, CosS3Client=fake.CosS3Client, CosS3Auth=mock.Mock(),
            requests=requests, HTTPAdapter=HTTPAdapter,
        )
        _client, _audit, session = cos_verifier.build_real_client(
            {"COS_BUCKET": "test-12345", "COS_REGION": "ap-hongkong", "COS_SECRET_ID": "fixture-id",
             "COS_SECRET_KEY": "fixture-key"}, self.prefix)
        self.addCleanup(session.close)
        self.assertEqual(fake.CosS3Client.call_args.kwargs["retry"], 0)
        self.assertIs(fake.CosS3Client.call_args.kwargs["session"], session)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.get_adapter("https://example.test/").max_retries.total, 0)
        self.assertFalse(fake.CosConfig.call_args.kwargs["AllowRedirects"])
        self.assertFalse(fake.CosConfig.call_args.kwargs["AutoSwitchDomainOnRetry"])
        self.assertTrue(fake.CosConfig.call_args.kwargs["VerifySSL"])

    def test_real_sdk_notification_reader_fails_closed_on_http_or_xml(self):
        try:
            import qcloud_cos  # noqa: F401
        except ImportError:
            self.skipTest("COS SDK absent locally; verifier SDK transport must run in the Linux SDK environment")
        import requests
        from urllib.parse import parse_qs, urlsplit

        cases = (
            (403, b"<Error><Code>AccessDenied</Code></Error>"),
            (404, b"<Error><Code>NoSuchConfiguration</Code></Error>"),
            (200, b"<NotificationConfiguration><CloudFunctionConfiguration/></NotificationConfiguration>"),
            (200, b"<NotificationConfiguration>"),
            (200, b"<!DOCTYPE NotificationConfiguration><NotificationConfiguration/>"),
        )
        for status, body in cases:
            with self.subTest(status=status, body_sha=hashlib.sha256(body).hexdigest()):
                client, http, session = cos_verifier.build_real_client(
                    {"COS_BUCKET": "test-12345", "COS_REGION": "ap-hongkong",
                     "COS_SECRET_ID": "fixture-id", "COS_SECRET_KEY": "fixture-key"}, self.prefix)

                def no_network_send(_session, request, **kwargs):
                    parsed = urlsplit(request.url)
                    query = {key: values[0] for key, values in parse_qs(
                        parsed.query, keep_blank_values=True).items()}
                    self.assertEqual(request.method, "GET")
                    self.assertEqual(parsed.scheme + "://" + parsed.netloc + parsed.path,
                                     "https://test-12345.cos.ap-hongkong.myqcloud.com/")
                    self.assertEqual(query, {"notification": "", "notify-type": "2"})
                    self.assertFalse(kwargs["allow_redirects"])
                    self.assertTrue(kwargs["verify"])
                    response = requests.Response()
                    response.status_code, response.request = status, request
                    response.headers["Content-Length"] = str(len(body))
                    if status == 200:
                        response.raw = io.BytesIO(body)
                    else:
                        response._content = body
                    return response

                try:
                    with mock.patch.object(requests.sessions.Session, "send", no_network_send), \
                            self.assertRaises(cos_verifier.VerificationError) as caught:
                        cos_verifier.get_bucket_notification_v2(client, bucket="test-12345")
                    self.assertEqual(caught.exception.code,
                                     "notification_configuration_not_empty_or_unverified")
                    self.assertEqual(dict(http.calls), {"get_bucket_notification_v2": 1})
                    self.assertEqual(sum(http.statuses.values()), 1)
                finally:
                    session.close()

    def test_real_sdk_verifier_protocol_uses_mock_transport_only(self):
        try:
            import qcloud_cos  # noqa: F401
        except ImportError:
            self.skipTest("COS SDK absent locally; verifier SDK transport must run in the Linux SDK environment")
        import requests
        from urllib.parse import parse_qs, urlsplit
        from xml.etree import ElementTree as ET
        notification_queries = []
        anonymous_queries = []

        def xml_response(root_name, values):
            root = ET.Element(root_name)
            for name, value in values.items():
                if isinstance(value, list):
                    for entry in value:
                        child = ET.SubElement(root, name)
                        for key, item in entry.items():
                            ET.SubElement(child, key).text = str(item)
                else:
                    ET.SubElement(root, name).text = str(value)
            return ET.tostring(root)

        def no_network_send(_session, request, **_kwargs):
            # Exercise real SDK XML, parameter/header conversion and requests
            # preparation. No socket or real COS object exists in this test.
            query = {key: values[0] for key, values in parse_qs(urlsplit(request.url).query, keep_blank_values=True).items()}
            headers = {key.lower(): value.decode() if isinstance(value, bytes) else value
                       for key, value in request.headers.items()}
            common = {"Bucket": "test-12345", "Key": self.prefix + "material.mp4"}
            response = requests.Response()
            response.status_code, response.request, response.url = 200, request, request.url
            response._content = b""
            if request.method == "HEAD" and not headers.get("authorization"):
                anonymous_queries.append(request.url)
                response.status_code = 403
                response.raw = io.BytesIO(b"")
            elif request.method == "HEAD":
                try:
                    response.headers.update(self.client.head_object(**common))
                except FakeCosError:
                    response.status_code = 404
                    response._content = b"<Error><Code>NoSuchKey</Code><Message>fixture</Message></Error>"
            elif request.method == "GET" and "prefix" in query:
                name = "list_multipart_uploads" if "uploads" in query else "list_objects"
                value = getattr(self.client, name)(Bucket="test-12345", Prefix=query["prefix"])
                response._content = xml_response("ListResult", value)
            elif request.method == "GET" and "versioning" in query:
                response._content = xml_response("VersioningConfiguration", self.client.get_bucket_versioning(Bucket="test-12345"))
            elif request.method == "GET" and "notification" in query:
                self.assertEqual(query, {"notification": "", "notify-type": "2"})
                self.assertEqual(headers.get("accept-encoding"), "identity")
                self.assertTrue(headers.get("authorization"))
                notification_queries.append(dict(query))
                body = b"<NotificationConfiguration/>"
                response.headers["Content-Type"] = "application/xml"
                response.headers["Content-Length"] = str(len(body))
                response.raw = io.BytesIO(body)
            elif request.method == "GET" and "acl" in query:
                policy = ET.Element("AccessControlPolicy")
                owner = ET.SubElement(policy, "Owner")
                ET.SubElement(owner, "ID").text = "fixture-owner"
                access = ET.SubElement(policy, "AccessControlList")
                grant = ET.SubElement(access, "Grant")
                grantee = ET.SubElement(grant, "Grantee", {"type": "CanonicalUser"})
                ET.SubElement(grantee, "ID").text = "fixture-owner"
                ET.SubElement(grant, "Permission").text = "FULL_CONTROL"
                response._content = ET.tostring(policy)
            elif request.method == "POST" and "uploads" in query:
                value = self.client.create_multipart_upload(
                    **common, ACL=headers["x-cos-acl"], ContentType=headers["content-type"],
                    Metadata={key: value for key, value in headers.items() if key.startswith("x-cos-meta-")})
                response._content = xml_response("InitiateMultipartUploadResult", value)
            elif request.method == "GET" and "uploadId" in query:
                value = self.client.list_parts(**common, UploadId=query["uploadId"],
                                               MaxParts=int(query["max-parts"]), PartNumberMarker=int(query["part-number-marker"]))
                response._content = xml_response("ListPartsResult", value)
            elif request.method == "PUT":
                response.headers.update(self.client.upload_part(**common, UploadId=query["uploadId"],
                                                                 PartNumber=int(query["partNumber"]), Body=request.body))
            elif request.method == "POST" and "uploadId" in query:
                parts = [{"PartNumber": int(item.findtext("PartNumber")), "ETag": item.findtext("ETag")}
                         for item in ET.fromstring(request.body).findall("Part")]
                value = self.client.complete_multipart_upload(
                    **common, UploadId=query["uploadId"], MultipartUpload={"Part": parts},
                    Metadata={cos_upload.FORBID_OVERWRITE_HEADER: headers[cos_upload.FORBID_OVERWRITE_HEADER]})
                response._content = xml_response("CompleteMultipartUploadResult", value)
            elif request.method == "GET" and not query:
                response.headers.update(self.client.object_headers)
                response.raw = io.BytesIO(self.client.object_body)
            else:
                raise AssertionError("unexpected offline verifier transport operation")
            return response

        deadline = cos_verifier.AcceptanceDeadline()
        client, http, session = cos_verifier.build_real_client(
            {"COS_BUCKET": "test-12345", "COS_REGION": "ap-hongkong", "COS_SECRET_ID": "fixture-id",
             "COS_SECRET_KEY": "fixture-key"}, self.prefix, deadline=deadline)
        self.addCleanup(session.close)
        anonymous_head, anonymous_session = cos_verifier.build_anonymous_head_gate(
            bucket="test-12345", region="ap-hongkong", key=self.prefix + "material.mp4", deadline=deadline)
        self.addCleanup(anonymous_session.close)
        self.source.write_bytes(b"a" * cos_verifier.MIN_BYTES + b"unit-test-tail")
        cos_verifier.fresh_evidence_directory(self.evidence)
        with mock.patch.object(requests.sessions.Session, "send", no_network_send):
            result = cos_verifier.verify_upload(
                client, bucket="test-12345", prefix=self.prefix, source=self.source, evidence_dir=self.evidence,
                artifact=cos_verifier.file_fingerprint(self.source), persist=lambda _item: None,
                anonymous_head=anonymous_head, deadline=deadline)
        self.assertEqual(dict(http.calls), result["audit"]["sdk_calls"])
        self.assertEqual(result["audit"]["injections"], {"part_response_loss": 1, "complete_response_loss": 1})
        self.assertEqual(http.calls["create_multipart_upload"], 1)
        self.assertEqual(http.calls["upload_part"], 2)
        self.assertEqual(http.calls["complete_multipart_upload"], 1)
        self.assertEqual(http.calls["get_bucket_notification_v2"], 2)
        self.assertEqual(http.calls["get_object_acl"], 1)
        self.assertEqual(len(notification_queries), 2)
        expected_anonymous_url = "https://test-12345.cos.ap-hongkong.myqcloud.com/" + self.prefix + "material.mp4"
        self.assertEqual(anonymous_queries, [expected_anonymous_url, expected_anonymous_url])
        self.assertEqual(result["audit"]["notification_configuration"], {
            "hash": [hashlib.sha256(b"<NotificationConfiguration/>").hexdigest()] * 2,
            "count": 2, "verified": True,
        })
        self.assertEqual(result["audit"]["anonymous_head"], {
            "count": 2, "status": [403, 403], "verified": True,
        })
        self.assertEqual(result["audit"]["object_acl"], {
            "grant_count": 1, "public_grants": 0, "owner_only": True, "verified": True,
        })

    def test_top_level_failure_never_prints_private_exception_text(self):
        output = io.StringIO()
        with mock.patch.object(cos_verifier, "apply", side_effect=RuntimeError("https://private/?token=secret")), \
                mock.patch("sys.stdout", output):
            self.assertEqual(cos_verifier.main(["--apply"]), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "not_passed")
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("https://", output.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
