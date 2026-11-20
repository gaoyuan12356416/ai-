from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.moboreels_library import cli
from features.moboreels_library.orders import _period


class ScheduleTests(unittest.TestCase):
    def invoke(self, command):
        captured = []
        def response(request, timeout):
            captured.append(json.loads(request.data))
            return BytesIO(b'{"run":{"run_id":"receipt","status":"queued"},"reused":false}')
        opener = SimpleNamespace(open=response)
        with patch.object(cli.Settings, 'from_env', return_value=SimpleNamespace(internal_token='fixture', bind_port=8864)), \
             patch.object(cli.urllib.request, 'build_opener', return_value=opener), \
             patch.object(cli, 'datetime') as clock, patch('sys.argv', ['cli', command]), \
             patch('sys.stdout', new_callable=StringIO) as output:
            clock.now.return_value = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)
            cli.main()
        return captured[0], json.loads(output.getvalue())

    def test_hourly_orders_include_today_without_exceeding_31_days(self):
        request, receipt = self.invoke('report-sync')
        self.assertEqual(request['path'], '/report-sync-runs')
        self.assertEqual(request['body'], {'trigger':'scheduled','date_from':'2026-10-21','date_to':'2026-11-20'})
        start,end = _period(request['body'], datetime(2026,11,20).date())
        self.assertEqual((end-start).days,30)
        self.assertEqual(receipt['run_id'],'receipt')

    def test_catalog_sync_keeps_existing_request(self):
        request,_ = self.invoke('sync')
        self.assertEqual(request['path'],'/sync-runs')
        self.assertEqual(request['body'],{'trigger':'scheduled'})

if __name__ == '__main__': unittest.main()
