"""Restage the held X lane on the independently completed manual-time release."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('sha')
    parser.add_argument('--deployment', required=True, type=Path)
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[2]
    d = load(release / 'ops/moboreels-attribution-20261113/deploy.py', 'common_deploy')
    d.verify(release, args.sha)
    directory = args.deployment.resolve()
    d.require(directory == Path('/mnt/data-disk/moboreels-library/deployments/drama-catalog-complete-20261117T094503Z'), 'Unexpected deployment')
    state = json.loads((directory / 'manifest.json').read_text())
    d.require(state['commit'] == '198eb51086edfeb9a1b709bb02295e02a0468495', 'Reconcile prior rebase')
    d.require(state['groups']['x']['status'] == 'draining' and not state['groups']['x'].get('resumed'), 'X admission must remain held')
    d.require(not any(g['status'] == 'installing' for g in state['groups'].values()), 'Wait for existing activation')
    upstream = json.loads(Path('/mnt/data-disk/x-manual-release-time-20261117/20261117T091346Z-5f3ebbeb/record.json').read_text())
    d.require(upstream['commit'] == '5f3ebbeb66d28b9b6a5438a76485b54da27b3bcb' and upstream['status'] == 'active', 'Wait for upstream release')
    plan = json.loads((release / 'ops/drama-catalog-complete-20261117/manifest.json').read_text())
    root = Path(plan['roots']['x']['logical'])
    old = root.resolve()
    d.require(str(old) == upstream['after'] == plan['roots']['x']['resolved'], 'Upstream runtime changed')
    new = Path('/mnt/data-disk/drama-catalog-releases') / args.sha / 'x'
    d.require(not new.exists(), 'Reconcile existing merged candidate')
    before = d.tree_digest(old)
    entries = []
    for row in plan['files']:
        if row['lane'] != 'x':
            saved = next(e for e in state['files'] if e['lane'] == row['lane'] and e['relative'] == row['relative'])
            d.require(saved['after'] == row['after'], 'Unrelated payload changed')
            continue
        entry = dict(row, target=str(root / row['relative']), source=str(release / row['source']), phase='x')
        d.fence(entry)
        d.require(d.digest(entry['source']) == entry['after'], 'Merged source differs')
        compile(Path(entry['source']).read_bytes(), entry['source'], 'exec')
        entry['mode'] = Path(entry['target']).stat().st_mode & 0o777
        entries.append(entry)
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(old, new, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    for index, entry in enumerate(entries):
        d.atomic_copy(entry['source'], new / entry['relative'], entry['mode'])
        backup = directory / 'private' / ('x-merged-before-%d.py' % index)
        d.require(not backup.exists(), 'Backup exists')
        shutil.copy2(entry['target'], backup)
        os.chmod(backup, 0o600)
        d.require(d.digest(backup) == entry['before'], 'Merged backup differs')
        entry['backup'] = str(backup)
    d.require(root.resolve() == old and d.tree_digest(old) == before, 'Runtime changed while merging')
    d.save(directory / 'private/manifest-before-x-merge.json', state)
    old_release = Path(state['release'])
    for entry in state['files']:
        if entry['lane'] != 'x':
            entry['source'] = str(release / Path(entry['source']).relative_to(old_release))
            d.require(d.digest(entry['source']) == entry['after'], 'Unrelated source differs')
    state['files'] = [e for e in state['files'] if e['lane'] != 'x'] + entries
    state['previous_release'] = {'commit': state['commit'], 'source': state['release']}
    state['commit'], state['release'] = args.sha, str(release)
    state['roots']['x']['resolved'] = str(old)
    state['candidates']['x'] = str(new)
    state['original_trees']['x'], state['candidate_trees']['x'] = before, d.tree_digest(new)
    state['merged_upstream'] = upstream['commit']
    d.save(directory / 'manifest.json', state)
    print(json.dumps({'status': 'x-restaged', 'commit': args.sha, 'preserved_upstream': upstream['commit']}))


if __name__ == '__main__':
    main()
