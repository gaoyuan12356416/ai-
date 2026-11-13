"""Record a compatible later runtime release and preserve it in our rollback base."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy

GROUP = 'fb'
CURRENT = Path('/mnt/data-disk/fb-legacy-pool-unblock-20261113/deployment-v3/candidate')
FOLLOWUP = '3171151268611909609ef649e051482dbd102d71'
CHANGED = {
    'features/fb_auto_posts/repositories.py': 'deffa57ec692ac0e79e9701bc219e63c02985f7079fe667b0fbc1094a792d1e4',
    'tests/test_fb_legacy_queue_independence.py': '213b8da0b73203b01c63d84702ef51dfdc67787a6eaa4e848221686fda04138f',
    'tests/test_fb_page_task_isolation.py': 'fe0e8ead33d8c4731d97a42c7b1f35c5330f0bcddb52f87cf5e2ba90f407265e',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('sha'); parser.add_argument('--deployment', required=True, type=Path)
    args = parser.parse_args(); release = Path(__file__).resolve().parents[2]
    d = deploy.module(release / 'ops/moboreels-attribution-20261113/deploy.py', 'original_deploy')
    d.verify(release, args.sha)
    directory = args.deployment.resolve()
    d.require(directory.parent == d.DATA / 'deployments' and directory.name.startswith('post-products-'), 'Unexpected deployment')
    state = json.loads((directory / 'manifest.json').read_text())
    d.require(state['groups'][GROUP]['status'] == 'activated' and state['groups'][GROUP]['resumed'], 'FB not active')
    d.require(GROUP + '_followup' not in state, 'Followup already recorded')
    old = Path(state['candidates'][GROUP])
    d.require(d.tree_digest(old) == state['candidate_trees'][GROUP], 'Our FB candidate changed')
    d.require(Path(state['roots'][GROUP]['logical']).resolve() == CURRENT, 'FB current changed again')

    def files(root):
        return {str(p.relative_to(root)): d.digest(p) for p in root.rglob('*')
                if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo')}

    a, b = files(old), files(CURRENT)
    d.require({k for k in a.keys() | b.keys() if a.get(k) != b.get(k)} == set(CHANGED), 'Unreviewed FB followup files')
    for relative, digest in CHANGED.items():
        d.require(b[relative] == digest, 'FB followup file changed')
    rows = [r for r in state['files'] if r['lane'] == GROUP]
    for row in rows:
        d.fence(row, installed=True)
    current_tree = d.tree_digest(CURRENT)
    rollback = Path('/mnt/data-disk/post-product-scope-rollback') / args.sha / GROUP
    d.require(not rollback.exists(), 'Rollback tree already exists')
    rollback.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CURRENT, rollback, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    os.chmod(rollback, 0o755)
    for row in rows:
        target = rollback / row['relative']
        if row['before'] is None:
            target.unlink()
        else:
            d.require(d.digest(row['backup']) == row['before'], 'Original product backup changed')
            d.atomic_copy(row['backup'], target, row['mode'])
            if target.suffix == '.py': compile(target.read_bytes(), str(target), 'exec')
    for relative, digest in CHANGED.items():
        d.require(d.digest(rollback / relative) == digest, 'Rollback lost followup changes')
    d.require(d.tree_digest(CURRENT) == current_tree, 'FB changed during rollback staging')
    receipt = directory / ('private/manifest-before-' + GROUP + '-followup.json')
    shutil.copy2(directory / 'manifest.json', receipt); os.chmod(receipt, 0o600)
    state[GROUP + '_followup'] = {'record_commit': args.sha, 'preserved_commit': FOLLOWUP,
                           'previous_candidate': str(old), 'preserved_files': CHANGED}
    state['roots'][GROUP]['resolved'] = str(rollback)
    state['original_trees'][GROUP] = d.tree_digest(rollback)
    state['candidates'][GROUP] = str(CURRENT)
    state['candidate_trees'][GROUP] = current_tree
    d.save(directory / 'manifest.json', state)
    print(json.dumps({'status': 'recorded', 'live': str(CURRENT), 'rollback': str(rollback), 'live_files_changed': 0}))


if __name__ == '__main__':
    main()
