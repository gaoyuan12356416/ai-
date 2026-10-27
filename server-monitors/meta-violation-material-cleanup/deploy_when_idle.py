#!/usr/bin/env python3
"""Switch a verified release only in an idle gap; never stop/start the worker."""
import argparse
import ast
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid


BASE = Path('/mnt/data-disk/meta-violation-material-cleanup')
DATA_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
SERVICE = 'meta-violation-material-cleanup.service'
TIMER = 'meta-violation-material-cleanup.timer'
ACTOR = 'automation:meta-violation-material-cleanup'
SOURCE = Path('server-monitors/meta-violation-material-cleanup')
REQUIRED = {str(SOURCE / name) for name in ('run.py', 'deploy_when_idle.py', 'run_idle_deployment.py')}
REQUIRED |= {str(SOURCE / 'meta_violation_cleanup' / (name + '.py')) for name in (
    'policy', 'material_scan', 'backlog_policy', 'execution_v3', 'phased_execution', 'reference_scan',
    'reports', 'runner', 'concurrent_execution', 'dependency_store', 'graph',
    'skip_policy', 'throttle', 'runtime', 'current_contexts', 'sql_source')}
REQUIRED.add('features/fb_ad_asset_delete/store.py')


class DeploymentError(RuntimeError):
    def __init__(self, code, busy=False):
        self.code, self.busy = code, busy
        super().__init__(code)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def fsync_directory(path):
    if os.name != 'nt':
        fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, content, mode=0o600):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def atomic_json(path, value):
    atomic_bytes(path, json_bytes(value))


def atomic_link(link, target):
    temp = link.with_name('.' + link.name + '.' + uuid.uuid4().hex)
    try:
        temp.symlink_to(target, target_is_directory=True)
        os.replace(temp, link)
        fsync_directory(link.parent)
    finally:
        if temp.is_symlink():
            temp.unlink()


