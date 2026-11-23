import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

from features.gpu_offload.cache import Response, SingleFlightCache, Unavailable, annotated, cache_key
from features.gpu_offload.gateway import Gateway, authenticated_actor, navigation_allowed, Denied
from features.gpu_offload.reader import accept_snapshot
from features.gpu_offload.reports import render, validate_actor, validate_target
from tests.test_social_analytics import DAY, catalog, metric


class CacheTests(unittest.TestCase):
    def test_one_refresh_for_concurrent_requests(self):
        cache, calls = SingleFlightCache(), []
        started, release = threading.Event(), threading.Event()
        def loader():
            calls.append(1); started.set(); release.wait(2)
            return Response(200, {}, b'snapshot')
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(cache.get, 'key', loader) for _ in range(12)]
            self.assertTrue(started.wait(1)); release.set()
            self.assertTrue(all(f.result()[0].body == b'snapshot' for f in futures))
        self.assertEqual(len(calls), 1)

    def test_expired_failure_serves_old_data_with_backoff(self):
        now, attempts = [100.0], []
        cache = SingleFlightCache(ttl=10, clock=lambda: now[0])
        original = Response(200, {'Content-Type': 'application/json'}, b'{"totals":{"value":12}}')
        cache.get('k', lambda: original); now[0] += 11
        def failed(): attempts.append(1); raise OSError('offline')
        value, stale, hit = cache.get('k', failed)
        cache.get('k', failed)
        self.assertTrue(stale and hit)
        self.assertEqual(attempts, [1])
        payload = json.loads(annotated(value, stale=stale).body)
        self.assertEqual(payload['totals']['value'], 12)
        self.assertTrue(payload['warnings'])

    def test_cold_failure_remains_unknown_and_backs_off(self):
        cache, calls = SingleFlightCache(), []
        def failed(): calls.append(1); raise OSError('timeout')
        with self.assertRaises(OSError): cache.get('k', failed)
        with self.assertRaises(Unavailable): cache.get('k', failed)
        self.assertEqual(calls, [1])

    def test_scope_and_version_are_part_of_key(self):
        actor = dict(tenant_key='t', user_id='a', role='user', permissions={'social_analytics': True})
        key = cache_key(actor, '/api/social-analytics/report', 'v1')
        for field, value in [('tenant_key', 'other'), ('user_id', 'b'), ('role', 'admin'), ('permissions', {'social_analytics': False})]:
            self.assertNotEqual(key, cache_key(dict(actor, **{field: value}), '/api/social-analytics/report', 'v1'))
        self.assertNotEqual(key, cache_key(actor, '/api/social-analytics/report', 'v2'))

    def test_cache_memory_is_bounded(self):
        cache = SingleFlightCache(max_bytes=10, max_entries=2)
        for key in 'abc': cache.get(key, lambda: Response(200, {}, b'12345'))
        self.assertEqual(cache.size, 10)
        self.assertEqual(list(cache.entries), ['b', 'c'])


