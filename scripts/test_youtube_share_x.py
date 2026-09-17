"""Public bridge tests: real task ownership, HTTP framing, text and source checks."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_youtube_auto_service as fixtures
import test_youtube_auto_http as http_fixtures
from features.x_accounts.youtube_share_text import canonical_url, preview, weighted_length
from features.youtube_auto_publish import x_share as share
from features.youtube_auto_publish.templates import WorkflowError

VIDEO = 'AbcdEFG_123'
CHANNEL = 'UC' + 'a' * 22
URL = canonical_url(VIDEO)


class TextTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(title='短剧测试', youtube_url=URL, channel_name='频道', channel_url='https://www.youtube.com/channel/' + CHANNEL, name='剧名', desc='简介')

    def test_macros_single_pass_and_link_insertion(self):
        self.values['title'] = '{name}'
        result = preview('{title}', self.values)
        self.assertEqual(result['text'], '{name}\n\n' + URL)
        self.assertTrue(result['appended_url'])
        self.assertTrue(result['valid'])
        self.assertFalse(preview('{youtube_url}', self.values)['appended_url'])

    def test_unknown_missing_nested_and_control_fail(self):
        for text in ('{url}', '{unknown}', '{{title}}', '{title', 'unsafe\x00'):
            self.assertFalse(preview(text, self.values)['valid'], text)
        self.values['name'] = ''
        self.assertFalse(preview('{name}', self.values)['valid'])

    def test_normalization_cjk_emoji_and_url_weights(self):
        for text, count in [('汉字', 4), ('cafe\u0301', 4), ('👨‍👩‍👧‍👦', 2), ('🙋🏽', 2), ('🇨🇳', 2), ('1️⃣', 2), (URL, 23), (URL + '。', 25)]:
            self.assertEqual(weighted_length(text), count, text)
        self.assertTrue(preview('a' * 255, self.values)['valid'])
        self.assertFalse(preview('a' * 256, self.values)['valid'])

    def test_similar_url_is_not_the_source(self):
        self.assertTrue(preview(URL + 'junk', self.values)['appended_url'])
        self.assertTrue(preview(self.values['channel_url'], self.values)['appended_url'])
        self.assertFalse(preview('(' + URL + ')', self.values)['appended_url'])

    def test_unrecognized_emoji_combinations_are_conservative(self):
        self.assertFalse(preview('a' * 253 + '😀\u200d😀', self.values)['valid'])
        self.assertGreaterEqual(weighted_length('😀🏽'), 4)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.actor = self.fixture.actor
        self.task = self.fixture.enqueue()
        self.ledger = self.fixture.store.tasks[self.task['publish_id']]
        self.ledger.update(video_id=VIDEO, channel_id=CHANNEL, video_state='published', status='published',
                           app_id='1479', channel_local_id='258', youtube_account_id='250', title='Actual video title')
        self.network = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.x = Mock(return_value={})
        self.check = Mock(return_value=12345.0)
        self.card = Mock(return_value={'eligible': True, 'state': 'ready'})
        self.payload = dict(operation_id='e602327e-c149-41b7-89ce-c111ae521cd4', account_ids=[7, 3], description_template='{title}\n{youtube_url}')

    def call(self, action='create', payload=None, actor=None):
        return share.handle(self.service, SimpleNamespace(), actor or self.actor, self.task['id'], action,
                            payload if payload is not None else self.payload, x_request=self.x, source_check=self.check, card_check=self.card)

    def test_real_task_ownership_before_every_external_call(self):
        for actor in (self.fixture.other, self.fixture.foreign):
            for action in ('options', 'preview', 'create', 'run'):
                with self.assertRaises(WorkflowError): self.call(action, actor=actor)
        self.x.assert_not_called(); self.check.assert_not_called()

    def test_nonpublic_and_bad_macro_never_enqueue(self):
        self.ledger['video_state'] = 'private'
        with self.assertRaises(WorkflowError): self.call()
        self.assertTrue(all(c.args[0] != 'create' for c in self.x.call_args_list))
        self.ledger['video_state'] = 'published'
        with self.assertRaises(WorkflowError): self.call(payload=dict(self.payload, description_template='{bad}'))
        self.check.assert_not_called()

    def test_valid_create_freezes_real_link_and_server_actor(self):
        self.call()
        action, actor = self.x.call_args.args
        data = self.x.call_args.kwargs
        self.assertEqual(action, 'create'); self.assertEqual(actor, self.actor)
        self.assertEqual(data['scope'], 'mine'); self.assertEqual(data['account_ids'], [3, 7])
        self.assertEqual(data['youtube_url'], URL); self.assertIn(URL, data['text'])
        self.check.assert_called_once()

    def test_short_url_reuses_frozen_task_link_in_options_preview_and_create(self):
        self.fixture.short_link.reset_mock()
        self.fixture.short_link.side_effect = AssertionError('must not generate another link')
        with patch.object(share.x_client, 'query_x_accounts', return_value={'items': []}):
            options = self.call('options')
        short_url = next(m['value'] for m in options['macros'] if m['key'] == 'short_url')
        self.assertEqual(short_url, 'https://example.invalid/short-for-tests')
        payload = dict(self.payload, description_template='{short_url}')
        rendered = self.call('preview', payload=payload)
        self.assertTrue(rendered['valid'])
        self.assertEqual(rendered['text'], URL + '\n\n' + short_url)
        self.assertEqual(rendered['weighted_length'], 48)
        self.call(payload=payload)
        self.assertEqual(self.x.call_args.args[0], 'create')
        self.assertEqual(self.x.call_args.kwargs['text'], rendered['text'])
        self.fixture.short_link.assert_not_called()

    def test_missing_short_link_blocks_only_templates_that_use_it(self):
        with self.service.db() as connection:
            _, body = self.service._row(connection, self.task['id'], self.actor)
            body['material'].pop('macro_url', None)
            self.service._save(connection, body)
        payload = dict(self.payload, description_template='{short_url}')
        rendered = self.call('preview', payload=payload)
        self.assertFalse(rendered['valid'])
        self.assertIn('推广短链为空，请修改描述', rendered['errors'])
        with self.assertRaises(WorkflowError): self.call(payload=payload)
        self.check.assert_not_called()
        self.assertTrue(all(c.args[0] != 'create' for c in self.x.call_args_list))
        self.assertTrue(self.call('preview')['valid'])

    def test_live_public_failure_prevents_enqueue(self):
        self.check.side_effect = WorkflowError('youtube_not_public', 'private', 409)
        with self.assertRaises(WorkflowError): self.call()
        self.assertEqual([c.args[0] for c in self.x.call_args_list], ['query'])

    def test_player_card_gate_is_exposed_and_rechecked_before_enqueue(self):
        with patch.object(share.x_client, 'query_x_accounts', return_value={'items': []}):
            self.assertTrue(self.call('options')['player_card']['eligible'])
        self.card.return_value = {'eligible': False, 'state': 'not_player', 'message': '此视频只提供图片卡片'}
        with self.assertRaises(WorkflowError) as error: self.call()
        self.assertEqual(error.exception.code, 'youtube_player_unavailable')
        self.assertTrue(all(c.args[0] != 'create' for c in self.x.call_args_list))

    def test_replay_uses_original_even_after_source_state_changes(self):
        run = dict(id='e'*32, account_ids=[3, 7], description_template=self.payload['description_template'])
        self.x.return_value = {'run': run}
        self.ledger['video_state'] = 'private'
        self.assertEqual(self.call(), {'run': run})
        self.check.assert_not_called()
        self.card.assert_not_called()
        with self.assertRaises(WorkflowError): self.call(payload=dict(self.payload, account_ids=[3]))

    def test_no_premium_gate_and_blocked_accounts_show_reasons(self):
        accounts = {'items': [dict(id=3, status='active', publish_approved=True, subscription_type='None', username='basic'),
                              dict(id=7, status='disabled', publish_approved=True), dict(id=9, status='active', publish_approved=False)]}
        with patch.object(share.x_client, 'query_x_accounts', return_value=accounts):
            result = self.call('options')
        self.assertEqual([a['selectable'] for a in result['accounts']], [True, False, False])
        self.assertEqual(result['source']['video_id'], VIDEO)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_compact_list_includes_authoritative_public_gate(self):
        row = self.service.list_tasks(self.actor, compact=True)['items'][0]
        self.assertTrue(row['can_share_x']); self.assertEqual(row['video_id'], VIDEO)
        self.ledger['video_state'] = 'private'
        self.assertFalse(self.service.list_tasks(self.actor, compact=True)['items'][0]['can_share_x'])

    def test_bounds_and_types_reject_before_platform_check(self):
        for ids in ([], [1,1], [True], ['1'], list(range(1,22))):
            with self.assertRaises(WorkflowError): self.call(payload=dict(self.payload, account_ids=ids))
        self.check.assert_not_called()


class SourceReadTests(unittest.TestCase):
    def test_live_source_identity_visibility_and_sanitized_failure(self):
        repository, client, session = Mock(), Mock(), Mock()
        client.refresh_access_token.return_value = 'DO_NOT_EXPOSE'
        good = {'items': [{'id': VIDEO, 'snippet': {'channelId': CHANNEL}, 'status': {'privacyStatus': 'public', 'uploadStatus': 'processed', 'embeddable': True}}]}
        def check(body):
            session.get.return_value = Mock(status_code=200, json=lambda: body)
            return share.verify_public(SimpleNamespace(), {}, CHANNEL, VIDEO, repository=repository, client=client, session_factory=lambda:session)
        self.assertGreater(check(good), 0)
        session.post.assert_not_called()
        for body in ({'items': []}, {'items': [dict(good['items'][0], id='wrong')]}, {'items': [dict(good['items'][0], status={'privacyStatus':'private','uploadStatus':'processed'})]}):
            with self.assertRaises(WorkflowError): check(body)
        not_embeddable = copy.deepcopy(good)
        not_embeddable['items'][0]['status']['embeddable'] = False
        with self.assertRaises(WorkflowError) as error: check(not_embeddable)
        self.assertEqual(error.exception.code, 'youtube_not_embeddable')
        self.assertFalse(session.trust_env)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])


class HttpTests(unittest.TestCase):
    setUpClass = classmethod(http_fixtures.YouTubeHttpTests.setUpClass.__func__)
    setUp = http_fixtures.YouTubeHttpTests.setUp
    request = http_fixtures.YouTubeHttpTests.request

    def test_cookie_module_and_csrf_gates_before_share_handler(self):
        path = '/tasks/' + 'a'*32 + '/x-share'
        with patch.object(share, 'handle', return_value={'run': {'id':'e'*32}}) as handle:
            self.assertEqual(self.request('GET', path, cookie=False).status, 401)
            self.assertEqual(self.request('GET', path).status, 403)
            self.session['permissions']['x_accounts'] = True
            self.assertEqual(self.request('POST', path, headers={'Origin':'https://evil.invalid'}).status, 403)
            self.assertEqual(self.request('POST', path, headers={'Content-Length':['2','2']}).status, 411)
            handle.assert_not_called()
            self.assertEqual(self.request('GET', path + '/preview').status, 405)
            self.assertEqual(self.request('POST', path).status, 202)
            self.assertEqual(handle.call_args.args[2]['user_id'], 'owner')

    def test_injected_actor_stripped_and_status_reads_do_not_write(self):
        self.session['permissions']['x_accounts'] = True
        with patch.object(share, 'handle', return_value={}) as handle:
            data = {'actor': {'role': 'admin'}, 'role': 'admin', 'tenant_key': 'foreign', 'description_template':'{title}'}
            path = '/tasks/' + 'a'*32 + '/x-share'
            response = self.request('POST', path + '/preview', body=json.dumps(data).encode())
            self.assertEqual(response.status, 200)
            self.assertEqual(handle.call_args.args[5], {'description_template':'{title}'})
            response = self.request('GET', path + '/runs/' + 'b'*32)
            self.assertEqual(response.status, 200)
            self.assertEqual(handle.call_args.args[4], 'run')

    def test_real_sidecar_error_class_produces_json_response(self):
        self.session['permissions']['x_accounts'] = True
        error = share.x_client.XAccountsClientError('x_account_disabled', '账号已停用', 409)
        with patch.object(share, 'handle', side_effect=error):
            response = self.request('POST', '/tasks/' + 'a'*32 + '/x-share')
        self.assertEqual(response.status, 409)
        self.assertEqual(response.payload, {'error':'x_account_disabled', 'message':'账号已停用'})


if __name__ == '__main__':
    unittest.main()
