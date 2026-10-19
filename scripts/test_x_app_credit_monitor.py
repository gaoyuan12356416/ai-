import copy
import unittest
from unittest.mock import Mock
import x_app_credit_monitor as monitor


def balance(total, prepaid=None, free='0'):
    return {'total_balance': total, 'prepaid_balance': prepaid or total, 'free_balance': free}


class CreditsTest(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.saved = []
        self.sender = Mock(return_value={'status': 'sent', 'message_id': 'test-message'})

    def run_check(self, value, setup=False):
        return monitor.update(self.state, value, notify=True, setup=setup,
                              persist=lambda state: self.saved.append(copy.deepcopy(state)), sender=self.sender)

    def test_real_components_and_negative_prepaid(self):
        self.assertEqual(monitor.parse_balance({'data': balance('9.97', '5.00', '4.97')})['total_balance'], '9.97')
        self.assertEqual(monitor.parse_balance({'data': balance('0', '-2', '1')})['total_balance'], '0')

    def test_invalid_never_becomes_zero(self):
        for value in ({}, {'data': {}}, {'data': balance('NaN')}, {'data': balance('-1')},
                      {'data': balance('Infinity')}, {'data': balance('1', '5', '4')},
                      {'data': balance('1'), 'errors': [{'title': 'partial'}]}, {'data': balance(True)}):
            with self.assertRaises((ValueError, TypeError)):
                monitor.parse_balance(value)

    def test_threshold_dedup_recovery(self):
        self.assertEqual(self.run_check(balance('5'))['status'], 'healthy')
        self.sender.assert_not_called()
        self.run_check(balance('4.99'))
        self.sender.assert_called_once()
        self.assertTrue(any(s.get('episode', {}).get('status') == 'sending' for s in self.saved))
        self.run_check(balance('0'))
        self.sender.assert_called_once()
        self.run_check(balance('5'))
        self.run_check(balance('4'))
        self.assertEqual(self.sender.call_count, 2)

    def test_ambiguous_delivery_never_retried_even_after_recovery(self):
        self.sender.side_effect = TimeoutError()
        self.assertEqual(self.run_check(balance('4'))['status'], 'delivery_unknown')
        self.run_check(balance('6'))
        self.run_check(balance('3'))
        self.sender.assert_called_once()

    def test_crash_intent_is_unknown(self):
        self.state['episode'] = {'uuid': 'frozen', 'status': 'sending'}
        self.assertEqual(self.run_check(balance('3'))['status'], 'delivery_unknown')
        self.sender.assert_not_called()

    def test_setup_once_is_not_false_alarm(self):
        self.run_check(balance('9.97', '5', '4.97'), setup=True)
        self.run_check(balance('9.97', '5', '4.97'), setup=True)
        self.sender.assert_called_once()
        self.assertIn('当前余额未触发', self.sender.call_args.args[0])

    def test_setup_under_threshold_is_real_alert(self):
        self.run_check(balance('3'), setup=True)
        self.assertIn('可用余额不足', self.sender.call_args.args[0])
        self.assertNotIn('配置成功', self.sender.call_args.args[0])

    def test_wrong_recipient_refused_before_network(self):
        with self.assertRaises(ValueError):
            monitor.send_private({'receive_id_type': 'chat_id', 'recipient_name': '郜远'}, 'test', 'id')


if __name__ == '__main__':
    unittest.main()