class Host:
    def command(self, args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            raise DeploymentError('host_command_unavailable') from None
        if result.returncode:
            raise DeploymentError('host_command_failed')
        return result.stdout

    def properties(self, unit):
        output = self.command(['systemctl', 'show', unit, '--property=LoadState,ActiveState,SubState,MainPID,Job,UnitFileState,FragmentPath'])
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def timer_stop(self):
        self.command(['systemctl', 'stop', TIMER])

    def timer_start(self):
        self.command(['systemctl', 'start', TIMER])

    def verify_mount(self, base):
        mount = Path('/mnt/data-disk').resolve()
        if mount not in base.resolve().parents:
            raise DeploymentError('runtime_outside_data_disk')
        if (self.command(['findmnt', '-no', 'UUID', '--target', '/mnt/data-disk']).strip() != DATA_UUID
                or self.command(['findmnt', '-no', 'UUID', '--target', str(base.resolve())]).strip() != DATA_UUID):
            raise DeploymentError('required_data_disk_unavailable')
        if not os.access('/mnt/data-disk', os.W_OK) or shutil.disk_usage(base).free < 1024 ** 3:
            raise DeploymentError('data_disk_unwritable_or_low_space')

    @contextmanager
    def lock(self, path):
        import fcntl
        try:
            stream = open(path, 'rb')
        except OSError:
            raise DeploymentError('process_lock_unavailable') from None
        try:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError('worker_lock_busy', busy=True) from None
            yield
        finally:
            stream.close()

    def owner_alive(self, pid, identity):
        if type(pid) is not int or pid < 1 or not isinstance(identity, str) or not identity:
            return None
        try:
            raw = Path('/proc/%s/stat' % pid).read_text(encoding='ascii')
            fields = raw[raw.rfind(')') + 2:].split()
            if fields[0] in ('Z', 'X', 'x'):
                return False
            ticks = fields[19]
            if identity.startswith('linux:'):
                boot = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
                return identity == 'linux:%s:%s' % (boot, ticks)
            return identity == ticks
        except FileNotFoundError:
            return False
        except (OSError, IndexError, UnicodeError):
            return None


class IdleDeployer:
    def __init__(self, candidate, expected_current, expected_sha, config, host=None, base=BASE):
        self.base, self.host = Path(base).resolve(), host or Host()
        self.candidate = Path(candidate)
        self.expected_current = Path(expected_current)
        self.sha, self.config_path = expected_sha, Path(config)
        self.current = self.base / 'current'
        self.receipt_path = self.base / 'deployments' / (str(expected_sha) + '.json')

    def validate(self):
        if not re.fullmatch('[0-9a-f]{40}', self.sha or ''):
            raise DeploymentError('invalid_commit_sha')
        if not all(path.is_absolute() for path in (self.candidate, self.expected_current, self.config_path)):
            raise DeploymentError('absolute_paths_required')
        releases = self.base / 'releases'
        if (self.candidate.resolve() != releases / self.sha or not self.candidate.is_dir()
                or self.candidate.is_symlink() or self.expected_current.resolve().parent != releases
                or self.expected_current.is_symlink() or not self.expected_current.is_dir()
                or self.config_path != self.base / 'config.json' or self.config_path.is_symlink()
                or not self.current.is_symlink()):
            raise DeploymentError('deployment_path_scope_invalid')
        for directory in (releases, self.base / 'backups', self.base / 'deployments'):
            if directory.is_symlink() or directory.resolve().parent != self.base:
                raise DeploymentError('deployment_directory_scope_invalid')
        self.candidate = self.candidate.resolve()
        self.expected_current = self.expected_current.resolve()
        self.host.verify_mount(self.base)
        proof_path = self.candidate / 'release-proof.json'
        if proof_path.is_symlink():
            raise DeploymentError('release_proof_invalid')
        try:
            proof = json.loads(proof_path.read_text(encoding='utf-8'))
            files = proof['files']
        except (OSError, ValueError, KeyError, TypeError):
            raise DeploymentError('release_proof_invalid') from None
        if proof.get('commit') != self.sha or not isinstance(files, dict) or not REQUIRED <= set(files):
            raise DeploymentError('release_proof_incomplete')
        for name, expected in files.items():
            relative = Path(name)
            source = self.candidate / relative
            if (relative.is_absolute() or '..' in relative.parts or not isinstance(expected, str)
                    or not re.fullmatch('[0-9a-f]{64}', expected)
                    or not source.is_file() or source.is_symlink()
                    or self.candidate not in source.resolve().parents):
                raise DeploymentError('release_file_scope_invalid')
            if digest(source.read_bytes()) != expected:
                raise DeploymentError('release_file_hash_mismatch')
        # Validate the business contract without importing/executing candidate code.
        tree = ast.parse((self.candidate / SOURCE / 'meta_violation_cleanup/policy.py').read_text(encoding='utf-8-sig'))
        literals = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ('POLICY_VERSION', 'POLICY_CONTRACT'):
                        literals[target.id] = ast.literal_eval(node.value)
        if (literals.get('POLICY_VERSION') != '20261027-v3' or literals.get('POLICY_CONTRACT') != {
                'min_distinct_violating_ads': 5, 'lookback_days': 3, 'pause_scope': 'any_level', 'allow_active': False}):
            raise DeploymentError('candidate_policy_contract_mismatch')
        self.proof_hash = digest(proof_path.read_bytes())
        self.config_bytes = self.config_path.read_bytes()
        try:
            self.config = json.loads(self.config_bytes.decode('utf-8-sig'))
        except (UnicodeError, ValueError):
            raise DeploymentError('config_invalid') from None
        if not isinstance(self.config, dict) or Path(self.config.get('runtime_dir', '')).resolve() != self.base:
            raise DeploymentError('runtime_scope_mismatch')
        self.shared = Path(self.config.get('shared_ledger', ''))
        if (not self.shared.is_absolute() or not self.shared.is_file() or self.shared.is_symlink()
                or self.base.parent not in self.shared.resolve().parents):
            raise DeploymentError('shared_ledger_scope_invalid')

    def check_service_idle(self):
        state = self.host.properties(SERVICE)
        if state.get('LoadState') != 'loaded':
            raise DeploymentError('worker_unit_unavailable')
        if (state.get('ActiveState') not in ('inactive', 'failed') or state.get('MainPID') != '0'
                or state.get('Job', '').split(' ', 1)[0] not in ('', '0')):
            raise DeploymentError('worker_service_busy', busy=True)
        return state

    def check_idle(self):
        state = self.check_service_idle()
        with closing(sqlite3.connect('file:' + str(self.base / 'runs.sqlite3') + '?mode=ro', uri=True, timeout=2)) as conn:
            conn.execute('PRAGMA query_only=ON')
            for raw, in conn.execute("SELECT data FROM runs WHERE status='running'"):
                owner = json.loads(raw).get('_owner') or {}
                live = self.host.owner_alive(owner.get('pid'), owner.get('start'))
                if live is not False:
                    raise DeploymentError('runtime_owner_busy', busy=True)
                raise DeploymentError('runtime_run_requires_recovery')
        with closing(sqlite3.connect('file:' + str(self.shared) + '?mode=ro', uri=True, timeout=2)) as conn:
            conn.execute('PRAGMA query_only=ON')
            for pid, start in conn.execute("SELECT owner_pid,owner_start FROM fb_asset_delete_v2_runs WHERE actor=? AND status='running'", (ACTOR,)):
                if self.host.owner_alive(pid, start) is not False:
                    raise DeploymentError('shared_owner_busy', busy=True)
                raise DeploymentError('shared_run_requires_recovery')
            active = conn.execute("SELECT 1 FROM fb_asset_delete_v2_object_locks l JOIN fb_asset_delete_v2_jobs j "
                                  "ON j.job_id=l.job_id WHERE j.actor=? AND l.status='in_progress' LIMIT 1", (ACTOR,)).fetchone()
            if active:
                raise DeploymentError('unsettled_shared_claim')
        return state

    def restore_timer(self, receipt):
        if receipt.get('timer_restore_pending'):
            if receipt.get('timer_was_active'):
                self.host.timer_start()
                if self.host.properties(TIMER).get('ActiveState') != 'active':
                    raise DeploymentError('timer_restore_unconfirmed')
            receipt.update(timer_restore_pending=False, timer_restored_at=timestamp())
            atomic_json(self.receipt_path, receipt)

    def recover_intent(self, receipt):
        if (receipt.get('status') not in ('prepared', 'rolled_back')
                or receipt.get('commit') != self.sha
                or receipt.get('candidate_release') != str(self.candidate)
                or receipt.get('previous_release') != str(self.expected_current)
                or type(receipt.get('timer_was_active')) is not bool):
            raise DeploymentError('deployment_receipt_requires_reconciliation')
        backup = Path(receipt.get('backup', ''))
        config = backup / 'config.json'
        if (not backup.is_absolute() or backup.is_symlink() or config.is_symlink()
                or backup.resolve().parent != self.base / 'backups'):
            raise DeploymentError('deployment_backup_scope_invalid')
        original = config.read_bytes()
        if digest(original) != receipt.get('previous_config_sha256'):
            raise DeploymentError('deployment_backup_hash_mismatch')
        updated = dict(json.loads(original.decode('utf-8-sig')),
                       lookback_days=3, shared_reference_scan_workers=4)
        encoded = json_bytes(updated)
        if digest(encoded) != receipt.get('candidate_config_sha256'):
            raise DeploymentError('deployment_intent_config_mismatch')
        if (self.current.resolve() not in (self.expected_current, self.candidate)
                or digest(self.config_bytes) not in (digest(original), digest(encoded))):
            raise DeploymentError('deployment_state_changed_requires_reconciliation')
        return original, encoded

    def run(self, apply=False):
        # Busy retries must not repeatedly hash a prepared release while the
        # old worker is still running. This fast check performs no mutation.
        if self.current.resolve() != self.candidate.resolve():
            self.check_service_idle()
        self.validate()
        current = self.current.resolve()
        receipt = None
        recovering = False
        if self.receipt_path.is_file():
            receipt = json.loads(self.receipt_path.read_text(encoding='utf-8'))
            if (receipt.get('status') == 'applied' and receipt.get('commit') == self.sha
                    and receipt.get('candidate_release') == str(self.candidate) and current == self.candidate):
                if apply:
                    self.restore_timer(receipt)
                return {'status': 'already_applied', 'commit': self.sha,
                        'timer_restore_pending': receipt.get('timer_restore_pending', False)}
            original, encoded = self.recover_intent(receipt)
            recovering = True
        else:
            if current != self.expected_current:
                raise DeploymentError('current_release_changed')
            original = self.config_bytes
            encoded = json_bytes(dict(self.config, lookback_days=3, shared_reference_scan_workers=4))
        self.check_idle()
        failure = None
        rollback_safe = True
        with self.host.lock(self.base / 'process.lock'):
            self.check_idle()
            if self.current.resolve() != current or self.config_path.read_bytes() != self.config_bytes:
                raise DeploymentError('state_changed_before_cutover')
            timer = self.host.properties(TIMER)
            if timer.get('LoadState') != 'loaded' or timer.get('ActiveState') not in ('active', 'inactive'):
                raise DeploymentError('timer_state_unavailable')
            timer_was_active = receipt['timer_was_active'] if recovering else timer['ActiveState'] == 'active'
            if not apply:
                return {'status': 'ready', 'commit': self.sha, 'timer_was_active': timer_was_active,
                        'recovering_prepared_cutover': recovering}
            # Persist original timer intent and a verified config backup BEFORE
            # stopping it. A crash at any following boundary can be resumed.
            if not recovering:
                backups = self.base / 'backups' / ('v3-idle-' + self.sha + '-' + uuid.uuid4().hex[:12])
                backups.mkdir(mode=0o700, parents=True)
                atomic_bytes(backups / 'config.json', original)
                atomic_json(backups / 'state.json', {'current_release': str(self.expected_current),
                    'config_sha256': digest(original), 'service': self.host.properties(SERVICE),
                    'timer': timer, 'created_at': timestamp()})
                self.receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                receipt = {'status': 'prepared', 'commit': self.sha, 'candidate_release': str(self.candidate),
                    'previous_release': str(self.expected_current), 'release_proof_sha256': self.proof_hash,
                    'backup': str(backups), 'created_at': timestamp(), 'timer_was_active': timer_was_active,
                    'previous_config_sha256': digest(original), 'candidate_config_sha256': digest(encoded)}
            receipt.update(status='prepared', timer_restore_pending=timer_was_active)
            atomic_json(self.receipt_path, receipt)
            switched = False
            try:
                if timer['ActiveState'] == 'active':
                    self.host.timer_stop()
                    if self.host.properties(TIMER).get('ActiveState') != 'inactive':
                        raise DeploymentError('timer_stop_unconfirmed')
                self.check_idle()
                if self.current.resolve() != current or self.config_path.read_bytes() != self.config_bytes:
                    raise DeploymentError('state_changed_before_cutover')
                switched = True
                atomic_bytes(self.config_path, encoded)
                atomic_link(self.current, self.candidate)
                receipt.update(status='applied', applied_at=timestamp(), config_sha256=digest(encoded))
                atomic_json(self.receipt_path, receipt)
            except Exception as exc:
                failure = exc
                if switched:
                    try:
                        self.check_idle()
                        # Once candidate was already present on entry, never
                        # downgrade it to the old active-Ad deletion policy.
                        target = self.candidate if current == self.candidate else self.expected_current
                        content = encoded if current == self.candidate else original
                        atomic_bytes(self.config_path, content)
                        atomic_link(self.current, target)
                    except Exception:
                        rollback_safe = False
                receipt.update(status=('prepared' if current == self.candidate else 'rolled_back')
                               if rollback_safe else 'needs_attention',
                               failure_at=timestamp(), error='cutover_failed')
                try:
                    atomic_json(self.receipt_path, receipt)
                except Exception:
                    pass  # Preserve the original failure and timer intent.
        # Starting a persistent timer under process.lock could launch a worker
        # that immediately fails its nonblocking lock. Restore only after exit.
        if failure is not None:
            if rollback_safe:
                self.restore_timer(receipt)
            else:
                raise DeploymentError('cutover_failed_manual_reconciliation_required') from None
            raise failure
        self.restore_timer(receipt)
        return {'status': 'applied', 'commit': self.sha, 'receipt': str(self.receipt_path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-release', required=True)
    parser.add_argument('--expected-current', required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = IdleDeployer(args.candidate_release, args.expected_current, args.expected_sha, args.config).run(args.apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except DeploymentError as exc:
        print(json.dumps({'status': 'busy' if exc.busy else 'error', 'code': exc.code}))
        return 75 if exc.busy else 1
    except Exception:
        print(json.dumps({'status': 'error', 'code': 'deployment_preflight_or_io_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
