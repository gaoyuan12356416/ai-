#!/usr/bin/env python3
"""Revoke only this run's temporary US-to-HK archive key; dry-run by default.

Never restarts sshd, removes private keys or edits the CPU's long-lived tunnel
authorization. Reports only hashes, the pinned fingerprint and removal count.
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import socket
import stat
import subprocess
import tempfile

RUN_ROOT = pathlib.Path('/data/migrations/gpu-service-migration-20260828T1502')
BACKUP = RUN_ROOT / 'control/receiver-revoke'
KEYS = pathlib.Path('/root/.ssh/authorized_keys')
UUID = '659e6f89-71fa-463d-842e-ccdf2c06e0fe'
KEY_FP = 'SHA256:XjJCbxOEVNtFX0ZIjy7EvZvHRMRoc1UXL4fMh7dgoRw'
COMMENT = b'gpu-migration-receive-20260828T1502'
RECEIVE_COMMAND = ('/usr/bin/python3.9 /data/migrations/gpu-service-migration-20260828T1502'
                   + '/control-code/7c54dedd9d6f59a9c46431aac7f1782f00ba71d1'
                   + '/ops/gpu-service-migration-20260828/control/receive_archive.py')


def fingerprint(blob):
    digest = hashlib.sha256(base64.b64decode(blob, validate=True)).digest()
    return 'SHA256:' + base64.b64encode(digest).decode('ascii').rstrip('=')


def field(value):
    """Read one SSH field without interpreting quotes in the trailing comment."""
    value = value.lstrip(b' \t')
    quoted = escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == 92 and quoted:
            escaped = True
        elif char == 34:
            quoted = not quoted
        elif char in (9, 32) and not quoted:
            return value[:index], value[index:]
    if quoted or escaped:
        raise RuntimeError('ambiguous authorized_keys field')
    return value, b''


def check_options(options):
    parts = options.split(b',')
    required = {b'restrict', b'from="43.166.178.132"',
                b'command="' + RECEIVE_COMMAND.encode('ascii') + b'"'}
    if len(parts) != 3 or set(parts) != required:
        raise RuntimeError('temporary receiver restrictions changed')


def rewrite(raw, expected_fingerprint=KEY_FP):
    """Remove exactly one scoped key, preserving every other byte and newline."""
    lines = raw.splitlines(keepends=True)
    matches = []
    for index, line in enumerate(lines):
        body = line.rstrip(b'\r\n').lstrip(b' \t')
        if not body or body.startswith(b'#'):
            continue
        first, rest = field(body)
        options = b''
        if re.fullmatch(rb'(?:ssh|ecdsa|sk)-[^ \t]+', first):
            kind = first
        else:
            options = first
            kind, rest = field(rest)
        blob, comment = field(rest)
        comment = comment.strip(b' \t')
        try:
            pinned = fingerprint(blob) == expected_fingerprint
        except ValueError:
            pinned = False
        if pinned or comment == COMMENT:
            if not pinned or kind != b'ssh-ed25519' or comment != COMMENT:
                raise RuntimeError('temporary receiver fingerprint/type/comment changed')
            matches.append((index, options))
    if len(matches) != 1:
        raise RuntimeError('expected exactly one pinned temporary receiver key')
    index, options = matches[0]
    check_options(options)
    return b''.join(lines[:index] + lines[index + 1:])


def command(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=10)
    if result.returncode:
        raise RuntimeError('read-only prerequisite command failed')
    return result.stdout.strip()


def validate_data_mount(row, data_is_mount, data_device, root_device):
    """Accept the approved filesystem whether /data is its root or a directory."""
    fields = row.split(None, 2)
    if len(fields) != 3:
        raise RuntimeError('approved HK filesystem identity is ambiguous')
    target, uuid, options = fields
    if (target not in {'/', '/data'} or uuid != UUID
            or 'rw' not in options.split(',')):
        raise RuntimeError('approved HK filesystem identity changed')
    if target == '/data' and not data_is_mount:
        raise RuntimeError('approved HK data mount is no longer a mount point')
    if target == '/' and data_device != root_device:
        raise RuntimeError('HK data directory moved off the approved root filesystem')


def host_guard():
    if socket.gethostname() != 'VM-0-125-centos' or os.geteuid() != 0:
        raise RuntimeError('HK root host required; no privilege elevation')
    data = pathlib.Path('/data')
    if (not data.is_dir() or data.resolve(strict=True) != data
            or not os.path.ismount('/') or not os.access('/data', os.W_OK)):
        raise RuntimeError('approved HK data path is not available read-write')
    validate_data_mount(
        command(['findmnt', '-rn', '-o', 'TARGET,UUID,OPTIONS', '-T', '/data']),
        os.path.ismount('/data'), os.stat('/data').st_dev, os.stat('/').st_dev,
    )
    if (RUN_ROOT.resolve(strict=True) != RUN_ROOT or BACKUP.resolve() != BACKUP
            or os.stat(str(RUN_ROOT)).st_dev != os.stat('/data').st_dev
            or KEYS.parent.resolve(strict=True) != KEYS.parent):
        raise RuntimeError('backup or authorization path changed')


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_keys():
    with os.fdopen(os.open(str(KEYS), os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise RuntimeError('authorized_keys ownership/type/permissions changed')
        raw = handle.read()
        if signature(os.fstat(handle.fileno())) != signature(info):
            raise RuntimeError('authorized_keys changed while reading')
    return raw, signature(info)


def sync_directory(path):
    directory = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def apply_change(before, after, original):
    command(['/usr/sbin/sshd', '-t'])
    host_guard()
    BACKUP.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(BACKUP), 0o700)
    if (BACKUP.resolve(strict=True) != BACKUP or BACKUP.stat().st_uid != 0
            or BACKUP.stat().st_dev != os.stat('/data').st_dev):
        raise RuntimeError('private backup is not on the approved data disk')
    backup = BACKUP / ('authorized_keys.before.' + hashlib.sha256(before).hexdigest())
    with os.fdopen(os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600), 'wb') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(before)
        handle.flush()
        os.fsync(handle.fileno())
    if backup.read_bytes() != before or stat.S_IMODE(backup.stat().st_mode) != 0o600:
        raise RuntimeError('private backup verification failed')
    sync_directory(BACKUP)
    sync_directory(BACKUP.parent)
    fd, temporary = tempfile.mkstemp(prefix='.receiver-revoke-', dir=str(KEYS.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            os.fchown(handle.fileno(), original[3], original[4])
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        # Preserve SELinux labeling so unrelated authorized keys keep working.
        if 'security.selinux' in os.listxattr(str(KEYS), follow_symlinks=False):
            os.setxattr(temporary, 'security.selinux',
                        os.getxattr(str(KEYS), 'security.selinux', follow_symlinks=False))
        host_guard()
        if read_keys() != (before, original):
            raise RuntimeError('authorized_keys changed concurrently; replacement refused')
        os.replace(temporary, str(KEYS))
        sync_directory(KEYS.parent)
        if read_keys()[0] != after:
            raise RuntimeError('authorized_keys post-replacement verification failed')
        command(['/usr/sbin/sshd', '-t'])
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    host_guard()
    before, original = read_keys()
    after = rewrite(before)
    if args.apply:
        import fcntl
        # Cooperative invocations serialize; the content+inode reread also
        # rejects concurrent editors. Never restore keys automatically on error.
        with os.fdopen(os.open(str(KEYS), os.O_RDONLY | os.O_NOFOLLOW), 'rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if signature(os.fstat(lock.fileno())) != original:
                raise RuntimeError('authorized_keys changed before locking')
            apply_change(before, after, original)
    print(json.dumps({'dry_run': not args.apply, 'before_sha256': hashlib.sha256(before).hexdigest(),
                      'after_sha256': hashlib.sha256(after).hexdigest(),
                      'fingerprint': KEY_FP, 'removed_count': 1}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never print raw subprocess output, key material or exception arguments.
        print(json.dumps({'error': type(error).__name__}))
        raise SystemExit(1)
