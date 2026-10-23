import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
