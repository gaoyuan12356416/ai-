"""Switch only the immutable SocialKit refresh release; no service restart."""
import argparse
import datetime
import json
import os
import subprocess
from pathlib import Path

from install import DATA, RELEASE, atomic_write, check_mount

BASE = 'b5290500817c3054a018378ed2dc28f994a2c4a1'
CURRENT = Path('/opt/socialkit-analytics/current')


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def switch(target):
    temporary = CURRENT.with_name('.refresh-writer-' + str(os.getpid()))
    os.symlink(str(target), str(temporary))
    os.replace(str(temporary), str(CURRENT))


def install():
    check_mount()
    old = CURRENT.resolve()
    commit = git(RELEASE, 'rev-parse', 'HEAD')
    if git(old, 'rev-parse', 'HEAD') != BASE or git(old, 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('Current refresh release changed; reconcile before switching')
    if git(RELEASE, 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('New refresh release has local changes')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = DATA / 'backups' / ('refresh-writer-' + stamp)
    backup.mkdir(parents=True)
    manifest = dict(commit=commit, release=str(RELEASE), old_release=str(old), old_commit=BASE,
                    restart_required=False, snapshots_preserved=True)
    path = backup / 'manifest.json'
    atomic_write(path, json.dumps(manifest, indent=2).encode())
    switch(RELEASE)
    print(json.dumps(dict(manifest, backup=str(path))))


def rollback(path):
    path = Path(path).resolve()
    if DATA / 'backups' not in path.parents:
        raise ValueError('Unexpected backup path')
    manifest = json.loads(path.read_text())
    if str(CURRENT.resolve()) != manifest['release']:
        raise RuntimeError('A later release prevents automatic rollback')
    old = Path(manifest['old_release'])
    if git(old, 'rev-parse', 'HEAD') != manifest['old_commit'] or git(old, 'status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('Rollback release changed')
    switch(old)
    print('Refresh code restored. Current snapshots, report facts and business receipts retained; no restart required.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--apply', action='store_true')
    action.add_argument('--rollback')
    args = parser.parse_args()
    install() if args.apply else rollback(args.rollback)
