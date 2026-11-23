import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from features.tt_auto_posts.publisher import AutoPostExecutor
from features.tt_posts.service import GPUClientError


class PostingBanTests(unittest.TestCase):
    def result(self, code, *, publish_id=''):
        executor=object.__new__(AutoPostExecutor)
        executor.store=SimpleNamespace(transition_task=Mock())
        executor._now=lambda:dt.datetime.now(dt.timezone.utc)
        task=SimpleNamespace(id=1,publish_id=publish_id,unknown_outcome=False,status='selecting',material_id='')
        error=GPUClientError('tt_upstream_rejected','upstream rejected',502,details={'upstream_code':code})
        executor._retry_or_fail(task,'selection',error,'original-claim')
        return executor.store.transition_task.call_args

    def test_explicit_posting_ban_is_terminal(self):
        call=self.result('spam_risk_user_banned_from_posting')
        self.assertEqual(call.args,(1,'failed'))
        self.assertEqual(call.kwargs['updates']['error_code'],'tt_upstream_rejected')

    def test_transient_upstream_failure_still_retries(self):
        self.assertEqual(self.result('internal_error').args,(1,'retry_wait'))

    def test_existing_publish_evidence_always_reconciles(self):
        call=self.result('spam_risk_user_banned_from_posting',publish_id='original-publish')
        self.assertEqual(call.args,(1,'retry_wait'))
        self.assertEqual(call.kwargs['updates']['claim_phase'],'reconcile')


if __name__=='__main__':unittest.main()
