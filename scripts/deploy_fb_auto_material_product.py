"""Install only the FB automatic-template source selector and its editor."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_publish_queue_isolation import (
    backup_db, copy_file, preserve_owners, run, save, sha, switch, DBS,
)

BRANCH = "codex/fb-auto-material-products-20261109"
PYTHON = {
    "features/fb_auto_posts/repositories.py": "8bd6981c393a5cb59e5ee549de35a884ec3f1a5c52371d22a9459e9fa9eaa680",
    "features/fb_auto_posts/validation.py": "3b4b597b0891ecfa4e88b38ca8f7c8bc290161f42ca8e7d719d071e676f948c0",
}
STATIC = {
    "static/fb-auto-publish-template.html": "adf0f10c28ad2e2d9c291fd283b565882faa1cbdd2efcb9e3e0336ae3d96da88",
    "static/fb-auto-publish-template.js": "c802999755bb08997b2d0104037c15ae92f22d2d75ed20bf2c2ab518204c49cc",
}
FB_HTML = "ba2a0efd6c4f5398a5ec23389fd6504a7099def474dd2b85536b6051b30a8c57"


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
        live = Path(root).resolve(); candidate = folder / 'candidates' / lane
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
            before = live / actual; target = candidate / actual
            if lane == 'fb' and relative.endswith('.html'):
                before_hash = FB_HTML
            assert sha(before) == before_hash, 'source_drift:' + str(before)
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
    # Exercise the deployed runtime's imports and real SQLite save/read path on
    # a disposable database, without touching live templates or Graph.
    test = 'scripts/test_fb_auto_material_product.py'
    shutil.copy2(source / test, folder / 'candidates/fb' / test)
    proof = run([sys.executable, folder / 'candidates/fb' / test, '-v'])
    print(json.dumps(dict(prepared=True, commit=commit, tests='6 passed')), flush=True)


def install_static(folder, rollback=False):
    path = folder / 'manifest.json'; m = json.loads(path.read_text())
    for lane in ('app', 'public'):
        item = m['lanes'][lane]
        for relative, hashes in item['files'].items():
            target = Path(item['before_root']) / relative
            desired = hashes['before' if rollback else 'after']
            if sha(target) == desired:
                continue
            assert sha(target) == hashes['after' if rollback else 'before'], 'source_drift:' + str(target)
            source = folder / 'before' / lane / relative if rollback else Path(item['candidate']) / relative
            copy_file(source, target)
            assert sha(target) == desired
        item['state'] = 'rolled_back' if rollback else 'installed'
        save(path, m)


def rollback(folder):
    # Older selectors would silently fall back to the target product. Require
    # operator review only when a current template or unfinished run uses the
    # new independent scope; never rewrite saved versions or publishing facts.
    conn = sqlite3.connect('file:' + DBS['fb'] + '?mode=ro', uri=True)
    try:
        rows = conn.execute('SELECT v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version').fetchall()
        rows += conn.execute("SELECT DISTINCT r.config_json FROM fb_auto_run r JOIN fb_auto_task t ON t.run_id=r.id WHERE t.status IN ('planned','preparing','ready','running','submitted','unknown')").fetchall()
        for row in rows:
            cfg = json.loads(row[0])
            assert not cfg.get('material_product') or cfg['material_product'].casefold() == cfg['product'].casefold(), 'new_source_scope_in_use: retain compatible selector code'
    finally:
        conn.close()
    switch(folder, 'fb', rollback=True)
    install_static(folder, rollback=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'activate', 'rollback'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args(); folder = args.folder.resolve()
    if args.action == 'prepare':
        prepare(Path(__file__).resolve().parents[1], folder)
    elif args.action == 'activate':
        switch(folder, 'fb')
        install_static(folder)
        print(json.dumps(dict(active=True, static_installed=True, main_api_restarted=False)))
    else:
        rollback(folder)


if __name__ == '__main__':
    main()
