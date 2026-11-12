"""Scoped code install/rollback. Service activation is a separate safe API switch."""
import argparse
import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess

from install import DATA, RUNTIME, WEB, RELEASE, atomic_write, check_mount

BASE = 'df6d9ec3cfab9a220586c118adcd38d145968ea4'
# The main API only serves queries. Collection runs from CURRENT, and its older
# unused main-runtime copy must not be overwritten by this report change.
FILES = ['features/socialkit_analytics/query.py']
FILES += ['static/socialkit-analytics.js', 'static/socialkit-analytics.html']
CURRENT = Path('/opt/socialkit-analytics/current')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def switch_release(target):
    temporary = CURRENT.with_name('.current-published-accounts-' + str(os.getpid()))
    os.symlink(str(target), str(temporary))
    os.replace(str(temporary), str(CURRENT))


def install():
    check_mount()
    commit = subprocess.check_output(['git', '-C', str(RELEASE), 'rev-parse', 'HEAD'], text=True).strip()
    changed = subprocess.check_output(['git', '-C', str(RELEASE), 'status', '--porcelain', '--untracked-files=no'], text=True)
    if changed:
        raise RuntimeError('Release code has local changes')
    old_release = str(CURRENT.resolve())
    if subprocess.check_output(['git', '-C', old_release, 'rev-parse', 'HEAD'], text=True).strip() != BASE:
        raise RuntimeError('Refresh release changed; reconcile before installing')
    writes = []
    for relative in FILES:
        source = RELEASE / relative
        base = subprocess.run(['git', '-C', str(RELEASE), 'show', BASE + ':' + relative], capture_output=True)
        baseline = base.stdout if base.returncode == 0 else None
        targets = [RUNTIME / relative]
        if relative.startswith('static/'):
            targets.append(WEB / source.name)
        for destination in targets:
            old = destination.read_bytes() if destination.exists() else None
            expected, new = baseline, source.read_bytes()
            if relative == 'static/socialkit-analytics.html' and old is not None:
                # Shared navigation cache versions are maintained independently.
                pattern = rb'(?<=/quick-nav\.js\?v=)[^" ]+'
                versions = re.findall(pattern, old)
                if len(versions) == 1:
                    expected = re.sub(pattern, lambda _: versions[0], baseline)
                    new = re.sub(pattern, lambda _: versions[0], new)
            if old != expected:
                raise RuntimeError('Concurrent runtime change: ' + str(destination))
            writes.append((destination, old, new))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = DATA / 'backups' / ('published-accounts-' + stamp)
    backup.mkdir(parents=True)
    manifest = dict(commit=commit, release=str(RELEASE), old_release=old_release, files=[])
    for index, (destination, old, new) in enumerate(writes):
        name = str(index) + '.bak'
        mode = destination.stat().st_mode & 0o777 if old is not None else 0o644
        if old is not None:
            atomic_write(backup / name, old, mode)
        manifest['files'].append(dict(path=str(destination), existed=old is not None,
                                     backup=name, mode=mode, installed_sha256=digest(new)))
    path = backup / 'manifest.json'
    atomic_write(path, json.dumps(manifest, indent=2).encode())
    for destination, old, new in writes:
        atomic_write(destination, new, destination.stat().st_mode & 0o777 if old is not None else 0o644)
    switch_release(RELEASE)
    print(json.dumps(dict(commit=commit, backup=str(path), restart_required='drama-material-api.service')))


def rollback(path):
    path = Path(path).resolve()
    if DATA / 'backups' not in path.parents:
        raise ValueError('Unexpected backup path')
    manifest = json.loads(path.read_text())
    if str(CURRENT.resolve()) != manifest['release']:
        raise RuntimeError('Later release prevents automatic rollback')
    for entry in manifest['files']:
        if digest(Path(entry['path']).read_bytes()) != entry['installed_sha256']:
            raise RuntimeError('Later file change prevents rollback: ' + entry['path'])
    for entry in manifest['files']:
        target = Path(entry['path'])
        if entry['existed']:
            atomic_write(target, (path.parent / entry['backup']).read_bytes(), entry['mode'])
        else:
            target.unlink()
    switch_release(manifest['old_release'])
    print('Code restored; activate via a scoped safe API restart. Keep all current facts and snapshots.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--apply', action='store_true')
    action.add_argument('--rollback')
    args = parser.parse_args()
    install() if args.apply else rollback(args.rollback)
