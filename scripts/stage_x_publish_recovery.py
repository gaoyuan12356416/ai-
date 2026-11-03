#!/usr/bin/env python3
"""Stage exact GitHub patches on current runtime copies, without activation.

No service, database, environment, capacity or existing runtime writes. The
manifest records the baseline and candidate tree for a separate drained switch.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_pipeline_runtime import preserve_tree_owners, verify_source

FILES = {
    'x': [
        'features/x_posts/service.py', 'features/x_auto_posts/publisher.py',
        'features/x_auto_posts/receipt_reconciliation.py',
        'features/pipeline_dispatch/x_native.py',
        'features/pipeline_dispatch/x_prepost_recovery.py',
        'features/pipeline_dispatch/x_manual_recovery.py',
        'features/pipeline_dispatch/x_schedule_recovery.py',
    ],
    'coordinator': ['features/pipeline_dispatch/adapters.py',
        'features/pipeline_dispatch/x_prepost_recovery.py'],
    'main': ['features/x_posts/service.py',
        'features/pipeline_dispatch/x_manual_recovery.py',
        'features/pipeline_dispatch/x_schedule_recovery.py'],
}
ROOTS = {'x': '/opt/x-post-automation/current',
         'coordinator': '/opt/pipeline-dispatch/current',
         'main': '/root/drama_material_service'}


def run(argv, cwd=None, data=None):
    p = subprocess.run(argv, cwd=cwd, input=data, capture_output=True, timeout=120)
    if p.returncode:
        raise RuntimeError('staging_command_failed:' + argv[0])
    return p.stdout


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_target(root, name):
    target = root / name
    for part in (target, *target.parents):
        if part == root: break
        if part.is_symlink(): raise ValueError('overlay_symlink:'+name)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('overlay_escapes_candidate:'+name)
    return target


def tree(path):
    result = {}
    for current, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in ('.git', '__pycache__', 'work'))
        for name in sorted(files) + dirs:
            p = Path(current) / name
            if name.endswith('.pyc'): continue
            if p.is_symlink(): result[str(p.relative_to(path))] = {'link': os.readlink(p)}
            elif p.is_file(): result[str(p.relative_to(path))] = {'sha256': sha(p)}
    return result


def stage(repo, commit, base, folder):
    repo, folder = Path(repo).resolve(), Path(folder)
    verify_source(repo, commit)
    if len(base) != 40 or run(['git', 'rev-parse', base], cwd=repo).decode().strip() != base:
        raise ValueError('exact_base_required')
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'commit': commit, 'base': base, 'status': 'prepared', 'lanes': {}}
    for lane, names in FILES.items():
        root = Path(ROOTS[lane]); previous = root.resolve()
        candidate = folder / 'candidates' / lane
        before = tree(previous) if lane != 'main' else {
            n: {'sha256': sha(previous/n)} if (previous/n).exists() else {'missing': True} for n in names}
        if lane == 'main':
            candidate.mkdir(parents=True)
            for n in names:
                if (previous/n).exists():
                    (candidate/n).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(previous/n, candidate/n)
        else:
            shutil.copytree(previous, candidate, symlinks=True,
                            ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc', 'work'))
            preserve_tree_owners(previous, candidate)
        changed = {}
        for name in names:
            p = safe_target(candidate, name)
            old = sha(p) if p.exists() else None
            p.parent.mkdir(parents=True, exist_ok=True)
            exists_in_base = subprocess.run(['git', 'cat-file', '-e', base+':'+name], cwd=repo,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if exists_in_base:
                if not p.is_file(): raise ValueError('runtime_file_missing:'+name)
                patch = run(['git', 'diff', '--binary', '--full-index', base, commit, '--', name], cwd=repo)
                if not patch: raise ValueError('expected_patch_missing:'+name)
                p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n'))
                argv = ['git', '-c', 'core.autocrlf=false', 'apply', '--whitespace=nowarn']
                run(argv+['--check', '-'], cwd=candidate, data=patch)
                run(argv+['-'], cwd=candidate, data=patch)
            else:
                if p.exists(): raise ValueError('new_file_collision:'+name)
                p.write_bytes(run(['git', 'show', commit+':'+name], cwd=repo)); p.chmod(0o644)
            compile(p.read_bytes(), name, 'exec')
            changed[name] = {'before_sha256': old, 'after_sha256': sha(p)}
        if lane != 'main':
            if tree(previous) != before: raise ValueError('runtime_changed_during_stage:'+lane)
            after = tree(candidate)
            if {k for k in set(before)|set(after) if before.get(k)!=after.get(k)} != set(names):
                raise ValueError('unexpected_candidate_delta:'+lane)
        else:
            fresh = {n: {'sha256': sha(previous/n)} if (previous/n).exists() else {'missing': True} for n in names}
            if fresh != before: raise ValueError('runtime_changed_during_stage:'+lane)
        if root.resolve() != previous: raise ValueError('runtime_root_changed:'+lane)
        report['lanes'][lane] = {'root': str(root), 'previous': str(previous),
                                'candidate': str(candidate), 'files': changed, 'baseline': before}
    path = folder/'manifest.json'
    with path.open('x') as f:
        os.chmod(path, 0o600); json.dump(report, f, indent=2); f.flush(); os.fsync(f.fileno())
    return {'status': 'prepared', 'commit': commit, 'manifest': str(path),
            'changed': {lane: list(files) for lane, files in FILES.items()}}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'commit', 'base', 'folder'): p.add_argument('--'+name, required=True)
    a = p.parse_args()
    print(json.dumps(stage(a.repo, a.commit, a.base, a.folder)))
