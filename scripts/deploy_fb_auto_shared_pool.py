"""Deploy shared FB template pools while preserving the active publishing ledger."""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_publish_queue_isolation import (
    backup_db, copy_file, preserve_owners, run, save, sha, switch, DBS,
)
from scripts.deploy_fb_auto_material_product import install_static

BRANCH = 'codex/fb-auto-shared-pool-20261109'
PYTHON = {
    'features/fb_auto_posts/core.py': '64594b91f192d472d54f58c92fde69049b1b8352a108c1dcb2587a29542c65bf',
    'features/fb_auto_posts/service.py': '029fcfd5343aa36b2cc3ed0deda33d24b06bd46001b8fd2aa19c51a2fb9de495',
    'features/fb_auto_posts/hit_material.py': 'ac15c2203df33602ee04301c2553b4e3daa059adbcbf10008699bcaa61ba539d',
}
STATIC = {
    'static/fb-auto-publish-template.html': 'd0d87eadd7a2498055960f7cb50eefd9211bb4fad3857f353a5edda4ef140662',
}


def prepare(source, folder):
    assert run(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk'))
    assert shutil.disk_usage('/mnt/data-disk').free > 512 * 1024 * 1024
    commit = run(['git', 'rev-parse', 'HEAD'], cwd=source)
    assert not run(['git', 'status', '--porcelain'], cwd=source)
    assert run(['git', 'ls-remote', 'origin', 'refs/heads/' + BRANCH], cwd=source).split()[0] == commit
    folder.mkdir(parents=True, mode=0o711, exist_ok=False)
    manifest = dict(commit=commit, source=str(source), lanes={})
    for lane, root in [('fb', '/opt/fb-auto-post/current'), ('app', '/root/drama_material_service'), ('public', '/usr/share/nginx/html')]:
        live = Path(root).resolve()
        candidate = folder / 'candidates' / lane
        candidate.parent.mkdir(exist_ok=True)
        expected = {**PYTHON, **STATIC} if lane == 'fb' else STATIC
        if lane == 'fb':
            shutil.copytree(live, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
            preserve_owners(live, candidate)
        else:
            candidate.mkdir()
        item = dict(before_root=str(live), candidate=str(candidate), state='prepared', files={})
        manifest['lanes'][lane] = item
        for relative, before_hash in expected.items():
            actual = relative.removeprefix('static/') if lane == 'public' else relative
            before, target = live / actual, candidate / actual
            assert sha(before) == before_hash, 'source_drift:' + str(before)
            assert target.resolve().is_relative_to(candidate), 'overlay_symlink_escape'
            backup = folder / 'before' / lane / actual
            backup.parent.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(before, backup)
            copy_file(before, target)
            copy_file(source / relative, target)
            if relative.endswith('.py'):
                compile(target.read_bytes(), relative, 'exec')
            item['files'][actual] = dict(before=before_hash, after=sha(target))
    backup_db(Path(DBS['fb']), folder / 'fb-before.sqlite3')
    save(folder / 'manifest.json', manifest)
    for test in ('test_fb_auto_material_product.py', 'test_fb_auto_shared_pool.py'):
        relative = Path('scripts') / test
        shutil.copy2(source / relative, folder / 'candidates/fb' / relative)
        proof = run([sys.executable, folder / 'candidates/fb' / relative, '-v'])
        (folder / (test + '.log')).write_text(proof + '\n')
    print(json.dumps(dict(prepared=True, commit=commit, tests='12 passed')), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'activate', 'rollback'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.action == 'prepare':
        prepare(Path(__file__).resolve().parents[1], folder)
        return
    rollback = args.action == 'rollback'
    # Roll back code only. Existing scheduled tasks, receipts, configurations
    # and independent material-product selection stay in the current database.
    switch(folder, 'fb', rollback=rollback)
    install_static(folder, rollback=rollback)
    print(json.dumps(dict(action=args.action, static_installed=True, main_api_restarted=False)))


if __name__ == '__main__':
    main()
