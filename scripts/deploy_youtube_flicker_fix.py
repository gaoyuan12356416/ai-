#!/usr/bin/env python3
"""Deploy only YouTube HTML/JS; never stop services or mutate publishing data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time


ROOT = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
MOUNT_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
KIND = 'youtube-flicker-fix'
EXPECTED = {
    'static/youtube-publish.js': 'a3e1df0d87f749598a1190f806463bc0c6d8731d546746a21a4ac7cfec325d68',
    'static/youtube-publish.html': 'faddceb3c861ac44b1cd6794102a93c6f3f0c781185a7e1a3630065973ef06c3',
}


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=60).strip()


def file_bytes(path):
    path = Path(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 8 * 1024 * 1024:
        raise RuntimeError('Expected a bounded regular file: ' + str(path))
    return path.read_bytes(), metadata


def sha(path):
    return hashlib.sha256(file_bytes(path)[0]).hexdigest()


def targets():
    # Install JS before the HTML that references its new cache version.
    return [(name, target, expected) for name, expected in EXPECTED.items()
            for target in (ROOT / name, PUBLIC / Path(name).name)]


def verify_stage(stage, commit):
    if re.fullmatch(r'[a-f0-9]{40}', str(commit or '')) is None:
        raise RuntimeError('An exact GitHub commit is required')
    marker, _ = file_bytes(stage / '.github-verified-commit')
    if marker.decode('ascii').strip() != commit:
        raise RuntimeError('Exact GitHub-fetched archive marker mismatch')


def guard(stage, root, public=None):
    args = [sys.executable, str(stage / 'scripts/verify_live_feature_guard.py'),
            '--manifest', str(stage / 'deploy/live_feature_guard.json'), '--root', str(root)]
    if public is not None:
        args += ['--public-root', str(public)]
    return run(*args)


def verify_mount():
    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != MOUNT_UUID:
        raise RuntimeError('Data mount mismatch')
    if not BASE.is_dir() or not os.access(BASE, os.W_OK):
        raise RuntimeError('Deployment backup directory is unavailable')
    if shutil.disk_usage(BASE).free < 32 * 1024 * 1024:
        raise RuntimeError('Insufficient space for static file backups')


def install(data, target, metadata):
    """Write a sibling temporary file, then atomically replace one target."""
    target = Path(target)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.flicker-', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            if hasattr(os, 'fchown'):
                os.fchown(output.fileno(), metadata['uid'], metadata['gid'])
            os.chmod(temporary, metadata['mode'])
        os.replace(temporary, target)
        if os.name == 'posix':
            directory = os.open(str(target.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def candidate_inputs(stage):
    result = []
    for name, target, expected in targets():
        data, _ = file_bytes(stage / name)
        original, metadata = file_bytes(target)
        if hashlib.sha256(original).hexdigest() != expected:
            raise RuntimeError('Live baseline changed: ' + str(target))
        result.append((data, original, {
            'target': str(target), 'sha256': expected,
            'installed_sha256': hashlib.sha256(data).hexdigest(),
            'mode': stat.S_IMODE(metadata.st_mode), 'uid': metadata.st_uid, 'gid': metadata.st_gid,
        }))
    return result


def load_backup(backup):
    backup = Path(backup).resolve(strict=True)
    if (BASE / 'backups').resolve() not in backup.parents:
        raise RuntimeError('Backup outside the deployment backup directory')
    raw, _ = file_bytes(backup / 'manifest.json')
    manifest = json.loads(raw)
    records = manifest.get('files') or []
    allowed = {str(target) for _, target, _ in targets()}
    if (manifest.get('kind') != KIND or len(records) != 4
            or {row.get('target') for row in records} != allowed
            or re.fullmatch(r'[a-f0-9]{40}', str(manifest.get('commit') or '')) is None):
        raise RuntimeError('Invalid flicker-fix backup manifest')
    for row in records:
        if (any(re.fullmatch(r'[a-f0-9]{64}', str(row.get(key) or '')) is None
                for key in ('sha256', 'installed_sha256'))
                or any(type(row.get(key)) is not int or row[key] < 0 for key in ('mode', 'uid', 'gid'))
                or row['mode'] > 0o7777):
            raise RuntimeError('Invalid backup file metadata')
        saved = backup / 'files' / row['target'].lstrip('/')
        if sha(saved) != row['sha256']:
            raise RuntimeError('Backup integrity mismatch: ' + row['target'])
    return backup, manifest


def verify_rollback_state(manifest, partial=False):
    for row in manifest['files']:
        allowed = {row['installed_sha256']}
        if partial:
            allowed.add(row['sha256'])
        if sha(row['target']) not in allowed:
            raise RuntimeError('Newer live bytes exist; refuse rollback: ' + row['target'])


def restore(backup, manifest, partial=False):
    # Validate every file before restoring any file. Public rollback requires
    # all four installed SHAs; only immediate failure recovery allows old bytes.
    verify_rollback_state(manifest, partial=partial)
    for row in manifest['files']:
        source = backup / 'files' / row['target'].lstrip('/')
        data, _ = file_bytes(source)
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise RuntimeError('Backup changed during restoration')
        if sha(row['target']) != row['sha256']:
            install(data, row['target'], row)
    for row in manifest['files']:
        if sha(row['target']) != row['sha256']:
            raise RuntimeError('Restored bytes mismatch: ' + row['target'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--commit')
    mode.add_argument('--rollback', type=Path)
    parser.add_argument('--check', action='store_true', help='Validate only; do not create backups or install files')
    args = parser.parse_args()
    stage = Path(__file__).resolve().parents[1]
    verify_mount()
    if args.rollback:
        backup, manifest = load_backup(args.rollback)
        verify_stage(stage, manifest['commit'])
        verify_rollback_state(manifest)
        if args.check:
            print(json.dumps({'check': 'passed', 'rollback': str(backup), 'files': 4}))
            return
    else:
        verify_stage(stage, args.commit)
        inputs = candidate_inputs(stage)
        guard(stage, stage)
        guard(stage, ROOT, PUBLIC)
        if args.check:
            print(json.dumps({'check': 'passed', 'commit': args.commit, 'files': 4,
                              'sha256': {row['target']: row['installed_sha256'] for _, _, row in inputs},
                              'services_restarted': False}))
            return
    # Static files do not require idle publishers. No service or job state gates.
    import fcntl
    with (BASE / 'flicker-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            backup, manifest = load_backup(args.rollback)
            restore(backup, manifest)
            checked = guard(stage, ROOT, PUBLIC)
            print(json.dumps({'rollback': str(backup), 'files': 4, 'feature_guard': checked,
                              'services_restarted': False, 'database': 'untouched', 'material_sql': 'untouched'}))
            return
        # Freeze candidate bytes and recheck current files after acquiring the lock.
        inputs = candidate_inputs(stage)
        backup = BASE / 'backups' / ('flicker-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(parents=True, exist_ok=False)
        records = []
        for _, original, row in inputs:
            saved = backup / 'files' / row['target'].lstrip('/')
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(original)
            os.chmod(saved, row['mode'])
            records.append(row)
        manifest = {'kind': KIND, 'commit': args.commit, 'files': records}
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        backup, manifest = load_backup(backup)
        try:
            for data, _, row in inputs:
                if sha(row['target']) != row['sha256']:
                    raise RuntimeError('Live bytes changed before install: ' + row['target'])
                install(data, row['target'], row)
            for _, _, row in inputs:
                if sha(row['target']) != row['installed_sha256']:
                    raise RuntimeError('Installed bytes mismatch: ' + row['target'])
            checked = guard(stage, ROOT, PUBLIC)
        except BaseException:
            # Never overwrite an unrelated intervening deployment during recovery.
            restore(backup, manifest, partial=True)
            raise
        result = {'commit': args.commit, 'backup': str(backup), 'files': 4,
                  'sha256': {row['target']: sha(row['target']) for row in records},
                  'feature_guard': checked, 'services_restarted': False,
                  'database': 'untouched', 'material_sql': 'untouched'}
        (backup / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
