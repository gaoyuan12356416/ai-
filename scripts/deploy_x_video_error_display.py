"""Scoped X task-error readback overlay; never restore publication data."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_x_pool_local_failures as base

BASE_COMMIT = '344d5d78d46364d9a83e1404eaa3b0c0f966088d'
FILES = ('features/x_video_publish/core.py', 'features/x_video_publish/service.py')
TEST = 'scripts/test_x_video_publish_core.py'
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
DAEMON = 'x-video-pool-scheduler.service'
# Keep the daemon in every drain/status/activate check even if the base timer
# inventory changes; SIGTERM may still be finishing its bounded HTTP request.
if DAEMON not in base.CALLERS:
    base.CALLERS.append(DAEMON)


def health(action, folder):
    args = ['python3', HEALTH, action]
    if action == 'snapshot':
        args += ['--lane', 'x', '--output', str(folder / 'x-before.json')]
    else:
        args += ['--baseline', str(folder / 'x-before.json'), '--wait-seconds', '25',
                 '--output', str(folder / 'x-after.json')]
    return base.run(*args)


def prepare():
    assert base.run('findmnt', '-no', 'UUID', '/mnt/data-disk') == base.UUID
    assert shutil.disk_usage(base.ROOT).free > 2 * 1024**3
    assert not base.run('git', '-C', str(base.SOURCE), 'status', '--porcelain')
    commit = base.run('git', '-C', str(base.SOURCE), 'rev-parse', 'HEAD')
    assert base.run('git', '-C', str(base.SOURCE), 'branch', '-r', '--contains', commit)
    old = base.CURRENT.resolve()
    changed = []
    for rel in FILES:
        original = subprocess.check_output(['git', '-C', str(base.SOURCE), 'show', BASE_COMMIT + ':' + rel])
        assert (old / rel).read_bytes().replace(b'\r\n', b'\n') == original.replace(b'\r\n', b'\n'), 'live drift: ' + rel
        assert base.digest(base.MAIN / rel) == base.digest(old / rel), 'main drift: ' + rel
        if (base.SOURCE / rel).read_bytes().replace(b'\r\n', b'\n') != original.replace(b'\r\n', b'\n'):
            changed.append(rel)
    assert changed, 'no changed error-readback files'
    tag = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-video-errors-' + commit[:8]
    folder, release = base.ROOT / 'backups' / tag, base.ROOT / 'releases' / tag
    folder.mkdir(mode=0o700)
    before = base.tree(old)
    shutil.copytree(old, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for rel in changed + [TEST]:
        base.copy(base.SOURCE / rel, release / rel)
        compile((release / rel).read_bytes(), rel, 'exec')
    after = base.tree(release)
    assert {p for p in before if before[p] != after.get(p)} <= set(changed + [TEST])
    assert before == base.tree(old)
    external = []
    for index, rel in enumerate(changed):
        target = base.MAIN / rel
        saved, candidate = folder / ('before-' + str(index)), folder / ('next-' + str(index))
        shutil.copy2(target, saved)
        shutil.copy2(base.SOURCE / rel, candidate)
        external.append({'path': str(target), 'before': str(saved), 'after': str(candidate),
                         'before_sha': base.digest(saved), 'after_sha': base.digest(candidate)})
    manifest = {'phase': 'prepared', 'commit': commit, 'source': str(base.SOURCE),
                'old_release': str(old), 'new_release': str(release), 'old_tree': before,
                'new_tree': after, 'external': external, 'changed_files': changed}
    base.save(folder, manifest)
    return {'phase': 'prepared', 'folder': str(folder), 'release': str(release), 'commit': commit}


def restore(folder, manifest):
    # Check owners before restoring admission. Requires= may have stopped Auto.
    base.ready(8810)
    if manifest.get('units', {}).get('x-auto-post-service.service') == 'active':
        base.run('systemctl', 'start', 'x-auto-post-service.service')
        base.ready(18833)
    try:
        health('restore', folder)
    finally:
        # A scheduler check failure must not silently leave the deployment pause.
        base.resume(folder, manifest)
        if manifest.get('personal_scheduler_was_active'):
            base.run('systemctl', 'start', DAEMON)
    manifest['schedulers_verified'] = True
    manifest['main_pid_after'] = base.run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value')
    manifest['main_pid_unchanged'] = manifest['main_pid_after'] == manifest['main_pid_before']
    base.save(folder, manifest)


def drain(folder, manifest, rollback=False):
    assert manifest['phase'] == ('complete' if rollback else 'prepared')
    health('snapshot', folder)
    manifest['personal_scheduler_was_active'] = base.state(DAEMON) == 'active'
    manifest['main_pid_before'] = base.run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value')
    manifest['schedulers_verified'] = False
    try:
        base.drain(folder, manifest, rollback)
        if manifest['personal_scheduler_was_active']:
            assert base.run('systemctl', 'show', DAEMON, '-p', 'Restart', '--value') == 'on-failure'
            # Its handler finishes the current bounded call and exits normally.
            base.run('systemctl', 'kill', '--kill-who=main', '--signal=SIGTERM', DAEMON)
    except BaseException:
        restore(folder, manifest)
        manifest['phase'] = 'complete' if rollback else 'prepared'
        base.save(folder, manifest)
        raise
    return {'phase': 'draining', **base.active()}


def activate(folder, manifest):
    original_resume = base.resume
    base.resume = lambda *_: None
    restored = False
    try:
        result = base.activate(folder, manifest)
    except BaseException:
        # Pre-switch drift checks run before the base installer's recovery block.
        # No code changed there; remove this attempt's temporary admission pause.
        if manifest['phase'] == 'draining':
            base.resume = original_resume
            restore(folder, manifest)
            restored = True
            manifest['phase'] = 'complete' if manifest.get('rollback') else 'prepared'
            base.save(folder, manifest)
        raise
    finally:
        base.resume = original_resume
        if not restored and manifest['phase'] in {'complete', 'rolled_back', 'failed_restored'}:
            restore(folder, manifest)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('prepare', 'drain', 'switch', 'status', 'abort', 'rollback'), required=True)
    parser.add_argument('--folder', type=Path)
    args = parser.parse_args()
    if args.phase == 'prepare':
        result = prepare()
    else:
        folder = args.folder.resolve()
        assert folder.parent == base.ROOT / 'backups' and '-video-errors-' in folder.name
        manifest = json.loads((folder / 'manifest.json').read_text())
        if args.phase in ('drain', 'rollback'):
            result = drain(folder, manifest, args.phase == 'rollback')
        elif args.phase == 'switch':
            result = activate(folder, manifest)
        elif args.phase == 'status':
            result = {'phase': manifest['phase'], **base.active()}
        else:
            assert manifest['phase'] == 'draining'
            restore(folder, manifest)
            manifest['phase'] = 'complete' if manifest.get('rollback') else 'prepared'
            base.save(folder, manifest)
            result = {'phase': manifest['phase'], 'admissions_restored': True}
    print(json.dumps(result), flush=True)
