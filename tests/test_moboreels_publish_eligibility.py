import ast
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from features.moboreels_library import macro_client as client
from features.fb_video_publish import moboreels as fb
from features.tt_posts.moboreels import prepare_caption
from features.x_posts.moboreels import assert_queue_publishable
from features.youtube_auto_publish.eligibility import assert_body_publishable, assert_task_publishable
from features.youtube_auto_publish.templates import WorkflowError

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / 'ops/moboreels-publish-eligibility-20261113/runtime'


def method(lane, relative, name):
    tree = ast.parse((OPS / lane / relative).read_text())
    node = next(n for cls in tree.body if isinstance(cls, ast.ClassDef)
                for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    scope = {'AdapterError': RuntimeError}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<production method>', 'exec'), scope)
    return scope[name]


class EligibilityTests(unittest.TestCase):
    def test_exact_current_catalog_match_required(self):
        for status in (0, None, -1, 'unknown', True):
            with self.subTest(status=status), patch.object(client, '_request', return_value={'drama': {'story_id': '123', 'is_show': status}}):
                with self.assertRaises(client.MoboreelsEligibilityError): client.lookup_story('123')
        for row in ({}, {'story_id': 'other', 'is_show': 1}):
            with patch.object(client, '_request', return_value={'drama': row}):
                with self.assertRaises(client.MoboreelsEligibilityError): client.assert_publishable('123')
        with patch.object(client, '_request', return_value={'drama': {'story_id': '123', 'is_show': '1'}}):
            self.assertEqual(client.assert_publishable('123')['story_id'], '123')

    def test_status_is_reread_after_a_successful_preflight(self):
        with patch.object(client, '_request', side_effect=[{'drama': {'story_id': '123', 'is_show': 1}}, {'drama': {'story_id': '123', 'is_show': 0}}]) as request:
            client.lookup_story('123')
            with self.assertRaisesRegex(client.MoboreelsEligibilityError, '未上架'):
                client.resolve_m_url({}, story_id='123', platform='x', account_id='1', task_id='t', material_id='2')
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.args[0] == '/internal/story-metadata' for call in request.call_args_list))

    def test_unavailable_catalog_never_authorizes_publication(self):
        with patch.object(client, '_request', side_effect=client.MoboreelsLinkError('剧库暂不可用')):
            with self.assertRaisesRegex(client.MoboreelsEligibilityError, '已拦截发布'):
                client.assert_publishable('123')

    def test_other_products_never_query_moboreels(self):
        with patch.object(client, '_request', side_effect=AssertionError('legacy must not query')):
            assert_queue_publishable({'body_template': '{url}', 'content_id': 'legacy'})
            assert_body_publishable({'material': {'content_id': 'legacy'}, 'request': {'description_template': '{url}'}})
            assert_task_publishable('missing.sqlite', {'preparation_id': ''})

    def test_frozen_fb_link_does_not_bypass_delisting(self):
        link = {'status': 'succeeded', 'story_id': '123', 'short_url': 'https://ai.yingliangads.com/m/abcdefgh1234', 'target_url': 'https://moboreels.com/story/123'}
        with patch.object(client, '_request', return_value={'drama': {'story_id': '123', 'is_show': 0}}):
            with self.assertRaisesRegex(fb.MoboreelsFBError, '未上架'): fb.validate(link, '123')
        self.assertEqual(link['status'], 'succeeded')

    def test_frozen_tt_caption_is_rechecked_without_recreating_link(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'ledger.sqlite'
            row = {'id': 1, 'caption_template': '{{m_url}}', 'caption': 'https://ai.yingliangads.com/m/old', 'content_id': '123', 'claim_token': 'lease', 'publish_id': '', 'unknown_outcome': 0, 'status': 'publishing'}
            with closing(sqlite3.connect(db)) as conn:
                conn.execute('CREATE TABLE tt_post_queue(id,caption_template,caption,content_id,claim_token,publish_id,unknown_outcome,status)')
                conn.execute('INSERT INTO tt_post_queue VALUES (?,?,?,?,?,?,?,?)', tuple(row.values()))
                conn.execute("CREATE TABLE tt_moboreels_context(task_key PRIMARY KEY,actor_json,short_url,caption)")
                conn.execute('INSERT INTO tt_moboreels_context VALUES (?,?,?,?)', ('tt-queue:1', '{}', row['caption'], row['caption']))
                conn.commit()
            with patch.object(client, '_request', return_value={'drama': {'story_id': '123', 'is_show': 0}}), patch('features.tt_posts.moboreels.freeze_url', side_effect=AssertionError('must not recreate')):
                with self.assertRaises(client.MoboreelsEligibilityError): prepare_caption(SimpleNamespace(db_path=str(db)), row, 'lease')
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute('SELECT caption FROM tt_post_queue').fetchone()[0], row['caption'])
                self.assertEqual(conn.execute('SELECT short_url FROM tt_moboreels_context').fetchone()[0], row['caption'])

    def test_youtube_reads_frozen_provider_and_leaves_receipts_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'ledger.sqlite'
            body = {'material': {'promotion_provider': 'moboreels', 'content_id': '123'}, 'request': {}}
            with closing(sqlite3.connect(db)) as conn:
                conn.execute('CREATE TABLE youtube_auto_preparation(id,body)')
                conn.execute('INSERT INTO youtube_auto_preparation VALUES (?,?)', ('p', json.dumps(body)))
                conn.commit()
            with patch.object(client, '_request', return_value={'drama': {'story_id': '123', 'is_show': 0}}):
                with self.assertRaisesRegex(WorkflowError, '未上架'): assert_task_publishable(db, {'preparation_id': 'p'})
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(json.loads(conn.execute('SELECT body FROM youtube_auto_preparation').fetchone()[0]), body)

    def test_x_native_cached_descriptor_rechecks_catalog(self):
        links = method('x', 'features/pipeline_dispatch/x_native.py', '_links')
        with patch.object(client, '_request', return_value={'drama': {'story_id': '123', 'is_show': 0}}):
            with self.assertRaises(client.MoboreelsEligibilityError):
                links(object(), {'body_template': '{{m_url}}', 'content_id': '123'}, {'post_text': 'frozen', 'long_url': 'old', 'short_url': 'old'})

    def test_tt_native_blocks_before_publish_intent_but_preserves_reconciliation(self):
        begin = method('tt', 'features/pipeline_dispatch/tt_native.py', 'begin_operation')
        current = {'caption_template': '{{m_url}}', 'content_id': '123', 'publish_id': '', 'unknown_outcome': 0}
        descriptor = {'account_resource': 'tt:account:1', 'request': {'post_info': {'title': 'frozen'}}, 'artifact': {'url': 'https://cdn.test/video'}}
        owner = SimpleNamespace(_source_lock=lambda job: nullcontext(), _stored=lambda *args: ({'receipt_json': '', 'native_token': 't'}, descriptor), _native_dict=lambda job: current, automatic=False, store=SimpleNamespace(begin_publish=Mock()))
        operation = {'operation': 'tt_initialize_video', 'account_resource': 'tt:account:1', 'details': {'post_info': descriptor['request']['post_info'], 'video_url': descriptor['artifact']['url']}}
        with patch.object(client, '_request', return_value={'drama': {'story_id': '123', 'is_show': 0}}) as request:
            with self.assertRaises(client.MoboreelsEligibilityError): begin(owner, {'stage': 'publish', 'family': 'queue'}, {}, operation)
            owner.store.begin_publish.assert_not_called()
            current['publish_id'] = 'already-created'
            with self.assertRaisesRegex(RuntimeError, 'tt_reconcile_only'): begin(owner, {'stage': 'publish', 'family': 'queue'}, {}, operation)
            self.assertEqual(request.call_count, 1)


if __name__ == '__main__': unittest.main()
