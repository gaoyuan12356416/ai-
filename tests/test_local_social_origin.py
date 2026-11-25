import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from features.gpu_offload.cache import Response
from features.gpu_offload.gateway import Gateway
from features.gpu_offload.local_social import LocalReports


def response(status, value):
    return Response(status, {'Content-Type': 'application/json'}, json.dumps(value).encode())


class OriginTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        nav = root / 'nav'
        nav.write_text(json.dumps([{'items': [{'key': 'socialAnalytics', 'module': 'social_analytics'}]}]))
        sync = root / 'sync'
        sync.write_text('{"version":"v"}')
        self.config = root / 'cfg'
        self.settings = dict(api_port=1, reader_port=2, report_percentage=50, navigation_path=str(nav), sync_state_path=str(sync))
        self.config.write_text(json.dumps(self.settings))
        self.local = Mock(paths=LocalReports.paths)
        self.local.render.return_value = response(200, {'marker': 'computed'})
        self.primary = 200
        self.actor = {'user_id': 'a', 'tenant_key': 't', 'role': 'user', 'permissions': {'social_analytics': True}}
        self.calls = []

        def route(port, target, **kwargs):
            self.calls.append((port,target,kwargs))
            if target.startswith('/api/social-analytics/refresh?'):
                return response(self.primary, {'state': 'complete', 'version': 'v'})
            if target == '/api/auth/status':
                return response(200, {'authenticated': True, 'user': self.actor})
            return response(200, {'marker': 'standard'})

        patched = patch('features.gpu_offload.gateway.request', side_effect=route)
        patched.start()
        self.addCleanup(patched.stop)
        self.server = Gateway(('127.0.0.1', 0), self.config, 'test-only', self.local)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, target='/api/social-analytics/report', chance=99, cookie='session=opaque'):
        with patch('features.gpu_offload.gateway.random.randrange', return_value=chance):
            conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
            try:
                conn.request('GET',target,headers={'Cookie':cookie,'Authorization':'Bearer forged','X-Tenant':'wrong'})
                reply = conn.getresponse()
                return reply.status, json.loads(reply.read())
            finally:
                conn.close()

    def test_local_scope_primary_permission_and_fresh_actor(self):
        self.assertEqual(self.get(),(200,{'marker':'computed'}))
        self.assertEqual([v[1] for v in self.calls],['/api/social-analytics/refresh?after=','/api/auth/status'])
        self.assertTrue(all(v[2]['cookie']=='session=opaque' and 'token' not in v[2] and 'body' not in v[2] for v in self.calls))
        self.assertEqual(self.local.render.call_args.args[0], self.actor)
        self.primary = 403
        self.assertEqual(self.get()[0],403)
        self.primary = 200
        self.actor['permissions'] = {}
        self.assertEqual(self.get()[0],403)
        self.local.render.assert_called_once()

    def test_only_existing_local_share_and_safe_fallback(self):
        self.assertEqual(self.get(chance=0)[0],200)
        self.assertEqual([v[1] for v in self.calls],['/api/auth/status','/v1/query'])
        self.local.render.assert_not_called()
        self.local.compatible.return_value=False
        self.calls.clear()
        self.assertEqual(self.get()[1]['marker'],'standard')
        self.assertEqual([v[1] for v in self.calls],['/api/auth/status','/api/social-analytics/report'])
        self.local.compatible.return_value=True
        self.local.render.return_value=None
        self.assertEqual(self.get('/api/social-analytics/export.csv')[1]['marker'],'standard')
        self.assertEqual(self.get('/api/social-analytics/options')[1]['marker'],'standard')
        self.assertEqual(self.get('/api/pipeline-monitor/summary')[0],404)
        self.assertEqual(self.get(cookie='')[0],401)
