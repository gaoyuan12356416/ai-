"""Apply the Mobo launch-window change to the current FB owner and preserve its ledger."""
import argparse
from contextlib import closing
import json
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

BASE = '081ff958c62d87066a2c6a294648732acd41f79a'
MODULE = 'features/fb_auto_posts/moboreels.py'
TEST_FILES = ['tests/test_fb_mobo_launch_window.py', 'scripts/test_auto_material_products.py']


def prepare(folder):
    commit = verify_source(SOURCE, 'HEAD')
    current, _, db = release.settings('fb')
    previous = current.resolve()
    candidate = folder / 'candidate'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    preserve_tree_owners(previous, candidate)

    # Apply only our committed hunks. Production also has the story-copy hook.
    patch = subprocess.check_output(['git', '-C', str(SOURCE), 'diff', BASE, commit, '--', MODULE])
    assert patch and (candidate / MODULE).resolve().is_relative_to(candidate)
    subprocess.run(['git', 'apply', '--check', '-'], cwd=candidate, input=patch, check=True)
    subprocess.run(['git', 'apply', '-'], cwd=candidate, input=patch, check=True)
    shutil.copy2(previous / MODULE, folder / 'moboreels-before.py')
    for name in TEST_FILES:
        shutil.copy2(SOURCE / name, candidate / name)
    checks = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'tests.test_fb_mobo_launch_window',
                             'scripts.test_moboreels_hit.FacebookTests', 'scripts.test_auto_material_products.FBTests'],
                            cwd=candidate, capture_output=True, text=True, timeout=120)
    (folder / 'tests.txt').write_text(checks.stdout + checks.stderr)
    checks.check_returncode()
    # No migration. Preserve the pre-activation template rows with their version hashes.
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        rows = conn.execute("SELECT t.id,t.name,t.status,t.current_version,v.config_sha256 "
                            "FROM fb_auto_template t JOIN fb_auto_template_version v "
                            "ON v.template_id=t.id AND v.version=t.current_version ORDER BY t.id").fetchall()
    release.save(folder / 'templates-before.json', rows)
    (folder / 'templates-before.json').chmod(0o600)
    hashes = dict(before=release.sha(previous / MODULE), after=release.sha(candidate / MODULE))
    info = dict(platform='fb', commit=commit, source=str(SOURCE), previous=str(previous),
                candidate=str(candidate), files={MODULE: hashes}, static={}, status='prepared')
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--folder', required=True, type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    if args.action == 'prepare':
        prepare(folder)
    else:
        # This command runs in its own process. Durable independent renders
        # can continue while accepted HTTP requests and local claims drain.
        fb.native_active = owner_active
        release.activate('fb', folder, args.action == 'rollback', defer_plan_read=False)
