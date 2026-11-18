#!/usr/bin/env python3
"""Scoped GitHub-first daily-report release; preserve composite code and ledgers."""
import argparse
import ast
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.install_social_daily_11am import CURRENT, STATE, digest, link, replace_function, run

FB = 'features/social_analytics/facebook.py'
CLI = 'scripts/social_publisher_daily_report.py'
UNIT = 'social-daily-prepare.timer'
BASELINE = {
    FB: 'd3771f4761a53e797845d592ebd1e842e11c81d35f4e7313eec87ccdfe45994e',
    CLI: 'b3cce45ac3a488603901a593f7fcd7b9140c61c4c8753be5967539b939e40bd8',
}


def idle():
    for unit in ('youtube-publisher-daily-report.service', 'social-daily-prepare.service',
                 'social-facebook-attribution.service'):
        if int(run('systemctl', 'show', unit, '-p', 'MainPID', '--value') or 0):
            raise RuntimeError('report_in_flight:' + unit)


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
    shutil.copy2(ROOT / CLI, release / CLI)
    old, committed = (CURRENT / FB).read_text(), (ROOT / FB).read_text()
    patched = replace_function(old, committed, '_upsert_record')
    helper = next(n for n in ast.parse(committed).body
                  if isinstance(n, ast.FunctionDef) and n.name == '_display_title_refresh')
    patched += '\n\n' + '\n'.join(committed.splitlines()[helper.lineno - 1:helper.end_lineno]) + '\n'
    before, after = ast.parse(old), ast.parse(patched)
    for tree in (before, after):
        tree.body = [n for n in tree.body if not (isinstance(n, ast.FunctionDef)
                     and n.name in {'_upsert_record', '_display_title_refresh'})]
    if ast.dump(before) != ast.dump(after):
        raise RuntimeError('unrelated_facebook_code_changed')
    (release / FB).write_text(patched)
    (release / 'deploy').mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'deploy' / UNIT, release / 'deploy' / UNIT)
    for name in (FB, CLI):
        py_compile.compile(str(release / name), doraise=True)
    subprocess.run(['systemd-analyze', 'verify', str(release / 'deploy' / UNIT)], check=True)
    manifest = dict(sha=args.sha, previous=args.expected_release, release=str(release),
                    source=str(ROOT), backup=str(folder / 'backup'), status='staged',
                    files={name: digest(release / name) for name in (FB, CLI)},
                    previous_files=BASELINE, unit=digest(release / 'deploy' / UNIT),
                    previous_unit=digest(Path('/etc/systemd/system') / UNIT))
    path = folder / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(dict(status='staged', manifest=str(path), release=str(release), sha=args.sha)))


def activate(path):
    manifest = json.loads(path.read_text())
    if str(CURRENT.resolve()) != manifest['previous']:
        raise RuntimeError('report_release_changed')
    release, backup = Path(manifest['release']), Path(manifest['backup'])
    for name, expected in manifest['files'].items():
        if digest(release / name) != expected or digest(CURRENT / name) != manifest['previous_files'][name]:
            raise RuntimeError('report_file_changed:' + name)
    unit = Path('/etc/systemd/system') / UNIT
    if digest(release / 'deploy' / UNIT) != manifest['unit'] or digest(unit) != manifest['previous_unit']:
        raise RuntimeError('timer_changed')
    idle()
    backup.mkdir()
    for name in (FB, CLI):
        target = backup / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CURRENT / name, target)
    shutil.copy2(unit, backup / UNIT)
    for source_path in (STATE / 'delivery.sqlite3', Path('/mnt/data-disk/social-analytics/attribution.sqlite3')):
        with sqlite3.connect('file:' + str(source_path) + '?mode=ro', uri=True) as source:
            with sqlite3.connect(backup / source_path.name) as target:
                source.backup(target)
    (path.parent / 'rollback.sh').write_text('#!/bin/bash\nset -euo pipefail\n/usr/bin/python3 ' +
        str(ROOT / 'scripts/install_social_daily_reliability.py') + ' --rollback ' + str(path) + '\n')
    manifest['timer_active'] = run('systemctl', 'show', UNIT, '-p', 'ActiveState', '--value') == 'active'
    shutil.copy2(release / 'deploy' / UNIT, unit)
    link(release)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    if manifest['timer_active']:
        subprocess.run(['systemctl', 'restart', UNIT], check=True)
    manifest['status'] = 'active'
    path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(dict(status='active', sha=manifest['sha'], release=str(release),
                         rollback='bash ' + str(path.parent / 'rollback.sh'))))


def rollback(path):
    manifest = json.loads(path.read_text())
    if str(CURRENT.resolve()) != manifest['release']:
        raise RuntimeError('report_release_changed')
    idle()
    shutil.copy2(Path(manifest['backup']) / UNIT, Path('/etc/systemd/system') / UNIT)
    link(manifest['previous'])
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    if manifest['timer_active']:
        subprocess.run(['systemctl', 'restart', UNIT], check=True)
    print(json.dumps(dict(status='rolled_back', current=str(CURRENT.resolve()), ledgers='preserved')))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sha')
    p.add_argument('--deployment-dir')
    p.add_argument('--expected-release')
    p.add_argument('--activate', type=Path)
    p.add_argument('--rollback', type=Path)
    args = p.parse_args()
    os.umask(0o077)
    if run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('report_data_disk_unavailable')
    if args.activate:
        activate(args.activate)
    elif args.rollback:
        rollback(args.rollback)
    else:
        if not all((args.sha, args.deployment_dir, args.expected_release)):
            p.error('staging requires sha, deployment-dir and expected-release')
        stage(args)


if __name__ == '__main__':
    main()
