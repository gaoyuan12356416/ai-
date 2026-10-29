import unittest
from unittest.mock import Mock, patch

from features.fb_ad_asset_delete.graph import GraphError
from scripts.meta_video_inventory_audit import read_inventory, pair_outcomes, receipt_basis


def page(ids, after=None):
    result = {'data': [{'id': value} for value in ids]}
    if after:
        result['paging'] = {'next': 'https://example.invalid/?access_token=do-not-use',
                            'cursors': {'after': after}}
    return result


class InventoryAuditTests(unittest.TestCase):
    def client(self, replies):
        client = Mock(max_inventory=50000)
        client._identity_id.side_effect = lambda value: value if isinstance(value, str) and value.isdigit() else ''
        client.request.side_effect = replies
        return client

    def test_retry_keeps_completed_pages_and_never_uses_next_url(self):
        client = self.client([page(['101'], 'cursor1'), GraphError('read_unavailable', 'timeout'), page(['102'])])
        progress = []
        with patch('scripts.meta_video_inventory_audit.time.sleep'):
            result = read_inventory(client, '10', 'secret', progress=progress.append)
        self.assertEqual(result, dict(ids={'101', '102'}, pages=2, read_retries=1, complete=True))
        self.assertEqual([c.args[3].get('after') for c in client.request.call_args_list], [None, 'cursor1', 'cursor1'])
        for call in client.request.call_args_list:
            self.assertEqual(call.args[:2], ('GET', 'act_10/advideos'))
            self.assertGreater(call.kwargs['timeout'], 5)
        self.assertNotIn('secret', str(progress))

    def test_permission_error_stops_without_retry_and_keeps_positive_sighting(self):
        client = self.client([page(['101'], 'cursor1'), GraphError('200', 'not allowed', detail={'http_status': 403})])
        result = read_inventory(client, '10', 'secret')
        self.assertFalse(result['complete'])
        self.assertEqual(client.request.call_count, 2)
        targets = [{'video_id': '101', 'account_id': '10'}, {'video_id': '102', 'account_id': '10'}]
        self.assertEqual([r['status'] for r in pair_outcomes(targets, result)], ['still_present', 'unverified'])

    def test_exhausted_page_retries_never_prove_absence(self):
        client = self.client([page(['101'], 'cursor1')] + [GraphError('read_unavailable', 'timeout')]*3)
        with patch('scripts.meta_video_inventory_audit.time.sleep'):
            result = read_inventory(client, '10', 'secret')
        self.assertFalse(result['complete'])
        self.assertEqual(result['pages'], 1)
        self.assertEqual(result['read_retries'], 2)
        self.assertEqual(pair_outcomes([{'video_id': '999'}], result)[0]['status'], 'unverified')

    def test_invalid_or_repeated_pagination_cannot_complete(self):
        for replies in ([page(['101'], 'same'), page(['102'], 'same')],
                        [page(['101'], 'next'), page(['101'])],
                        [page(['101'], 'next'), {'data': [{'id': None}]}]):
            result = read_inventory(self.client(replies), '10', 'secret')
            self.assertFalse(result['complete'])
            self.assertEqual(result['code'], 'account_video_read_incomplete')

    def test_full_inventory_keeps_receipt_separate_from_read_result(self):
        client = self.client([page(['101'])])
        result = read_inventory(client, '10', 'secret')
        targets = [{'video_id': '999', 'receipt_basis': 'delete_success_receipt'},
                   {'video_id': '101', 'receipt_basis': 'account_library_absent'}]
        outcomes = pair_outcomes(targets, result)
        self.assertEqual(outcomes[0]['status'], 'confirmed_absent')
        self.assertEqual(outcomes[0]['receipt_basis'], 'delete_success_receipt')
        self.assertEqual(outcomes[1]['status'], 'still_present')
        self.assertEqual(outcomes[1]['receipt_basis'], 'account_library_absent')
        self.assertEqual(receipt_basis('already_deleted', {'confirmed_absent': True}), 'account_library_absent')
        self.assertEqual(receipt_basis('deleted', {'success': True}), 'delete_success_receipt')
        self.assertEqual(receipt_basis('deleted', {}), 'recorded_terminal_without_receipt')
        self.assertIsNone(receipt_basis('failed', {'code': '10'}))


if __name__ == '__main__':
    unittest.main()
