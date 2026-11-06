"""Scoped GitHub-derived Page pool overlay using the existing FB safe switch."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_publish_queue_isolation import (
    copy_file, preserve_owners, run, save, sha, switch,
)

FILES = ['features/fb_video_publish/' + name + '.py'
         for name in ('sources', 'pool', 'pool_scheduler', 'worker')]
BASE = 'b5606afe'


def prepare(source, folder):
    assert run(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk'))
    commit = run(['git', 'rev-parse', 'HEAD'], cwd=source)
    assert not run(['git', 'status', '--porcelain'], cwd=source)
    assert run(['git', 'ls-remote', 'origin', 'refs/heads/codex/fb-page-pool-any-product-20261106'], cwd=source).split()[0] == commit
    folder.mkdir(parents=True, mode=0o755, exist_ok=False)
    manifest = {'commit': commit, 'source': str(source), 'lanes': {}}
    for lane, root in [('fb', Path('/opt/fb-auto-post/current')), ('app', Path('/root/drama_material_service'))]:
        live = root.resolve()
        candidate = folder / 'candidates' / lane
        candidate.parent.mkdir(exist_ok=True)
        if lane == 'fb':
            shutil.copytree(live, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
            preserve_owners(live, candidate)
        else:
            candidate.mkdir()
        item = {'before_root': str(live), 'candidate': str(candidate), 'state': 'prepared', 'files': {}}
        manifest['lanes'][lane] = item
        for relative in FILES:
            baseline = subprocess.check_output(['git', 'show', BASE + ':' + relative], cwd=source)
            assert (live / relative).read_bytes().replace(b'\r\n', b'\n') == baseline, 'source_drift:' + relative
            backup = folder / 'before' / lane / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live / relative, backup)
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_file(source / relative, target)
            compile(target.read_bytes(), relative, 'exec')
            item['files'][relative] = {'before': sha(live / relative), 'after': sha(target)}
    save(folder / 'manifest.json', manifest)
    print(json.dumps({'prepared': True, 'commit': commit, 'folder': str(folder)}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'activate', 'rollback'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    folder = args.folder.resolve()
    if args.action == 'prepare':
        prepare(source, folder)
    else:
        # No database restoration and no main API restart: it proxies this path.
        switch(folder, 'fb', args.action == 'rollback')
        switch(folder, 'app', args.action == 'rollback')


if __name__ == '__main__':
    main()
