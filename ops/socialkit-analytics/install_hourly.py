"""Install the hourly release without replacing the shared API/navigation/publishers."""
import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
RELEASE = HERE.parents[1]
spec = importlib.util.spec_from_file_location('socialkit_install_base', HERE / 'install.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
DATA = Path('/mnt/data-disk/socialkit-analytics')
CURRENT = Path('/opt/socialkit-analytics/current')
TIMER = 'socialkit-analytics-refresh.timer'
SERVICE = 'socialkit-analytics-refresh.service'
DIGEST_TIMER = 'socialkit-gap-digest.timer'
DIGEST_SERVICE = 'socialkit-gap-digest.service'


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def unit_value(unit, prop):
    return command('systemctl', 'show', unit, '-p', prop, '--value')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def intended_writes():
    # The active collector imports shared analytics from this immutable Git release.
    # The live main API and other independent releases are deliberately not overlaid.
    writes = {}
    for name in ('socialkit-analytics.html', 'socialkit-analytics.js'):
        content = (RELEASE / 'static' / name).read_bytes()
        for target in (Path('/root/drama_material_service/static'), Path('/usr/share/nginx/html')):
            writes[target / name] = content
    for name in (SERVICE, TIMER, DIGEST_SERVICE, DIGEST_TIMER):
        writes[Path('/etc/systemd/system') / name] = (HERE / name).read_bytes()
    return writes


def verify_release(commit):
    if command('git', '-C', str(RELEASE), 'rev-parse', 'HEAD') != commit:
        raise RuntimeError('Release commit mismatch')
    command('git', '-C', str(RELEASE), 'diff', '--exit-code', 'HEAD')
    for folder in ('socialkit_analytics', 'socialkit_gap_alert', 'social_analytics'):
        for path in (RELEASE / 'features' / folder).glob('*.py'):
            compile(path.read_bytes(), str(path), 'exec')
    command('node', '--check', str(RELEASE / 'static/socialkit-analytics.js'))
    command('systemd-analyze', 'verify', *(str(HERE / name) for name in (SERVICE, TIMER, DIGEST_SERVICE, DIGEST_TIMER)))


@contextlib.contextmanager
def quiesced():
    import fcntl
    active = unit_value(TIMER, 'ActiveState') == 'active'
    digest_active = unit_value(DIGEST_TIMER, 'ActiveState') == 'active'
    entered = False
    command('systemctl', 'stop', TIMER)
    if unit_value(DIGEST_TIMER, 'LoadState') != 'not-found':
        command('systemctl', 'stop', DIGEST_TIMER)
    try:
        with contextlib.ExitStack() as stack:
            for path in (Path('/run/lock/socialkit-analytics-refresh.lock'), DATA / 'refresh.lock',
                         Path('/mnt/data-disk/socialkit-gap-alert/run.lock')):
                handle = stack.enter_context(path.open('a+b'))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if unit_value(SERVICE, 'ActiveState') not in ('inactive', 'failed'):
                raise RuntimeError('Refresh still running; retry deployment after completion')
            if unit_value(DIGEST_SERVICE, 'ActiveState') not in ('inactive', 'failed'):
                raise RuntimeError('Digest still running; retry after completion')
            entered = True
            yield active
    except BaseException:
        if active and not entered:
            command('systemctl', 'start', TIMER)
        if digest_active and not entered:
            command('systemctl', 'start', DIGEST_TIMER)
        raise


def switch(target):
    temp = CURRENT.with_name('.current-hourly-' + str(os.getpid()))
    try:
        os.symlink(str(target), temp)
        os.replace(temp, CURRENT)
    finally:
        if temp.is_symlink():
            temp.unlink()


def install(commit):
    base.check_mount()
    verify_release(commit)
    writes = intended_writes()
    if not CURRENT.is_symlink():
        raise RuntimeError('Existing SocialKit release pointer is required')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = DATA / 'backups' / ('hourly-' + stamp)
    digest_state = dict(active=unit_value(DIGEST_TIMER, 'ActiveState') == 'active',
                        enabled=unit_value(DIGEST_TIMER, 'UnitFileState'))
    with quiesced() as was_active:
        backup.mkdir(parents=True, mode=0o700)
        manifest = dict(commit=commit, release=str(RELEASE), old_current=os.readlink(CURRENT),
                        timer_was_active=was_active, timer_enabled=unit_value(TIMER, 'UnitFileState'),
                        digest_timer_state=digest_state, files=[])
        for number, (path, content) in enumerate(writes.items()):
            row = dict(path=str(path), backup=str(number), existed=path.exists(),
                       new_sha256=hashlib.sha256(content).hexdigest())
            if path.exists():
                row.update(old_sha256=digest(path), mode=path.stat().st_mode & 0o777)
                shutil.copy2(path, backup / str(number))
            manifest['files'].append(row)
        base.atomic_write(backup / 'manifest.json', json.dumps(manifest, indent=2).encode(), 0o600)
        # The complete backup exists before any source/unit/pointer replacement.
        for path, content in writes.items():
            base.atomic_write(path, content)
        switch(RELEASE)
        command('systemctl', 'daemon-reload')
    print(json.dumps(dict(status='installed', commit=commit, backup=str(backup / 'manifest.json'),
                          timer='stopped_pending_validation', api_restart_required=False)))


def validate_rollback(manifest_path):
    path = Path(manifest_path).resolve()
    if DATA / 'backups' not in path.parents:
        raise ValueError('Manifest is outside SocialKit backups')
    manifest = json.loads(path.read_text())
    if not CURRENT.is_symlink() or CURRENT.resolve() != Path(manifest['release']).resolve():
        raise RuntimeError('Release pointer changed after this deployment')
    for item in manifest['files']:
        target = Path(item['path'])
        if not target.is_file() or digest(target) != item['new_sha256']:
            raise RuntimeError('Later file change prevents automatic rollback: ' + str(target))
        if item['existed'] and digest(path.parent / item['backup']) != item['old_sha256']:
            raise RuntimeError('Rollback backup checksum mismatch')
    return path, manifest


def rollback(manifest_path, verify_only=False):
    base.check_mount()
    path, manifest = validate_rollback(manifest_path)
    if verify_only:
        print(json.dumps(dict(status='rollback_verified', manifest=str(path))))
        return
    with quiesced():
        if 'digest_timer_state' in manifest:
            command('systemctl', 'disable', DIGEST_TIMER)
        for item in manifest['files']:
            target = Path(item['path'])
            if item['existed']:
                base.atomic_write(target, (path.parent / item['backup']).read_bytes(), item['mode'])
            elif target.exists():
                target.unlink()
        switch(manifest['old_current'])
        command('systemctl', 'daemon-reload')
    if manifest['timer_was_active']:
        command('systemctl', 'start', TIMER)
    if manifest.get('digest_timer_state', {}).get('enabled') == 'enabled':
        command('systemctl', 'enable', DIGEST_TIMER)
    if manifest.get('digest_timer_state', {}).get('active'):
        command('systemctl', 'start', DIGEST_TIMER)
    print(json.dumps(dict(status='rolled_back', facts_and_receipts='preserved')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--install', metavar='COMMIT')
    mode.add_argument('--rollback', metavar='MANIFEST')
    mode.add_argument('--verify-rollback', metavar='MANIFEST')
    args = parser.parse_args()
    if args.install:
        install(args.install)
    else:
        rollback(args.rollback or args.verify_rollback, verify_only=bool(args.verify_rollback))


if __name__ == '__main__':
    main()
