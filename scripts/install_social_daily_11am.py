#!/usr/bin/env python3
"""Stage/activate only the independent social daily report, preserving receipts."""
import argparse
import ast
import hashlib
import json
import os
import py_compile
import shutil
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = Path('/opt/youtube-publisher-daily-report/current')
STATE = Path('/mnt/data-disk/youtube-publisher-daily-report')
UNITS = ('social-daily-prepare.service', 'social-daily-prepare.timer')
FILES = ('features/social_analytics/backend_scope.py', 'features/social_daily_report/report.py',
         'features/social_daily_report/prepared.py', 'scripts/social_publisher_daily_report.py')
BASELINE = {
    'features/social_analytics/backend_scope.py': '8b38c1ae7c46e75ec95ee1e2340f44868b96acfe1b81c21d10002480098112f5',
    'features/social_analytics/routes.py': 'c0c1c8d71f2365095e53142bef43b299053264c6408349e337d5f59a7eb63e40',
    'features/social_daily_report/report.py': 'c3afc29259db5f2dca2b35c10ae92a380ab10e34e544baddfbb28dd85976cddd',
    'scripts/social_publisher_daily_report.py': 'aa80aa7e66fc45ad93cfb26b19f8b1ffbc10c5c832e1ca6dedcbbc6ed1fa169f',
}


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_function(original, committed, name):
    """The report is a composite release; preserve its other API functions."""
    old = next(n for n in ast.parse(original).body if isinstance(n, ast.FunctionDef) and n.name == name)
    new = next(n for n in ast.parse(committed).body if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = original.splitlines(keepends=True)
    replacement = committed.splitlines(keepends=True)[new.lineno - 1:new.end_lineno]
    lines[old.lineno - 1:old.end_lineno] = replacement
    return ''.join(lines)


def link(target):
    temporary = CURRENT.with_name('current.next-social-daily')
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(temporary, CURRENT)


def stage(args):
    if run('git', '-C', str(ROOT), 'rev-parse', 'HEAD') != args.sha:
        raise RuntimeError('source_commit_mismatch')
    if run('git', '-C', str(ROOT), 'status', '--porcelain'):
        raise RuntimeError('source_checkout_dirty')
    if str(CURRENT.resolve()) != args.expected_release:
        raise RuntimeError('report_release_changed')
    for name, expected in BASELINE.items():
        if digest(CURRENT / name) != expected:
            raise RuntimeError('report_file_changed:' + name)
    folder = Path(args.deployment_dir).resolve()
    if Path('/mnt/data-disk') not in folder.parents:
        raise RuntimeError('deployment_must_use_data_disk')
    folder.mkdir(parents=True, exist_ok=True)
    release = folder / 'release'
    shutil.copytree(CURRENT.resolve(), release, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    for name in FILES:
        shutil.copy2(ROOT / name, release / name)
    target = release / 'features/social_analytics/routes.py'
    content = replace_function(target.read_text(), (ROOT / 'features/social_analytics/routes.py').read_text(), 'query_rows')
    content = content.replace('import json\n', 'import json\nimport hashlib\nimport logging\nimport re\n', 1)
    content = content.replace('def query_rows(', 'logger = logging.getLogger(__name__)\n\n\ndef query_rows(', 1)
    target.write_text(content)
    (release / 'deploy').mkdir(exist_ok=True)
    for name in UNITS:
        shutil.copy2(ROOT / 'deploy' / name, release / 'deploy' / name)
    for name in (*FILES, 'features/social_analytics/routes.py'):
        py_compile.compile(str(release / name), doraise=True)
    subprocess.run(['systemd-analyze', 'verify', *(str(release / 'deploy' / name) for name in UNITS)], check=True)
    manifest = dict(sha=args.sha, previous=args.expected_release, release=str(release), status='staged',
                    files={name: digest(release / name) for name in (*FILES, 'features/social_analytics/routes.py')},
                    units={name: digest(release / 'deploy' / name) for name in UNITS},
                    source=str(ROOT), backup=str(folder / 'backup'))
    path = folder / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(dict(status='staged', manifest=str(path), **{k: manifest[k] for k in ('sha', 'release')})))


def activate(path):
    manifest = json.loads(path.read_text())
    release, backup = Path(manifest['release']), Path(manifest['backup'])
    if str(CURRENT.resolve()) != manifest['previous']:
        raise RuntimeError('report_release_changed')
    for name, expected in manifest['files'].items():
        if digest(release / name) != expected:
            raise RuntimeError('staged_file_changed:' + name)
    for name, expected in manifest['units'].items():
        if digest(release / 'deploy' / name) != expected:
            raise RuntimeError('staged_unit_changed:' + name)
    for name in ('youtube-publisher-daily-report.service', 'social-daily-prepare.service'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode == 0:
            raise RuntimeError('report_in_flight:' + name)
    backup.mkdir()
    manifest['timer_was_enabled'] = subprocess.run(['systemctl', 'is-enabled', '--quiet', UNITS[1]]).returncode == 0
    manifest['timer_was_active'] = subprocess.run(['systemctl', 'is-active', '--quiet', UNITS[1]]).returncode == 0
    for name in UNITS:
        old = Path('/etc/systemd/system') / name
        if old.exists():
            shutil.copy2(old, backup / name)
    source = sqlite3.connect('file:' + str(STATE / 'delivery.sqlite3') + '?mode=ro', uri=True, timeout=5)
    destination = sqlite3.connect(backup / 'delivery.sqlite3')
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()
    path.write_text(json.dumps(manifest, indent=2))
    (path.parent / 'rollback.sh').write_text('#!/bin/bash\nset -euo pipefail\n/usr/bin/python3 ' +
        str(ROOT / 'scripts/install_social_daily_11am.py') + ' --rollback ' + str(path) + '\n')
    for name in UNITS:
        shutil.copy2(release / 'deploy' / name, Path('/etc/systemd/system') / name)
    link(release)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', UNITS[1]], check=True)
    manifest['status'] = 'active'
    path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(dict(status='active', sha=manifest['sha'], release=str(release),
                         rollback='bash ' + str(path.parent / 'rollback.sh'))))


def rollback(path):
    manifest = json.loads(path.read_text())
    if str(CURRENT.resolve()) != manifest['release']:
        raise RuntimeError('current_release_changed_since_deployment')
    subprocess.run(['systemctl', 'disable', '--now', UNITS[1]], check=True)
    backup = Path(manifest['backup'])
    for name in UNITS:
        destination = Path('/etc/systemd/system') / name
        if (backup / name).exists():
            shutil.copy2(backup / name, destination)
        elif destination.exists():
            destination.unlink()
    link(manifest['previous'])
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    if manifest['timer_was_enabled']:
        subprocess.run(['systemctl', 'enable', UNITS[1]], check=True)
    if manifest['timer_was_active']:
        subprocess.run(['systemctl', 'start', UNITS[1]], check=True)
    # Never restore delivery.sqlite3: confirmed messages are business facts.
    print(json.dumps(dict(status='rolled_back', current=str(CURRENT.resolve()), delivery_state='preserved')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha')
    parser.add_argument('--deployment-dir')
    parser.add_argument('--expected-release')
    parser.add_argument('--activate', type=Path)
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('report_data_disk_unavailable')
    if args.activate:
        activate(args.activate)
    elif args.rollback:
        rollback(args.rollback)
    else:
        if not all((args.sha, args.deployment_dir, args.expected_release)):
            parser.error('staging requires sha, deployment-dir and expected-release')
        stage(args)


if __name__ == '__main__':
    main()
