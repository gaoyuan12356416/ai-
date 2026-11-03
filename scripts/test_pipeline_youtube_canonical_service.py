"""Isolated canonical RPC startup, live-module binding and HTTP boundary."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from scripts import pipeline_youtube_canonical as rpc


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Mock()
        self.bridge.dispatch.return_value = {'accepted': True}
        self.token = 'test-token-only-' + 'x' * 40
        self.server = rpc.CanonicalServer(('127.0.0.1', 0), rpc.handler_type(self.bridge, self.token))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def request(self, action='reserve', *, method='POST', body=b'{}', token=None, path=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        fields = {'Authorization': 'Bearer ' + (self.token if token is None else token)}
        fields.update(headers or {})
        try:
            conn.request(method, path or rpc.PREFIX + action, None if method == 'GET' else body, fields)
            result = conn.getresponse()
            return result.status, json.loads(result.read())
        finally:
            conn.close()

    def test_health_has_no_business_action_and_all_seven_actions_route_exactly(self):
        self.assertEqual(self.request(method='GET', path='/health', token='')[0], 200)
        self.bridge.dispatch.assert_not_called()
        for action in sorted(rpc.ACTIONS):
            self.assertEqual(self.request(action, body=b'{"job":{"id":123}}'), (200, {'accepted': True}))
            self.bridge.dispatch.assert_called_with(action, {'job': {'id': 123}})
        self.assertEqual(self.bridge.dispatch.call_count, 7)

    def test_unauthorized_unknown_routes_and_invalid_json_never_dispatch(self):
        for token in ('', 'wrong', 'non-ascii-\xe9'):
            self.assertEqual(self.request(token=token)[0], 403)
        for path in ('/internal/pipeline-dispatch/x/reserve', rpc.PREFIX + 'generate', rpc.PREFIX + 'reserve?x=1'):
            self.assertEqual(self.request(path=path)[0], 404)
        for body in (b'null', b'[]', b'{bad', b'\xff'):
            self.assertEqual(self.request(body=body)[0], 400)
        self.bridge.dispatch.assert_not_called()

    def test_size_and_chunked_requests_rejected_before_reading_or_dispatch(self):
        self.assertEqual(self.request(headers={'Content-Length': str(rpc.MAX_REQUEST + 1)})[0], 413)
        self.assertEqual(self.request(headers={'Transfer-Encoding': 'chunked'})[0], 413)
        self.assertEqual(self.request(headers={'Content-Length': '-1'})[0], 413)
        self.bridge.dispatch.assert_not_called()

    def test_exception_text_and_secret_fields_never_leave_error_response(self):
        exc = RuntimeError('Bearer SECRET https://private.example/?token=SECRET')
        self.bridge.dispatch.side_effect = exc
        self.assertEqual(self.request(), (409, {'code': 'youtube_dispatch_failed'}))
        exc.code = 'youtube_preparation_version_changed'
        self.assertEqual(self.request(), (409, {'code': exc.code}))
        exc.code = 'youtube_bad?token=SECRET'
        self.assertEqual(self.request(), (409, {'code': 'youtube_dispatch_failed'}))


class LoaderTests(unittest.TestCase):
    def test_release_dispatch_uses_live_business_modules_without_starting_app(self):
        # Separate interpreter mirrors production import ordering, without the
        # unittest runner's already-imported local features package.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); live = root / 'live'; release = root / 'release'
            files = {
                live / 'app.py': "EVENTS=['app-import']\ndef main(): raise AssertionError('main called')\ndef recover_screenshots(): raise AssertionError('recovery called')\n",
                live / 'features/__init__.py': '',
                live / 'features/business.py': "VALUE='live-business'\n",
                live / 'features/pipeline_dispatch/__init__.py': "VALUE='old-live-dispatch'\n",
                live / 'features/youtube_auto_publish/__init__.py': '',
                live / 'features/youtube_auto_publish/runtime.py': "from features.business import VALUE\ndef build_service(app):\n app.EVENTS.append('build-service')\n return {'business':VALUE,'events':app.EVENTS}\n",
                release / 'features/pipeline_dispatch/__init__.py': '',
                release / 'features/pipeline_dispatch/helper.py': "VALUE='new-release-dispatch'\n",
                release / 'features/pipeline_dispatch/youtube_native.py': "from .helper import VALUE\nfrom features.business import VALUE as BUSINESS\nclass YouTubeNativeBridge:\n def __init__(self,workflow):\n  self.workflow=workflow; self.release=VALUE; self.business=BUSINESS\n",
                release / 'features/business.py': "raise AssertionError('release business imported')\n",
            }
            for path, value in files.items():
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value, encoding='utf-8')
            code = '''import importlib.util,sys,threading,socket,subprocess,json
def forbidden(*args,**kwargs): raise AssertionError('background or external work started')
threading.Thread.start=forbidden
socket.create_connection=forbidden
subprocess.Popen=forbidden
spec=importlib.util.spec_from_file_location('standalone',sys.argv[1]);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
bridge=mod.load_bridge(sys.argv[2],sys.argv[3])
import features.pipeline_dispatch as live_dispatch
assert live_dispatch.VALUE=='old-live-dispatch'
assert bridge.workflow=={'business':'live-business','events':['app-import','build-service']}
assert bridge.release=='new-release-dispatch' and bridge.business=='live-business'
print('isolated-loader-ok')
'''
            result = subprocess.run([sys.executable, '-c', code, rpc.__file__, str(live), str(release)], text=True, capture_output=True, timeout=15, cwd=tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'isolated-loader-ok')

    def test_actual_workflow_and_bridge_construction_has_no_background_or_platform_work(self):
        from features.drama_synthesis.core import DramaSynthesisStore
        from features.youtube_auto_publish.runtime import build_service
        from features.pipeline_dispatch.youtube_native import YouTubeNativeBridge
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = DramaSynthesisStore(root / 'native.sqlite3'); store.ensure_storage()
            app = types.SimpleNamespace(JOB_DB_PATH=store.db_path, DB_NAME='test', DRAMA_SYNTHESIS_STORE=store,
                                        DRAMA_SHORT_LINK_PUBLISHER=Mock(), PUBLIC_BASE_URL='https://test.example/drama-materials')
            with patch.dict(os.environ, {'YOUTUBE_AUTO_STORAGE_ROOT': str(root / 'storage'), 'YOUTUBE_AUTO_ENABLED': '1'}), \
                 patch('threading.Thread.start', side_effect=AssertionError('thread started')), \
                 patch('subprocess.Popen', side_effect=AssertionError('process started')), \
                 patch('requests.Session.request', side_effect=AssertionError('platform requested')), \
                 patch('socket.create_connection', side_effect=AssertionError('network requested')):
                workflow = build_service(app)
                bridge = YouTubeNativeBridge(workflow)
                self.assertIs(bridge.store, store)
                self.assertEqual(Path(workflow.db_path), Path(store.db_path))
                with workflow.db() as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM youtube_auto_preparation').fetchone()[0], 0)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM drama_youtube_publish').fetchone()[0], 0)
                app.DRAMA_SHORT_LINK_PUBLISHER.assert_not_called()

    def test_main_has_only_loopback_listener_and_no_implicit_worker_start(self):
        self.assertFalse(rpc.CanonicalServer.daemon_threads)
        server = Mock()
        server.__enter__ = Mock(return_value=server); server.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {'PIPELINE_YOUTUBE_CANONICAL_BRIDGE_ENABLED': '1', 'PIPELINE_YOUTUBE_CANONICAL_TOKEN': 't' * 40}), \
             patch.object(rpc, 'load_bridge', return_value=Mock()) as load, \
             patch.object(rpc, 'CanonicalServer', return_value=server) as factory, patch.object(rpc.signal, 'signal'):
            self.assertEqual(rpc.main(['--runtime-root', '/live', '--dispatch-root', '/release']), 0)
            load.assert_called_once_with('/live', '/release')
            self.assertEqual(factory.call_args.args[0], ('127.0.0.1', 18847))
            server.serve_forever.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
