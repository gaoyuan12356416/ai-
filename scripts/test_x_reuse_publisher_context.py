"""Publisher permission regression, explicit errors, and unchanged duplicate guards."""
import ast
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.x_material_effects import reuse
from features.x_posts.service import XPostError, XPostStore
from scripts.test_x_post_priority_manual_store import material_candidate


def source(tid=2, account='3', pool=0):
    return dict(id=tid, run_id=7, account_id=account, material_id='701',
                _pool_run=pool, _effect_policy=True, _lane='tag')


class PublisherContextTests(unittest.TestCase):
    def test_context_is_read_only_and_contains_only_identity(self):
        frozen = dict(source(), account_snapshot_json='private', selection_json='private')
        with patch.object(reuse, 'task', return_value=frozen), patch.object(reuse.sqlite3, 'connect', side_effect=AssertionError('no grant writes')):
            result = reuse.handle(dict(action='context', lane='tag', task_id=2))
        self.assertEqual(result, dict(ok=True, item=source()))

    def test_publisher_never_reads_auto_database(self):
        with patch.object(reuse, 'client', return_value=dict(ok=True, item=source(pool=7))), patch.object(reuse, 'readonly', side_effect=PermissionError('private auto database')):
            self.assertEqual(reuse.pool_scope(dict(lane='tag', task_id=2)), 'tag:7')
            first = reuse.publisher_task('tag', 2)
            with patch.object(reuse, 'client', return_value=dict(ok=True, item=source(3, '4', 7))):
                self.assertTrue(reuse.same_pool(first, 'tag', 3, '4'))
                self.assertFalse(reuse.same_pool(first, 'tag', 3, '3'))

    def test_context_identity_mismatch_is_rejected(self):
        with patch.object(reuse, 'client', return_value=dict(ok=True, item=source(99))):
            with self.assertRaises(XPostError) as caught:
                reuse.publisher_task('tag', 2)
        self.assertEqual(caught.exception.code, 'x_effect_context_invalid')

    def test_errors_do_not_masquerade_as_account_service_or_leak_details(self):
        cases = [(URLError('private-url-and-secret'), 'x_effect_context_unavailable', 503),
                 (HTTPError('private', 401, 'secret', {}, io.BytesIO(b'secret')), 'x_effect_broker_auth_failed', 503),
                 (HTTPError('private', 400, 'secret', {}, io.BytesIO(b'secret')), 'x_effect_context_invalid', 409),
                 (RuntimeError('x_effect_material_reserved'), 'x_effect_material_reserved', 409)]
        for exc, code, status in cases:
            with self.subTest(code=code), patch.object(reuse, 'client', side_effect=exc):
                with self.assertRaises(XPostError) as caught:
                    reuse.publisher_request('execution', lane='tag', task_id=2)
                self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
                self.assertNotIn('secret', str(caught.exception))
                self.assertFalse(caught.exception.unknown_outcome)

    def test_handler_preserves_specific_error_before_creating_run(self):
        code = ast.parse((ROOT/'features/x_accounts/oauth_service.py').read_text(encoding='utf-8'))
        selected = [node for node in code.body if isinstance(node, ast.FunctionDef) and node.name in
                    ('create_post_auto_template_run_request', '_raise_x_post_error')]
        store = Mock()
        class ServiceError(XPostError):
            pass
        env = dict(ServiceError=ServiceError, AUTO_TEMPLATE_ACTOR_LABEL='x_auto_post_service',
                   AUTO_TEMPLATE_ACTOR={}, POST_DB_PATH='unused', clean_text=lambda v, *a: v,
                   _manual_publish_accounts=lambda ids: [dict(id=3)],
                   _x_posts_api=lambda: (XPostError, store, None))
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'handler', 'exec'), env)
        with patch.object(reuse, 'client', side_effect=URLError('private')):
            with self.assertRaises(ServiceError) as caught:
                env['create_post_auto_template_run_request'](dict(actor='x_auto_post_service', account_id=3,
                    material_id='701', reuse_task=dict(lane='tag', task_id=2)))
        self.assertEqual(caught.exception.code, 'x_effect_context_unavailable')
        store.assert_not_called()

    def test_resume_creates_exactly_one_run_and_one_queue(self):
        actor = dict(user_id='admin', name='Admin')
        def broker(action, **payload):
            return dict(ok=True, item=source()) if action == 'context' else dict(ok=True)
        with tempfile.TemporaryDirectory() as directory, patch.object(reuse, 'client', side_effect=broker), patch.object(reuse, 'readonly', side_effect=PermissionError('auto DB forbidden')):
            path = Path(directory)/'posts.sqlite3'
            publisher = XPostStore(path)
            args = ('701', 3, 'x-auto-task-2', 'template', 1, '{{drama_name}}\n{{desc}}', actor)
            first = publisher.create_auto_template_run(*args, reuse_context=dict(lane='tag', task_id=2))
            again = publisher.create_auto_template_run(*args, reuse_context=dict(lane='tag', task_id=2))
            self.assertEqual(first['id'], again['id'])
            candidate = material_candidate(first, 3, '701')
            plan = publisher.create_manual_plan(first['id'], [candidate], 'auto_template')
            replay = publisher.create_manual_plan(first['id'], [candidate], 'auto_template')
            self.assertEqual(plan['queues'][0]['id'], replay['queues'][0]['id'])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM x_post_manual_run').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM x_post_queue').fetchone()[0], 1)

    def test_same_account_and_unknown_or_active_publications_still_block(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.executescript('''
                CREATE TABLE x_post_material_pool(id INTEGER,material_key TEXT);
                CREATE TABLE x_post_queue(id INTEGER,pool_item_id INTEGER,account_id TEXT,status TEXT,material_key TEXT,manual_run_id INTEGER);
                CREATE TABLE x_post_publish_log(queue_id INTEGER,unknown_outcome INTEGER,status TEXT);
                CREATE TABLE x_post_repost_ledger(queue_id INTEGER,unknown_outcome INTEGER,status TEXT);
                CREATE TABLE x_post_manual_run(id INTEGER,reuse_context_json TEXT);
                INSERT INTO x_post_queue VALUES(1,NULL,'2','published','701',1);
                INSERT INTO x_post_publish_log VALUES(1,0,'published');
                INSERT INTO x_post_manual_run VALUES(1,'{}');
            ''')
            with patch.object(reuse, 'publisher_task', return_value=dict(source(), _publisher_context=True)):
                context = dict(lane='tag', task_id=2)
                self.assertFalse(reuse.local_conflict(db, '701', '3', reuse_context=context))
                self.assertTrue(reuse.local_conflict(db, '701', '2', reuse_context=context))
                db.execute("UPDATE x_post_queue SET status='publishing'")
                self.assertTrue(reuse.local_conflict(db, '701', '3', reuse_context=context))
                db.execute("UPDATE x_post_queue SET status='published'")
                db.execute('UPDATE x_post_publish_log SET unknown_outcome=1')
                self.assertTrue(reuse.local_conflict(db, '701', '3', reuse_context=context))


if __name__ == '__main__':
    unittest.main()
