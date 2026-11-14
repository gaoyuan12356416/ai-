"""Install isolated maintenance without restarting or interrupting X uploads.

Run from verified GitHub checkout. Overlay only the two new committed modules
on a copy of the current runtime, then install the new isolated unit/timer.
Code rollback stops this timer; it never restores a stale publishing database.
"""
import hashlib,json,os,shutil,sqlite3,subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FILES=['features/x_video_publish/account_recovery.py','scripts/x_video_account_recovery.py']

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',required=True);a=p.parse_args()
    out=Path(a.directory);out.mkdir(mode=0o755,parents=True,exist_ok=False)
    rev=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    remote=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','origin/codex/x-excel-account-release-20261114'],text=True).strip()
    assert rev==remote and not subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain'],text=True).strip()
    current=Path('/opt/x-post-automation/current').resolve(strict=True)
    release=out/'release';shutil.copytree(current,release,ignore=shutil.ignore_patterns('__pycache__','.git'))
    for f in FILES: shutil.copy2(ROOT/f,release/f)
    subprocess.check_call(['python3','-m','py_compile',*[str(release/f) for f in FILES]])
    backup=out/'backup';backup.mkdir(mode=0o700)
    c=sqlite3.connect('file:/var/lib/x-post-automation/accounts.sqlite3?mode=ro',uri=True);d=sqlite3.connect(str(backup/'accounts.sqlite3'));c.backup(d);d.close();c.close();(backup/'accounts.sqlite3').chmod(0o600)
    pointer=Path('/opt/x-video-account-recovery/current');previous=str(pointer.resolve()) if pointer.exists() else None
    units={}
    for name in ['x-video-account-recovery.service','x-video-account-recovery.timer']:
        target=Path('/etc/systemd/system')/name
        units[name]=target.read_text() if target.exists() else None
        if target.exists():shutil.copy2(target,backup/name)
        shutil.copy2(ROOT/'deploy'/name,target)
    pointer.parent.mkdir(parents=True,exist_ok=True);tmp=pointer.with_name('current.new');tmp.symlink_to(release);tmp.replace(pointer)
    manifest=dict(revision=rev,previous=previous,runtime_base=str(current),release=str(release),units=units,
                  hashes={f:hashlib.sha256((release/f).read_bytes()).hexdigest() for f in FILES})
    (out/'deployment.json').write_text(json.dumps(manifest,indent=2))
    subprocess.check_call(['systemctl','daemon-reload'])
    subprocess.check_call(['systemctl','start','x-video-account-recovery.service'])
    subprocess.check_call(['systemctl','enable','--now','x-video-account-recovery.timer'])
    print(json.dumps(dict(status='active',revision=rev,release=str(release))))
