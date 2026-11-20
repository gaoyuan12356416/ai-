#!/usr/bin/env python3
"""Deploy product-aware FB attribution into the existing composite report only."""
import argparse
import ast
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.install_social_daily_11am import CURRENT, STATE, digest, link, replace_function, run

FB = 'features/social_analytics/facebook.py'
BASELINE = 'cf2fb67067473c4c6f4e8e9e773386849c61ad3d76459261ec81d42416eeb8b0'
CHANGED = {'_read_old', 'identity_old', 'identity_moboreels', 'read_facebook'}


def compose(old, committed):
    patched = old
    for name in ('_read_old', 'identity_old', 'read_facebook'):
        patched = replace_function(patched, committed, name)
    node = next(n for n in ast.parse(committed).body if isinstance(n, ast.FunctionDef)
                and n.name == 'identity_moboreels')
    patched += '\n\n' + ''.join(committed.splitlines(keepends=True)[node.lineno-1:node.end_lineno]) + '\n'
    before, after = ast.parse(old), ast.parse(patched)
    for tree in (before, after):
        tree.body = [n for n in tree.body if not (isinstance(n, ast.FunctionDef) and n.name in CHANGED)]
    if ast.dump(before) != ast.dump(after):
        raise RuntimeError('unrelated_code_changed')
    return patched


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
    if str(CURRENT.resolve()) != args.expected_release or digest(CURRENT / FB) != BASELINE:
        raise RuntimeError('report_release_changed')
    folder = Path(args.deployment_dir).resolve()
    if Path('/mnt/data-disk') not in folder.parents:
        raise RuntimeError('deployment_must_use_data_disk')
    folder.mkdir(parents=True, exist_ok=True)
    release = folder / 'release'
    shutil.copytree(CURRENT.resolve(), release, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    (release / FB).write_text(compose((CURRENT / FB).read_text(), (ROOT / FB).read_text()))
    py_compile.compile(str(release / FB), doraise=True)
    manifest = dict(sha=args.sha, previous=args.expected_release, release=str(release),
                    source=str(ROOT), backup=str(folder / 'backup'), status='staged',
                    before=BASELINE, after=digest(release / FB))
    path = folder / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest))


def activate(path):
    manifest = json.loads(path.read_text())
    release, backup = Path(manifest['release']), Path(manifest['backup'])
    if (str(CURRENT.resolve()) != manifest['previous'] or digest(CURRENT / FB) != manifest['before']
            or digest(release / FB) != manifest['after']):
        raise RuntimeError('report_release_changed')
    idle()
    backup.mkdir()
    shutil.copy2(CURRENT / FB, backup / 'facebook.py')
    for source_path in (STATE / 'delivery.sqlite3', Path('/mnt/data-disk/social-analytics/attribution.sqlite3')):
        with sqlite3.connect('file:' + str(source_path) + '?mode=ro', uri=True) as source:
            with sqlite3.connect(backup / source_path.name) as target:
                source.backup(target)
    (path.parent / 'rollback.sh').write_text('#!/bin/bash\nset -euo pipefail\n/usr/bin/python3 ' +
        str(ROOT / 'scripts/install_social_daily_moboreels.py') + ' --rollback ' + str(path) + '\n')
    link(release)
    manifest['status'] = 'active'
    path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(dict(status='active', sha=manifest['sha'], release=str(release),
                         rollback='bash ' + str(path.parent / 'rollback.sh'))))


def rollback(path):
    manifest = json.loads(path.read_text())
    if str(CURRENT.resolve()) != manifest['release']:
        raise RuntimeError('report_release_changed')
    idle()
    link(manifest['previous'])
    print(json.dumps(dict(status='rolled_back', ledgers='preserved', current=str(CURRENT.resolve()))))


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
        raise RuntimeError('data_disk_unavailable')
    if args.activate:
        activate(args.activate)
    elif args.rollback:
        rollback(args.rollback)
    elif all((args.sha, args.deployment_dir, args.expected_release)):
        stage(args)
    else:
        parser.error('staging requires sha, deployment-dir, expected-release')


if __name__ == '__main__':
    main()
