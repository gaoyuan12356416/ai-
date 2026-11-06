"""Install reviewed startup dependencies and independent scheduler checks.

No parent service is restarted. Rollback only restores installed files and
the health-tool pointer, never databases, running schedules or receipts.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import publish_scheduler_health as health

PARENTS = {
    'fb-auto-post-service.service': ('fb', ['fb-group-pool-scheduler.service']),
    'x-post-automation.service': ('x', ['x-auto-post-service.service', 'x-video-pool-scheduler.service']),
    'x-auto-post-service.service': ('x', []),
    'tt-post-service.service': ('tt', []),
    'tt-auto-post-service.service': ('tt', []),
    'drama-material-api.service': ('youtube', []),
    'youtube-auto-publish-worker.service': ('youtube', []),
    'pipeline-coordinator.service': ('coordinator', []),
}
BASE = Path('/mnt/data-disk/publish-scheduler-health')
POINTER = Path('/opt/publish-scheduler-health/current')
REPAIR = ['fb-group-pool-scheduler.service', 'x-video-pool-scheduler.service']


def run(args):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError('command_failed:' + str(args[0]))
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def files(source):
    result = {'publish-scheduler-check@.service': (source / 'deploy/publish-scheduler-check@.service').read_text()}
    for parent, (lane, dependencies) in PARENTS.items():
        result[parent + '.d/95-publish-scheduler-health.conf'] = (
            '[Unit]\nWants=' + ' '.join(dependencies + ['publish-scheduler-check@' + lane + '.service']) + '\n')
    for lane, definition in health.LANES.items():
        result['publish-scheduler-check@' + lane + '.service.d/ordering.conf'] = (
            '[Unit]\nAfter=' + ' '.join(definition['daemons'] + list(definition['timers'])) + '\n')
    return result


def install(source, commit):
    source = Path(source).resolve()
    repo = '/mnt/data-disk/pipeline-dispatch/github'
    assert run(['git', '-C', repo, 'rev-parse', commit + '^{commit}']) == commit
    assert run(['git', '-C', repo, 'for-each-ref', '--contains', commit, '--format=%(refname)', 'refs/remotes/origin'])
    for relative in ['scripts/publish_scheduler_health.py', 'scripts/deploy_publish_scheduler_health.py',
                     'deploy/publish-scheduler-check@.service']:
        expected = subprocess.check_output(['git', '-C', repo, 'show', commit + ':' + relative])
        assert (source / relative).read_bytes() == expected, relative
    assert run(['findmnt', '-no', 'UUID', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    BASE.mkdir(exist_ok=True, mode=0o750)
    backup = BASE / ('backup-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    manifest = {'commit': commit, 'source': str(source), 'files': {}, 'pointer_before':
                str(POINTER.readlink()) if POINTER.is_symlink() else None,
                'before_pids': {u: health.show([u])[u].get('MainPID') for u in PARENTS}}
    assert not POINTER.exists() or POINTER.is_symlink(), 'existing_non_release_directory'
    baseline = health.collect(list(health.LANES))
    health.save(backup / 'before.json', baseline)
    for name, database in [('fb', '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'),
                           ('x', '/var/lib/x-post-automation/accounts.sqlite3')]:
        with sqlite3.connect('file:' + database + '?mode=ro', uri=True) as reader:
            with sqlite3.connect(str(backup / (name + '-before.sqlite3'))) as writer:
                reader.backup(writer)
    for relative, body in files(source).items():
        target = Path('/etc/systemd/system') / relative
        entry = {'existed': target.exists(), 'installed_sha256': hashlib.sha256(body.encode()).hexdigest()}
        if target.exists():
            entry['before_sha256'] = digest(target)
            old = backup / 'files' / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, old)
        manifest['files'][relative] = entry
    health.save(backup / 'manifest.json', manifest)
    POINTER.parent.mkdir(parents=True, exist_ok=True)
    temporary = POINTER.with_name('current.next-' + str(os.getpid()))
    temporary.symlink_to(source)
    temporary.replace(POINTER)
    for relative, body in files(source).items():
        target = Path('/etc/systemd/system') / relative
        entry = manifest['files'][relative]
        assert (digest(target) == entry['before_sha256']) if entry['existed'] else not target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        target.chmod(0o644)
    run(['systemctl', 'daemon-reload'])
    # These two missed daemons were explicitly audited and authorized. No
    # business paused pools, historical oneshots, or legacy publishers start.
    run(['systemctl', 'start', *REPAIR])
    run(['systemctl', 'start', *['publish-scheduler-check@' + lane + '.service' for lane in health.LANES]])
    after = health.collect(list(health.LANES))
    health.save(backup / 'after.json', after)
    manifest['after_pids'] = {u: health.show([u])[u].get('MainPID') for u in PARENTS}
    assert manifest['before_pids'] == manifest['after_pids'], 'parent_pid_changed'
    manifest['state'] = 'verified' if after['ok'] else 'installed_with_health_errors'
    health.save(backup / 'manifest.json', manifest)
    print(json.dumps({'backup': str(backup), 'commit': commit, 'state': manifest['state'],
                      'parents_unchanged': True, 'problems': [x for x in after['checks'] if not x['ok']]}))
    return 0 if after['ok'] else 1


def rollback(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert str(folder).startswith(str(BASE) + '/backup-')
    for relative, entry in manifest['files'].items():
        path = Path('/etc/systemd/system') / relative
        assert digest(path) == entry['installed_sha256'], 'configuration_changed:' + relative
    assert POINTER.resolve() == Path(manifest['source']).resolve()
    for relative, entry in manifest['files'].items():
        target = Path('/etc/systemd/system') / relative
        if entry['existed']:
            shutil.copy2(folder / 'files' / relative, target)
        else:
            target.unlink()
    POINTER.unlink()
    if manifest['pointer_before']:
        POINTER.symlink_to(manifest['pointer_before'])
    run(['systemctl', 'daemon-reload'])
    print('Restored deployment checks/configuration; retained running schedules, data and receipts.')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source')
    parser.add_argument('--commit')
    parser.add_argument('--rollback')
    args = parser.parse_args()
    if args.rollback:
        raise SystemExit(rollback(args.rollback))
    if not args.source or not args.commit:
        parser.error('source and pushed commit required')
    raise SystemExit(install(args.source, args.commit))
