"""Publisher safety tests use memory objects, never production credentials."""
import copy
import gzip
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import report
import thumbnails


class FakePublisher:
    def __init__(self):
        self.objects, self.puts, self.verifies = {}, [], []
        self.fail_key = None

    def get(self, key):
        return copy.deepcopy(self.objects.get(key))

    def put(self, key, data, meta):
        report.check_key(key)
        self.objects[key] = dict(data=data, metadata=copy.deepcopy(meta))
        self.puts.append(key)

    def verify(self, key, data, meta, decoded=None):
        self.verifies.append(key)
        if key == self.fail_key:
            raise RuntimeError('simulated_unknown_public_readback')
        assert self.objects[key] == dict(data=data, metadata=meta)
        raw = gzip.decompress(data) if meta.get('ContentEncoding') == 'gzip' else data
        assert decoded is None or raw == decoded
        return dict(url='https://example.test/' + key, sha256=report.sha(raw),
                    stored_sha256=report.sha(data), bytes=len(raw), stored_bytes=len(data), http_status=200)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.run = self.state / 'runs' / 'r1'
        self.run.mkdir(parents=True)
        self.date = '2026-10-21'
        self.entry = dict(date=self.date, url='https://example.test/daily/' + self.date)
        self.data = dict(schema_version=3, report_date=self.date, generated_at='2026-10-22T09:00:00+08:00',
                         validation={'status': 'passed'}, summary={})
        def artifact(data, content_type, immutable):
            return dict(data=data, content_type=content_type, immutable=immutable, compress=True)
        self.bundle = {
            'index.html': artifact(b'<html>latest</html>', 'text/html; charset=utf-8', False),
            'daily/' + self.date + '/index.html': artifact(b'<html>snapshot</html>', 'text/html; charset=utf-8', True),
            'history.json': artifact(report.json_bytes([self.entry]), 'application/json; charset=utf-8', False),
            'assets/v1/details-all.json': artifact(b'{"materials":[]}', 'application/json; charset=utf-8', True),
            'assets/v1/report.js': artifact(b'console.log("ready")', 'application/javascript; charset=utf-8', True),
        }
        self.manifest = report.freeze_bundle(self.state, self.run, self.data, self.entry,
                                              [self.entry], self.bundle, 'v1')
        self.pub = FakePublisher()

    def publish(self):
        with patch('builtins.print'):
            return report.publish_pending(self.state, self.pub)

    def test_frozen_payloads_gzip_and_cache_headers(self):
        for item in self.manifest['objects']:
            raw = (self.run / item['raw_file']).read_bytes()
            encoded = (self.run / item['stored_file']).read_bytes()
            self.assertEqual(gzip.decompress(encoded), raw)
            self.assertEqual(encoded, gzip.compress(raw, compresslevel=6, mtime=0))
            self.assertEqual(item['metadata']['CacheControl'], report.IMMUTABLE_CACHE if item['immutable'] else report.NO_CACHE)
        self.assertFalse(self.pub.puts)

    def test_order_all_resources_verified_before_latest_and_redirect_last(self):
        status = self.publish()
        ranks = [report.phase(key) for key in self.pub.puts if not key.endswith('/status.json')]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(self.pub.puts[-2], report.LEGACY_INDEX)
        self.assertEqual(status['report_date'], self.date)
        self.assertFalse((self.state / 'pending.json').exists())
        self.assertEqual(report.load_json(self.state / 'history.json'), [self.entry])

    def test_partial_unknown_replays_exact_without_reupload_or_query(self):
        self.pub.fail_key = report.PREFIX + '/index.html'
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            self.publish()
        puts = list(self.pub.puts)
        self.assertTrue((self.state / 'pending.json').exists())
        self.assertFalse((self.state / 'history.json').exists())
        self.pub.fail_key = None
        with patch('report.collect_lineage', side_effect=AssertionError('must_not_requery')):
            self.publish()
        for key in puts:
            self.assertEqual(self.pub.puts.count(key), 1)

    def test_immutable_archive_conflict_prevents_any_write(self):
        key = report.PREFIX + '/daily/' + self.date + '/index.html'
        self.pub.objects[key] = dict(data=b'prior', metadata={})
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.publish()
        self.assertFalse(self.pub.puts)

    def test_metadata_conflict_on_immutable_is_not_overwritten(self):
        item = self.manifest['objects'][0]
        self.pub.objects[item['key']] = dict(data=(self.run / item['stored_file']).read_bytes(), metadata={})
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.publish()
        self.assertFalse(self.pub.puts)

    def test_tampering_raw_stored_and_manifest_is_rejected(self):
        for filename in (self.manifest['objects'][0]['raw_file'], self.manifest['objects'][0]['stored_file'], 'manifest.json'):
            path = self.run / filename
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.publish()
            path.write_bytes(original)
        self.assertFalse(self.pub.puts)

    def test_older_pending_never_replaces_newer_local_or_remote(self):
        report.write_json(self.state / 'history.json', [dict(date='2026-10-22')])
        with self.assertRaisesRegex(ValueError, 'older_than_latest'):
            self.publish()
        (self.state / 'history.json').unlink()
        self.pub.objects[report.PREFIX + '/status.json'] = dict(data=b'{"report_date":"2026-10-22"}', metadata={})
        with self.assertRaisesRegex(ValueError, 'older_than_latest'):
            self.publish()
        self.assertFalse(self.pub.puts)

    def test_pending_recovery_tolerates_failed_live_health(self):
        self.pub.fail_key = report.PREFIX + '/index.html'
        with self.assertRaises(RuntimeError):
            self.publish()
        self.pub.fail_key = None
        report.publish_health(self.pub, dict(state='failed', report_date='2026-10-20', error_type='RuntimeError'))
        self.publish()
        self.assertEqual(report.object_json(self.pub.get(report.PREFIX + '/status.json'))['state'], 'published')

    def test_public_history_regression_rejected(self):
        self.pub.objects[report.PREFIX + '/history.json'] = dict(data=b'[{"date":"2026-10-20"}]', metadata={})
        with self.assertRaisesRegex(ValueError, 'history_regression'):
            self.publish()
        self.assertFalse(self.pub.puts)

    def test_partial_recovery_refuses_external_mutable_drift(self):
        self.pub.fail_key = report.PREFIX + '/index.html'
        with self.assertRaises(RuntimeError):
            self.publish()
        self.pub.fail_key = None
        self.pub.objects[report.PREFIX + '/index.html']['metadata']['CacheControl'] = 'drift'
        puts = list(self.pub.puts)
        with self.assertRaisesRegex(ValueError, 'drift'):
            self.publish()
        self.assertEqual(puts, self.pub.puts)

    def test_rollback_restores_exact_bytes_and_headers_preserves_archives(self):
        old = dict(data=b'<html>old homepage</html>', metadata=dict(ContentType='text/html', CacheControl='max-age=60'))
        self.pub.objects[report.LEGACY_INDEX] = copy.deepcopy(old)
        status = self.publish()
        with patch('builtins.print'):
            result = report.restore_public(self.state, self.pub, status['backup'])
        self.assertEqual(self.pub.objects[report.LEGACY_INDEX], old)
        self.assertTrue(result['immutable_objects_preserved'])
        self.assertIn(report.PREFIX + '/daily/' + self.date + '/index.html', self.pub.objects)

    def test_rollback_preflights_every_object_before_restoring(self):
        self.pub.objects[report.LEGACY_INDEX] = dict(data=b'old', metadata={})
        status = self.publish()
        self.pub.objects[report.PREFIX + '/index.html']['data'] = b'drift'
        puts = list(self.pub.puts)
        with self.assertRaisesRegex(ValueError, 'drift'):
            report.restore_public(self.state, self.pub, status['backup'])
        self.assertEqual(puts, self.pub.puts)

    def test_prefix_guard_allows_only_exact_legacy_entry(self):
        for key in ('other/index.html', report.PREFIX + '/../secret', report.PREFIX + '//index.html',
                    report.PREFIX + '/x\\y', report.PREFIX + '/%2e%2e/x',
                    'codex-artifacts/html/dramawave-ai-image-daily/daily/2026-10-18/index.html'):
            with self.assertRaises(ValueError):
                report.check_key(key)
        report.check_key(report.LEGACY_INDEX)
        self.assertIn(b'+location.hash', report.legacy_redirect())

    def test_prepare_only_failure_has_no_public_mutation(self):
        class Args:
            prepare_only = True
            restore_public = False
        with patch('report.Publisher', side_effect=AssertionError('no_network')):
            report.publish_failure(Args(), {'at': 'now', 'error_type': 'RuntimeError'})


