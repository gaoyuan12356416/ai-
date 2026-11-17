"""Narrow FB/X template release-range overlay; preserve publishers and ledgers."""
import argparse
from contextlib import closing
import difflib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama_launch_window as ops
from scripts.deploy_hit_drama_multi_id import atomic

BASE = 'cfe5b19f'
SHARED = 'features/drama_launch_window.py'
TEST = 'scripts/test_drama_launch_range.py'
FILES = {
    'fb': ['features/fb_auto_posts/' + name + '.py' for name in ('validation', 'repositories', 'core')],
    'x': ['features/x_auto_posts/' + name + '.py' for name in ('validation', 'selector', 'core', 'publisher')],
}
STATIC = ['static/' + name for name in ('x-tag-auto-publish-template.html', 'x-tag-auto-publish-template.js',
          'fb-auto-publish-template.html', 'fb-auto-publish-template.js', 'drama-launch-window.js')]


def original(name):
    return subprocess.check_output(['git', '-C', str(SOURCE), 'show', BASE + ':' + name])


def remember(folder, info, path, data):
    path = path.resolve()
    index = len(info['files'])
    before = folder / (str(index) + '.before')
    after = folder / (str(index) + '.next')
    metadata = path.stat() if path.exists() else path.parent.stat()
    exists = path.exists()
    if exists:
        shutil.copy2(path, before)
    after.write_bytes(data)
    info['files'][str(path)] = dict(before=ops.digest(path) if exists else '', after=ops.digest(after),
        backup=str(before), next=str(after), existed=exists,
        mode=stat.S_IMODE(metadata.st_mode) if exists else 0o644, uid=metadata.st_uid, gid=metadata.st_gid)


def prepare(folder):
    assert ops.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk'))
    assert shutil.disk_usage('/mnt/data-disk').free > 2 * 1024**3
    assert not ops.run('git', '-C', SOURCE, 'status', '--porcelain')
    commit = ops.run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert ops.run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit)
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = dict(commit=commit, files={}, lanes={}, config_before={}, services_before=ops.services())
    ops.save(folder / 'manifest.json', info)
    for lane, names in FILES.items():
        root, _, _, unit, db = ops.LANES[lane]
        candidate = folder / ('candidate-' + lane)
        shutil.copytree(root.resolve(), candidate, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
        for name in names:
            assert (root / name).read_bytes().replace(b'\r\n', b'\n') == original(name), 'live_code_changed:' + name
        for name in [*names, SHARED]:
            assert name != SHARED or not (root / name).exists(), 'new_module_collision'
            data = (SOURCE / name).read_bytes()
            (candidate / name).write_bytes(data)
            compile(data, name, 'exec')
            remember(folder, info, root / name, data)
        shutil.copy2(SOURCE / TEST, candidate / TEST)
        info['lanes'][lane] = dict(root=str(root.resolve()), candidate=str(candidate), unit=unit)
        info['config_before'][lane] = ops.config_hash(db, lane)
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as src, \
             closing(sqlite3.connect(folder / ('ledger-before-' + lane + '.sqlite3'))) as dst:
            src.backup(dst)
        (folder / ('ledger-before-' + lane + '.sqlite3')).chmod(0o600)
        result = subprocess.run([sys.executable, '-B', TEST, '--platform', lane], cwd=candidate,
                                capture_output=True, text=True, timeout=90)
        (folder / ('tests-' + lane + '.txt')).write_text(result.stdout + result.stderr)
        if result.returncode:
            print(result.stdout + result.stderr)
            raise RuntimeError('candidate_tests_failed:' + lane)
    roots = [Path('/root/drama_material_service'), Path('/usr/share/nginx/html'),
             ops.LANES['fb'][0], ops.LANES['x'][0]]
    for root in roots:
        for name in STATIC:
            path = root / (Path(name).name if root.name == 'html' else name)
            if not path.exists() and not name.endswith('drama-launch-window.js'):
                continue
            data = (SOURCE / name).read_bytes()
            if path.exists():
                old = original(name)
                live = path.read_bytes().replace(b'\r\n', b'\n')
                if live != old:
                    # Apply only the reviewed UI delta to older runtime copies.
                    scratch = folder / ('static-' + str(len(info['files'])))
                    scratch.write_bytes(live)
                    delta = ''.join(difflib.unified_diff(old.decode().splitlines(True), data.decode().splitlines(True), fromfile='a/' + scratch.name, tofile='b/' + scratch.name))
                    result = subprocess.run(['git', 'apply', '--whitespace=nowarn', '-'], cwd=folder,
                                            input=delta, text=True, capture_output=True)
                    assert not result.returncode, 'static_variant_requires_review:' + str(path)
                    data = scratch.read_bytes()
            remember(folder, info, path, data)
    info['status'] = 'prepared'
    ops.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, files=len(info['files']), tests='fb+x passed')))


def switch(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    assert info['status'] in ('prepared', 'active', 'switch_requested', 'rolled_back')
    for lane, row in info['lanes'].items():
        assert str(ops.LANES[lane][0].resolve()) == row['root'], 'preserve_later_runtime:' + lane
    for name, row in info['files'].items():
        current = ops.digest(name) if Path(name).exists() else ''
        assert current in (row['before'], row['after']), 'preserve_later_file:' + name
    action = 'rollback' if rollback else 'apply'
    for lane in FILES:
        ops.run(sys.executable, ops.HEALTH, 'snapshot', '--lane', lane,
                '--output', folder / (action + '-schedulers-before-' + lane + '.json'))
    for name, row in info['files'].items():
        if rollback and not row['existed']:
            # Leave the inert helper installed: old source does not import it.
            continue
        source = Path(row['backup' if rollback else 'next'])
        assert ops.digest(source) == row['before' if rollback else 'after']
        atomic(Path(name), source.read_bytes(), row)
    ops.restart_fb(folder, action)
    ops.run('systemctl', 'restart', '--no-block', ops.LANES['x'][3])
    info.update(status='switch_requested', requested_action=action)
    ops.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='switch_requested', action=action, commit=info['commit'])))


def verify(folder):
    info = json.loads((folder / 'manifest.json').read_text())
    rollback = info['requested_action'] == 'rollback'
    action = 'rollback' if rollback else 'apply'
    for lane in FILES:
        root, _, _, unit, db = ops.LANES[lane]
        assert ops.run('systemctl', 'show', unit, '-p', 'ActiveState', '--value') == 'active'
        ops.run('curl', '--max-time', '5', '-fsS', 'http://127.0.0.1:' + ('18835' if lane == 'fb' else '18843') + '/health')
        ops.run(sys.executable, ops.HEALTH, 'restore', '--baseline', folder / (action + '-schedulers-before-' + lane + '.json'),
                '--wait-seconds', '25', '--output', folder / (action + '-schedulers-after-' + lane + '.json'))
    for name, row in info['files'].items():
        expected = row['before'] if rollback and row['existed'] else row['after']
        assert ops.digest(name) == expected, 'installed_hash_mismatch:' + name
    info['status'] = 'rolled_back' if rollback else 'active'
    ops.save(folder / 'manifest.json', info)
    result = dict(status=info['status'], commit=info['commit'], files=len(info['files']),
                  services_after=ops.services(), real_test_posts=0, historical_replays=0)
    ops.save(folder / ('verification-' + action + '.json'), result)
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback', 'verify'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.action == 'prepare': prepare(folder)
    elif args.action == 'verify': verify(folder)
    else: switch(folder, args.action == 'rollback')
