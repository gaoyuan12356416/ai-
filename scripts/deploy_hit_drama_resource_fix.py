"""Stage the multilingual resolver over the currently deployed hit-drama lanes."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release


def prepare(platform, folder, *, base=None, files=None, static=True, tests=None):
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    commit = release.run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert not release.run('git', '-C', SOURCE, 'status', '--porcelain')
    assert release.run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit), 'pushed_source_required'
    base = base or {'x': '277c76b4', 'fb': 'b51461ec'}[platform]
    files = files or ['features/hit_drama_target.py'] + (
        ['features/x_tag_publish/hit_drama.py', 'features/x_tag_publish/selection.py'] if platform == 'x' else
        ['features/fb_auto_posts/' + name + '.py' for name in ('hit_drama', 'core', 'repositories')])
    current, _, db = release.settings(platform)
    previous, candidate = current.resolve(), folder / 'candidate'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    for target in [candidate, *candidate.rglob('*')]:
        origin = previous / target.relative_to(candidate)
        if origin.exists() and not target.is_symlink():
            stat = origin.stat()
            os.chown(target, stat.st_uid, stat.st_gid)
    info = dict(platform=platform, commit=commit, source=str(SOURCE), previous=str(previous),
                candidate=str(candidate), files={}, static={}, status='prepared')
    for name in files:
        path = previous / name
        baseline = subprocess.run(['git', '-C', str(SOURCE), 'show', base + ':' + name], capture_output=True)
        if baseline.returncode == 0:
            assert path.read_bytes().replace(b'\r\n', b'\n') == baseline.stdout, 'baseline_changed:' + name
        else:
            assert not path.exists(), 'new_file_collision:' + name
        release.atomic(candidate / name, (SOURCE / name).read_bytes())
        compile((candidate / name).read_bytes(), name, 'exec')
        info['files'][name] = dict(before=release.sha(path), after=release.sha(candidate / name))
    prefix = 'x-tag' if platform == 'x' else 'fb'
    asset = (SOURCE / 'static/hit-drama-publish.js').read_bytes()
    version = hashlib.sha256(asset).hexdigest()[:12]
    for root in ((candidate / 'static', release.MAIN, release.WEB) if static else ()):
        html = root / (prefix + '-auto-publish-templates.html')
        page, count = re.subn(r'(/hit-drama-publish\.js)(?:\?[^"\']*)?', r'\1?v=' + version, html.read_text())
        assert count == 1, 'shared_asset_reference_changed:' + str(html)
        for path, content in [(html, page.encode()), (root / 'hit-drama-publish.js', asset)]:
            if root == candidate / 'static':
                release.atomic(path, content)
                continue
            index = str(len(info['static']))
            old, new = folder / ('static-' + index + '.old'), folder / ('static-' + index + '.new')
            shutil.copy2(path, old)
            new.write_bytes(content)
            info['static'][str(path)] = dict(before=release.sha(path), after=hashlib.sha256(content).hexdigest(), backup=str(old), next=str(new))
    tests = tests or ['test_hit_drama_target', 'test_x_hit_drama' if platform == 'x' else 'test_fb_hit_drama']
    for name in tests:
        shutil.copy2(SOURCE / 'scripts' / (name + '.py'), candidate / 'scripts' / (name + '.py'))
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', *['scripts.' + name for name in tests], '-q'], cwd=candidate, capture_output=True, text=True, timeout=120)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr)
    assert proof.returncode == 0, 'candidate_tests_failed'
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(folder / 'ledger-before.sqlite3')) as dst:
        src.backup(dst)
    (folder / 'ledger-before.sqlite3').chmod(0o600)
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', platform=platform, commit=commit, folder=str(folder))), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback'])
    parser.add_argument('--platform', choices=['x', 'fb'], required=True)
    parser.add_argument('--folder', required=True)
    args = parser.parse_args()
    folder = Path(args.folder).resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    if args.action == 'prepare':
        prepare(args.platform, folder)
    else:
        release.activate(args.platform, folder, args.action == 'rollback')
