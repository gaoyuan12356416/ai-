"""Stage/apply a monitor-only overlay; retain live workers and all ledgers."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from deploy_post_monitor_accounting import (
    GIT, COORD, STATIC, atomic_copy, digest, health, run, service_pids, switch_coordinator,
)

BASE = '82dcc3528df7ad0bbe889660886081650d87ec7e'
FILES = ('features/pipeline_dispatch/views.py', 'features/post_daily_report/accounting.py',
         'features/post_daily_report/plans.py')
ASSETS = ('pipeline-monitor.js', 'pipeline-monitor.html')
ROOT = Path('/mnt/data-disk/monitor-read-fix-20261105')


def stage(sha):
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('exact_commit_required')
    assert run(['findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    env = dict(os.environ, GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
    run(['git', '-C', str(GIT), 'fetch', 'origin', sha], env=env)
    assert run(['git', '-C', str(GIT), 'rev-parse', 'FETCH_HEAD']) == sha
    old = COORD.resolve()
    expected = {}
    for relative, target in [(r, old/r) for r in FILES] + [('static/'+a, root/a) for root in STATIC for a in ASSETS]:
        baseline = subprocess.check_output(['git', '-C', str(GIT), 'show', BASE+':'+relative])
        if target.read_bytes() != baseline:
            raise RuntimeError('affected_live_file_changed:'+str(target))
        expected[str(target)] = digest(target)
    backup = ROOT / ('deployment-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(parents=True, mode=0o700)
    source = backup/'github'; source.mkdir()
    archive = subprocess.check_output(['git', '-C', str(GIT), 'archive', sha])
    subprocess.run(['tar', '-x', '-C', str(source)], input=archive, check=True)
    candidate = backup/'coordinator'
    shutil.copytree(old, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '.git'))
    for relative in FILES:
        target = candidate/relative
        assert not target.is_symlink() and not target.parent.is_symlink()
        shutil.copy2(source/relative, target)
    static = []
    for index, root in enumerate(STATIC):
        for name in ASSETS:
            saved = f'static-{index}-{name}'
            shutil.copy2(root/name, backup/saved)
            static.append(dict(path=str(root/name), backup=saved, old_sha256=digest(root/name),
                               new_sha256=digest(source/'static'/name)))
    manifest = dict(commit=sha, old_coordinator=str(old), new_coordinator=str(candidate),
                    expected=expected, static=static, status='staged')
    path = backup/'manifest.json'; path.write_text(json.dumps(manifest, indent=2))
    (backup/'rollback.sh').write_text("#!/bin/sh\nset -eu\nexec /usr/bin/python3 '"+str(source/ 'scripts/deploy_monitor_read_fix.py')+"' --rollback '"+str(path)+"'\n")
    (backup/'rollback.sh').chmod(0o700)
    run(['/opt/pipeline-dispatch/coordinator-venv/bin/python', '-B', '-c',
         'from features.pipeline_dispatch import views; from features.post_daily_report import accounting, plans'], cwd=candidate)
    print(json.dumps(dict(status='staged', manifest=str(path), candidate=str(candidate), commit=sha)))


def rollback(path):
    manifest = json.loads(path.read_text()); backup = path.parent
    current = str(COORD.resolve())
    if current not in (manifest['new_coordinator'], manifest['old_coordinator']):
        raise RuntimeError('later_runtime_preserved')
    for item in manifest['static']:
        if digest(item['path']) not in (item['new_sha256'], item['old_sha256']):
            raise RuntimeError('later_static_change_preserved')
    if current == manifest['new_coordinator']:
        switch_coordinator(manifest['old_coordinator'], backup/'rollback-drain.json'); health()
    for item in manifest['static']:
        if digest(item['path']) == item['new_sha256']:
            atomic_copy(backup/item['backup'], Path(item['path']))
    print(json.dumps(dict(status='rolled_back', manifest=str(path))))


def apply(path):
    manifest = json.loads(path.read_text()); backup = path.parent
    assert str(COORD.resolve()) == manifest['old_coordinator'], 'concurrent_release_preserved'
    for filename, sha in manifest['expected'].items():
        assert digest(filename) == sha, 'affected_file_changed:'+filename
    before = service_pids()
    try:
        switch_coordinator(manifest['new_coordinator'], backup/'http-drain.json'); health()
        for item in manifest['static']:
            target = Path(item['path'])
            assert digest(target) == item['old_sha256'], 'concurrent_static_change'
            atomic_copy(backup/'github/static'/target.name, target)
    except BaseException:
        rollback(path)
        raise
    after = service_pids()
    result = dict(status='deployed', commit=manifest['commit'], manifest=str(path),
                  rollback=str(backup/'rollback.sh'), service_pids_before=before, service_pids_after=after,
                  other_services_unchanged=all(after.get(k)==v for k,v in before.items() if k!='pipeline-coordinator.service'))
    (backup/'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--stage'); group.add_argument('--apply', type=Path); group.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if args.stage: stage(args.stage)
    elif args.apply: apply(args.apply)
    else: rollback(args.rollback)
