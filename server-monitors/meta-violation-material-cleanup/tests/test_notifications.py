import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meta_violation_cleanup import notifications, feishu_transport
from meta_violation_cleanup.runtime import Runtime, RuntimeLedgerError


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(self.tmp.name)
        self.runtime.begin('run-1', 'run', {})
        self.config = Path(self.tmp.name) / 'feishu.json'
        self.config.write_text(json.dumps({
            'app_id': 'test-app', 'app_secret': 'test-secret',
            'receive_id_type': 'open_id',
            'receive_id': 'ou_62fed22c4918524e48ded4eab8b82b1e',
        }), encoding='utf-8')
        self.notifier = notifications.Notifier(self.runtime, self.config)
        self.messages = []

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def transport(self, url, payload, token=None):
        if '/auth/' in url:
            return {'code': 0, 'tenant_access_token': 'test-token'}
        self.messages.append(dict(payload))
        return {'code': 0, 'data': {'message_id': 'msg-1'}}

    def test_success_duplicate_does_not_send_again_and_keeps_frozen_body(self):
        original_id, original_secret = feishu_transport.APP_ID, feishu_transport.APP_SECRET
        with patch.object(feishu_transport, 'post_json', side_effect=self.transport) as post:
            receipt = self.notifier.send('run-1', 'start', 'original')
            self.assertEqual(self.notifier.send('run-1', 'start', 'replacement'), receipt)
            self.assertEqual(post.call_count, 2)  # auth plus one message
        self.assertEqual(self.messages[0]['uuid'], receipt['uuid'])
        self.assertEqual(json.loads(self.messages[0]['content'])['text'], 'original')
        self.assertEqual(feishu_transport.APP_ID, original_id)
        self.assertEqual(feishu_transport.APP_SECRET, original_secret)
        self.assertEqual(self.runtime.notice('run-1', 'start')['status'], 'sent')

    def test_invalid_config_is_known_unsent_failure(self):
        self.config.write_text('{}')
        with patch.object(feishu_transport, 'post_json') as post:
            with self.assertRaisesRegex(RuntimeError, 'not_confirmed'):
                self.notifier.send('run-1', 'start', 'original')
        post.assert_not_called()
        state = self.runtime.notice('run-1', 'start')
        self.assertEqual(state['status'], 'failed')
        self.assertFalse(json.loads(state['receipt'])['request_sent'])

    def test_card_auth_failure_retry_uses_frozen_json_uuid_and_deduplicates(self):
        card = {'elements': [{'tag': 'markdown', 'content': '原始内容'}]}
        with patch.object(feishu_transport, 'post_json', side_effect=TimeoutError()):
            with self.assertRaises(RuntimeError):
                self.notifier.send_card('run-1', 'end', card)
        old = self.runtime.notice('run-1', 'end')
        self.assertEqual(old['status'], 'failed')
        with patch.object(feishu_transport, 'post_json', side_effect=self.transport):
            receipt = self.notifier.send_card('run-1', 'end', {'elements': []})
            self.assertEqual(self.notifier.send_card('run-1', 'end', card), receipt)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0]['msg_type'], 'interactive')
        self.assertEqual(json.loads(self.messages[0]['content']), card)
        self.assertEqual(self.messages[0]['uuid'], old['uuid'])

    def test_card_unknown_never_retries_or_falls_back_to_text(self):
        def timeout(url, payload, token=None):
            if '/auth/' in url:
                return {'code': 0, 'tenant_access_token': 'test-token'}
            self.messages.append(payload)
            raise TimeoutError()
        with patch.object(feishu_transport, 'post_json', side_effect=timeout):
            with self.assertRaises(RuntimeError):
                self.notifier.send_card('run-1', 'end', {'elements': []})
        self.assertEqual(self.runtime.notice('run-1', 'end')['status'], 'unknown')
        with patch.object(feishu_transport, 'post_json') as post:
            with self.assertRaisesRegex(RuntimeError, 'unknown_requires_reconciliation'):
                self.notifier.send_card('run-1', 'end', {'elements': []})
        post.assert_not_called()
        self.assertEqual(len(self.messages), 1)

    def test_explicit_group_recipient_uses_chat_id_and_retains_once_delivery(self):
        destination = {'receive_id_type': 'chat_id', 'receive_id': 'oc_b633b6fba2c78f43ef1ebacd314d518c'}
        config = json.loads(self.config.read_text())
        config.update(destination)
        self.config.write_text(json.dumps(config))
        notifier = notifications.Notifier(self.runtime, self.config, destination)
        urls = []
        def send(url, payload, token=None):
            urls.append(url)
            return self.transport(url, payload, token)
        with patch.object(feishu_transport, 'post_json', side_effect=send):
            first = notifier.send('run-1', 'end', 'group report')
            self.assertEqual(notifier.send('run-1', 'end', 'changed'), first)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0]['receive_id'], destination['receive_id'])
        self.assertIn('receive_id_type=chat_id', urls[-1])

    def test_recipient_config_mismatch_is_unsent_and_default_stays_personal(self):
        destination = {'receive_id_type': 'chat_id', 'receive_id': 'oc_b633b6fba2c78f43ef1ebacd314d518c'}
        notifier = notifications.Notifier(self.runtime, self.config, destination)
        with patch.object(feishu_transport, 'post_json') as post:
            with self.assertRaisesRegex(RuntimeError, 'not_confirmed'):
                notifier.send('run-1', 'end', 'group report')
        post.assert_not_called()
        self.assertFalse(json.loads(self.runtime.notice('run-1', 'end')['receipt'])['request_sent'])
        self.assertEqual(self.notifier.recipient, notifications.DEFAULT_RECIPIENT)

    def test_invalid_recipient_rejected_without_touching_notice(self):
        for recipient in ({}, {'receive_id_type': [], 'receive_id': 'oc_group'},
                          {'receive_id_type': 'chat_id', 'receive_id': 'ou_wrong'},
                          {'receive_id_type': 'email', 'receive_id': 'user@example.com'}):
            with self.subTest(recipient=recipient), self.assertRaisesRegex(ValueError, 'invalid_notification_recipient'):
                notifications.Notifier(self.runtime, self.config, recipient)
        self.assertIsNone(self.runtime.notice('run-1', 'end'))

    def test_authentication_failure_can_retry_original_message(self):
        with patch.object(feishu_transport, 'post_json', side_effect=TimeoutError('secret-error')):
            with self.assertRaisesRegex(RuntimeError, '^notification_not_confirmed$'):
                self.notifier.send('run-1', 'start', 'original')
        old = self.runtime.notice('run-1', 'start')
        self.assertEqual(old['status'], 'failed')
        with patch.object(feishu_transport, 'post_json', side_effect=self.transport):
            receipt = self.notifier.send('run-1', 'start', 'changed')
        self.assertEqual(receipt['uuid'], old['uuid'])
        self.assertEqual(json.loads(self.messages[0]['content'])['text'], 'original')
        self.assertNotIn('secret-error', old['receipt'])

    def test_explicit_rejection_can_retry_but_not_change_body_or_uuid(self):
        def reject(url, payload, token=None):
            if '/auth/' in url:
                return {'code': 0, 'tenant_access_token': 'test-token'}
            return {'code': 230001, 'msg': 'sensitive provider diagnostic'}
        with patch.object(feishu_transport, 'post_json', side_effect=reject):
            with self.assertRaises(RuntimeError):
                self.notifier.send('run-1', 'end', 'original')
        old = self.runtime.notice('run-1', 'end')
        self.assertEqual(old['status'], 'failed')
        self.assertEqual(json.loads(old['receipt']), {'code': 230001})
        with patch.object(feishu_transport, 'post_json', side_effect=self.transport):
            self.notifier.send('run-1', 'end', 'changed')
        self.assertEqual(self.messages[0]['uuid'], old['uuid'])

    def test_message_timeout_is_unknown_and_never_replayed(self):
        def timeout(url, payload, token=None):
            if '/auth/' in url:
                return {'code': 0, 'tenant_access_token': 'test-token'}
            raise TimeoutError('potentially accepted')
        with patch.object(feishu_transport, 'post_json', side_effect=timeout):
            with self.assertRaises(RuntimeError):
                self.notifier.send('run-1', 'end', 'body')
        self.assertEqual(self.runtime.notice('run-1', 'end')['status'], 'unknown')
        with patch.object(feishu_transport, 'post_json') as post:
            with self.assertRaisesRegex(RuntimeError, 'unknown_requires_reconciliation'):
                self.notifier.send('run-1', 'end', 'body')
        post.assert_not_called()

    def test_malformed_success_receipt_is_unknown(self):
        for result in ({'code': 0}, {'code': 0, 'data': {}},
                       {'code': False, 'data': {'message_id': 'x'}}, ['invalid']):
            with self.subTest(result=result):
                phase = 'end-' + str(2 + len(self.messages))
                def invalid(url, payload, token=None):
                    if '/auth/' in url:
                        return {'code': 0, 'tenant_access_token': 'test-token'}
                    self.messages.append(payload)
                    return result
                with patch.object(feishu_transport, 'post_json', side_effect=invalid):
                    with self.assertRaises(RuntimeError):
                        self.notifier.send('run-1', phase, 'body')
                self.assertEqual(self.runtime.notice('run-1', phase)['status'], 'unknown')

    def test_interrupt_after_message_dispatch_is_unknown(self):
        def interrupt(url, payload, token=None):
            if '/auth/' in url:
                return {'code': 0, 'tenant_access_token': 'test-token'}
            raise KeyboardInterrupt()
        with patch.object(feishu_transport, 'post_json', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.notifier.send('run-1', 'start', 'body')
        self.assertEqual(self.runtime.notice('run-1', 'start')['status'], 'unknown')

    def test_receipt_write_failure_stops_without_attempting_another_write(self):
        original_finish = self.runtime.finish_notice
        def fail_sent(run_id, phase, status, receipt):
            if status == 'sent':
                self.runtime.db.execute('PRAGMA query_only=ON')
            return original_finish(run_id, phase, status, receipt)
        with patch.object(feishu_transport, 'post_json', side_effect=self.transport):
            with patch.object(self.runtime, 'finish_notice', side_effect=fail_sent) as finish:
                with self.assertRaises(RuntimeLedgerError):
                    self.notifier.send('run-1', 'start', 'body')
                self.assertEqual(finish.call_count, 1)
        self.assertEqual(self.runtime.notice('run-1', 'start')['status'], 'sending')
        with self.assertRaises(RuntimeLedgerError):
            self.runtime.write_barrier('run-1')

    def test_unknown_is_preserved_after_reopening_runtime(self):
        self.runtime.claim_notice('run-1', 'end', 'body')
        self.runtime.start_notice('run-1', 'end')
        self.runtime.finish_notice('run-1', 'end', 'unknown', {})
        self.runtime.close()
        self.runtime = Runtime(self.tmp.name)
        notifier = notifications.Notifier(self.runtime, self.config)
        with patch.object(feishu_transport, 'post_json') as post:
            with self.assertRaisesRegex(RuntimeError, 'unknown_requires_reconciliation'):
                notifier.send('run-1', 'end', 'body')
        post.assert_not_called()

    def test_continuation_notices_have_independent_uuid(self):
        with patch.object(feishu_transport, 'post_json', side_effect=self.transport):
            first = self.notifier.send('run-1', 'start', 'first')
            second = self.notifier.send('run-1', 'start-2', 'second')
        self.assertNotEqual(first['uuid'], second['uuid'])
        self.assertEqual(len(self.messages), 2)

    def test_multipart_preserves_all_text_and_caps_actual_utf8_json_size(self):
        body = ('Ad 120250000000000001 | 产品中文😀 | 引号"与反斜杠\\\n' * 7000)
        with patch.object(feishu_transport, 'post_json', side_effect=self.transport):
            receipt = self.notifier.send('run-1', 'end-2', body)
        self.assertGreater(receipt['part_count'], 2)
        self.assertEqual(self.runtime.notice('run-1', 'end-2')['body'], body)
        restored = []
        for index, payload in enumerate(self.messages, 1):
            self.assertLessEqual(len(feishu_transport.encode_payload(payload)), 100000)
            text = json.loads(payload['content'])['text']
            restored.append(text.split('\n', 1)[1])
            phase = 'end-2-part-' + str(index).zfill(3)
            child = self.runtime.notice('run-1', phase)
            self.assertEqual(child['status'], 'sent')
            self.assertEqual(child['uuid'], payload['uuid'])
        self.assertEqual(''.join(restored), body)
        self.assertEqual(len({payload['uuid'] for payload in self.messages}), len(self.messages))
        self.assertEqual(self.runtime.notice('run-1', 'end-2')['status'], 'sent')
        with patch.object(feishu_transport, 'post_json') as post:
            self.assertEqual(self.notifier.send('run-1', 'end-2', 'changed'), receipt)
        post.assert_not_called()

    def test_multipart_long_single_line_preserves_unicode_boundaries(self):
        body = '汉😀"\\' * 40000
        parts = notifications._split_body(body)
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(part.split('\n', 1)[1] for part in parts), body)
        self.assertTrue(all(notifications._payload_size(part) <= 100000 for part in parts))

    def test_multipart_known_failure_retries_only_unsent_parts_after_reopen(self):
        body = '素材及其他引用Ad 120250000000000000\n' * 7000
        calls = []
        def reject_second(url, payload, token=None):
            if '/auth/' in url:
                return {'code': 0, 'tenant_access_token': 'test-token'}
            calls.append(dict(payload))
            if len(calls) == 2:
                raise feishu_transport.FeishuRejected(230025, 400)
            return {'code': 0, 'data': {'message_id': 'msg-first'}}
        with patch.object(feishu_transport, 'post_json', side_effect=reject_second):
            with self.assertRaises(RuntimeError):
                self.notifier.send('run-1', 'end', body)
        self.assertEqual(self.runtime.notice('run-1', 'end')['status'], 'failed')
        self.assertEqual(self.runtime.notice('run-1', 'end-part-001')['status'], 'sent')
        failed = self.runtime.notice('run-1', 'end-part-002')
        self.assertEqual(json.loads(failed['receipt']), {'code': 230025, 'http_status': 400})
        self.runtime.close()
        self.runtime = Runtime(self.tmp.name)
        notifier = notifications.Notifier(self.runtime, self.config)
        with patch.object(notifications, '_split_body', side_effect=AssertionError('must reuse frozen plan')):
            with patch.object(feishu_transport, 'post_json', side_effect=self.transport):
                receipt = notifier.send('run-1', 'end', 'replacement ignored')
        self.assertEqual(self.messages[0]['uuid'], failed['uuid'])
        self.assertNotIn(calls[0]['uuid'], [payload['uuid'] for payload in self.messages])
        self.assertEqual(len(self.messages), receipt['part_count'] - 1)
        self.assertEqual(self.runtime.notice('run-1', 'end')['status'], 'sent')

    def test_multipart_unknown_part_fences_parent_and_never_replays(self):
        calls = []
        def uncertain_second(url, payload, token=None):
            if '/auth/' in url:
                return {'code': 0, 'tenant_access_token': 'test-token'}
            calls.append(payload)
            if len(calls) == 2:
                raise TimeoutError('accepted outcome unknown')
            return {'code': 0, 'data': {'message_id': 'msg-first'}}
        with patch.object(feishu_transport, 'post_json', side_effect=uncertain_second):
            with self.assertRaises(RuntimeError):
                self.notifier.send('run-1', 'end', '广告引用\n' * 30000)
        self.assertEqual(self.runtime.notice('run-1', 'end')['status'], 'unknown')
        self.assertEqual(self.runtime.notice('run-1', 'end-part-001')['status'], 'sent')
        self.assertEqual(self.runtime.notice('run-1', 'end-part-002')['status'], 'unknown')
        self.assertEqual(self.runtime.notice('run-1', 'end-part-003')['status'], 'pending')
        with patch.object(feishu_transport, 'post_json') as post:
            with self.assertRaisesRegex(RuntimeError, 'unknown_requires_reconciliation'):
                self.notifier.send('run-1', 'end', 'ignored')
        post.assert_not_called()

    def test_multipart_payload_refusal_before_network_is_not_unknown(self):
        self.runtime.claim_notice('run-1', 'end-part-001', '大' * 100000)
        with patch.object(feishu_transport, 'post_json', side_effect=self.transport):
            with self.assertRaises(RuntimeError):
                self.notifier._send_single('run-1', 'end-part-001', 'ignored')
        self.assertEqual(self.messages, [])
        self.assertEqual(self.runtime.notice('run-1', 'end-part-001')['status'], 'failed')

    def test_explicit_http_rejection_is_failed_not_unknown(self):
        def rejected(url, payload, token=None):
            if '/auth/' in url:
                return {'code': 0, 'tenant_access_token': 'test-token'}
            raise feishu_transport.FeishuRejected(230025, 400)
        with patch.object(feishu_transport, 'post_json', side_effect=rejected):
            with self.assertRaisesRegex(RuntimeError, 'notification_not_confirmed'):
                self.notifier.send('run-1', 'end', 'body')
        row = self.runtime.notice('run-1', 'end')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(json.loads(row['receipt']), {'code': 230025, 'http_status': 400})


