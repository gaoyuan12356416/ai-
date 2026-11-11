"""Overlay account-scoped cooldowns without replacing other live X changes."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release

BASE = '5ae79aa9b702fa7ff71549f6b13375ab4348627a'
CODE = ['features/x_auto_posts/core.py', 'features/x_auto_posts/selector.py']
TESTS = ['test_x_auto_post_store', 'test_x_auto_post_selector']
OLD_HELP = '仅在当前模板内按短剧身份冷却；填 0 不限制。'
NEW_HELP = '同一模板内，每个目标频道独立计算剧目冷却，从素材预占时起算；填 0 不限制。'
LANES = {
    'standard': dict(current='/opt/x-post-automation/current', service='x-auto-post-service.service',
                     prefix='x-auto-post', port=18833,
                     db='/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3'),
    'tag': dict(current='/opt/x-tag-publisher/current', service='x-tag-auto.service',
                prefix='x-tag-auto', port=18843,
                db='/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3'),
}


def active(unit):
    return release.run('systemctl', 'show', unit, '-p', 'ActiveState', '--value') in ('active', 'activating')


def prepare(folder):
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 1024 ** 3, 'insufficient_data_disk_space'
    commit = release.run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert not release.run('git', '-C', SOURCE, 'status', '--porcelain'), 'source_dirty'
    assert release.run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit), 'pushed_source_required'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    patch = folder / 'cooldown.patch'
    patch.write_bytes(subprocess.run(['git', '-C', str(SOURCE), 'diff', BASE, commit, '--', *CODE],
                                    capture_output=True, check=True).stdout)
    info = dict(commit=commit, source=str(SOURCE), status='prepared', lanes={}, static={})
    for lane, config in LANES.items():
        previous = Path(config['current']).resolve()
        candidate = folder / lane
        shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
        for target in [candidate, *candidate.rglob('*')]:
            origin = previous / target.relative_to(candidate)
            if origin.exists() and not target.is_symlink():
                stat = origin.stat()
                os.chown(target, stat.st_uid, stat.st_gid)
        release.run('git', '-C', candidate, 'apply', '--check', patch)
        release.run('git', '-C', candidate, 'apply', patch)
        files = {}
        for name in CODE:
            compile((candidate / name).read_bytes(), name, 'exec')
            files[name] = dict(before=release.sha(previous / name), after=release.sha(candidate / name))
        for name in ['x-auto-publish-template.html', 'x-tag-auto-publish-template.html']:
            path = candidate / 'static' / name
            if path.exists():
                release.atomic(path, release.replace(path.read_text(), OLD_HELP, NEW_HELP).encode())
                files['static/' + name] = dict(before=release.sha(previous / 'static' / name), after=release.sha(path))
        for name in TESTS:
            shutil.copy2(SOURCE / 'scripts' / (name + '.py'), candidate / 'scripts' / (name + '.py'))
        tests = TESTS + (['test_x_hit_drama'] if lane == 'tag' else [])
        proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', *['scripts.' + name for name in tests], '-q'],
                               cwd=candidate, capture_output=True, text=True, timeout=120)
        (folder / (lane + '-tests.txt')).write_text(proof.stdout + proof.stderr)
        assert proof.returncode == 0, 'candidate_tests_failed:' + lane
        with closing(sqlite3.connect(Path(config['db']).as_uri() + '?mode=ro', uri=True)) as src, \
                closing(sqlite3.connect(folder / (lane + '-ledger-before.sqlite3'))) as dst:
            src.backup(dst)
        (folder / (lane + '-ledger-before.sqlite3')).chmod(0o600)
        info['lanes'][lane] = dict(previous=str(previous), candidate=str(candidate), files=files, status='prepared')
    for root in [release.MAIN, release.WEB]:
        for name in ['x-auto-publish-template.html', 'x-tag-auto-publish-template.html']:
            path = root / name
            content = release.replace(path.read_text(), OLD_HELP, NEW_HELP).encode()
            index = str(len(info['static']))
            old, new = folder / ('static-' + index + '.old'), folder / ('static-' + index + '.new')
            shutil.copy2(path, old)
            new.write_bytes(content)
            info['static'][str(path)] = dict(before=release.sha(old), after=release.sha(new), backup=str(old), next=str(new))
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, folder=str(folder))), flush=True)


def verify(row, config, rollback):
    current = Path(config['current'])
    assert str(current.resolve()) == row['candidate' if rollback else 'previous'], 'later_release_present'
    for name, hashes in row['files'].items():
        assert release.sha(current / name) == hashes['after' if rollback else 'before'], 'live_file_changed:' + name
    return current


def apply(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    phase = 'rollback' if rollback else 'apply'
    baseline = folder / (phase + '-schedulers-before.json')
    release.run(sys.executable, release.HEALTH, 'snapshot', '--lane', 'x', '--output', baseline)
    try:
        for lane, config in LANES.items():
            row = info['lanes'][lane]
            if row['status'] == ('rolled_back' if rollback else 'active'):
                continue
            current = verify(row, config, rollback)
            prefix = config['prefix']
            triggers = [prefix + '-scheduler.timer', prefix + '-runner.timer', prefix + '-runner.path']
            workers = [prefix + '-scheduler.service', prefix + '-runner.service']
            states = {unit: active(unit) for unit in triggers}
            release.save(folder / (phase + '-' + lane + '-triggers.json'), states)
            try:
                for unit, enabled in states.items():
                    if enabled:
                        release.run('systemctl', 'stop', unit)
                deadline = time.monotonic() + 55
                while any(active(unit) for unit in workers) or release.run('ss', '-Hnt', 'state', 'established', '( sport = :' + str(config['port']) + ' )'):
                    assert time.monotonic() < deadline, 'active_auto_work_preserved_retry_later:' + lane
                    time.sleep(.5)
                verify(row, config, rollback)
                destination = row['previous' if rollback else 'candidate']
                temp = current.with_name('current.account-cooldown-next')
                temp.symlink_to(destination)
                temp.replace(current)
                release.run('systemctl', 'restart', config['service'], timeout=60)
                release.health(config['port'])
                row['status'] = 'rolled_back' if rollback else 'active'
                release.save(folder / 'manifest.json', info)
            finally:
                for unit, enabled in states.items():
                    if enabled:
                        release.run('systemctl', 'start', unit)
                assert states == {unit: active(unit) for unit in triggers}, 'auto_trigger_restore_failed:' + lane
        release.publish_static(info, rollback)
    finally:
        release.run(sys.executable, release.HEALTH, 'restore', '--baseline', baseline, '--wait-seconds', '25',
                    '--output', folder / (phase + '-schedulers-after.json'), timeout=55)
    info['status'] = 'rolled_back' if rollback else 'active'
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status=info['status'], commit=info['commit'], folder=str(folder))), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback'])
    parser.add_argument('--folder', required=True)
    args = parser.parse_args()
    folder = Path(args.folder).resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    if args.action == 'prepare':
        prepare(folder)
    else:
        apply(folder, args.action == 'rollback')
