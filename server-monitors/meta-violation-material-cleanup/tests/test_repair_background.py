import unittest
from unittest.mock import Mock
import repair_background as repair
from meta_violation_cleanup.throttle import Governor


class BackgroundSafetyTests(unittest.TestCase):
    def test_live_service_never_allows_followup_start(self):
        for status in ('running','completed','partial','failed','interrupted'):
            self.assertFalse(repair.may_start({'status':status},
                {'ActiveState':'active','MainPID':'3031550'}))

    def test_only_natural_completion_and_no_process_allows_start(self):
        for status in ('running','failed','interrupted','previewed'):
            self.assertFalse(repair.may_start({'status':status},{'ActiveState':'inactive','MainPID':'0'}))
        for status in ('completed','partial'):
            self.assertTrue(repair.may_start({'status':status},{'ActiveState':'inactive','MainPID':'0'}))
            self.assertFalse(repair.may_start({'status':status},{'ActiveState':'inactive','MainPID':'123'}))

    def test_transport_rejects_every_write_before_network(self):
        raw=Mock();session=repair.ReadOnlySession(raw,Governor(maximum=1,per_account=1,rps=2),lambda:False)
        for method in ('DELETE','POST','PUT','PATCH'):
            with self.assertRaisesRegex(RuntimeError,'read_only'):session.request(method,'https://example.test/')
        raw.request.assert_not_called()

    def test_transport_observes_rate_headers_on_get(self):
        raw=Mock();raw.request.return_value.status_code=200
        raw.request.return_value.headers={'x-app-usage':'{"call_count":75}'}
        raw.request.return_value.json.return_value={}
        governor=Governor(maximum=1,per_account=1,rps=2)
        session=repair.ReadOnlySession(raw,governor,lambda:False)
        session.request('GET','https://example.test/')
        self.assertEqual(governor.snapshot()['recent_usage_max_percent'],75)
        self.assertEqual(raw.request.call_count,1)


if __name__=='__main__':unittest.main()
