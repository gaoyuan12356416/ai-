import unittest
from unittest.mock import Mock, patch

import requests
from scripts.finish_meta_creative_scope_once import refresh_job, retryable_recheck


class ContinuationTests(unittest.TestCase):
    def test_restart_poll_only_retries_reads(self):
        http = Mock()
        http.get.side_effect = [requests.ConnectionError(), Mock()]
        with patch('scripts.finish_meta_creative_scope_once.time.sleep'):
            refresh_job(http, 'http://localhost/job')
        self.assertEqual(http.get.call_count, 2)
        http.post.assert_not_called()
        http.delete.assert_not_called()
        http.get.side_effect = requests.Timeout()
        with patch('scripts.finish_meta_creative_scope_once.time.sleep'):
            with self.assertRaises(requests.Timeout):
                refresh_job(http, 'http://localhost/job')
        self.assertEqual(http.get.call_count, 5)

    def test_only_known_read_interruptions_can_continue(self):
        self.assertTrue(retryable_recheck({'error': {'code': 'source_unavailable', 'retryable': True}}))
        self.assertTrue(retryable_recheck({'status': 'interrupted', 'step': '核验进程已中断，进度保留，请人工重新核验；未自动恢复删除'}))
        for error in ({'code': 'source_unavailable'}, {'code': 'permission_denied', 'retryable': True}, {'code': 'network_uncertain', 'retryable': True}):
            self.assertFalse(retryable_recheck({'error': error}))
        self.assertFalse(retryable_recheck({'status': 'interrupted'}))
        http = Mock()
        http.get.return_value.raise_for_status.side_effect = requests.HTTPError()
        with self.assertRaises(requests.HTTPError):
            refresh_job(http, 'http://localhost/job')
        self.assertEqual(http.get.call_count, 1)


if __name__ == '__main__':
    unittest.main()
