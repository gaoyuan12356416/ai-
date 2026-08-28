import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
import migrate_cpu as migration
import mount_guard


# Opt-in only: run this one test as an already-authorized Linux root user. The
# source travels over stdin, so even a checkout on the data disk can be hidden.
# No source/temp files, service commands, production writes or credentials are
# needed in the child. A direct mount syscall avoids mount(8)'s mtab/helpers.
MOUNT_NAMESPACE_PROBE = r'''
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess
import sys

payload = json.load(sys.stdin)
libc = ctypes.CDLL(None, use_errno=True)
mount = libc.mount
mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                  ctypes.c_ulong, ctypes.c_char_p]
mount.restype = ctypes.c_int
print(json.dumps({'phase': 'started'}), flush=True)

write_attempts = []
guard_commands = []
write_events = {
    'os.mkdir', 'os.remove', 'os.rmdir', 'os.rename', 'os.link', 'os.symlink',
    'os.chmod', 'os.chown', 'os.truncate', 'os.utime', 'os.setxattr',
    'os.removexattr', 'os.mknod', 'shutil.copyfile', 'shutil.copymode',
    'shutil.copystat', 'shutil.rmtree', 'shutil.move',
}
write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
guard_command = ['findmnt', '-rn', '-o', 'TARGET,UUID,OPTIONS', '-T', payload['disk']]

def read_only_audit(event, args):
    writing = event in write_events
    if event == 'open':
        mode, flags = args[1], args[2]
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags, int) and bool(flags & write_flags))
    if writing:
        write_attempts.append(event)
        raise RuntimeError('probe attempted a filesystem write')
    if event == 'subprocess.Popen':
        if args[1] != guard_command:
            raise RuntimeError('probe attempted an unexpected subprocess')
        guard_commands.append(args[1])

sys.addaudithook(read_only_audit)

def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)

def mount_rows():
    with open('/proc/self/mountinfo', encoding='utf-8') as handle:
        return [line.split() for line in handle]

def main():
    # Compile/import the real guard before shadowing the source filesystem.
    guard = {'__name__': 'mount_guard_namespace_probe'}
    exec(compile(payload['source'], '<mount_guard from stdin>', 'exec'), guard)
    require(str(guard['DISK']) == payload['disk'] and guard['UUID'] == payload['uuid'],
            'guard disk identity changed')
    child_namespace = os.readlink('/proc/self/ns/mnt')
    require(child_namespace != payload['parent_namespace'], 'mount namespace was not isolated')
    rows = mount_rows()
    # unshare --propagation private must have removed propagation from every
    # inherited mount. Never mount anything if this proof is unavailable.
    require(all(not field.startswith(('shared:', 'master:', 'propagate_from:'))
                for row in rows for field in row[6:row.index('-')]),
            'mount propagation is not private')
    disk = payload['disk']
    require(os.path.ismount(disk) and os.path.realpath(disk) == disk,
            'expected existing data mount point is unavailable')
    original_mount_ids = {row[0] for row in rows}
    # MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC; only the private namespace
    # receives this empty 1 MiB mount. No directory is created or file opened
    # for writing, and no external mount helper can update host bookkeeping.
    if mount(b'cpu-migration-guard-test', os.fsencode(disk), b'tmpfs', 1 | 2 | 4 | 8,
             b'size=1048576') != 0:
        error = ctypes.get_errno()
        if error in (errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENODEV, errno.EINVAL):
            return {'status': 'blocked', 'reason': 'private read-only tmpfs unavailable',
                    'errno': error}
        raise RuntimeError('private tmpfs mount syscall failed')
    new_rows = [row for row in mount_rows() if row[0] not in original_mount_ids]
    require(len(new_rows) == 1 and new_rows[0][4] == disk,
            'unexpected private mount topology')
    overlay = new_rows[0]
    require(overlay[overlay.index('-') + 1] == 'tmpfs' and 'ro' in overlay[5].split(','),
            'overlay is not a read-only tmpfs')
    size = os.statvfs(disk)
    require(0 < size.f_blocks * size.f_frsize <= 1048576, 'overlay exceeds 1 MiB')
    require(os.listdir(disk) == [], 'overlay is not empty')
    rejected = []
    for path in payload['paths']:
        try:
            guard['check_mount']((path,))
        except RuntimeError as error:
            require(str(error) == 'approved CPU data disk is not mounted read-write',
                    'guard did not reject the missing approved disk')
            rejected.append(path)
        else:
            raise RuntimeError('guard accepted a missing approved data disk')
    require(not write_attempts and len(guard_commands) == len(payload['paths']),
            'guard attempted writes or did not run real findmnt checks')
    require(os.listdir(disk) == [], 'probe wrote files to the overlay')
    require(os.readlink('/proc/self/ns/mnt') == child_namespace,
            'child mount namespace changed')
    return {'status': 'passed', 'namespace': child_namespace, 'rejected_paths': rejected,
            'write_attempts': write_attempts, 'guard_findmnt_calls': len(guard_commands)}

try:
    result = main()
except BaseException as error:
    # No raw exception/traceback, environment or guard source in test output.
    result = {'status': 'failed', 'reason': type(error).__name__}
print(json.dumps(result), flush=True)
sys.exit(0 if result['status'] == 'passed' else 77 if result['status'] == 'blocked' else 1)
'''


