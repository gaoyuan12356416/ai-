"""Idle cutover rehearsals use only temporary files and fake systemctl."""
from contextlib import closing, contextmanager, redirect_stdout
import copy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploy_when_idle as deploy


class Link:
    """Windows tests do not require privileged creation of directory symlinks."""
    def __init__(self, target):
        self.target = target

    def resolve(self):
        return self.target.resolve()

    def is_symlink(self):
        return True


class SimulatedCrash(BaseException):
    pass


class FakeHost:
    def __init__(self):
        self.service = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead',
                        'MainPID': '0', 'Job': '', 'UnitFileState': 'static'}
        self.timer = {'LoadState': 'loaded', 'ActiveState': 'active', 'UnitFileState': 'enabled'}
        self.events = []
        self.locked = False
        self.busy_lock = False
        self.mount_ok = True
        self.owners = {}
        self.on_timer_stop = lambda: None
        self.fail_timer_start = False

    def verify_mount(self, base):
        if not self.mount_ok:
            raise deploy.DeploymentError('required_data_disk_unavailable')

    def properties(self, unit):
        self.events.append(('show', unit, self.locked))
        return dict(self.service if unit == deploy.SERVICE else self.timer)

    def timer_stop(self):
        self.events.append(('stop', deploy.TIMER, self.locked))
        self.timer['ActiveState'] = 'inactive'
        self.on_timer_stop()

    def timer_start(self):
        if self.locked:
            raise AssertionError('Timer restored under worker lock')
        self.events.append(('start', deploy.TIMER, self.locked))
        if self.fail_timer_start:
            raise deploy.DeploymentError('host_command_failed')
        self.timer['ActiveState'] = 'active'

    @contextmanager
    def lock(self, path):
        if self.busy_lock:
            raise deploy.DeploymentError('worker_lock_busy', busy=True)
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def owner_alive(self, pid, start):
        return self.owners.get((pid, start), False)


class IdleDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / 'cleanup'
        self.base.mkdir()
        self.sha = 'a' * 40
        self.old = self.base / 'releases' / ('b' * 40)
        self.candidate = self.base / 'releases' / self.sha
        self.old.mkdir(parents=True)
        self.candidate.mkdir()
        self.config = self.base / 'config.json'
        self.shared = self.base / 'shared.sqlite3'
        self.original_config = {'runtime_dir': str(self.base), 'shared_ledger': str(self.shared),
            'lookback_days': 3, 'execution_workers': 32, 'live_enabled': True,
            'test_secret': 'DO-NOT-PRINT-SECRET'}
        self.config.write_text(json.dumps(self.original_config), encoding='utf-8')
        (self.base / 'process.lock').touch()
        with closing(sqlite3.connect(self.base / 'runs.sqlite3')) as conn:
            conn.execute('CREATE TABLE runs (status TEXT,data TEXT)')
            conn.commit()
        with closing(sqlite3.connect(self.shared)) as conn:
            conn.executescript('CREATE TABLE fb_asset_delete_v2_runs(actor TEXT,status TEXT,owner_pid INTEGER,owner_start TEXT);'
                'CREATE TABLE fb_asset_delete_v2_jobs(job_id TEXT,actor TEXT);'
                'CREATE TABLE fb_asset_delete_v2_object_locks(job_id TEXT,status TEXT);')
            conn.commit()
        for name in deploy.REQUIRED:
            path = self.candidate / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# isolated release fixture\n', encoding='utf-8')
        (self.candidate / deploy.SOURCE / 'meta_violation_cleanup/policy.py').write_text(
            'POLICY_VERSION="20261027-v3"\nPOLICY_CONTRACT={"min_distinct_violating_ads":5,'
            '"lookback_days":3,"pause_scope":"any_level","allow_active":False}\n', encoding='utf-8')
        self.make_proof()
        self.host = FakeHost()
        self.link = Link(self.old)
        self.patcher = patch.object(deploy, 'atomic_link', side_effect=lambda link, target: setattr(link, 'target', target))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def make_proof(self):
        proof = {'commit': self.sha, 'files': {name: deploy.digest((self.candidate/name).read_bytes())
                                             for name in deploy.REQUIRED}}
        (self.candidate / 'release-proof.json').write_text(json.dumps(proof), encoding='utf-8')

    def worker(self):
        worker = deploy.IdleDeployer(self.candidate, self.old, self.sha, self.config, host=self.host, base=self.base)
        worker.current = self.link
        return worker

    def files(self):
        return {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def test_preflight_is_read_only(self):
        before = self.files()
        result = self.worker().run()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(before, self.files())
        self.assertEqual(self.link.target, self.old)
        self.assertFalse(any(e[0] in ('start','stop') for e in self.host.events))

    def test_cutover_preserves_ledgers_and_other_config_restores_timer_after_unlock(self):
        before = {p: p.read_bytes() for p in (self.shared, self.base/'runs.sqlite3')}
        result = self.worker().run(apply=True)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.link.target, self.candidate)
        new = json.loads(self.config.read_text())
        self.assertEqual(new, dict(self.original_config, shared_reference_scan_workers=4))
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        actions = [e for e in self.host.events if e[0] in ('start','stop')]
        self.assertEqual(actions, [('stop', deploy.TIMER, True), ('start', deploy.TIMER, False)])
        receipt = json.loads(self.worker().receipt_path.read_text())
        self.assertEqual(receipt['status'], 'applied')
        self.assertFalse(receipt['timer_restore_pending'])
        self.assertNotIn('DO-NOT-PRINT-SECRET', json.dumps(receipt))
        self.assertEqual((Path(receipt['backup'])/'config.json').read_bytes(),
                         json.dumps(self.original_config).encode())

    def test_active_worker_or_lock_is_busy_without_any_mutation(self):
        for lock in (False, True):
            self.host.busy_lock = lock
            self.host.service.update(ActiveState='inactive' if lock else 'active', MainPID='0' if lock else '42')
            before = self.files()
            with self.assertRaises(deploy.DeploymentError) as error:
                self.worker().run(apply=True)
            self.assertTrue(error.exception.busy)
            self.assertEqual(before, self.files())
        self.assertFalse(any(e[0] in ('start','stop') for e in self.host.events))

    def test_runtime_owner_and_unknown_owner_are_busy_even_when_systemd_inactive(self):
        with closing(sqlite3.connect(self.base/'runs.sqlite3')) as conn:
            conn.execute('INSERT INTO runs VALUES (?,?)', ('running', json.dumps({'_owner': {'pid': 42, 'start': 'ticks'}})))
            conn.commit()
        for alive in (True, None):
            self.host.owners[(42, 'ticks')] = alive
            with self.subTest(alive=alive), self.assertRaises(deploy.DeploymentError) as error:
                self.worker().run(apply=True)
            self.assertTrue(error.exception.busy)
        self.host.owners[(42, 'ticks')] = False
        with self.assertRaisesRegex(deploy.DeploymentError, 'requires_recovery'):
            self.worker().run(apply=True)

    def test_shared_live_claim_blocks_but_unknown_fence_is_preserved(self):
        with closing(sqlite3.connect(self.shared)) as conn:
            conn.execute('INSERT INTO fb_asset_delete_v2_jobs VALUES (?,?)', ('old', deploy.ACTOR))
            conn.execute('INSERT INTO fb_asset_delete_v2_object_locks VALUES (?,?)', ('old', 'unknown'))
            conn.commit()
        before = self.shared.read_bytes()
        self.assertEqual(self.worker().run()['status'], 'ready')
        self.assertEqual(self.shared.read_bytes(), before)
        with closing(sqlite3.connect(self.shared)) as conn:
            conn.execute("UPDATE fb_asset_delete_v2_object_locks SET status='in_progress'")
            conn.commit()
        with self.assertRaisesRegex(deploy.DeploymentError, 'unsettled_shared_claim'):
            self.worker().run(apply=True)

    def test_new_worker_winning_race_is_never_stopped(self):
        self.host.on_timer_stop = lambda: self.host.service.update(ActiveState='active', MainPID='42')
        before = self.files()
        with self.assertRaises(deploy.DeploymentError) as error:
            self.worker().run(apply=True)
        self.assertTrue(error.exception.busy)
        self.assertEqual(self.host.service['MainPID'], '42')
        self.assertEqual(self.host.timer['ActiveState'], 'active')
        after = self.files()
        self.assertEqual(before, {path: after[path] for path in before})
        self.assertTrue(json.loads(self.worker().receipt_path.read_text())['timer_was_active'])
        self.assertEqual(self.link.target, self.old)

    def test_inactive_timer_stays_inactive_and_second_call_is_idempotent(self):
        self.host.timer['ActiveState'] = 'inactive'
        self.worker().run(apply=True)
        before = self.files()
        self.host.service.update(ActiveState='active', MainPID='88')
        self.assertEqual(self.worker().run(apply=True)['status'], 'already_applied')
        self.assertEqual(before, self.files())
        self.assertFalse(any(e[0] in ('start','stop') for e in self.host.events))

    def test_partial_switch_failure_restores_config_and_link_only_while_idle(self):
        original = self.config.read_bytes()
        count = [0]
        def failed_link(link, target):
            count[0] += 1
            if count[0] == 1:
                raise OSError('simulated link failure')
            link.target = target
        with patch.object(deploy, 'atomic_link', side_effect=failed_link), self.assertRaises(OSError):
            self.worker().run(apply=True)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.link.target, self.old)
        self.assertEqual(self.host.timer['ActiveState'], 'active')
        self.assertEqual(json.loads(self.worker().receipt_path.read_text())['status'], 'rolled_back')

    def test_cannot_restore_old_release_if_worker_appears_during_failure(self):
        def failed_link(link, target):
            self.host.service.update(ActiveState='active', MainPID='42')
            raise OSError('simulated race')
        with patch.object(deploy, 'atomic_link', side_effect=failed_link), self.assertRaisesRegex(
                deploy.DeploymentError, 'manual_reconciliation_required'):
            self.worker().run(apply=True)
        self.assertEqual(self.host.service['MainPID'], '42')
        self.assertEqual(self.host.timer['ActiveState'], 'inactive')
        self.assertEqual(json.loads(self.config.read_text())['shared_reference_scan_workers'], 4)

    def test_timer_restore_failure_is_retryable_without_second_cutover(self):
        self.host.fail_timer_start = True
        with self.assertRaises(deploy.DeploymentError):
            self.worker().run(apply=True)
        receipt = json.loads(self.worker().receipt_path.read_text())
        self.assertEqual(receipt['status'], 'applied')
        self.assertTrue(receipt['timer_restore_pending'])
        backups = list((self.base/'backups').iterdir())
        self.host.fail_timer_start = False
        self.assertEqual(self.worker().run(apply=True)['status'], 'already_applied')
        self.assertEqual(list((self.base/'backups').iterdir()), backups)
        self.assertFalse(json.loads(self.worker().receipt_path.read_text())['timer_restore_pending'])

    def test_bad_mount_release_hash_and_policy_fail_before_timer_actions(self):
        self.host.mount_ok = False
        with self.assertRaisesRegex(deploy.DeploymentError, 'data_disk'):
            self.worker().run(apply=True)
        self.host.mount_ok = True
        policy = self.candidate/deploy.SOURCE/'meta_violation_cleanup/policy.py'
        policy.write_text(policy.read_text().replace(':5', ':4'))
        with self.assertRaisesRegex(deploy.DeploymentError, 'hash_mismatch'):
            self.worker().run(apply=True)
        self.make_proof()
        with self.assertRaisesRegex(deploy.DeploymentError, 'policy_contract_mismatch'):
            self.worker().run(apply=True)
        self.assertFalse(any(e[0] in ('start','stop') for e in self.host.events))

    def test_prepared_receipt_with_unexpected_current_is_not_silently_replayed(self):
        self.worker().receipt_path.parent.mkdir()
        self.worker().receipt_path.write_text(json.dumps({'status': 'prepared', 'commit': self.sha}))
        self.link.target = self.candidate
        with self.assertRaisesRegex(deploy.DeploymentError, 'requires_reconciliation'):
            self.worker().run(apply=True)

    def test_crash_after_timer_stop_recovers_saved_original_timer_state(self):
        def crash():
            raise SimulatedCrash()
        self.host.on_timer_stop = crash
        with self.assertRaises(SimulatedCrash):
            self.worker().run(apply=True)
        prepared = json.loads(self.worker().receipt_path.read_text())
        self.assertEqual(prepared['status'], 'prepared')
        self.assertTrue(prepared['timer_was_active'])
        self.assertEqual(self.host.timer['ActiveState'], 'inactive')
        self.host.on_timer_stop = lambda: None
        self.assertTrue(self.worker().run()['recovering_prepared_cutover'])
        self.assertEqual(self.worker().run(apply=True)['status'], 'applied')
        self.assertEqual(self.host.timer['ActiveState'], 'active')
        self.assertEqual(len(list((self.base/'backups').iterdir())), 1)

    def test_crash_after_config_replace_recovers_mixed_config_and_old_link(self):
        original_write = deploy.atomic_bytes
        def crash_write(path, content, mode=0o600):
            original_write(path, content, mode)
            if path == self.config:
                raise SimulatedCrash()
        with patch.object(deploy, 'atomic_bytes', side_effect=crash_write), self.assertRaises(SimulatedCrash):
            self.worker().run(apply=True)
        self.assertEqual(self.link.target, self.old)
        self.assertEqual(json.loads(self.config.read_text())['shared_reference_scan_workers'], 4)
        self.assertEqual(self.host.timer['ActiveState'], 'inactive')
        self.worker().run(apply=True)
        self.assertEqual(self.link.target, self.candidate)
        self.assertEqual(self.host.timer['ActiveState'], 'active')
        self.assertEqual(len(list((self.base/'backups').iterdir())), 1)

    def test_crash_after_release_switch_finalizes_receipt_without_old_policy_rollback(self):
        def crash_link(link, target):
            link.target = target
            raise SimulatedCrash()
        with patch.object(deploy, 'atomic_link', side_effect=crash_link), self.assertRaises(SimulatedCrash):
            self.worker().run(apply=True)
        self.assertEqual(self.link.target, self.candidate)
        self.assertEqual(json.loads(self.worker().receipt_path.read_text())['status'], 'prepared')
        self.worker().run(apply=True)
        self.assertEqual(self.link.target, self.candidate)
        self.assertEqual(self.host.timer['ActiveState'], 'active')

    def test_crash_recovery_does_not_overwrite_unexpected_config_edits(self):
        def crash():
            raise SimulatedCrash()
        self.host.on_timer_stop = crash
        with self.assertRaises(SimulatedCrash):
            self.worker().run(apply=True)
        changed = dict(self.original_config, unrelated_operator_edit=123)
        self.config.write_text(json.dumps(changed))
        self.host.on_timer_stop = lambda: None
        with self.assertRaisesRegex(deploy.DeploymentError, 'state_changed_requires_reconciliation'):
            self.worker().run(apply=True)
        self.assertEqual(json.loads(self.config.read_text()), changed)
        self.assertEqual(self.host.timer['ActiveState'], 'inactive')

    def test_mount_guard_checks_runtime_target_not_only_mount_label(self):
        host = deploy.Host()
        with patch.object(host, 'command') as command, self.assertRaisesRegex(
                deploy.DeploymentError, 'outside_data_disk'):
            host.verify_mount(self.base)
        command.assert_not_called()
        with patch.object(host, 'command', side_effect=[deploy.DATA_UUID, 'other-device']), self.assertRaisesRegex(
                deploy.DeploymentError, 'data_disk_unavailable'):
            host.verify_mount(Path('/mnt/data-disk/test-cleanup'))

    def test_cli_busy_code_and_exception_output_are_bounded(self):
        args = ['--candidate-release', str(self.candidate), '--expected-current', str(self.old),
                '--expected-sha', self.sha, '--config', str(self.config)]
        for error, code in ((deploy.DeploymentError('worker_service_busy', busy=True),75),
                            (RuntimeError('DO-NOT-PRINT-SECRET'),1)):
            output = io.StringIO()
            with patch.object(deploy.IdleDeployer, 'run', side_effect=error), redirect_stdout(output):
                result = deploy.main(args)
            self.assertEqual(result, code)
            self.assertNotIn('DO-NOT-PRINT-SECRET', output.getvalue())


if __name__ == '__main__':
    unittest.main()
