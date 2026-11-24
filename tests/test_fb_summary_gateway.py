import datetime as dt
import json
import unittest
from unittest.mock import patch

from features.gpu_offload.cache import Response
from features.gpu_offload.gateway import recheck_fb_summary


class FacebookSummaryTests(unittest.TestCase):
    path = '/api/pipeline-monitor/summary'
    admin = {'role': 'admin'}

    def source(self, age=1):
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)).isoformat()
        return {'collected_at': at, 'code': 'snapshot_stale', 'available': False}

    def value(self, panel):
        return Response(200, {}, json.dumps({'fb_support': panel, 'warnings': ['unrelated']}).encode())

    @patch('features.gpu_offload.gateway.read_snapshot')
    def test_only_confirmed_sampler_data_can_resolve_the_warning(self, read):
        panel = self.source()
        response = self.value(panel)
        read.return_value = {'available': True, 'ok': False, 'code': 'attention',
                             'collected_at': panel['collected_at'], 'alerts': ['real service fault']}
        data = json.loads(recheck_fb_summary(response, self.admin, self.path).body)
        self.assertEqual(data, {'fb_support': read.return_value, 'warnings': ['unrelated']})
        for result in ({'available':False}, dict(read.return_value, collected_at=self.source(10)['collected_at'])):
            read.return_value = result
            self.assertIs(recheck_fb_summary(response, self.admin, self.path), response)

    @patch('features.gpu_offload.gateway.read_snapshot')
    def test_expired_future_unauthorized_and_unrelated_views_never_read_the_sampler(self, read):
        response = self.value(self.source())
        for value, actor, path in (
            (response, {'role':'user'}, self.path), (response, self.admin, '/api/pipeline-monitor/tasks'),
            (self.value(self.source(151)), self.admin, self.path), (self.value(self.source(-2)), self.admin, self.path),
            (self.value(None), self.admin, self.path), (self.value({'code':'snapshot_unavailable'}), self.admin, self.path),
            (Response(403, {}, response.body), self.admin, self.path),
        ):
            self.assertIs(recheck_fb_summary(value, actor, path), value)
        read.assert_not_called()
