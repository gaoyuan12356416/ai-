"""Deploy the duration admission overlay using the established scoped X drain.

GitHub source only; preserve the live composite and all publication data.
"""
import argparse
from datetime import datetime, timezone
import json
import shutil
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_x_pool_local_failures as base

FILES = {
    'features/x_video_publish/core.py': 'e3651c8f83155e6b2034e4d60c58fcdabf33a13e87da2d96f15f21e776952297',
    'features/x_video_publish/service.py': 'e49cd068c212a9db3deba22c84412f57e90383124698145007c785d9713506ec',
    'scripts/x_post_schedule_runner.py': '05d7cb6c9c42fac50c4ee78e008f1e0eca1e7313b5ce17d4ae41027d8fed94d7',
    'static/x-video-publish.js': 'e285338ff2898d04a7d14bff70e59cc0977d441897303124a0e5c4e2ad2f4d36',
    'static/x-video-publish.html': '9c46cfe9418c09ea2796a4c7ee30c9290a94b8eff335a0d50f956e4f3d02bee6',
}
TESTS = ['scripts/test_x_video_publish_core.py', 'scripts/test_x_video_publish_pool.py',
         'scripts/test_x_post_material_random_relay.py']


def prepare():
    assert base.run('findmnt', '-no', 'UUID', '/mnt/data-disk') == base.UUID
    assert shutil.disk_usage(base.ROOT).free > 2 * 1024**3
    assert not base.run('git', '-C', str(base.SOURCE), 'status', '--porcelain')
    commit = base.run('git', '-C', str(base.SOURCE), 'rev-parse', 'HEAD')
    assert base.run('git', '-C', str(base.SOURCE), 'branch', '-r', '--contains', commit)
    old = base.CURRENT.resolve()
    for rel, expected in FILES.items():
        assert base.digest(old / rel) == expected, 'concurrent live change: ' + rel
    tag = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-duration-admission-' + commit[:8]
    folder, release = base.ROOT / 'backups' / tag, base.ROOT / 'releases' / tag
    folder.mkdir(mode=0o700)
    before = base.tree(old)
    shutil.copytree(old, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for rel in list(FILES) + TESTS:
        base.copy(base.SOURCE / rel, release / rel)
        if rel.endswith('.py'): compile((release / rel).read_bytes(), rel, 'exec')
    assert base.tree(old) == before
    external = []
    for rel in FILES:
        targets = [] if rel.startswith('scripts/') else [base.MAIN / rel]
        if rel.startswith('static/'):
            targets.append(Path('/usr/share/nginx/html') / Path(rel).name)
        for target in targets:
            assert base.digest(target) == FILES[rel], 'concurrent external change: ' + str(target)
            saved, candidate = folder / ('before-' + str(len(external))), folder / ('next-' + str(len(external)))
            shutil.copy2(target, saved)
            shutil.copy2(base.SOURCE / rel, candidate)
            external.append({'path': str(target), 'before': str(saved), 'after': str(candidate),
                             'before_sha': base.digest(saved), 'after_sha': base.digest(candidate)})
    manifest = {'phase': 'prepared', 'commit': commit, 'source': str(base.SOURCE),
        'old_release': str(old), 'new_release': str(release), 'old_tree': before,
        'new_tree': base.tree(release), 'external': external}
    base.save(folder, manifest)
    return {'phase': 'prepared', 'folder': str(folder), 'release': str(release), 'commit': commit}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('prepare','drain','switch','abort','rollback','status'), required=True)
    parser.add_argument('--folder', type=Path)
    args = parser.parse_args()
    if args.phase == 'prepare':
        result = prepare()
    else:
        folder = args.folder.resolve()
        assert folder.parent == base.ROOT / 'backups' and '-duration-admission-' in folder.name
        manifest = json.loads((folder / 'manifest.json').read_text())
        if args.phase in ('drain','rollback'):
            result = base.drain(folder, manifest, args.phase == 'rollback')
        elif args.phase == 'switch':
            result = base.activate(folder, manifest)
        elif args.phase == 'status':
            result = {'phase': manifest['phase'], **base.active()}
        else:
            assert manifest['phase'] == 'draining'
            base.resume(folder, manifest)
            manifest['phase'] = 'complete' if manifest.get('rollback') else 'prepared'
            base.save(folder, manifest)
            result = {'phase': manifest['phase'], 'admissions_restored': True}
    print(json.dumps(result), flush=True)
