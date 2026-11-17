#!/usr/bin/env python3
"""Deploy only daily-report transport and preparation timing; retain all receipts."""
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

FILES = ('features/social_daily_report/collector.py', 'scripts/social_publisher_daily_report.py')
ROUTES = 'features/social_analytics/routes.py'
UNIT = 'social-daily-prepare.service'
BASELINE = {
    FILES[0]: '59d9e8966a413c5364961d5a3b117ab77b2643b5afb917a47eb82a9ed052e024',
    FILES[1]: 'f22a4c32ae9e55370d98e549e34adc6d5498990c7ebb3c6dd799ead538a09b3e',
    ROUTES: 'd3877790c748fe03e7a7b21f508cbac599993f2d2f3ec8c031aeba3e3a047228',
}
UNIT_BASELINE = '1248bb5f034161c7cd959e710ebb58ab570cbe9993727376c68e0696bd839cc0'


def idle():
    for unit in ('youtube-publisher-daily-report.service', UNIT):
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
    if digest(Path('/etc/systemd/system') / UNIT) != UNIT_BASELINE:
        raise RuntimeError('prepare_unit_changed')
    folder = Path(args.deployment_dir).resolve()
    if Path('/mnt/data-disk') not in folder.parents:
        raise RuntimeError('deployment_must_use_data_disk')
    folder.mkdir(parents=True, exist_ok=True)
    release = folder / 'release'
    shutil.copytree(CURRENT.resolve(), release, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    for name in FILES:
        shutil.copy2(ROOT / name, release / name)
    route = release / ROUTES
    before = ast.parse(route.read_text())
    route.write_text(replace_function(route.read_text(), (ROOT / ROUTES).read_text(), 'query_rows'))
    after = ast.parse(route.read_text())
    # A composite report release has independent changes in other functions.
    for tree in (before, after):
        tree.body = [node for node in tree.body
                     if not (isinstance(node, ast.FunctionDef) and node.name == 'query_rows')]
    if ast.dump(before) != ast.dump(after):
        raise RuntimeError('unrelated_route_code_changed')
    (release / 'deploy').mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'deploy' / UNIT, release / 'deploy' / UNIT)
    for name in (*FILES, ROUTES):
        py_compile.compile(str(release / name), doraise=True)
    subprocess.run(['systemd-analyze', 'verify', str(release / 'deploy' / UNIT)], check=True)
    manifest = dict(sha=args.sha, previous=args.expected_release, release=str(release),
                    source=str(ROOT), backup=str(folder / 'backup'), status='staged',
                    files={name: digest(release / name) for name in (*FILES, ROUTES)},
                    previous_files=BASELINE, unit=digest(release / 'deploy' / UNIT))
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
    if digest(release / 'deploy' / UNIT) != manifest['unit']:
        raise RuntimeError('staged_unit_changed')
    if digest(Path('/etc/systemd/system') / UNIT) != UNIT_BASELINE:
        raise RuntimeError('prepare_unit_changed')
    idle()
    backup.mkdir()
    for name in (*FILES, ROUTES):
        target = backup / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CURRENT / name, target)
    shutil.copy2(Path('/etc/systemd/system') / UNIT, backup / UNIT)
    with sqlite3.connect('file:' + str(STATE / 'delivery.sqlite3') + '?mode=ro', uri=True) as source:
        with sqlite3.connect(backup / 'delivery.sqlite3') as target:
            source.backup(target)
    (path.parent / 'rollback.sh').write_text('#!/bin/bash\nset -euo pipefail\n/usr/bin/python3 ' +
        str(ROOT / 'scripts/install_social_daily_transport.py') + ' --rollback ' + str(path) + '\n')
    shutil.copy2(release / 'deploy' / UNIT, Path('/etc/systemd/system') / UNIT)
    link(release)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    manifest['status'] = 'active'
    path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(dict(status='active', sha=manifest['sha'], release=str(release),
                         restarted_services=[], rollback='bash ' + str(path.parent / 'rollback.sh'))))


def rollback(path):
    manifest = json.loads(path.read_text())
    if str(CURRENT.resolve()) != manifest['release']:
        raise RuntimeError('report_release_changed')
    idle()
    shutil.copy2(Path(manifest['backup']) / UNIT, Path('/etc/systemd/system') / UNIT)
    link(manifest['previous'])
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
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
