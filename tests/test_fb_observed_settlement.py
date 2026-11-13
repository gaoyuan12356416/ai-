import copy
import json
import unittest

from scripts.settle_fb_observed_video import select_observed_operation


def chain():
    central, native, attempts = [], [], []
    for sequence, kind, code, trace in [(1, 'definite_failure', 'fb_graph_190', 'trace1'), (2, 'unknown', 'fb_graph_response_outcome_unknown', '')]:
        operation_id = 'operation' + str(sequence)
        result = {'operation_id': operation_id, 'kind': kind, 'error_code': code, 'trace_id': trace, 'post_id': ''}
        central.append({'operation_id': operation_id, 'operation': 'fb_video_publish', 'generation': 1, 'request_sha256': 'request'+str(sequence)})
        native.append({'operation_id': operation_id, 'sequence': sequence, 'stage': 'publish', 'generation': 1,
                       'request_sha256': 'request'+str(sequence), 'credential_id': 'credential'+str(sequence),
                       'fb_user_id': 'user'+str(sequence), 'result_json': json.dumps(result), 'checkpoint_json': json.dumps(result)})
        attempts.append({'sequence': sequence, 'credential_id': 'credential'+str(sequence), 'fb_user_id': 'user'+str(sequence),
                         'result_kind': kind, 'error_code': code, 'trace_id': trace})
    return central, native, attempts


class ObservedSettlementTest(unittest.TestCase):
    def test_final_unknown_after_explicit_rejection(self):
        c, n, a = chain()
        self.assertEqual(select_observed_operation(c, n, a)['operation_id'], 'operation2')

    def test_prior_unknown_is_not_permission_to_settle_later_write(self):
        c, n, a = chain()
        r = json.loads(n[0]['result_json']);r['kind'] = a[0]['result_kind'] = 'unknown'
        n[0]['result_json'] = n[0]['checkpoint_json'] = json.dumps(r)
        with self.assertRaisesRegex(ValueError, 'prior_attempt_not_explicit'):
            select_observed_operation(c, n, a)

    def test_generic_failed_result_is_not_graph_rejection(self):
        c, n, a = chain()
        r = json.loads(n[0]['result_json']);r['error_code'] = a[0]['error_code'] = 'source_connection_failed'
        n[0]['result_json'] = n[0]['checkpoint_json'] = json.dumps(r)
        with self.assertRaisesRegex(ValueError, 'prior_attempt_not_explicit'):
            select_observed_operation(c, n, a)

    def test_missing_or_extra_operation_is_rejected(self):
        c, n, a = chain();c.append(copy.deepcopy(c[-1]));c[-1]['operation_id'] = 'unresolved-third'
        with self.assertRaisesRegex(ValueError, 'attempt_chain_incomplete'):
            select_observed_operation(c, n, a)

    def test_changed_checkpoint_or_credential_is_rejected(self):
        for field, value in [('checkpoint_json', '{}'), ('credential_id', 'another-credential')]:
            c, n, a = chain();n[0][field] = value
            with self.assertRaisesRegex(ValueError, 'attempt_proof_mismatch'):
                select_observed_operation(c, n, a)

    def test_confirmed_prior_post_is_rejected(self):
        c, n, a = chain();r = json.loads(n[0]['result_json']);r['post_id'] = '123'
        n[0]['result_json'] = n[0]['checkpoint_json'] = json.dumps(r)
        with self.assertRaisesRegex(ValueError, 'attempt_proof_mismatch'):
            select_observed_operation(c, n, a)


if __name__ == '__main__':
    unittest.main()