class TransportTests(unittest.TestCase):
    def error(self, status, body):
        return HTTPError('https://example.invalid/messages', status, 'sensitive detail', {}, io.BytesIO(body))

    def test_http_explicit_rejection_retains_only_numeric_whitelist_fields(self):
        payload = json.dumps({'code': 230025, 'msg': 'token=do-not-log', 'data': {'secret': 'private'}}).encode()
        with patch.object(feishu_transport.request, 'urlopen', side_effect=self.error(400, payload)):
            with self.assertRaises(feishu_transport.FeishuRejected) as caught:
                feishu_transport.post_json('https://example.invalid/messages', {'text': 'body'})
        self.assertEqual(caught.exception.code, 230025)
        self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(str(caught.exception), 'feishu_request_rejected')
        self.assertEqual(vars(caught.exception), {'code': 230025, 'http_status': 400})

    def test_http_unknown_responses_do_not_become_known_rejections(self):
        for status, data in ((500, b'{"code":1500}'), (400, b'{"code":0}'),
                             (400, b'{"code":true}'), (400, b'{"code":"230025"}'),
                             (400, b'not-json'), (400, b'x' * 16385)):
            with self.subTest(status=status, data=data[:30]):
                with patch.object(feishu_transport.request, 'urlopen', side_effect=self.error(status, data)):
                    with self.assertRaisesRegex(RuntimeError, '^feishu_transport_unavailable$') as caught:
                        feishu_transport.post_json('https://example.invalid/messages', {})
                self.assertNotIsInstance(caught.exception, feishu_transport.FeishuRejected)


if __name__ == '__main__':
    unittest.main()
