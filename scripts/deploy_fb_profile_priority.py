"""Scoped GitHub patch overlay for FB profile priority and shared Arabic aliases."""
import argparse
from contextlib import closing
import difflib
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
from scripts.deploy_pipeline_runtime import preserve_tree_owners

BASE = 'e06eb8829fa53ed09746479776d1fc75ba9098d0'
DICT = 'features/x_tag_revenue/tag_dictionary.json'
FILES = ['features/fb_auto_posts/hit_profile.py', 'features/fb_auto_posts/hit_drama.py', DICT,
         'static/hit-drama-publish.js', 'static/fb-auto-publish-templates.html']
TESTS = ['tests.test_fb_hit_profile', 'tests.test_arabic_tag_mapping', 'tests.test_fb_hit_receipt',
         'scripts.test_fb_hit_drama', 'tests.test_fb_hit_material_batch',
         'tests.test_fb_profile_selection', 'tests.test_fb_profile_integration', 'tests.test_hit_publish']


def updated(path, relative):
    """Apply only committed changed blocks, retaining concurrent live changes."""
    old = subprocess.check_output(['git', '-C', str(SOURCE), 'show', BASE + ':' + relative]).decode()
    new = (SOURCE / relative).read_text()
    current = Path(path).read_text()
    if relative in (DICT, 'features/fb_auto_posts/hit_profile.py'):
        assert current == old, 'source_baseline_changed:' + str(path)
        return new.encode()
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    for kind, i, j, k, l in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if kind == 'equal':
            continue
        before, after = ''.join(a[i:j]), ''.join(b[k:l])
        assert before and current.count(before) == 1, 'patch_anchor_changed:' + str(path)
        current = current.replace(before, after, 1)
    return current.encode()


def external(folder, info, path, content):
    index = str(len(info['static']))
    old, new = folder / ('external-' + index + '.old'), folder / ('external-' + index + '.new')
    shutil.copy2(path, old)
    new.write_bytes(content)
    info['static'][str(path)] = dict(before=release.sha(path), after=hashlib.sha256(content).hexdigest(),
                                   backup=str(old), next=str(new))


def prepare(folder):
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    commit = release.run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert not release.run('git', '-C', SOURCE, 'status', '--porcelain')
    assert release.run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit)
    current, _, db = release.settings('fb')
    previous, candidate = current.resolve(), folder / 'candidate'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    preserve_tree_owners(previous, candidate)
    info = dict(platform='fb', commit=commit, source=str(SOURCE), previous=str(previous),
                candidate=str(candidate), files={}, static={}, status='prepared')
    for relative in FILES:
        path = candidate / relative
        assert path.resolve().is_relative_to(candidate.resolve()), 'overlay_symlink_escape'
        release.atomic(path, updated(previous / relative, relative))
        info['files'][relative] = dict(before=release.sha(previous / relative), after=release.sha(path))
    # Version the modified dialog, keeping every other live HTML change intact.
    js_hash = release.sha(candidate / 'static/hit-drama-publish.js')[:12]
    html = candidate / 'static/fb-auto-publish-templates.html'
    content, n = re.subn(r'(/hit-drama-publish\.js)(?:\?[^"\']*)?', r'\1?v=' + js_hash, html.read_text())
    assert n == 1
    release.atomic(html, content.encode())
    info['files']['static/fb-auto-publish-templates.html']['after'] = release.sha(html)
    main = Path('/root/drama_material_service')
    external(folder, info, main / DICT, updated(main / DICT, DICT))
    for root in (main / 'static', release.WEB):
        js = root / 'hit-drama-publish.js'
        content = updated(js, 'static/hit-drama-publish.js')
        external(folder, info, js, content)
        html = root / 'fb-auto-publish-templates.html'
        page = updated(html, 'static/fb-auto-publish-templates.html').decode()
        page, n = re.subn(r'(/hit-drama-publish\.js)(?:\?[^"\']*)?', r'\1?v=' + hashlib.sha256(content).hexdigest()[:12], page)
        assert n == 1
        external(folder, info, html, page.encode())
    for module in TESTS:
        name = module.replace('.', '/') + '.py'
        release.atomic(candidate / name, (SOURCE / name).read_bytes())
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', *TESTS, '-q'], cwd=candidate,
                           capture_output=True, text=True, timeout=180)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr)
    assert proof.returncode == 0, 'candidate_tests_failed:' + proof.stderr[-2500:]
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(folder / 'ledger-before.sqlite3')) as dst:
        src.backup(dst)
    (folder / 'ledger-before.sqlite3').chmod(0o600)
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, candidate=str(candidate))), flush=True)


def activate(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    if rollback:
        # The old selector cannot consume v2 frozen plans. Preserve accepted
        # work; code rollback is available once those plans are terminal.
        _, _, db = release.settings('fb')
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
            rows = conn.execute("SELECT r.config_json FROM fb_auto_run r WHERE EXISTS "
                                "(SELECT 1 FROM fb_auto_task t WHERE t.run_id=r.id "
                                "AND t.status NOT IN ('published','failed','canceled','skipped'))").fetchall()
        assert not any(json.loads(r[0]).get('hit_drama_publish', {}).get('match_policy') ==
                       'language_profile_priority_v2' for r in rows), 'unfinished_v2_plans_preserved'
    # The shared release controller verifies hashes, drains FB, and restores only
    # previously active schedulers. Do not restore publication databases.
    for path, record in info['static'].items():
        assert release.sha(path) == record['after' if rollback else 'before'], 'external_drift:' + path
    release.activate('fb', folder, rollback)
    if rollback:
        # Generic rollback keeps shared JS for older additive releases. Here both
        # versions support historical receipts, so restore our exact backup.
        for path, record in info['static'].items():
            if Path(path).name == 'hit-drama-publish.js':
                assert release.sha(path) == record['after']
                release.atomic(path, Path(record['backup']).read_bytes())


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    p.add_argument('--folder', required=True, type=Path)
    args = p.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    if args.action == 'prepare':
        prepare(folder)
    else:
        activate(folder, args.action == 'rollback')
