"""X GET retries use fresh streams and never repeat publication calls."""
import errno
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from features.drama_synthesis.youtube import YouTubeHTTPClient, YouTubeHTTPError
from features.pipeline_dispatch.executors import NodeExecutors, ExecutionError
from features.pipeline_dispatch.worker import LeaseLost
from scripts.test_pipeline_executors import Context


class Stream:
    def __init__(self, chunks=(), status=200):
        self.chunks, self.status_code = chunks, status

    def iter_content(self, size):
        assert size == 1024 * 1024
        for chunk in self.chunks:
            if callable(chunk):
                chunk()
            elif isinstance(chunk, BaseException):
                raise chunk
            else:
                yield chunk


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.executor = NodeExecutors({'state_root': self.temp.name, 'backend': 'cpu'})
        self.target = self.root / 'media.bin'
        self.context = Context()
        self.body = b'complete-frozen-media'
        self.artifact = {'url': 'https://media.example/video?token=do-not-log', 'size': len(self.body),
                         'sha256': hashlib.sha256(self.body).hexdigest(), 'mime': 'video/mp4'}
        self.requests, self.closed, self.attempt_paths = [], [], []
        self.responses = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'PIPELINE_MEDIA_ALLOWED_HOSTS': 'media.example'}).start()
        patch('features.pipeline_dispatch.executors.time.sleep').start()
        patch('features.drama_synthesis.youtube.YouTubeHTTPClient', side_effect=lambda:
              YouTubeHTTPClient(session_factory=self.session)).start()

    def session(self):
        test = self
        class Session:
            trust_env = True
            def get(self, url, **kw):
                test.requests.append((url, kw))
                test.attempt_paths.extend(test.root.rglob('x-get-*'))
                response = test.responses.pop(0)
                if isinstance(response, BaseException):
                    raise response
                return response
            def close(self):
                test.closed.append(True)
        return Session()

    def download(self):
        return self.executor._download(self.artifact, self.target, self.context, x_network=True)

    def platform(self):
        platform = Mock()
        platform.upload_media.return_value = {'media_id': '77'}
        platform.create_post.return_value = {'post_id': '88'}
        self.executor.platform_clients['x_publish'] = platform
        return platform

    def publish(self):
        return self.executor.x_publish({'account_resource': 'x:account:1', 'request': {'body': 'frozen'}},
                                       self.artifact, self.context)

    def test_stream_interruption_restarts_fresh_then_single_post(self):
        self.responses = [Stream([b'partial', requests.exceptions.ChunkedEncodingError('https://secret/?token=bad')]),
                          Stream([self.body])]
        platform = self.platform()
        platform.upload_media.side_effect = lambda token, path, **kw: (
            self.assertEqual(path.read_bytes(), self.body) or {'media_id': '77'})
        with self.assertLogs('features.pipeline_dispatch.executors', level='WARNING') as logs:
            self.assertEqual(self.publish()['post_id'], '88')
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(set(self.attempt_paths)), 2)
        self.assertTrue(all(not p.exists() for p in self.attempt_paths))
        self.assertTrue(all(kw['timeout'] == (30, 120) and kw['allow_redirects'] is False for _, kw in self.requests))
        self.assertEqual(len(self.closed), 2)
        platform.upload_media.assert_called_once(); platform.create_post.assert_called_once()
        platform.repost.assert_not_called()
        self.assertIn('ChunkedEncodingError', '\n'.join(logs.output))
        self.assertNotIn('secret', '\n'.join(logs.output)); self.assertNotIn('token', '\n'.join(logs.output))

    def test_hash_mismatch_never_retries_or_uploads(self):
        self.responses = [Stream([b'x' * len(self.body)])]
        platform = self.platform()
        with self.assertRaisesRegex(ExecutionError, 'artifact_fingerprint_changed'):
            self.publish()
        self.assertEqual(len(self.requests), 1)
        platform.upload_media.assert_not_called(); platform.create_post.assert_not_called()

    def test_size_mismatch_never_retries(self):
        for body in (self.body[:-1], self.body + b'oversize'):
            with self.subTest(size=len(body)):
                self.responses = [Stream([body])]
                before = len(self.requests)
                with self.assertRaises((ExecutionError, YouTubeHTTPError)):
                    self.download()
                self.assertEqual(len(self.requests) - before, 1)

    def test_forbidden_not_found_and_host_denied_never_retry(self):
        for status in (403, 404):
            with self.subTest(status=status):
                self.responses = [Stream(status=status)]
                before = len(self.requests)
                with self.assertLogs('features.pipeline_dispatch.executors', level='WARNING') as logs:
                    with self.assertRaises(YouTubeHTTPError): self.download()
                self.assertEqual(len(self.requests) - before, 1)
                self.assertIn('http_status=' + str(status), '\n'.join(logs.output))
        self.artifact['url'] = 'https://denied.example/video'
        before = len(self.requests)
        with self.assertRaisesRegex(YouTubeHTTPError, '允许范围'): self.download()
        self.assertEqual(len(self.requests), before)

    def test_lease_loss_after_interrupted_stream_prevents_retry_and_post(self):
        class LeaseContext(Context):
            lost = False
            def check(self):
                if self.lost: raise LeaseLost()
        self.context = LeaseContext()
        self.responses = [Stream([b'partial', lambda: setattr(self.context, 'lost', True),
                                  requests.ConnectionError('connection reset')])]
        platform = self.platform()
        with self.assertRaises(LeaseLost): self.publish()
        self.assertEqual(len(self.requests), 1)
        platform.upload_media.assert_not_called(); platform.create_post.assert_not_called()

    def test_three_total_attempts_then_no_post(self):
        self.responses = [Stream([b'partial', requests.ReadTimeout('private-url')]) for _ in range(3)]
        platform = self.platform()
        with self.assertLogs('features.pipeline_dispatch.executors', level='WARNING') as logs:
            with self.assertRaises(YouTubeHTTPError): self.publish()
        self.assertEqual(len(self.requests), 3)
        self.assertIn('attempt=3 retry=False', logs.output[-1])
        platform.upload_media.assert_not_called(); platform.create_post.assert_not_called()
        self.assertFalse(list(self.root.rglob('x-get-*')))

    def test_retryable_server_status_then_success(self):
        self.responses = [Stream(status=503), Stream([self.body])]
        self.assertEqual(self.download().read_bytes(), self.body)
        self.assertEqual(len(self.requests), 2)

    def test_local_disk_error_does_not_retry(self):
        self.responses = [Stream([b'partial', OSError(errno.ENOSPC, 'private-disk-path')])]
        with self.assertRaises(YouTubeHTTPError): self.download()
        self.assertEqual(len(self.requests), 1)

    def test_shared_non_x_download_still_attempts_once_with_original_timeout(self):
        self.responses = [Stream([requests.ConnectionError('connection reset')])]
        with self.assertRaises(YouTubeHTTPError):
            self.executor._download(self.artifact, self.target, self.context)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][1]['timeout'], 120)

    def test_x_preflight_source_uses_same_read_retry_before_probe(self):
        self.responses = [Stream([requests.ReadTimeout('private-url')]), Stream([self.body])]
        with patch('features.pipeline_dispatch.executors.subprocess.run', side_effect=ExecutionError('probe_marker')):
            with self.assertRaisesRegex(ExecutionError, 'probe_marker'):
                self.executor.x_preflight({'identity_mode': 'source_to_fingerprint', 'source_url': self.artifact['url']}, self.context)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.context.operations, [])


if __name__ == '__main__':
    unittest.main()