class AuthorizationTests(unittest.TestCase):
    def test_disabled_auth_without_real_user_cannot_read(self):
        with self.assertRaises(Denied): authenticated_actor({'enabled': False, 'authenticated': True, 'user': None})

    def test_revoked_permission_is_checked_against_current_payload(self):
        user = dict(tenant_key='t', user_id='a', role='user', permissions={'social_analytics': True})
        navigation = [{'items': [{'key': 'socialAnalytics', 'module': 'social_analytics'}]}]
        first = authenticated_actor({'authenticated': True, 'user': user})
        self.assertTrue(navigation_allowed(first, navigation, 'socialAnalytics'))
        second = authenticated_actor({'authenticated': True, 'user': dict(user, permissions={'social_analytics': False})})
        self.assertFalse(navigation_allowed(second, navigation, 'socialAnalytics'))
        self.assertNotEqual(cache_key(first, 'report', 'v1'), cache_key(second, 'report', 'v1'))

    def test_disabled_navigation_does_not_allow_admin(self):
        self.assertFalse(navigation_allowed({'role': 'admin'}, [{'enabled': False, 'items': [{'key': 'socialAnalytics'}]}], 'socialAnalytics'))

    def test_internal_actor_cannot_supply_collector_capability_or_arbitrary_path(self):
        actor = dict(tenant_key='t', user_id='a', role='admin', _system_report=True)
        with self.assertRaises(ValueError): validate_actor(actor)
        for target in ['/v1/sql', '/api/social-analytics/report#fragment', 'http://elsewhere/api/social-analytics/report']:
            with self.assertRaises(ValueError): validate_target(target)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        root = Path(self.temp.name)
        self.user = dict(tenant_key='t', user_id='alice', role='user', permissions={'social_analytics': True})
        self.auth_calls, self.query_calls, self.failed = 0, 0, False
        owner = self
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                owner.auth_calls += 1
                data = json.dumps({'authenticated': True, 'user': owner.user}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(data)
            def do_POST(self):
                owner.query_calls += 1
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(503 if owner.failed else 200)
                self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'visible_owner': value['actor']['user_id']}).encode())
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        navigation = root/'navigation.json'; navigation.write_text(json.dumps([{'items': [{'key': 'socialAnalytics', 'module': 'social_analytics'}]}]))
        state = root/'sync.json'; state.write_text('{"version":"v1"}')
        config = root/'gateway.json'
        config.write_text(json.dumps({'api_port': self.upstream.server_port, 'reader_port': self.upstream.server_port,
                                      'navigation_path': str(navigation), 'sync_state_path': str(state), 'report_percentage': 100}))
        self.gateway = Gateway(('127.0.0.1', 0), config, 'internal-token'*4)
        self.threads = []
        for server in (self.upstream, self.gateway):
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); self.threads.append(thread)

    def tearDown(self):
        for server in (self.gateway, self.upstream): server.shutdown(); server.server_close()
        for thread in self.threads: thread.join()
        self.temp.cleanup()

    def read(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port)
        try:
            connection.request('GET', '/api/social-analytics/report', headers={'Cookie': 'test_session=real_cookie_only'})
            response = connection.getresponse(); return response.status, json.loads(response.read())
        finally: connection.close()

    def test_revocation_is_checked_before_cached_response(self):
        self.assertEqual(self.read()[0], 200); self.assertEqual(self.read()[0], 200)
        self.assertEqual(self.query_calls, 1)
        self.user['permissions']['social_analytics'] = False
        self.assertEqual(self.read()[0], 403)
        self.assertEqual(self.auth_calls, 3)
        self.assertEqual(self.query_calls, 1)

    def test_changed_owner_never_receives_previous_owners_cache(self):
        self.assertEqual(self.read()[1]['visible_owner'], 'alice')
        self.user['user_id'] = 'bob'
        self.assertEqual(self.read()[1]['visible_owner'], 'bob')
        self.assertEqual(self.query_calls, 2)

    def test_gpu_failure_retains_successful_snapshot(self):
        self.assertEqual(self.read()[0], 200)
        self.gateway.report_cache.ttl = -1; self.failed = True
        status, value = self.read()
        self.assertEqual(status, 200)
        self.assertEqual(value['visible_owner'], 'alice')
        self.assertTrue(value['meta']['offload_cache_stale'])
        self.assertTrue(value['warnings'])
        self.read(); self.assertEqual(self.query_calls, 2)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        self.root = Path(self.temp.name).resolve()
        (self.root/'snapshots').mkdir()
        self.version = '20261123T000000Z'
        path = self.root/'snapshots'/(self.version+'.sqlite3')
        with closing(sqlite3.connect(path)) as db:
            db.executescript('CREATE TABLE catalog(row_key TEXT PRIMARY KEY,payload TEXT);'
                             'CREATE TABLE effects(platform TEXT,dt TEXT,row_key TEXT,payload TEXT);'
                             'CREATE TABLE titles(content_id TEXT PRIMARY KEY,payload TEXT);')
            for campaign, owner, tenant, cents in [('a', 'alice', 't', 100), ('b', 'bob', 't', 200), ('c', 'alice', 'other', 900)]:
                row = catalog(campaign=campaign, owner=owner, tenant=tenant, link_type='manual')
                value = metric(campaign=campaign, cents=cents, site_id=row['site_id'])
                db.execute('INSERT INTO catalog VALUES(?,?)', (campaign, json.dumps(row)))
                db.execute('INSERT INTO effects VALUES(?,?,?,?)', ('youtube', DAY, campaign, json.dumps(value)))
            db.commit()
        self.manifest = {'version': self.version, 'database': 'snapshots/'+path.name,
                         'generated_at': datetime.now(timezone.utc).isoformat(), 'start': DAY, 'end': DAY,
                         'coverage': {'youtube:2': [DAY]}, 'catalog_errors': {}, 'source_metadata': {}}
        (self.root/'current.json').write_text(json.dumps(self.manifest))
        self.path = path

    def tearDown(self): self.temp.cleanup()

    def test_report_options_and_export_do_not_query_sql(self):
        actor = dict(tenant_key='t', user_id='alice', role='user')
        with patch('features.social_analytics.routes.query_rows', side_effect=AssertionError('SQL forbidden')) as sql:
            for target in ['/api/social-analytics/options', '/api/social-analytics/report?start='+DAY+'&end='+DAY+'&platform=youtube', '/api/social-analytics/export.csv?start='+DAY+'&end='+DAY+'&platform=youtube']:
                value = render(self.root, actor, target)
                self.assertEqual(value.status, 200, value.body)
            sql.assert_not_called()

    def test_report_preserves_tenant_and_owner_boundary(self):
        target = '/api/social-analytics/report?start='+DAY+'&end='+DAY+'&platform=youtube'
        for user, tenant, expected in [('alice', 't', 100), ('bob', 't', 200), ('alice', 'other', 900)]:
            payload = json.loads(render(self.root, dict(tenant_key=tenant, user_id=user, role='user'), target).body)
            self.assertEqual(payload['totals']['revenue_cents'], expected)
            self.assertEqual(payload['meta']['warehouse_version'], self.version)

    def test_one_request_pins_one_immutable_version(self):
        actor = dict(tenant_key='t', user_id='alice', role='user')
        with patch('features.social_analytics.warehouse_reader.current', side_effect=[(self.manifest, self.path)]) as pointer:
            result = render(self.root, actor, '/api/social-analytics/report?start='+DAY+'&end='+DAY+'&platform=youtube')
            self.assertEqual(result.status, 200)
            self.assertEqual(pointer.call_count, 1)

    def packet(self, manifest=None):
        raw = json.dumps(manifest or self.manifest).encode(); data = self.path.read_bytes()
        return struct.pack('!I', len(raw)) + raw + data, hashlib.sha256(data).hexdigest()

    def test_partial_or_corrupt_upload_preserves_successful_snapshot(self):
        before = (self.root/'current.json').read_bytes()
        packet, digest = self.packet()
        with self.assertRaises(ValueError): accept_snapshot(self.root, io.BytesIO(packet[:-10]), len(packet), digest)
        with self.assertRaises(ValueError): accept_snapshot(self.root, io.BytesIO(packet), len(packet), 'a'*64)
        self.assertEqual((self.root/'current.json').read_bytes(), before)
        self.assertFalse(list((self.root/'snapshots').glob('.incoming-*')))

    def test_snapshot_path_escape_rejected(self):
        packet, digest = self.packet(dict(self.manifest, database='../outside.sqlite3'))
        with self.assertRaises(ValueError): accept_snapshot(self.root, io.BytesIO(packet), len(packet), digest)

    def test_same_immutable_version_accepts_only_identical_content(self):
        packet, digest = self.packet()
        self.assertEqual(accept_snapshot(self.root, io.BytesIO(packet), len(packet), digest)['version'], self.version)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE changed(value TEXT)'); db.commit()
        with self.assertRaises(ValueError): accept_snapshot(self.root, io.BytesIO(packet), len(packet), digest)


if __name__ == '__main__': unittest.main()