class ThumbnailTests(unittest.TestCase):
    def make_report(self):
        views = {key: dict(materials=[]) for key in ('all',) + thumbnails.PRODUCTS}
        for product in thumbnails.PRODUCTS:
            for index in range(150):
                row = dict(id=product + '_' + str(index), source_product_id=product,
                    created_at='2026-10-21 01:00:00', spend=150-index,
                    url='https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/x.jpg')
                views['all']['materials'].append(row)
                views[product]['materials'].append(copy.deepcopy(row))
        return dict(report_date='2026-10-21', views=views)

    def test_per_product_limit_priority_and_actual_delivery_scope(self):
        data = self.make_report()
        cross = dict(id='cross', source_product_id='2373', created_at='2026-10-01', spend=999, url='https://x')
        data['views']['1479']['materials'].append(cross)
        chosen = thumbnails.choose_materials(data)
        self.assertEqual({key: len(rows) for key, rows in chosen.items()}, {'1479': 100, '2373': 100, '3360': 100})
        self.assertEqual(chosen['1479'][0]['id'], 'cross')

    def test_only_allowlisted_https_hosts_no_userinfo_or_ports(self):
        good = 'advertising-1306474899.cos.ap-hongkong.myqcloud.com'
        self.assertEqual(thumbnails.normalize_url('http://' + good + '/a.jpg?q=secret#x'), 'https://' + good + '/a.jpg')
        for value in ('http://127.0.0.1/a', 'https://example.com/a', 'https://user@' + good + '/a',
                      'https://' + good + ':4430/a', 'file:///etc/passwd', 'https://' + good + '.evil.test/a'):
            with self.assertRaises(ValueError):
                thumbnails.normalize_url(value)

    def test_real_thumbnail_dimensions_dedup_cache_and_decorate(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow unavailable in this runtime')
        buffer = io.BytesIO()
        Image.new('RGB', (1000, 600), 'red').save(buffer, 'PNG')
        calls = []
        def fetch(url, deadline):
            calls.append(url)
            return buffer.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            data = self.make_report()
            artifacts, status = thumbnails.prepare_thumbnails(data, folder, 'https://report.test', fetch=fetch)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(artifacts), 1)
            blob = next(iter(artifacts.values()))['data']
            with Image.open(io.BytesIO(blob)) as image:
                self.assertLessEqual(max(image.size), 160)
                self.assertEqual(image.format, 'WEBP')
            self.assertTrue(data['views']['all']['materials'][0]['thumbnail_url'])
            self.assertEqual(data['views']['all']['materials'][-1]['thumbnail_url'], '')
            _, second = thumbnails.prepare_thumbnails(self.make_report(), folder, 'https://report.test', fetch=fetch)
            self.assertEqual(len(calls), 1)
            self.assertEqual(second['cached'], 1)

    def test_zero_budget_never_downloads_and_records_placeholder(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('thumbnails.download', side_effect=AssertionError('no_network')):
                artifacts, status = thumbnails.prepare_thumbnails(self.make_report(), folder, 'https://report.test', budget=0)
            self.assertEqual(artifacts, {})
            self.assertTrue(status['failures'])
            self.assertLess(status['elapsed_seconds'], 1)


if __name__ == '__main__':
    unittest.main()
