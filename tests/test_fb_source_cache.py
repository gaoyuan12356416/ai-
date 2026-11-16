from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

from features.fb_gpu.source_cache import SourceCache, SourceDownloadError


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.body, self.etag = b'original-media' * 500000, '"version1"'
        self.body_requests, self.validations = 0, 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if owner.etag and self.headers.get('If-None-Match') == owner.etag:
                    owner.validations += 1
                    self.send_response(304)
                    self.end_headers()
                    return
                owner.body_requests += 1
                self.send_response(200)
                if owner.etag:
                    self.send_header('ETag', owner.etag)
                self.send_header('Content-Length', str(len(owner.body)))
                self.end_headers()
                self.wfile.write(owner.body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d/media' % server.server_port
        self.cache = SourceCache(self.root/'cache', reserve_bytes=0)

    def fetch(self, name):
        return self.cache.fetch(self.url, self.root/name,
            session_factory=requests.Session, max_source_bytes=10**7)

    def test_different_jobs_share_bytes_but_revalidate_each_hit(self):
        cold, warm = self.fetch('job1'), self.fetch('job2')
        self.assertFalse(cold['cache_hit'])
        self.assertTrue(warm['cache_hit'])
        self.assertEqual((1, 1), (self.body_requests, self.validations))
        self.assertEqual(0, warm['network_bytes'])
        self.assertEqual(hashlib.sha256(self.body).hexdigest(), warm['sha256'])
        self.assertEqual(self.body, (self.root/'job2').read_bytes())

    def test_parallel_jobs_download_one_body(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            receipts = list(pool.map(self.fetch, ['job%d' % i for i in range(12)]))
        self.assertEqual(1, self.body_requests)
        self.assertEqual(11, sum(r['cache_hit'] for r in receipts))
        self.assertEqual(1, len({r['sha256'] for r in receipts}))

    def test_new_etag_keeps_old_job_immutable(self):
        first = self.fetch('oldjob')
        self.body, self.etag = b'changed-media', '"version2"'
        second = self.fetch('newjob')
        self.assertFalse(second['cache_hit'])
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertEqual(first['sha256'], hashlib.sha256((self.root/'oldjob').read_bytes()).hexdigest())
        self.assertEqual(self.body, (self.root/'newjob').read_bytes())

    def test_corruption_redownloads_without_trusting_304(self):
        self.fetch('oldjob')
        blob = next((self.root/'cache').glob('*/*.mp4'))
        blob.write_bytes(b'X' * len(self.body))
        fresh = self.fetch('newjob')
        self.assertFalse(fresh['cache_hit'])
        self.assertEqual(2, self.body_requests)
        self.assertEqual(self.body, (self.root/'newjob').read_bytes())

    def test_missing_or_weak_validator_uses_regular_download(self):
        for i, tag in enumerate([None, 'W/"version1"']):
            self.etag = tag
            self.fetch('first%d' % i)
            self.assertFalse(self.fetch('second%d' % i)['cache_hit'])
        self.assertEqual(4, self.body_requests)

    def test_unavailable_cache_does_not_fail_download(self):
        blocked = self.root/'not-a-directory'
        blocked.write_text('occupied')
        cache = SourceCache(blocked, reserve_bytes=0)
        receipt = cache.fetch(self.url, self.root/'job',
            session_factory=requests.Session, max_source_bytes=10**7)
        self.assertEqual('cache_storage_unavailable', receipt['cache_note'])
        self.assertEqual(self.body, (self.root/'job').read_bytes())

    def test_size_limit_and_cancel_preserve_original_error(self):
        with self.assertRaisesRegex(SourceDownloadError, 'source_too_large'):
            self.cache.fetch(self.url, self.root/'large',
                session_factory=requests.Session, max_source_bytes=1)
        self.assertFalse((self.root/'large').exists())
        def cancel():
            raise RuntimeError('pipeline_lease_lost')
        with self.assertRaisesRegex(RuntimeError, 'pipeline_lease_lost'):
            self.cache.fetch(self.url, self.root/'cancelled',
                session_factory=requests.Session, max_source_bytes=10**7, check=cancel)
        self.assertFalse((self.root/'cancelled').exists())

    def test_eviction_does_not_remove_in_use_job_links(self):
        self.cache.max_bytes = 1
        self.fetch('job1')
        self.etag, self.body = '"another"', b'new'
        self.url += '2'
        self.fetch('job2')
        self.assertTrue((self.root/'job1').is_file())
        self.assertEqual(2, len(list((self.root/'cache').glob('*/*.mp4'))))
        (self.root/'job1').unlink()
        self.cache._prune(exclude='',check=lambda:None)
        self.assertEqual(1,len(list((self.root/'cache').glob('*/*.mp4'))))
        self.assertTrue((self.root/'job2').is_file())

    def test_preparer_download_preserves_hash_and_records_observation(self):
        from types import SimpleNamespace
        from features.fb_gpu.prepare_worker import PrepareProcessor
        owner=object.__new__(PrepareProcessor)
        owner.config=SimpleNamespace(work_root=self.root,max_source_bytes=10**7)
        owner.session_factory=requests.Session
        class CacheWithoutReserve(SourceCache):
            def __init__(self,root):
                super().__init__(root,reserve_bytes=0)
        with patch('features.fb_gpu.prepare_worker.SourceCache',CacheWithoutReserve):
            for name in ['one','two']:
                folder=self.root/name
                folder.mkdir()
                self.assertEqual(hashlib.sha256(self.body).hexdigest(),owner._download(self.url,folder/'download.tmp'))
        result=json.loads((self.root/'two'/'source-cache.json').read_text())
        self.assertTrue(result['cache_hit'])
        self.assertEqual(0,result['network_bytes'])


if __name__ == '__main__':
    unittest.main()
