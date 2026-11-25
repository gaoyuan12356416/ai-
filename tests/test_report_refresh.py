import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from features import report_refresh as refresh


class ReportRefreshTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.old = '20261125T010000Z-12345'
        self.new = '20261125T020000Z-67890'
        self.publish(self.old)
        self.state = {'ActiveState': 'inactive', 'ExecMainStartTimestamp': 'Wed 2026-11-25 02:00:00 UTC'}
        state = patch.object(refresh, 'unit_state', return_value=self.state)
        state.start()
        self.addCleanup(state.stop)

    def publish(self, version):
        (self.root / 'current.json').write_text(json.dumps({'version': version}))

    def test_start_is_nonblocking_and_old_snapshot_is_not_success(self):
        with patch.object(refresh, 'systemctl') as ctl:
            started = refresh.start('unit', self.root)
            ctl.assert_called_once_with('start', '--no-block', 'unit')
        self.assertEqual(started, {'state': 'running', 'after': self.old})
        self.state['ActiveState'] = 'activating'
        self.assertEqual(refresh.progress('unit', self.root, self.old), started)
        self.state['ActiveState'] = 'inactive'
        self.assertEqual(refresh.progress('unit', self.root, self.old)['state'], 'failed')
        self.publish(self.new)
        self.assertEqual(refresh.progress('unit', self.root, self.old), {'state': 'complete', 'version': self.new})

    def test_active_timer_and_post_publication_cleanup(self):
        self.state['ActiveState'] = 'activating'
        with patch.object(refresh, 'systemctl') as ctl:
            self.assertEqual(refresh.start('unit', self.root)['after'], self.old)
            self.publish(self.new)
            self.assertEqual(refresh.start('unit', self.root), {'state': 'complete', 'version': self.new})
            ctl.assert_not_called()

    def test_only_explicit_browser_post_can_start(self):
        url = urlsplit('/api/social-analytics/refresh')
        handler = SimpleNamespace(command='POST', headers={})
        reply = lambda status, data: status
        with patch.object(refresh, 'start') as start:
            self.assertEqual(refresh.dispatch(handler, url, 'social', reply), 403)
            handler.headers = {'X-Report-Refresh': '1', 'Content-Length': '3'}
            self.assertEqual(refresh.dispatch(handler, url, 'social', reply), 400)
            handler.command = 'GET'
            self.assertEqual(refresh.dispatch(handler, url, 'social', reply), 400)
            start.assert_not_called()

    def test_both_routes_check_permission_before_refresh(self):
        from features.social_analytics.routes import dispatch as social
        from features.socialkit_analytics.routes import dispatch as kit
        from unittest.mock import Mock
        for name, dispatch in [('social', social), ('socialkit', kit)]:
            handler = Mock(command='POST')
            actor = getattr(handler, '_' + name + '_analytics_actor')
            url = urlsplit('/api/' + name + '-analytics/refresh')
            with patch.object(refresh, 'dispatch') as endpoint:
                actor.return_value = None
                dispatch(handler, url, {})
                endpoint.assert_not_called()
                actor.return_value = {'role': 'user'}
                dispatch(handler, url, {})
                self.assertEqual(endpoint.call_args.args[:3], (handler, url, name))
