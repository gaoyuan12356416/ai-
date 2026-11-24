"""Overlay read-only FB run statistics; preserve execution code and receipts."""
import argparse
from contextlib import closing
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release, deploy_fb_skipped_recheck as fb
from scripts.deploy_fb_profile_priority import owner_active
from scripts.deploy_pipeline_runtime import preserve_tree_owners, verify_source
from scripts.fb_run_status_patch import patch_html

BASE = '6ac5ffaf8b8eeba8eb60169fb1f05081e6e06c06'
CORE = 'features/fb_auto_posts/core.py'
SUMMARY = 'features/fb_auto_posts/run_summary.py'
HTML = 'static/fb-auto-publish-runs.html'
JS = 'static/fb-auto-publish-run-summary.js'


def patch_core(current):
    old = subprocess.check_output(['git', '-C', str(SOURCE), 'show', BASE + ':' + CORE]).decode()
    new = (SOURCE / CORE).read_text(encoding='utf-8')
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    for group in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_grouped_opcodes(2):
        before = ''.join(a[group[0][1]:group[-1][2]])
        after = ''.join(b[group[0][3]:group[-1][4]])
        if not before or current.count(before) != 1:
            raise RuntimeError('core_read_path_context_changed')
        current = current.replace(before, after, 1)
    return current.encode()


def external(folder, info, path, content):
    index = str(len(info['static']))
    old, new = folder / ('external-' + index + '.old'), folder / ('external-' + index + '.new')
    if path.exists():
        shutil.copy2(path, old)
    new.write_bytes(content)
    info['static'][str(path)] = dict(before=release.sha(path), after=hashlib.sha256(content).hexdigest(),
                                    backup=str(old), next=str(new))


def prepare(folder):
    commit = verify_source(SOURCE, 'HEAD')
    current, _, db = release.settings('fb')
    previous, candidate = current.resolve(), folder / 'candidate'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    preserve_tree_owners(previous, candidate)
    info = dict(platform='fb', commit=commit, source=str(SOURCE), previous=str(previous),
                candidate=str(candidate), files={}, static={}, status='prepared')
    assert not (previous / SUMMARY).exists() and not (previous / JS).exists(), 'new_file_collision'
    contents = {CORE: patch_core((previous / CORE).read_text()), SUMMARY: (SOURCE / SUMMARY).read_bytes(),
                HTML: patch_html((previous / HTML).read_text()).encode(), JS: (SOURCE / JS).read_bytes()}
    for name, data in contents.items():
        dest = candidate / name
        assert dest.resolve().is_relative_to(candidate), 'overlay_symlink_escape'
        if name.endswith('.py'):
            compile(data, name, 'exec')
        release.atomic(dest, data)
        info['files'][name] = dict(before=release.sha(previous / name), after=release.sha(dest))
    for root in (release.MAIN, release.WEB):
        assert not (root / Path(JS).name).exists(), 'external_new_file_collision'
        external(folder, info, root / Path(HTML).name, patch_html((root / Path(HTML).name).read_text()).encode())
        external(folder, info, root / Path(JS).name, contents[JS])
    test = 'tests/test_fb_run_summary.py'
    release.atomic(candidate / test, (SOURCE / test).read_bytes())
    env = dict(os.environ, STORY_COPY_ROOT=str(folder / 'test-story'), FB_AUTO_PROFILE_ROOT=str(folder / 'test-profile'))
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'tests.test_fb_run_summary', '-q'],
                           cwd=candidate, env=env, capture_output=True, text=True, timeout=90)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr)
    proof.check_returncode()
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(folder / 'ledger-before.sqlite3')) as dst:
        src.backup(dst)
    (folder / 'ledger-before.sqlite3').chmod(0o600)
    info['main_api_pid'] = release.run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value')
    release.save(folder / 'manifest.json', info)
    print(release.run(sys.executable, '-B', SOURCE / 'scripts/check_fb_run_status.py', '--runtime', candidate,
                      '--db', folder / 'ledger-before.sqlite3', '--output', folder / 'rehearsal.json'))
    print(json.dumps(dict(status='prepared', commit=commit, folder=str(folder))), flush=True)


def activate(folder, rollback):
    info = json.loads((folder / 'manifest.json').read_text())
    for name, row in info['static'].items():
        assert release.sha(name) == row['after' if rollback else 'before'], 'external_file_changed:' + name
    # Existing scoped switch drains accepted FB handlers and preserves durable
    # external render claims. Restore the previously active FB schedulers only.
    fb.native_active = owner_active
    release.activate('fb', folder, rollback, defer_plan_read=False)
    assert release.run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value') == info['main_api_pid']
    print(json.dumps(dict(main_api_restarted=False, rollback=rollback)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    if args.action == 'prepare':
        prepare(folder)
    else:
        activate(folder, args.action == 'rollback')
