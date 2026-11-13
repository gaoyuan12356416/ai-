"""Legacy queue flags must not stop native template activation or planning."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, ProxyHandler, build_opener

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore
from features.fb_auto_posts.repositories import PagePoolRepository
from features.fb_auto_posts.service import Handler, Runtime, ServiceError
from scripts.test_fb_auto_validation import payload
from scripts.test_fb_target_midnight import Materials, Pages


class LegacyQueueIndependenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.actor = ActorScope('tester', 'Tester', False, '248')
        self.store = FBAutoPostStore(Path(self.temp.name) / 'fb.sqlite3')
        self.mysql = SimpleNamespace(select=Mock(side_effect=AssertionError('unexpected legacy SQL')))
        self.pages = PagePoolRepository(self.mysql)
        self.pages.list_pages = Pages().list_pages
        self.metrics = SimpleNamespace(load_metric_window=Mock(return_value=None))
        self.runtime = Runtime(self.store, self.pages, Materials(), SimpleNamespace(live_enabled=False),
                               SimpleNamespace(), 'test-token', metric_store=self.metrics,
                               max_publishable_pages=10, max_jobs_per_slot=10, prebuild_enabled=False)
        self.template = self.store.create_template(payload(), self.actor,
            {'app_id': '1479', 'product': 'Dramawave', 'metric_product': 'Dramawave', 'metric_platform': 0})

    def test_external_queue_is_not_queried(self):
        self.assertEqual(self.pages.legacy_conflicts(['4', '25', '62']), [])
        self.mysql.select.assert_not_called()

    def test_http_enable_then_plan_uses_same_independent_policy(self):
        handler = type('TestHandler', (Handler,), {'runtime': self.runtime})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = Request('http://127.0.0.1:%s/api/admin/fb-auto-publish/templates/%s/enable' %
            (server.server_port, self.template['id']), data=json.dumps(
            {'expected_version': 1, '_actor': vars(self.actor)}).encode(),
            headers={'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            result = json.load(response)
        self.assertEqual(result['template']['status'], 'enabled')
        self.assertEqual(result['page_summary']['publishable_pages'], 2)
        with patch('features.fb_auto_posts.core.load_profiles', return_value=({'pages': {}}, {})):
            run = self.store.create_run(self.template['id'], 'manual:test-independent', 'manual',
                                       self.actor, self.pages, Materials())
        self.assertEqual(run['summary']['queued_tasks'], 2)
        self.mysql.select.assert_not_called()
        self.metrics.load_metric_window.assert_called_once()

    def test_empty_pool_and_capacity_checks_remain(self):
        self.runtime.max_publishable_pages = 1
        with self.assertRaises(ServiceError) as caught:
            self.runtime.validate_activation(self.template)
        self.assertEqual(caught.exception.code, 'fb_auto_capacity_exceeded')
        self.pages.list_pages = lambda *_args, **_kwargs: []
        with self.assertRaises(ServiceError) as caught:
            self.runtime.validate_activation(self.template)
        self.assertEqual(caught.exception.code, 'fb_auto_page_pool_empty')


if __name__ == '__main__':
    unittest.main()
