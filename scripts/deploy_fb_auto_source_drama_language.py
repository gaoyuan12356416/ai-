"""Deploy only the FB source-language fallback repository."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_publish_queue_isolation import (
    backup_db, copy_file, preserve_owners, run, save, sha, switch, DBS,
)

BRANCH = 'codex/fb-auto-source-drama-language-20261109'
RELATIVE = 'features/fb_auto_posts/repositories.py'
BEFORE = 'ee77b856d45b6dfb2dbc8d538f612cbf8c33f1a8443ce96c49f3764c9fbe6c27'


def prepare(source, folder):
    assert run(['findmnt','-n','-o','UUID','/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk'))
    assert shutil.disk_usage('/mnt/data-disk').free > 512*1024*1024
    commit = run(['git','rev-parse','HEAD'],cwd=source)
    assert not run(['git','status','--porcelain'],cwd=source)
    assert run(['git','ls-remote','origin','refs/heads/'+BRANCH],cwd=source).split()[0] == commit
    live = Path('/opt/fb-auto-post/current').resolve()
    assert sha(live/RELATIVE) == BEFORE, 'affected_source_drift'
    folder.mkdir(parents=True,mode=0o711,exist_ok=False)
    candidate = folder/'candidates/fb'
    shutil.copytree(live,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
    preserve_owners(live,candidate)
    target = candidate/RELATIVE
    assert target.resolve().is_relative_to(candidate), 'overlay_symlink_escape'
    before = folder/'before'/RELATIVE
    before.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(live/RELATIVE,before)
    copy_file(source/RELATIVE,target)
    compile(target.read_bytes(),RELATIVE,'exec')
    backup_db(Path(DBS['fb']),folder/'fb-before.sqlite3')
    save(folder/'manifest.json',dict(commit=commit,source=str(source),lanes={'fb':dict(
        before_root=str(live),candidate=str(candidate),state='prepared',
        files={RELATIVE:dict(before=BEFORE,after=sha(target))})}))
    for test in ('test_fb_auto_material_product.py','test_fb_auto_source_drama_language.py'):
        relative = Path('scripts')/test
        shutil.copy2(source/relative,candidate/relative)
        result = subprocess.run([sys.executable,str(candidate/relative),'-v'],capture_output=True,text=True,timeout=60)
        (folder/(test+'.log')).write_text(result.stdout+result.stderr)
        assert result.returncode == 0, 'candidate_tests_failed:'+test
    print(json.dumps(dict(prepared=True,commit=commit,tests='14 passed')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','activate','rollback'])
    parser.add_argument('--folder',type=Path,required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.action=='prepare':
        prepare(Path(__file__).resolve().parents[1],folder)
    else:
        # Current task facts and template settings are never restored from backup.
        switch(folder,'fb',rollback=args.action=='rollback')


if __name__=='__main__':main()
