import ast
from contextlib import closing
import inspect
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from features.x_posts import service as posts, duration_policy
from features.x_posts import shared_short_links as links
from scripts.deploy_x_shared_short_links import patch_native, patch_service
from scripts import deploy_x_shared_short_links as deployment
from scripts.test_x_posts import candidate

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_health_waits_for_simple_service_to_bind(self):
        ready = mock.MagicMock()
        ready.__enter__.return_value.status = 200
        with mock.patch.object(deployment.urllib.request, 'urlopen', side_effect=[OSError('not bound yet'), ready]) as request, \
                mock.patch.object(deployment.time, 'sleep'):
            deployment.health(8821)
        self.assertEqual(request.call_count, 2)

    def test_start_readiness_runs_after_temporary_gate_is_removed(self):
        commands = []
        def run(*args, **kwargs):
            commands.append(args)
            return '123' if args[:2] == ('systemctl', 'show') else ''
        with mock.patch.object(deployment, 'run', side_effect=run), mock.patch.object(deployment, 'health'):
            deployment.switch_unit('test.service', 8810, lambda: None)
        stopped = commands.index(('systemctl', 'stop', 'test.service'))
        ungated = next(i for i,c in enumerate(commands) if c[:3] == ('iptables','-D','INPUT'))
        started = commands.index(('systemctl', 'start', 'test.service'))
        self.assertLess(stopped, ungated)
        self.assertLess(ungated, started)

    def native_source(self):
        path = ROOT / 'features/pipeline_dispatch/x_native.py'
        if path.exists():
            return path.read_text(encoding='utf-8')
        # Local read-only production snapshots; server staging has its own copy.
        path = ROOT.parent / 'live/standard/features/pipeline_dispatch/x_native.py'
        if path.exists():
            return patch_native(path.read_text(encoding='utf-8'))
        self.skipTest('canonical runtime not available in this checkout')

    def test_source_patch_matches_maintained_service(self):
        text = (ROOT / 'features/x_posts/service.py').read_text(encoding='utf-8')
        self.assertEqual(text.count('allocated = allocate(base, log_id)'), 1)
        self.assertEqual(text.count('allocated = write_allocated('), 1)
        self.assertEqual(text.count('def _write_short_redirect_file('), 1)

    def test_native_durable_redirect_precedes_freeze_and_old_log_unchanged(self):
        source = self.native_source()
        node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == '_links')
        module = ast.Module(body=[node], type_ignores=[])
        namespace = dict(posts=posts, duration_policy=duration_policy, os=os, time=time, AdapterError=RuntimeError)
        exec(compile(module, 'native-links-test', 'exec'), namespace)
        method = namespace['_links']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shared = root / 'shared'
            links.initialize(shared)
            db = root / 'posts.sqlite3'
            store = posts.XPostStore(db)
            queue = store.enqueue(candidate())
            log = store.reserve_log(queue['id'])
            runtime = SimpleNamespace(POST_DB_PATH=db, POST_SHORT_BASE_URL=posts.DEFAULT_SHORT_BASE_URL,
                POST_PUBLIC_ROOT=root/'legacy', POST_STORAGE_MOUNT_ROOT=root,
                POST_STORAGE_ROOT=root, preflight_post_storage_request=lambda: None)
            bridge = SimpleNamespace(store=store, r=runtime)
            original = posts.build_post_text
            def build(*args, **kwargs):
                if 'copy_contract_version' not in inspect.signature(original).parameters:
                    kwargs.pop('copy_contract_version', None)
                return original(*args, **kwargs)
            with mock.patch.dict(os.environ, {'X_POST_SHARED_SHORT_ROOT':str(shared), 'X_POST_SHARED_SHORT_ALLOCATE':'1',
                                              'X_POST_ATTRIBUTION_V2_ENABLED':'0'}), \
                    mock.patch.object(posts, 'build_post_text', side_effect=build):
                with mock.patch.object(posts, 'write_short_redirect', side_effect=RuntimeError('disk fault')):
                    with self.assertRaises(RuntimeError):
                        method(bridge, queue, log)
                self.assertEqual(store.get_log(log['id'])['short_url'], '')
                ready = method(bridge, queue, log)
                self.assertEqual(ready['short_url'], links.PUBLIC_BASE + '/1')
                self.assertTrue((shared/'public/1.html').exists())
                self.assertEqual(method(bridge, queue, ready), ready)
                legacy = dict(ready, short_url=posts.DEFAULT_SHORT_BASE_URL + '/77.html')
                self.assertEqual(method(bridge, queue, legacy), legacy)
                with closing(sqlite3.connect(shared/'links.sqlite3')) as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM short_link').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
