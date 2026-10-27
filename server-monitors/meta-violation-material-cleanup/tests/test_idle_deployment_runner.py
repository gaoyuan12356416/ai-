import json
import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

import run_idle_deployment as wrapper


class DeploymentTimerTests(unittest.TestCase):
    def test_busy_does_not_touch_any_timer(self):
        host = Mock()
        with patch.object(wrapper.deploy_when_idle, 'main', return_value=75):
            self.assertEqual(wrapper.run(['--apply'], host), 75)
        host.command.assert_not_called()

    def test_preflight_does_not_retire_timer(self):
        host = Mock()
        with patch.object(wrapper.deploy_when_idle, 'main', return_value=0):
            self.assertEqual(wrapper.run([], host), 0)
        host.command.assert_not_called()

    def test_applied_receipt_retires_only_own_timer(self):
        host = Mock(); sha = '1' * 40
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp); release = base / 'releases' / sha
            release.mkdir(parents=True); (base / 'deployments').mkdir()
            (base / 'current').mkdir()
            record = {'status': 'applied', 'commit': sha, 'timer_restore_pending': False,
                      'candidate_release': str((base / 'current').resolve())}
            (base / 'deployments' / (sha + '.json')).write_text(json.dumps(record), encoding='utf-8')
            with patch.object(wrapper.deploy_when_idle, 'BASE', base), patch.object(wrapper.deploy_when_idle, 'main', return_value=0):
                self.assertEqual(wrapper.run(['--apply', '--expected-sha', sha], host), 0)
        host.command.assert_called_once_with(['systemctl', 'disable', '--now', wrapper.TIMER])


if __name__ == '__main__':
    unittest.main()
