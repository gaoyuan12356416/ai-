"""Mixed Page-pool template saves with isolated storage and no publishing."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from http.server import ThreadingHTTPServer

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore
from features.fb_auto_posts.repositories import PageGroup
from features.fb_auto_posts.service import Handler, Runtime, ServiceError
from scripts.test_fb_auto_validation import payload


class MixedPoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.actor = ActorScope('tester', 'Tester', False, '7')
        self.groups = [
            PageGroup('1', '7', 0, 'Drama', '1479', 'Dramawave', 2, 2),
            PageGroup('2', '7', 1, 'Mobo', '2373', 'MoboShort', 3, 3),
            PageGroup('3', '7', 0, 'Unlabelled', '', '', 1, 1),
            PageGroup('4', '8', 0, 'Other owner', '1479', 'Dramawave', 1, 1),
        ]
        self.store = FBAutoPostStore(Path(self.directory.name) / 'test.sqlite3')
        pages = SimpleNamespace(list_groups=lambda **scope: [
            g for g in self.groups if scope['is_admin'] or g.owner_user_id == scope['owner_user_id']])
        self.runtime = Runtime(self.store, pages, None, SimpleNamespace(live_enabled=False),
                               SimpleNamespace(), 'test-token', prebuild_enabled=False)

    def test_pool_product_does_not_choose_content_source(self):
        for groups in (['1'], ['1', '2'], ['2'], ['3'], ['1', '2', '3']):
            with self.subTest(groups=groups):
                source = self.runtime.resolve_source({'group_ids': groups}, self.actor)
                self.assertEqual(source, {'app_id': '1479', 'product': 'Dramawave',
                    'material_data_source': 6, 'metric_product': 'Dramawave', 'metric_platform': 0})

    def test_owner_and_missing_pool_checks_remain(self):
        for groups in (['1', '4'], ['99']):
            with self.subTest(groups=groups), self.assertRaises(ServiceError) as caught:
                self.runtime.resolve_source({'group_ids': groups}, self.actor)
            self.assertEqual(caught.exception.code, 'fb_auto_page_group_not_found')
        admin = ActorScope('admin', 'Admin', True, '')
        self.assertEqual(self.runtime.resolve_source({'group_ids': ['1', '4']}, admin)['app_id'], '1479')

    def test_empty_selection_is_invalid(self):
        for groups in ([], None, '1'):
            with self.subTest(groups=groups), self.assertRaises(ServiceError) as caught:
                self.runtime.resolve_source({'group_ids': groups}, self.actor)
            self.assertEqual(caught.exception.code, 'invalid_request')

    def test_create_and_update_http_routes_preserve_source_and_do_not_publish(self):
        handler = type('TestHandler', (Handler,), {'runtime': self.runtime})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = build_opener(ProxyHandler({}))

        def post(path, value):
            body = {**value, '_actor': vars(self.actor)}
            request = Request('http://127.0.0.1:%s%s' % (server.server_port, path),
                data=json.dumps(body).encode(), headers={'Authorization': 'Bearer test-token',
                'Content-Type': 'application/json'})
            try:
                response = opener.open(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)

        raw = {**payload(), 'group_ids': ['1', '2'], 'material_product': 'DramawaveSource',
               'app_id': '2373', 'product': 'MoboShort'}
        status, created = post('/api/admin/fb-auto-publish/templates', raw)
        self.assertEqual(status, 200, created)
        template = created['template']
        self.assertEqual(template['status'], 'disabled')
        self.assertEqual(template['config']['group_ids'], ['1', '2'])
        self.assertEqual(template['config']['app_id'], '1479')
        self.assertEqual(template['config']['material_product'], 'DramawaveSource')
        route = '/api/admin/fb-auto-publish/templates/%s' % template['id']
        status, updated = post(route, {**raw, 'group_ids': ['2', '3'], 'expected_version': 1})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated['template']['version'], 2)
        self.assertEqual(updated['template']['config']['group_ids'], ['2', '3'])
        status, stale = post(route, {**raw, 'expected_version': 1})
        self.assertEqual(status, 409, stale)
        self.assertEqual(stale['code'], 'fb_auto_template_version_conflict')
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fb_auto_task').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
