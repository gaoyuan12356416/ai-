"""Scoped report deployment; no publisher restart or business-state rollback."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

SOURCE = Path(__file__).resolve().parents[1]
MAIN = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
CURRENT = Path('/opt/drama-catalog/current')
BASE = '2a966358e0ad72f262b92283a9a0382de3ce5ea0'
FILES = ['features/drama_pay_rank/metadata.py', 'features/drama_pay_rank/refresh.py']
ASSETS = ['drama-pay-rank.html', 'drama-pay-rank.js', 'drama-pay-rank.css']


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic(target, content):
    target = Path(target)
    temporary = target.with_name(target.name + '.rank-next')
    temporary.write_bytes(content)
    shutil.copystat(target, temporary)
    temporary.replace(target)


def pointer(destination):
    temporary = CURRENT.with_name('current.rank-next')
    temporary.symlink_to(destination)
    temporary.replace(CURRENT)


def apply(folder, base=BASE, static_only=False):
    assert run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    assert not run('git', '-C', str(SOURCE), 'status', '--porcelain'), 'source_dirty'
    assert run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit), 'source_not_fetched_from_remote'
    previous = CURRENT.resolve()
    assert run('git', '-C', str(previous), 'rev-parse', 'HEAD') == base, 'catalog_release_changed'
    assert not run('git', '-C', str(previous), 'status', '--porcelain'), 'catalog_source_dirty'
    targets = {} if static_only else {MAIN / name: name for name in FILES}
    for root in (MAIN / 'static', WEB):
        targets.update({root / name: 'static/' + name for name in ASSETS})
    for target, name in targets.items():
        expected = subprocess.check_output(['git', '-C', str(SOURCE), 'show', base + ':' + name])
        assert target.read_bytes() == expected, 'live_file_changed:' + str(target)
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o700)
    manifest = dict(commit=commit, previous=str(previous), source=str(SOURCE), files={},
                    main_pid=run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value'))
    if not static_only:
        with sqlite3.connect('/mnt/data-disk/drama-pay-rank/rank.sqlite3') as source_db:
            with sqlite3.connect(str(folder / 'rank-before.sqlite3')) as backup_db:
                source_db.backup(backup_db)
    for i, (target, name) in enumerate(targets.items()):
        backup = folder / ('before-' + str(i))
        shutil.copy2(target, backup)
        manifest['files'][str(target)] = dict(backup=str(backup), before=sha(target), after=sha(SOURCE / name), name=name)
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    for target, name in targets.items():
        atomic(target, (SOURCE / name).read_bytes())
    pointer(SOURCE)
    assert all(sha(path) == row['after'] for path, row in manifest['files'].items())
    assert manifest['main_pid'] == run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value')
    print(json.dumps(dict(status='installed', commit=commit, backup=str(folder), restarted=False)))


def rollback(folder):
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert str(CURRENT.resolve()) == manifest['source'], 'catalog_release_changed'
    for path, row in manifest['files'].items():
        assert sha(path) == row['after'], 'live_file_changed:' + path
    for path, row in manifest['files'].items():
        atomic(path, Path(row['backup']).read_bytes())
    pointer(manifest['previous'])
    print(json.dumps(dict(status='rolled_back', data_preserved=True, restarted=False)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('apply', 'rollback'))
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--base', default=BASE)
    parser.add_argument('--static-only', action='store_true')
    args = parser.parse_args()
    if args.action == 'apply':
        apply(args.folder, args.base, args.static_only)
    else:
        rollback(args.folder)
