"""Offline integration with the exact deployed coordinator adapter protocol."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.pipeline_dispatch.adapters import AdapterError, AdapterRegistry, LedgerAdapter, LedgerSpec, ClosingConnection
from features.x_tag_publish.dispatch import ID_BASE, extend_registry, label_tasks


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {}
        for lane, ident in [('legacy', 17), ('tag', ID_BASE + 17)]:
            paths = {key: str(Path(self.tmp.name) / (lane + key + '.db')) for key in ('x', 'x_auto')}
            self.paths[lane] = paths
            with sqlite3.connect(paths['x'], factory=ClosingConnection) as c:
                c.executescript('''CREATE TABLE x_post_queue(id INTEGER PRIMARY KEY,status TEXT,account_id INTEGER,
                    created_at TEXT,material_id TEXT,preflight_sha256 TEXT,preflight_size INTEGER,
                    preflight_duration REAL,random_template_enabled INTEGER,media_validation_mode TEXT);
                    CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id INTEGER,status TEXT,
                    attempt_count INTEGER,unknown_outcome INTEGER,x_post_id TEXT,started_at TEXT,published_at TEXT,updated_at TEXT);''')
                c.execute('INSERT INTO x_post_queue VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (ident, 'queued', 92, '2026-11-10T03:00:00Z', '8001018', '', 0, 0, 1, 'deferred'))
            with sqlite3.connect(paths['x_auto'], factory=ClosingConnection) as c:
                c.executescript('''CREATE TABLE x_auto_task(id INTEGER PRIMARY KEY,status TEXT,account_id INTEGER,
                    execution_queue_id INTEGER,created_at TEXT,scheduled_at_utc TEXT,template_id INTEGER,
                    claim_token TEXT,lease_expires_at_utc TEXT,updated_at TEXT,material_id TEXT);
                    CREATE TABLE x_auto_template(id INTEGER PRIMARY KEY,created_by_user_id TEXT,created_by_tenant_key TEXT);''')
        original = AdapterRegistry([LedgerAdapter(LedgerSpec('x', family, self.paths['legacy'][key], table))
            for family, key, table in [('queue', 'x', 'x_post_queue'), ('auto', 'x_auto', 'x_auto_task')]])
        config = {'paths': self.paths['legacy'], 'x_tag': {'paths': self.paths['tag'],
                  'post_token_env': 'TAG_POST', 'auto_token_env': 'TAG_AUTO'}}
        self.registry = extend_registry(original, config, {'TAG_POST': 'p' * 40, 'TAG_AUTO': 'a' * 40})

    def job(self, ident):
        return {'channel': 'x', 'family': 'queue', 'source_id': str(ident), 'source_key': 'x:queue:' + str(ident)}

    def test_real_ledger_scan_routes_disjoint_ids_and_keeps_account_resource(self):
        offers = self.registry.scan(now='2026-11-10T04:00:00Z')
        self.assertEqual(len(offers), 2)
        for offer in offers:
            adapter = self.registry._adapter(offer)
            self.assertEqual(adapter.read(offer['source_id'])['id'], int(offer['source_id']))
            self.assertEqual(offer['resource_keys'], ['x:account:92'])
            self.assertEqual(offer['stage'], 'prepare')
            self.assertEqual(offer['capabilities'], ['prepare:x'])
        tag = self.registry._adapter(self.job(ID_BASE + 17))
        legacy = self.registry._adapter(self.job(17))
        self.assertEqual(tag.driver.port, 8821)
        self.assertIsNone(legacy.driver)
        self.assertEqual(tag.spec.path, self.paths['tag']['x'])
        self.assertEqual(set(self.registry.export_cursors()), {'x:auto', 'x:queue', 'x:tag_auto', 'x:tag_queue'})

    def test_parent_join_is_tag_local_and_does_not_create_second_publication(self):
        ident = ID_BASE + 17
        with sqlite3.connect(self.paths['tag']['x_auto'], factory=ClosingConnection) as c:
            c.execute('INSERT INTO x_auto_template VALUES(?,?,?)', (ID_BASE + 1, 'owner', 'tenant'))
            c.execute('INSERT INTO x_auto_task VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (ID_BASE + 19, 'ready', 92, ident, '2026-11-10T03:00:00Z', '2026-11-10T03:00:00Z',
                 ID_BASE + 1, '', '', '2026-11-10T03:00:00Z', '8001018'))
        offers = self.registry.scan(now='2026-11-10T04:00:00Z')
        queue = next(x for x in offers if x['source_key'] == 'x:queue:' + str(ident))
        parent = next(x for x in offers if x['source_key'] == 'x:auto:' + str(ID_BASE + 19))
        self.assertEqual(queue['owner_user_id'], 'owner')
        self.assertEqual(queue['ancestor_source_keys'], [parent['source_key']])
        self.assertEqual(queue['details_url'], '/x-tag-auto-publish-runs.html')
        self.assertTrue(parent['observation_only'])
        self.assertFalse(parent['execution_available'])

    def test_unknown_and_inflight_are_never_offered_for_resend(self):
        ident = ID_BASE + 17
        with sqlite3.connect(self.paths['tag']['x'], factory=ClosingConnection) as c:
            c.execute('UPDATE x_post_queue SET status=? WHERE id=?', ('publishing', ident))
            c.execute('INSERT INTO x_post_publish_log VALUES(?,?,?,?,?,?,?,?,?)',
                      (ID_BASE + 2, ident, 'post_creating', 1, 1, '', '2026-11-10T03:01:00Z', '', '2026-11-10T03:01:00Z'))
        item = next(x for x in self.registry.scan() if x['source_id'] == str(ident))
        self.assertTrue(item['reconcile_only'])
        self.assertFalse(item['execution_available'])
        with self.assertRaisesRegex(AdapterError, 'canonical_reconciliation_required'):
            self.registry.reserve(item, {})

    def test_receipt_readback_uses_tag_database_only(self):
        ident = ID_BASE + 17
        adapter = self.registry._adapter(self.job(ident))
        with sqlite3.connect(self.paths['tag']['x'], factory=ClosingConnection) as c:
            c.execute('UPDATE x_post_queue SET status=?', ('published',))
            c.execute('INSERT INTO x_post_publish_log VALUES(?,?,?,?,?,?,?,?,?)',
                      (ID_BASE + 2, ident, 'published', 1, 0, 'real-receipt', '', '2026-11-10T03:02:00Z', '2026-11-10T03:02:00Z'))
        proof = self.registry._verified_result(adapter, dict(self.job(ident), stage='publish'), {})
        self.assertTrue(proof['published'])
        self.assertEqual(proof['canonical_receipt']['publish_id'], 'real-receipt')
        self.assertEqual(self.registry._adapter(self.job(17)).read(17)['status'], 'queued')

    def test_bad_namespace_and_database_collision_fail_locally(self):
        adapter = self.registry._adapter(self.job(ID_BASE + 17))
        with self.assertRaisesRegex(AdapterError, 'namespace'):
            adapter.project({'id': 17, 'status': 'queued'})
        with self.assertRaisesRegex(ValueError, 'collision'):
            extend_registry(AdapterRegistry(), {'paths': self.paths['tag'], 'x_tag': {'paths': self.paths['tag']}})

    def test_monitor_labels_and_legacy_rows(self):
        result = label_tasks({'items': [dict(self.job(ID_BASE + 17), detail_url='/x-tag-auto-publish-runs.html'),
                                        dict(self.job(ID_BASE + 18), detail_url='/x-tag-post-logs.html'),
                                        dict(self.job(17), source_family='queue')]})
        self.assertEqual([x['source_family'] for x in result['items']], ['标签画像自动发布', '标签画像素材池', 'queue'])


class NativeTests(unittest.TestCase):
    def test_native_attribution_uses_tag_resolver_and_exact_frozen_identity(self):
        from features.pipeline_dispatch import x_native
        from features.x_tag_publish.dispatch_native import install_attribution
        from features.x_posts.attribution import AttributionError
        queue = ID_BASE + 77
        log = {'id': ID_BASE + 78, 'queue_id': queue, 'created_at': '2026-11-10T03:00:00Z'}
        context = dict(version='x-attribution-v2', channel_id='92', sub_user_id='248', af_c_id=str(queue),
                       tenant='tenant', owner='owner', channel_name='channel', material_id='8001018',
                       material_name='video', content_id='drama', language='es', drama_name='Drama',
                       tag='none', frozen_at=log['created_at'])
        calls = []
        def resolver(queue_id, log_id):
            calls.append((queue_id, log_id))
            return dict(context)
        with patch.object(x_native, '_resolve_attribution', x_native._resolve_attribution):
            install_attribution(resolver)
            self.assertEqual(x_native._resolve_attribution(queue, log), context)
            self.assertEqual(calls, [(queue, log['id'])])
            context['af_c_id'] = '1'
            with self.assertRaises(AttributionError):
                x_native._resolve_attribution(queue, log)

    def test_native_auth_and_namespace_reject_before_any_dispatch(self):
        from features.x_tag_publish import dispatch_native
        class Handler:
            headers = {'Authorization': 'Bearer ' + 's' * 40}
            def __init__(self, ident): self.ident, self.result = ident, None
            def read_json(self, limit):
                return {'job': {'channel': 'x', 'family': 'queue', 'stage': 'publish',
                               'source_id': str(self.ident), 'source_key': 'x:queue:' + str(self.ident)}}
            def send_json(self, status, payload): self.result = status, payload
        with patch.dict('os.environ', {'PIPELINE_X_CANONICAL_TOKEN': 's' * 40, 'PIPELINE_X_CANONICAL_BRIDGE_ENABLED': '1'}):
            h = Handler(17)
            dispatch_native.handle(h, None, '/internal/pipeline-dispatch/x/reserve')
            self.assertEqual(h.result, (409, {'code': 'x_tag_source_namespace_invalid'}))
            h.headers = {'Authorization': 'Bearer invalid'}
            dispatch_native.handle(h, None, '/internal/pipeline-dispatch/x/reserve')
            self.assertEqual(h.result[0], 403)


if __name__ == '__main__':
    unittest.main()
