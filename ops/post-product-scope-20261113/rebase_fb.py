"""Preserve the reviewed concurrent FB mixed-pool release before activation."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

BASE = 'e5b1ee06c07def5262b2eb2888d6cf94f784b8b3'
CURRENT = Path('/mnt/data-disk/fb-mixed-product-pools-20261113/deployment/candidate')
REVIEWED = {
    'features/fb_auto_posts/service.py': '7a7ae41df90df00b14c055aeaa47b4a41ce0183cf6c4479cb9afb790455dc79a',
    'static/fb-auto-publish-template.html': 'c44107c887a70d28787d4f89a9f51730dfa46a7c375c586cd0e678f7e2d72891',
    'tests/test_fb_mixed_product_pools.py': 'fae31af031932f0673721020d6136a37ae378bdc3c7843ed126dca3a43e3e08a',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('sha')
    parser.add_argument('--deployment', required=True, type=Path)
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('original_deploy', release / 'ops/moboreels-attribution-20261113/deploy.py')
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
    d.verify(release, args.sha)
    directory = args.deployment.resolve()
    d.require(directory.parent == d.DATA / 'deployments' and directory.name.startswith('post-products-'), 'Unexpected deployment')
    state = json.loads((directory / 'manifest.json').read_text())
    d.require(state['commit'] == BASE and state['groups']['fb']['status'] == 'draining', 'Expected original draining deployment')
    d.require('fb_rebase' not in state, 'Rebase already recorded')
    link = Path(state['roots']['fb']['logical'])
    old = Path(state['roots']['fb']['resolved'])
    d.require(link.resolve() == CURRENT, 'FB runtime changed again')
    d.require(d.tree_digest(old) == state['original_trees']['fb'], 'Old FB baseline changed')

    def entries(root):
        result = {}
        for path in root.rglob('*'):
            if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                continue
            value = ('link:' + os.readlink(path)) if path.is_symlink() else d.digest(path) if path.is_file() else 'directory'
            result[str(path.relative_to(root))] = (path.lstat().st_mode & 0o777, value)
        return result

    before = entries(old); after = entries(CURRENT)
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    d.require(changed == set(REVIEWED), 'Unreviewed concurrent FB files')
    for relative, digest in REVIEWED.items():
        d.require(d.digest(CURRENT / relative) == digest, 'Reviewed FB file changed')
    files = [row for row in state['files'] if row['lane'] == 'fb']
    for row in files:
        d.fence(row)
        d.require(d.digest(row['source']) == row['after'], 'Product source changed')
    tree = d.tree_digest(CURRENT)
    candidate = Path('/mnt/data-disk/post-product-scope-releases') / args.sha / 'fb'
    d.require(not candidate.exists(), 'Candidate already exists')
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CURRENT, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    os.chmod(candidate, 0o755)
    for row in files:
        d.atomic_copy(row['source'], candidate / row['relative'], row['mode'])
    d.require(d.tree_digest(CURRENT) == tree and link.resolve() == CURRENT, 'Concurrent FB runtime changed during copy')
    receipt = directory / 'private/manifest-before-fb-rebase.json'
    d.require(not receipt.exists(), 'Rebase receipt exists')
    shutil.copy2(directory / 'manifest.json', receipt); os.chmod(receipt, 0o600)
    state['fb_rebase'] = {'commit': args.sha, 'release': str(release), 'previous_root': str(old),
        'preserved_commit': '20281127b6725b4820c3e2430ff9e6612bfdaec9', 'preserved_files': REVIEWED}
    state['roots']['fb']['resolved'] = str(CURRENT)
    state['original_trees']['fb'] = tree
    state['candidates']['fb'] = str(candidate)
    state['candidate_trees']['fb'] = d.tree_digest(candidate)
    d.save(directory / 'manifest.json', state)
    # The concurrent deployment restored these triggers; drain only FB again.
    triggers = [x for x in state['specs']['fb']['must_be_inactive'] if x.endswith(('.timer', '.path'))]
    if triggers:
        d.run('systemctl', 'stop', *triggers)
    print(json.dumps({'status': 'rebased', 'candidate': str(candidate), 'preserved_files': sorted(REVIEWED)}))


if __name__ == '__main__':
    main()
