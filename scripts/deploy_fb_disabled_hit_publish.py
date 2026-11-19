"""Scoped FB explicit-publish release using the established FB drain/switch path."""
import argparse
from contextlib import closing
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
from scripts import deploy_hit_drama as release

BASE = {
    'features/fb_auto_posts/core.py': '1baf40a6b6efc1b1bf19eb6bb11dbf8b8cccd61988efc40ff2ac424ee06247f3',
    'features/fb_auto_posts/hit_drama.py': '8e7a75108c1896fbb4cead25f92d3eed9531fe9a7bbfe5dc4acc2e668761f7fa',
    'features/fb_auto_posts/hit_material.py': '081bb34d68ded7300fea4e2b22976ab42d89bee01b39c96eadc70f0dc69efa0e',
    'features/fb_auto_posts/hit_material_batch.py': 'fde785b5cce0335de30cec9f1e52852d32854f1981c397dd81713342f32089e4',
    'features/fb_auto_posts/explicit_publish.py': '',
    'static/fb-auto-publish-templates.js': 'dfac8ae4ffe316a84d0e1798ab57c51241877820687a52d4a807dae6cf7e1a04',
    'static/fb-auto-publish-templates.html': 'dfee42fa61fd1c8577e3f6f9cee89ea68536bfe6a14346f72bca5a84b9ba3ceb',
}
TESTS = ['tests.test_fb_disabled_hit_publish', 'scripts.test_fb_hit_drama', 'tests.test_fb_hit_material_batch']


def prepare(folder):
    assert release.run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not release.run('git', '-C', SOURCE, 'status', '--porcelain')
    commit = release.run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert release.run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit)
    current, _, ledger = release.settings('fb')
    previous, candidate = current.resolve(), folder / 'candidate'
    for name, expected in BASE.items():
        assert release.sha(previous / name) == expected, 'live_code_changed:' + name
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    for path in [candidate, *candidate.rglob('*')]:
        original = previous / path.relative_to(candidate)
        if original.exists() and not path.is_symlink():
            stat = original.stat()
            os.chown(path, stat.st_uid, stat.st_gid)
    info = dict(platform='fb', commit=commit, previous=str(previous), candidate=str(candidate),
                files={}, static={}, status='prepared')
    for name, expected in BASE.items():
        target = candidate / name
        assert target.resolve().is_relative_to(candidate.resolve()), 'overlay_symlink_escape'
        content = (SOURCE / name).read_bytes()
        if name.endswith('.py'):
            compile(content, name, 'exec')
        release.atomic(target, content)
        info['files'][name] = dict(before=expected, after=release.sha(target))
        if name.startswith('static/'):
            for root in (release.MAIN, release.WEB):
                dest = root / Path(name).name
                assert release.sha(dest) == expected, 'static_changed:' + str(dest)
                index = len(info['static'])
                backup, next_file = folder / f'static-{index}.before', folder / f'static-{index}.next'
                shutil.copy2(dest, backup)
                next_file.write_bytes(content)
                info['static'][str(dest)] = dict(before=expected, after=hashlib.sha256(content).hexdigest(), backup=str(backup), next=str(next_file))
    for module in TESTS:
        relative = module.replace('.', '/') + '.py'
        release.atomic(candidate / relative, (SOURCE / relative).read_bytes())
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', *TESTS, '-q'], cwd=candidate,
                           capture_output=True, text=True, timeout=120)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr)
    assert proof.returncode == 0, 'candidate_tests_failed'
    # The running candidate contains today's Mobo catalog integration. Exercise
    # its existing real-planner fixture with the template initially disabled.
    mobo_check = '''import unittest
from scripts.test_fb_hit_drama import HitDramaTests
original = HitDramaTests.setUp
def disabled_setup(self):
    original(self)
    self.store.set_template_status(self.template['id'], False, self.actor, 1)
HitDramaTests.setUp = disabled_setup
suite = unittest.defaultTestLoader.loadTestsFromName('scripts.test_moboreels_hit.FacebookTests.test_mobo_fb_real_planner_preserves_profile_cooldown_and_receipt')
result = unittest.TextTestRunner().run(suite)
raise SystemExit(not result.wasSuccessful())
'''
    mobo = subprocess.run([sys.executable, '-B', '-c', mobo_check], cwd=candidate,
                          capture_output=True, text=True, timeout=60)
    (folder / 'mobo-disabled-tests.txt').write_text(mobo.stdout + mobo.stderr)
    assert mobo.returncode == 0, 'candidate_mobo_disabled_test_failed'
    with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(folder / 'ledger-before.sqlite3')) as dst:
        src.backup(dst)
    (folder / 'ledger-before.sqlite3').chmod(0o600)
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, folder=str(folder))), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    if args.action == 'prepare':
        prepare(folder)
    else:
        release.activate('fb', folder, rollback=args.action == 'rollback')