class CpuMigrationTests(unittest.TestCase):
    def test_run_id_cannot_escape_backup_directory(self):
        self.assertEqual(migration.validate_run_id('gpu-service-migration-20260828T1502'),
                         'gpu-service-migration-20260828T1502')
        for value in ('../other', 'gpu-service-migration-20260828T1502/other', 'different-run'):
            with self.assertRaises(ValueError):
                migration.validate_run_id(value)

    def test_manifest_detects_changed_missing_files_without_deleting_extra_files(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source', Path(folder) / 'target'
            source.mkdir(); target.mkdir()
            (source / 'image.jpg').write_bytes(b'original-image')
            (source / 'second.jpg').write_bytes(b'second-image')
            shutil.copy2(str(source / 'image.jpg'), str(target / 'image.jpg'))
            (target / 'other-history.jpg').write_bytes(b'preserve-target-history')
            self.assertEqual(migration.compare_manifests(migration.tree_manifest(source),
                                                        migration.tree_manifest(target)), ['second.jpg'])
            (target / 'image.jpg').write_bytes(b'different-image')
            self.assertEqual(migration.compare_manifests(migration.tree_manifest(source),
                                                        migration.tree_manifest(target)), ['image.jpg', 'second.jpg'])

    def test_target_cannot_escape_data_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / 'base'; base.mkdir()
            with mock.patch.object(migration, 'BASE', base), mock.patch.object(migration, 'check_mount'):
                migration.validate_target(base / 'storage')
                with self.assertRaises(RuntimeError):
                    migration.validate_target(base / '..' / 'outside')

    def test_frozen_source_matches_exact_deployed_bytes(self):
        self.assertEqual(len(migration.verify_frozen_source(PACKAGE)['files']), 2)

    def test_frozen_source_refuses_changed_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder)
            (package / 'runtime').mkdir()
            (package / 'runtime/a.py').write_text('print(1)')
            manifest = {'files': [{'file': 'runtime/a.py', 'sha256': migration.digest(package / 'runtime/a.py')}]}
            (package / 'source-manifest.json').write_text(json.dumps(manifest))
            (package / 'runtime/a.py').write_text('print(2)')
            with self.assertRaises(RuntimeError):
                migration.verify_frozen_source(package)

    def valid_proof(self):
        return {'run_id': 'gpu-service-migration-20260828T1502', 'authorized_by_parent': True,
                'observed_at_epoch': 1000, 'writes_fenced': True, 'cron_fenced': True,
                'test_api_fenced': True, 'source_tunnels_stopped': True,
                'source_units': {unit: {'active': 'inactive', 'enabled': 'masked', 'children': 0}
                                 for unit in migration.SOURCE_UNITS}}

    def check_proof(self, value):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'proof.json'; path.write_text(json.dumps(value))
            with mock.patch.object(migration.time, 'time', return_value=1010):
                return migration.proof(path, 'gpu-service-migration-20260828T1502', source_stopped=True)

    def test_start_accepts_fresh_fenced_stopped_evidence(self):
        self.check_proof(self.valid_proof())

    def test_start_rejects_active_disabled_source(self):
        value = self.valid_proof()
        value['source_units'][migration.SOURCE_UNITS[0]]['active'] = 'active'
        with self.assertRaises(RuntimeError):
            self.check_proof(value)

    def test_start_rejects_remaining_source_children(self):
        value = self.valid_proof()
        value['source_units'][migration.SOURCE_UNITS[0]]['children'] = 1
        with self.assertRaises(RuntimeError):
            self.check_proof(value)

    def test_start_rejects_missing_test_or_cron_fence(self):
        for key in ('test_api_fenced', 'cron_fenced'):
            value = self.valid_proof(); value[key] = False
            with self.assertRaises(RuntimeError):
                self.check_proof(value)

    def test_start_rejects_stale_evidence(self):
        value = self.valid_proof(); value['observed_at_epoch'] = 1
        with self.assertRaises(RuntimeError):
            self.check_proof(value)

    def test_mount_guard_rejects_root_directory_fallback(self):
        with mock.patch.object(mount_guard.subprocess, 'check_output',
                               return_value='/ different-uuid rw,relatime\n'):
            with self.assertRaises(RuntimeError):
                mount_guard.check_mount()

    def test_mount_guard_rejects_readonly_disk(self):
        with mock.patch.object(mount_guard.subprocess, 'check_output',
                               return_value='/mnt/data-disk %s ro,relatime\n' % mount_guard.UUID):
            with self.assertRaises(RuntimeError):
                mount_guard.check_mount()

    def test_mount_guard_missing_disk_in_private_namespace(self):
        if os.environ.get('CPU_MIGRATION_MOUNT_NAMESPACE_TEST') != '1':
            self.skipTest('set CPU_MIGRATION_MOUNT_NAMESPACE_TEST=1 for the authorized Linux integration test')
        if sys.platform != 'linux' or not hasattr(os, 'geteuid') or os.geteuid() != 0:
            self.skipTest('Linux root is required; this test never elevates privileges')
        if sys.version_info < (3, 8):
            self.skipTest('Python 3.8+ is required for the read-only audit hook')
        # Deliberately do not inherit auth, proxy, SSH, Python or application env.
        environment = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}
        commands = {name: shutil.which(name, path=environment['PATH'])
                    for name in ('unshare', 'findmnt', 'systemctl')}
        if not all(commands.values()):
            self.skipTest('BLOCKED: unshare/findmnt/systemctl are required; no isolation fallback')
        if not Path('/proc/self/ns/mnt').exists():
            self.skipTest('BLOCKED: Linux mount namespaces are unavailable')
        source = (PACKAGE / 'mount_guard.py').read_text(encoding='utf-8')
        # These are the exact --path arguments in the three migrated units.
        guard_paths = ['/usr/share/nginx/html/drama-screenshot-materials', '/root/drama_screenshot_jobs',
                       '/usr/share/nginx/html/drama-materials', '/root/drama_material_jobs']
        properties = 'Id,LoadState,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestampMonotonic'

        def read_command(args):
            return subprocess.check_output(args, text=True, stderr=subprocess.PIPE,
                                           env=environment, timeout=5).strip()

        def host_snapshot():
            mount_row = read_command([commands['findmnt'], '-rn', '-o', 'TARGET,UUID,OPTIONS',
                                      '-T', str(mount_guard.DISK)])
            units = read_command([commands['systemctl'], 'show', '--no-pager',
                                  '--property=' + properties, *migration.OLD_CPU_UNITS])
            workers = {values['Id']: values for values in
                       (dict(line.split('=', 1) for line in block.splitlines())
                        for block in units.split('\n\n'))}
            return {'namespace': os.readlink('/proc/self/ns/mnt'),
                    'mount': mount_row, 'workers': workers}

        before = host_snapshot()
        row = before['mount'].split()
        if (len(row) != 3 or row[:2] != [str(mount_guard.DISK), mount_guard.UUID]
                or 'rw' not in row[2].split(',')):
            self.skipTest('BLOCKED: approved CPU data disk must be healthy before the isolated test')
        if (set(before['workers']) != set(migration.OLD_CPU_UNITS) or any(
                item.get('LoadState') != 'loaded' or item.get('ActiveState') != 'active'
                or not item.get('MainPID', '0').isdigit() or int(item.get('MainPID', '0')) <= 0
                for item in before['workers'].values())):
            self.skipTest('BLOCKED: all three original CPU workers must be active before this test')
        payload = json.dumps({'source': source, 'disk': str(mount_guard.DISK), 'uuid': mount_guard.UUID,
                              'parent_namespace': before['namespace'], 'paths': guard_paths})
        try:
            with subprocess.Popen(
                [commands['unshare'], '--mount', '--propagation', 'private',
                 sys.executable, '-I', '-B', '-c', MOUNT_NAMESPACE_PROBE],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=environment, cwd='/', start_new_session=True,
            ) as child:
                try:
                    stdout, stderr = child.communicate(payload, timeout=30)
                except subprocess.TimeoutExpired:
                    # Kill only this test's new process group, including any
                    # findmnt child; never signal services or try to restore PIDs.
                    os.killpg(child.pid, signal.SIGKILL)
                    child.communicate(timeout=5)
                    self.fail('isolated mount probe timed out; its private process group was terminated')
                if not stdout and child.returncode != 0 and any(message in stderr for message in (
                    'Operation not permitted', 'Permission denied', 'Function not implemented',
                    'Invalid argument', 'unrecognized option',
                )):
                    self.skipTest('BLOCKED: unshare could not establish a private namespace; no fallback')
                records = [json.loads(line) for line in stdout.splitlines()]
                self.assertEqual(len(records), 2, 'isolated probe did not return complete evidence')
                self.assertEqual(records[0], {'phase': 'started'})
                result = records[1]
                if child.returncode == 77 and result.get('status') == 'blocked':
                    self.skipTest('BLOCKED: private read-only tmpfs is unavailable; no fallback')
                self.assertEqual(child.returncode, 0, 'isolated guard probe failed (raw output suppressed)')
                self.assertEqual(result['status'], 'passed')
                self.assertNotEqual(result['namespace'], before['namespace'])
                self.assertEqual(result['rejected_paths'], guard_paths)
                self.assertEqual(result['write_attempts'], [])
                self.assertEqual(result['guard_findmnt_calls'], len(guard_paths))
        finally:
            # This runs after normal exit, blocked/skip, assertion and timeout.
            # Private mounts vanish with the child; the host never unmounts.
            self.assertEqual(host_snapshot(), before,
                             'host mount/namespace or original CPU worker state changed; no automatic repair')

    def test_start_never_steals_existing_source_port(self):
        socket_context = mock.MagicMock()
        socket_context.__enter__.return_value.bind.side_effect = OSError('Address already in use')
        with mock.patch.object(migration, 'assert_drained'), mock.patch.object(migration, 'check_mount'), \
             mock.patch.object(migration.socket, 'socket', return_value=socket_context), \
             mock.patch.object(migration, 'command') as execute:
            with self.assertRaises(OSError):
                migration.start_units()
            execute.assert_not_called()

    def test_storage_switch_waits_for_lease_after_job_reports_done(self):
        counts = {'drama_material_job': {'done': 1}, 'drama_material_job_worker_lease': {'running': 1}}
        with mock.patch.object(migration, 'status_counts', return_value=counts), \
             mock.patch.object(migration.os.path, 'isfile', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'lease must be released'):
                migration.assert_drained(include_drama=True)


if __name__ == '__main__':
    unittest.main()
