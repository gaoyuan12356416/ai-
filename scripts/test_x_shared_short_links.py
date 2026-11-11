"""Concurrency, immutable redirects, old links and publisher integration."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from features.x_posts import shared_short_links as links
from features.x_posts import service
from scripts.test_x_posts import ScriptedHttpClient, candidate, response, valid_probe_payload

BASE = service.DEFAULT_SHORT_BASE_URL
TAG = 'https://gy.g2flow.com/s2l-tag' if BASE.endswith('/s2l') else 'https://gy.g2flow.com/s2l'


def allocate_worker(args):
    root, origin, log_id = args
    os.environ['X_POST_SHARED_SHORT_ROOT'] = root
    os.environ['X_POST_SHARED_SHORT_ALLOCATE'] = '1'
    return links.allocate(origin, log_id)


class SharedShortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.shared = self.root / 'shared'
        links.initialize(self.shared)
        self.env = mock.patch.dict(os.environ, {'X_POST_SHARED_SHORT_ROOT': str(self.shared),
                                               'X_POST_SHARED_SHORT_ALLOCATE': '1',
                                               'X_POST_ATTRIBUTION_V2_ENABLED': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def rows(self):
        with closing(sqlite3.connect(self.shared / 'links.sqlite3')) as conn:
            return conn.execute('SELECT id,origin,log_id,long_url FROM short_link ORDER BY id').fetchall()

    def target(self, suffix=''):
        return service.build_w2a_url(dict(username='example', timestamp=1,
            material_language='en', drama_name='Example' + suffix, tag='romance', log_id=7,
            page_name='Example', page_id='2', material_name='Video', material_id='3',
            queue_id=8, content_id='9', video_duration_seconds=30))

    def test_processes_share_sequence_and_idempotency(self):
        jobs = [(str(self.shared), origin, i) for i in range(1, 17) for origin in (BASE, TAG)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            urls = list(pool.map(allocate_worker, jobs * 2))
        self.assertEqual(urls[:32], urls[32:])
        self.assertEqual(len(set(urls)), 32)
        self.assertEqual([r[0] for r in self.rows()], list(range(1, 33)))

    def test_competing_same_identity_allocates_once(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            urls = list(pool.map(lambda _: links.allocate(TAG, 9000000000321), range(60)))
        self.assertEqual(set(urls), {links.PUBLIC_BASE + '/1'})
        self.assertEqual(len(self.rows()), 1)

    def test_source_identity_separation_and_immutable_target(self):
        self.assertNotEqual(links.allocate(BASE, 7), links.allocate(TAG, 7))
        target = self.target()
        path = service.write_short_redirect(self.root / 'legacy', 7, target)
        self.assertEqual(path, self.shared / 'public/1.html')
        self.assertEqual(service.write_short_redirect(self.root / 'legacy', 7, target), path)
        with self.assertRaises(service.XPostError):
            service.write_short_redirect(self.root / 'legacy', 7, self.target('changed'))
        self.assertEqual(self.rows()[0][3], target)

    def test_legacy_log_retains_old_path_and_no_allocation(self):
        target = self.target()
        path = service.write_short_redirect(self.root / 'legacy', 15, target)
        self.assertEqual(path, self.root / 'legacy/15.html')
        self.assertEqual(self.rows(), [])

    def test_rollback_disables_new_allocations_but_keeps_existing_mapping(self):
        links.allocate(BASE, 7)
        with mock.patch.dict(os.environ, {'X_POST_SHARED_SHORT_ALLOCATE': '0'}):
            self.assertEqual(links.allocate(BASE, 7), links.PUBLIC_BASE + '/1')
            self.assertIsNone(links.allocate(BASE, 8))
            self.assertEqual(service._build_short_url(BASE, 8), BASE + '/8.html')
            self.assertEqual(service.write_short_redirect(self.root / 'legacy', 7, self.target()),
                             self.shared / 'public/1.html')

    def test_missing_database_does_not_create_or_fall_back(self):
        (self.shared / 'links.sqlite3').unlink()
        with self.assertRaises(service.XPostError):
            service._build_short_url(BASE, 7)
        self.assertFalse((self.shared / 'links.sqlite3').exists())

    def test_crash_after_file_before_commit_only_recovers_same_target(self):
        links.allocate(BASE, 7)
        def interrupted(*args):
            service._write_short_redirect_file(*args)
            raise RuntimeError('simulated interruption')
        with self.assertRaises(RuntimeError):
            links.write_allocated(BASE, 7, self.target(), interrupted)
        self.assertEqual(self.rows()[0][3], '')
        with self.assertRaises(service.XPostError):
            service.write_short_redirect(self.root / 'legacy', 7, self.target('other'))
        service.write_short_redirect(self.root / 'legacy', 7, self.target())
        self.assertEqual(self.rows()[0][3], self.target())

    def test_publish_freezes_short_url_without_changing_attribution_or_reposting(self):
        db = self.root / 'posts.sqlite3'
        store = service.XPostStore(db)
        queue = store.enqueue(candidate())
        client = ScriptedHttpClient([
            service.HttpResponse(200, {'content-type': 'video/mp4','content-length': '5'}, body=b'video'),
            response(200, {'data': {'id': 'media1'}}), response(200, {'data': {'expires_at': 1}}),
            response(200, {'data': {'id': 'media1'}}), response(201, {'data': {'id': '190002'}})])
        kwargs = dict(db_path=db, queue_id=queue['id'], account={'id':2, 'username':'ShortsDramhx'},
                      access_token='test-only', public_root=self.root / 'legacy', short_base_url=BASE,
                      allowed_media_hosts=['media.example.com'], http_client=client, sleeper=lambda _: None)
        with mock.patch.object(service, 'probe_media', return_value=valid_probe_payload()):
            result = service.publish_canary(**kwargs)
        self.assertEqual(result['short_url'], links.PUBLIC_BASE + '/1')
        self.assertTrue((self.shared / 'public/1.html').is_file())
        log = store.get_log(result['log_id'])
        from urllib.parse import parse_qs, urlsplit
        self.assertEqual(parse_qs(urlsplit(log['long_url']).query)['af_c_id'], [str(queue['id'])])
        calls = len(client.requests)
        self.assertEqual(service.publish_canary(**kwargs)['post_id'], result['post_id'])
        self.assertEqual(len(client.requests), calls)


if __name__ == '__main__':
    unittest.main()
