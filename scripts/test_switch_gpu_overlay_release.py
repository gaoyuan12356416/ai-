"""No SSH, systemd or production paths: verify the one-time switch contract."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import switch_gpu_overlay_release as bridge


class IdleTests(unittest.TestCase):
    def properties(self, **changes):
        return {'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': '0',
                'ControlPID': '0', 'ControlGroup': '', **changes}

    def test_rejects_active_deactivating_failed_and_live_control_process(self):
        for changes in ({'ActiveState': 'active'}, {'ActiveState': 'deactivating'},
                        {'ActiveState': 'failed'}, {'SubState': 'stop'},
                        {'MainPID': '8'}, {'ControlPID': '9'}):
            with self.subTest(changes=changes), patch.object(bridge, 'properties', return_value=self.properties(**changes)), \
                    patch.object(bridge, 'command') as command:
                with self.assertRaises(ValueError): bridge.assert_idle()
                command.assert_not_called()

    def test_pending_job_rejected_but_unrelated_units_allowed(self):
        with patch.object(bridge, 'properties', return_value=self.properties()), \
                patch.object(bridge, 'no_cgroup_processes') as cgroup, \
                patch.object(bridge, 'command', return_value='1 ' + bridge.UNIT + ' start waiting'):
            with self.assertRaisesRegex(ValueError, 'unit_has_pending_job'): bridge.assert_idle()
            cgroup.assert_not_called()
        with patch.object(bridge, 'properties', return_value=self.properties()), \
                patch.object(bridge, 'no_cgroup_processes') as cgroup, \
                patch.object(bridge, 'command', return_value='1 unrelated.service start running'):
            bridge.assert_idle()
            cgroup.assert_called_once_with('')

    def test_cgroup_v2_recursive_residual_process_and_empty_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'cgroup.controllers').touch()
            child = root / 'system.slice' / bridge.UNIT / 'child'; child.mkdir(parents=True)
            (child / 'cgroup.procs').write_text('42\n')
            with self.assertRaisesRegex(ValueError, 'unit_cgroup_not_empty'):
                bridge.no_cgroup_processes('', root=root)
            (child / 'cgroup.procs').write_text('')
            bridge.no_cgroup_processes('', root=root)

    def test_v1_absent_group_is_empty_and_unknown_mount_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'systemd_cgroup_mount_missing'):
                bridge.no_cgroup_processes('', root=root)
            (root / 'systemd').mkdir()
            bridge.no_cgroup_processes('', root=root)

    def test_user_ack_is_required_before_state_reads_or_writes(self):
        for function, args in ((bridge.apply, (Path('current'), Path('candidate'), Path('backup'))),
                               (bridge.rollback, (Path('current'), Path('backup')))):
            with patch.object(bridge, 'assert_idle') as idle:
                with self.assertRaisesRegex(ValueError, 'user_pause_confirmation_required'):
                    function(*args)
                idle.assert_not_called()


class SourceTests(unittest.TestCase):
    def test_git_origin_commit_and_cleanliness_are_required(self):
        good = ['a' * 40, '', 'https://github.com/gaoyuan12356416/ai-.git']
        for replies in (['b' * 40], ['a' * 40, ' M changed'],
                        ['a' * 40, '', 'https://example.org/other.git']):
            with patch.object(bridge, 'command', side_effect=replies):
                with self.assertRaises(ValueError): bridge.clean_github(Path('source'), 'a' * 40)
        with patch.object(bridge, 'command', side_effect=good):
            bridge.clean_github(Path('source'), 'a' * 40)

    def test_overlay_rejects_unrecorded_change_or_extra_python(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory); node = root / 'runtime' / 'node'
            source = root / 'source'; base = root / 'base'
            for path in (node, source, base): path.mkdir(parents=True)
            (base / 'old.py').write_bytes(b'old')
            (base / 'unchanged.py').write_bytes(b'unchanged')
            (node / 'unchanged.py').write_bytes(b'unchanged')
            files = {'old.py': bridge.digest(b'new'), 'new.py': bridge.digest(b'added')}
            for path in (node, source):
                (path / 'old.py').write_bytes(b'new'); (path / 'new.py').write_bytes(b'added')
            for name, value in [('RUNTIME', node.parent), ('NODE', node), ('SOURCE', source),
                                ('BASE', base), ('FILES', files)]:
                stack.enter_context(patch.object(bridge, name, value))
            manifest = {'status': 'applied', 'host': 'hk', 'commit': bridge.OVERLAY_SHA,
                        'source': str(source), 'lanes': {'node': {
                            'candidate': str(node), 'unit': bridge.UNIT, 'python': str(bridge.PYTHON),
                            'previous': str(base), 'files': {name: {'after': value} for name, value in files.items()}}}}
            (node.parent / 'manifest.json').write_text(json.dumps(manifest))
            stack.enter_context(patch.object(bridge, 'clean_github'))
            stack.enter_context(patch.object(bridge, 'command', return_value='old.py\nunchanged.py'))
            bridge.verify_overlay_files()
            (node / 'unchanged.py').write_bytes(b'unrecorded hotfix')
            with self.assertRaisesRegex(ValueError, 'unrecorded_runtime_change'): bridge.verify_overlay_files()
            (node / 'unchanged.py').write_bytes(b'unchanged')
            (node / 'extra.py').write_bytes(b'extra')
            with self.assertRaisesRegex(ValueError, 'unrecorded_runtime_python'): bridge.verify_overlay_files()
            (node / 'extra.py').unlink(); (node / 'new.py').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'overlay_file_changed'): bridge.verify_overlay_files()


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.state = self.root / 'pipeline-dispatch'; self.state.mkdir()
        self.systemd = self.root / 'systemd'; self.systemd.mkdir()
        self.runtime = self.root / 'runtime'; self.runtime.mkdir()
        self.node = self.runtime / 'node'; self.node.mkdir()
        self.env = self.root / 'old.env'; self.env.write_bytes(b'private-old-env')
        self.current = self.root / 'current.json'; self.current.write_bytes(b'{"old":true}')
        self.candidate = self.root / 'candidate.json'; self.candidate.write_bytes(b'{"new":true}')
        for name, value in [('STATE', self.state), ('SYSTEMD', self.systemd), ('RUNTIME', self.runtime),
                            ('NODE', self.node), ('OLD_ENV', self.env)]:
            self.stack.enter_context(patch.object(bridge, name, value))
        self.base, self.cache, self.lease = bridge.paths()
        self.cache.parent.mkdir()
        self.old_unit, self.new_unit = b'old-unit', b'new-unit'
        self.base.write_bytes(self.old_unit); self.cache.write_bytes(b'cache')
        self.lease.write_bytes(bridge.lease_bytes())
        self.overlay_manifest = b'{"runtime":"reviewed"}'
        self.files = {'features/test.py': bridge.digest(b'reviewed source')}
        self.stack.enter_context(patch.object(bridge, 'FILES', self.files))
        self.stack.enter_context(patch.object(bridge, 'BASE_UNIT_SHA', bridge.digest(self.old_unit)))
        self.stack.enter_context(patch.object(bridge, 'CACHE_SHA', bridge.digest(b'cache')))
        self.stack.enter_context(patch.object(bridge, 'assert_authorized', side_effect=lambda paused: bridge.require(paused, 'user_pause_confirmation_required')))
        self.idle = self.stack.enter_context(patch.object(bridge, 'assert_idle'))
        self.started = self.stack.enter_context(patch.object(bridge, 'assert_started'))
        self.stack.enter_context(patch.object(bridge, 'properties', side_effect=self.props))
        self.commands = []
        self.cmd = self.stack.enter_context(patch.object(bridge, 'command', side_effect=self.command))
        self.after = {'sha': 'a' * 40, 'release': str(self.root / 'release'),
                      'venv': str(self.root / 'venv'), 'staging': str(self.root / 'staged')}
        self.snapshot = {'install-manifest.before.json': self.current.read_bytes(),
                         'install-manifest.after.json': self.candidate.read_bytes(),
                         'node.service.before': self.old_unit, 'node.service.after': self.new_unit,
                         'node.env.before': self.env.read_bytes(), 'node.env.after': b'private-new-env',
                         'cache.conf.before': self.cache.read_bytes(), 'lease.conf.before': self.lease.read_bytes(),
                         'runtime-manifest.before.json': self.overlay_manifest,
                         'overlay/features/test.py': b'reviewed source'}
        self.inspector = self.stack.enter_context(patch.object(bridge, 'inspect', return_value=(self.after, self.snapshot)))
        self.stack.enter_context(patch.object(bridge, 'verify_overlay_files', return_value=self.overlay_manifest))
        self.folder = self.state / 'maintenance' / 'gpu-full-offload-test'

    def props(self):
        return {'FragmentPath': str(self.base), 'DropInPaths': ' '.join(map(str, [self.cache] + ([self.lease] if self.lease.exists() else []))),
                'KillMode': 'mixed', 'TimeoutStopUSec': 'infinity',
                'WorkingDirectory': str(self.node) if self.lease.exists() else self.after['release']}

    def command(self, argv):
        self.commands.append(argv)
        return ''

    def apply(self):
        return bridge.apply(self.current, self.candidate, self.folder, paused=True)

    def test_apply_and_rollback_preserve_cache_and_runtime_and_never_stop(self):
        result = self.apply()
        self.assertEqual(result['status'], 'started')
        self.assertEqual(self.base.read_bytes(), self.new_unit)
        self.assertEqual(self.current.read_bytes(), self.snapshot['install-manifest.after.json'])
        self.assertFalse(self.lease.exists())
        self.assertEqual(self.cache.read_bytes(), b'cache')
        self.assertEqual((self.folder / 'node.env.before').read_bytes(), b'private-old-env')
        bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(self.base.read_bytes(), self.old_unit)
        self.assertEqual(self.lease.read_bytes(), self.snapshot['lease.conf.before'])
        self.assertEqual(self.current.read_bytes(), self.snapshot['install-manifest.before.json'])
        self.assertEqual(self.cache.read_bytes(), b'cache')
        self.assertEqual(self.commands, [['systemctl', 'daemon-reload'], ['systemctl', 'start', bridge.UNIT]] * 2)
        self.assertGreaterEqual(self.idle.call_count, 6)
        self.assertEqual(json.loads((self.folder / 'audit.json').read_text())['status'], 'rolled_back')

    def test_state_drift_during_backup_refuses_live_mutation(self):
        changed = dict(self.snapshot, **{'node.env.after': b'changed'})
        self.inspector.side_effect = [(self.after, self.snapshot), (self.after, changed)]
        with self.assertRaisesRegex(ValueError, 'state_changed_during_backup'): self.apply()
        self.assertEqual(self.base.read_bytes(), self.old_unit)
        self.assertTrue(self.lease.exists()); self.assertEqual(self.commands, [])

    def test_failed_start_records_inspection_and_can_restore_after_external_stop(self):
        def fail(argv):
            self.commands.append(argv)
            if argv[1] == 'start': raise ValueError('start_failed')
            return ''
        self.cmd.side_effect = fail
        with self.assertRaisesRegex(ValueError, 'start_failed'): self.apply()
        self.assertEqual(json.loads((self.folder / 'audit.json').read_text())['status'], 'needs_inspection')
        self.assertFalse(self.lease.exists())
        self.cmd.side_effect = self.command
        bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(self.base.read_bytes(), self.old_unit)

    def test_interrupted_before_dropin_removal_can_restore(self):
        original = bridge.atomic
        def fault(path, data, mode):
            if Path(path) == self.current: raise OSError('simulated write failure')
            return original(path, data, mode)
        with patch.object(bridge, 'atomic', side_effect=fault):
            with self.assertRaisesRegex(OSError, 'simulated'): self.apply()
        self.assertEqual(self.base.read_bytes(), self.new_unit)
        self.assertTrue(self.lease.exists())
        bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(self.base.read_bytes(), self.old_unit)

    def test_interrupted_after_dropin_removal_before_reload_restores_cached_old_layout(self):
        cached_before = self.props()
        def fail_reload(argv):
            self.commands.append(argv)
            if argv[1] == 'daemon-reload': raise ValueError('reload_failed')
            return ''
        self.cmd.side_effect = fail_reload
        with self.assertRaisesRegex(ValueError, 'reload_failed'): self.apply()
        self.assertFalse(self.lease.exists())
        self.assertEqual(json.loads((self.folder / 'audit.json').read_text())['status'], 'applying')
        self.cmd.side_effect = self.command
        # Systemd still reports the exact old 99 drop-in even though disk no
        # longer has it. Rollback must accept this journalled partial state.
        with patch.object(bridge, 'properties', side_effect=[cached_before, cached_before]):
            bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(self.base.read_bytes(), self.old_unit)
        self.assertEqual(self.lease.read_bytes(), self.snapshot['lease.conf.before'])

    def test_failed_first_unit_write_leaves_backup_and_can_restore(self):
        original = bridge.atomic
        def fault(path, data, mode):
            if Path(path) == self.base: raise OSError('unit_write_failed')
            return original(path, data, mode)
        with patch.object(bridge, 'atomic', side_effect=fault):
            with self.assertRaisesRegex(OSError, 'unit_write_failed'): self.apply()
        self.assertEqual(self.base.read_bytes(), self.old_unit)
        self.assertEqual(self.commands, [])
        bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(self.base.read_bytes(), self.old_unit)

    def test_rollback_refuses_changed_cache_and_unrelated_unit(self):
        self.apply(); self.commands.clear()
        self.cache.write_bytes(b'new owner config')
        with self.assertRaisesRegex(ValueError, 'rollback_preserved_config_changed'):
            bridge.rollback(self.current, self.folder, paused=True)
        self.cache.write_bytes(b'cache'); self.base.write_bytes(b'unrelated release')
        with self.assertRaisesRegex(ValueError, 'rollback_unit_drift'):
            bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(self.commands, [])

    def test_rollback_start_failure_records_inspection_and_allows_later_retry(self):
        self.apply(); self.commands.clear()
        def fail_start(argv):
            self.commands.append(argv)
            if argv[1] == 'start':
                self.assertEqual(json.loads((self.folder / 'audit.json').read_text())['status'], 'rolling_back')
                raise ValueError('rollback_start_failed')
            return ''
        self.cmd.side_effect = fail_start
        with self.assertRaisesRegex(ValueError, 'rollback_start_failed'):
            bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(json.loads((self.folder / 'audit.json').read_text())['status'], 'needs_inspection')
        self.assertEqual(self.base.read_bytes(), self.old_unit)
        self.assertTrue(self.lease.exists())
        self.cmd.side_effect = self.command
        bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(json.loads((self.folder / 'audit.json').read_text())['status'], 'rolled_back')

    def test_rollback_first_write_failure_records_inspection_before_any_restore(self):
        self.apply(); self.commands.clear()
        original = bridge.atomic
        def fault(path, data, mode):
            if Path(path) == self.base:
                self.assertEqual(json.loads((self.folder / 'audit.json').read_text())['status'], 'rolling_back')
                raise OSError('rollback_first_write_failed')
            return original(path, data, mode)
        with patch.object(bridge, 'atomic', side_effect=fault):
            with self.assertRaisesRegex(OSError, 'rollback_first_write_failed'):
                bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(json.loads((self.folder / 'audit.json').read_text())['status'], 'needs_inspection')
        self.assertEqual(self.base.read_bytes(), self.new_unit)
        self.assertFalse(self.lease.exists())
        self.assertEqual(self.commands, [])
        bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(json.loads((self.folder / 'audit.json').read_text())['status'], 'rolled_back')

    def test_backup_corruption_and_extra_file_records_rejected(self):
        self.apply(); self.commands.clear()
        (self.folder / 'node.service.before').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'backup_file_changed'):
            bridge.rollback(self.current, self.folder, paused=True)
        self.assertEqual(self.commands, [])

    def test_backup_cannot_escape_state_maintenance_or_reuse_existing(self):
        with self.assertRaisesRegex(ValueError, 'backup_path_invalid'):
            bridge.backup_path(self.root / 'gpu-full-offload-wrong', exists=False)
        self.apply()
        with self.assertRaisesRegex(ValueError, 'backup_existence_mismatch'):
            bridge.backup_path(self.folder, exists=False)

    def test_unexpected_dropin_rejected_before_service_commands(self):
        props = self.props(); props['DropInPaths'] += ' /etc/systemd/system/x.conf'
        with self.assertRaisesRegex(ValueError, 'unit_dropins_changed'):
            bridge.verify_layout(props, overlay=True)
        self.assertEqual(self.commands, [])


if __name__ == '__main__':
    unittest.main()
