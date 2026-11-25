"""Stage just the planner delta on the current FB release, then use its scoped switch."""
import argparse
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release
from scripts import deploy_fb_skipped_recheck as fb
from scripts.deploy_fb_profile_priority import owner_active
from scripts.deploy_pipeline_runtime import preserve_tree_owners, verify_source

BASE = 'a70742e9fd1c42abe0dc6c49651ffb0b9ce56288'
FILES = ['features/fb_auto_posts/' + name + '.py' for name in
         ('core','page_caption','profile_material','catalog_plan')]
TEST = 'tests/test_fb_catalog_plan.py'


def prepare(folder):
    commit = verify_source(SOURCE, 'HEAD')
    link, _, db = release.settings('fb')
    old, candidate = link.resolve(), folder / 'candidate'
    baseline = json.loads((old.parent/'manifest.json').read_text())['commit']
    subprocess.run(['git','-C',str(SOURCE),'merge-base','--is-ancestor',BASE,baseline],check=True)
    subprocess.run(['git','-C',str(SOURCE),'merge-base','--is-ancestor',baseline,commit],check=True)
    folder.mkdir(exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(old, candidate, symlinks=True, ignore=shutil.ignore_patterns('.git','__pycache__','*.pyc'))
    preserve_tree_owners(old, candidate)
    patch = subprocess.check_output(['git','-C',str(SOURCE),'diff','--binary',baseline,commit,'--',*FILES,TEST])
    subprocess.run(['git','apply','--check','-'],cwd=candidate,input=patch,check=True)
    subprocess.run(['git','apply','-'],cwd=candidate,input=patch,check=True)
    for path in (*FILES, TEST):
        compile((candidate/path).read_bytes(), path, 'exec')
    proof = subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_fb_catalog_plan.py'],
                           cwd=candidate,capture_output=True,text=True)
    (folder/'tests.txt').write_text(proof.stdout+proof.stderr)
    proof.check_returncode()
    with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(folder/'ledger-before.sqlite3')) as dst:
        src.backup(dst)
    (folder/'ledger-before.sqlite3').chmod(0o600)
    release.save(folder/'manifest.json', dict(platform='fb',commit=commit,source=str(SOURCE),previous=str(old),
        candidate=str(candidate),status='prepared',static={},files={
            path:dict(before=release.sha(old/path),after=release.sha(candidate/path)) for path in FILES}))
    print(json.dumps(dict(status='prepared',commit=commit)),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare','apply','rollback'])
    parser.add_argument('--folder',type=Path,required=True)
    args=parser.parse_args()
    folder=args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk') and release.run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    if args.action=='prepare':
        prepare(folder)
    else:
        fb.native_active=owner_active
        release.activate('fb',folder,args.action=='rollback',defer_plan_read=False)
