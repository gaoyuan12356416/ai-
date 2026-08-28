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

from features.drama_synthesis import cos_upload, gpu_cache
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
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        module = load_fake_worker(SimpleNamespace(
            WORK_ROOT=directory.name, cached_gpu_video_result=mock.Mock(side_effect=gpu_cache.cache_error()),
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
        before = self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns
        versioning_checks = len(self.client.calls["versioning"])
        self.client.versioning = {"Status": "Enabled"}
        self.assertEqual(self.upload(), result)
        self.assertEqual(len(self.client.calls["versioning"]), versioning_checks)
        self.assertEqual((len(self.client.calls["create"]), len(self.client.calls["part"]),
                          len(self.client.calls["complete"])), (1, 3, 1))
        self.assertEqual((self.checkpoint.read_bytes(), self.checkpoint.stat().st_mtime_ns), before)
        self.assertLess(len(before[0]), 4096)
        self.assertNotIn("parts", json.loads(before[0]))
        if os.name == "posix":
            self.assertEqual(self.checkpoint.stat().st_mode & 0o777, 0o600)

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
        self.assertEqual(transport.headers[cos_upload.FORBID_OVERWRITE_HEADER], "true")


if __name__ == '__main__':
    unittest.main(verbosity=2)
