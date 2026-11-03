"""CPU activation preserves identity and requests one natural drain only."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts import activate_pipeline_cpu_transport as activation


class CpuActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / 'state'
        self.state.mkdir()
        self.old = {'node_id': 'cpu-main', 'backend': 'cpu', 'sha': 'a' * 40, 'state_root': '/mnt/data-disk/pipeline-dispatch'}
        self.new = {**self.old, 'sha': 'b' * 40}
        self.current, self.candidate = root / 'current.json', root / 'candidate.json'
        for path, value in ((self.current, self.old), (self.candidate, self.new)):
            path.write_text(json.dumps(value)); path.chmod(0o600)
        self.infos = {}
        self.env = {'PIPELINE_NODE_ID': 'cpu-main', 'PIPELINE_RENDER_BACKEND': 'cpu',
                    'PIPELINE_NODE_STATE_ROOT': str(self.state), 'PIPELINE_NODE_CONFIG': str(self.state) + '/credentials.json'}
        for spec in (self.old, self.new):
            sha = spec['sha']
            release, staging, venv = root / 'release' / sha, root / 'staged' / sha, root / 'venv' / sha
            (release / 'features/pipeline_dispatch').mkdir(parents=True)
            source = Path(activation.__file__).resolve().parents[1] / 'features/pipeline_dispatch/worker.py'
            # Production releases are Linux Git checkouts; normalize the local
            # Windows checkout's line endings to the reviewed repository bytes.
            (release / 'features/pipeline_dispatch/worker.py').write_bytes(source.read_text().encode())
            staging.mkdir(parents=True)
            (staging / '.verified').write_text(sha)
            (staging / 'node.env').write_text(''.join(key + '=' + json.dumps(value) + '\n' for key, value in self.env.items()))
            unit = 'pipeline-node-cpu-main.service'
            (staging / unit).write_text('KillMode=mixed\nTimeoutStopSec=infinity\nExecStart=' + str(release) + '/scripts/pipeline_worker.py\n')
            self.infos[sha] = {'node_id': 'cpu-main', 'sha': sha, 'state': str(self.state), 'root': str(root),
                               'unit': unit, 'release': str(release), 'staging': str(staging), 'venv': str(venv)}
        self.systemd = root / 'systemd'; self.systemd.mkdir()
        self.unit = self.systemd / unit
        shutil.copyfile(Path(self.infos[self.old['sha']]['staging']) / unit, self.unit)
        self.proc = root / 'proc'
        process = self.proc / '123'
        (process / 'task/123').mkdir(parents=True)
        (process / 'task/123/children').write_text('124 125 ')
        (process / 'cmdline').write_bytes((self.infos[self.old['sha']]['release'] + '/scripts/pipeline_worker.py\0').encode())
        (process / 'environ').write_bytes('\0'.join(key + '=' + value for key, value in self.env.items()).encode())
        self.credentials = self.state / 'credentials.json'
        self.credentials.write_text('{"node_id":"cpu-main","private":"test-only"}')
        self.calls = []
        self.runtime_state = 'active'

    def command(self, argv, **kwargs):
        args = [str(value) for value in argv]
        self.calls.append(args)
        if args[0] == 'git':
            return Path(args[2]).name if args[3] == 'rev-parse' else ''
        if args[:2] == ['systemctl', 'show']:
            return 'ActiveState=' + self.runtime_state + '\nMainPID=123\nKillMode=mixed\nTimeoutStopUSec=infinity'
        if args[0] == 'install':
            shutil.copyfile(args[-2], args[-1])
        return ''

    def invoke(self, *, apply=False, run=None):
        with patch.object(activation, 'plan', side_effect=lambda spec: self.infos[spec['sha']]):
            return activation.activate(self.current, self.candidate, apply=apply, run=run or self.command,
                                       systemd_root=self.systemd, proc_root=self.proc)

    def test_read_only_checks_dependencies_without_restart_or_filesystem_change(self):
        original = self.unit.read_bytes(), self.current.read_bytes(), self.credentials.read_bytes()
        result = self.invoke()
        self.assertEqual(result['children_before'], ['124', '125'])
        self.assertTrue(result['dependencies_verified'])
        self.assertEqual((self.unit.read_bytes(), self.current.read_bytes(), self.credentials.read_bytes()), original)
        self.assertFalse((self.state / 'maintenance').exists())
        self.assertFalse(any(args[:2] == ['systemctl', 'restart'] for args in self.calls))
        checks = [args for args in self.calls if args[0] == 'runuser']
        self.assertEqual(len(checks), 2)
        self.assertIn('scripts.test_pipeline_x_upload_transport', checks[1])

    def test_apply_preserves_credentials_and_signals_only_cpu_parent_once(self):
        original = self.credentials.read_bytes()
        result = self.invoke(apply=True)
        self.assertEqual(self.credentials.read_bytes(), original)
        self.assertEqual(json.loads(self.current.read_text()), self.new)
        self.assertEqual(result['status'], 'draining_current_jobs_then_start_verified_release')
        restarts = [args for args in self.calls if args[:2] == ['systemctl', 'restart']]
        self.assertEqual(restarts, [['systemctl', 'restart', '--no-block', 'pipeline-node-cpu-main.service']])
        self.assertFalse(any('kill' in arg or 'gpu' in arg for args in restarts for arg in args))
        backup = Path(result['backup'])
        self.assertEqual(json.loads((backup / 'install-manifest.before.json').read_text()), self.old)
        self.assertEqual((backup / 'node.service.before').read_bytes(), (Path(self.infos[self.old['sha']]['staging']) / self.unit.name).read_bytes())
        self.assertFalse(result['capacities_changed'])
        self.assertFalse(result['credentials_changed'])

    def test_gpu_and_configuration_changes_are_rejected(self):
        for change in ({'backend': 'gpu'}, {'node_id': 'other'}, {'environment': {'PIPELINE_PUBLISH_MAX': '20'}}):
            with self.subTest(change=change):
                self.candidate.write_text(json.dumps({**self.new, **change}))
                with self.assertRaises(ValueError):
                    self.invoke(apply=True)
        self.assertFalse(any(args[0] == 'install' for args in self.calls))

    def test_pending_restart_is_not_interfered_with(self):
        self.runtime_state = 'deactivating'
        with self.assertRaisesRegex(ValueError, 'runtime_not_active_or_safe'):
            self.invoke(apply=True)
        self.assertFalse(any(args[0] == 'install' for args in self.calls))

    def test_new_runtime_environment_must_remain_exact(self):
        env = Path(self.infos[self.new['sha']]['staging']) / 'node.env'
        env.write_text(env.read_text() + 'PIPELINE_PUBLISH_MAX="64"\n')
        with self.assertRaisesRegex(ValueError, 'node_environment_changed'):
            self.invoke(apply=True)

    def test_safe_stop_properties_and_reviewed_worker_are_required(self):
        candidate = Path(self.infos[self.new['sha']]['staging']) / self.unit.name
        candidate.write_text(candidate.read_text().replace('infinity', '90'))
        with self.assertRaisesRegex(ValueError, 'unsafe_unit_drain'):
            self.invoke(apply=True)
        worker = Path(self.infos[self.new['sha']]['release']) / 'features/pipeline_dispatch/worker.py'
        worker.write_text('unreviewed worker')
        with self.assertRaisesRegex(ValueError, 'unreviewed_drain_worker'):
            self.invoke(apply=True)

    def test_runtime_change_during_preflight_blocks_activation(self):
        seen = 0
        def changing(argv, **kwargs):
            nonlocal seen
            if argv[:2] == ['systemctl', 'show']:
                seen += 1
                if seen > 1:
                    self.runtime_state = 'deactivating'
            return self.command(argv, **kwargs)
        with self.assertRaisesRegex(ValueError, 'runtime_changed_before_activation'):
            self.invoke(apply=True, run=changing)
        self.assertFalse(any(args[0] == 'install' for args in self.calls))


if __name__ == '__main__':
    unittest.main()
