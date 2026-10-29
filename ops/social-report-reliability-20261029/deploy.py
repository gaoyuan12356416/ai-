"""Narrow, Git-pinned deployment; backup all preimages before changing runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
LINK = Path('/opt/youtube-publisher-daily-report/current')
BACKUP = Path('/mnt/data-disk/social-report-repair-20261029/deploy-backup')


def blob(commit, relative):
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', commit + ':' + relative])


def replace(path, data, mode=0o644):
    temp = path.with_name(path.name + '.report-repair-next')
    temp.write_bytes(data)
    temp.chmod(mode)
    os.replace(temp, path)


def main():
    p=argparse.ArgumentParser();p.add_argument('--commit', required=True);args=p.parse_args()
    commit=subprocess.check_output(['git','-C',str(ROOT),'rev-parse',args.commit],text=True).strip()
    fetched=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','FETCH_HEAD'],text=True).strip()
    if commit != fetched:raise RuntimeError('Exact GitHub fetch required before deployment')
    baseline=json.loads((Path(__file__).parent/'baseline.json').read_text())
    for name,expected in baseline.items():
        path=Path(name);actual=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if actual != expected:raise RuntimeError('Concurrent live change; deployment stopped: '+name)
    old_release=LINK.resolve()
    facebook='features/social_analytics/facebook.py'
    expected=baseline['/root/drama_material_service/'+facebook]
    if hashlib.sha256((old_release/facebook).read_bytes()).hexdigest()!=expected:
        raise RuntimeError('Collector module differs; reconcile before deploy')
    BACKUP.mkdir(exist_ok=False)
    manifest={'commit':commit,'original_collector_release':str(old_release),'files':[]}
    for index,(name,expected) in enumerate(baseline.items()):
        path=Path(name);saved=BACKUP/('%02d-'%index+path.name)
        if path.exists():shutil.copy2(path,saved)
        manifest['files'].append({'path':name,'backup':str(saved) if expected else None,
                                  'old_sha256':expected,'old_mode':path.stat().st_mode & 0o777 if path.exists() else 0o644})
    (BACKUP/'deployment.json').write_text(json.dumps(manifest,indent=2))
    for item in manifest['files']:
        path=Path(item['path'])
        relative=(str(path.relative_to('/root/drama_material_service')) if '/root/drama_material_service/' in str(path)
                  else 'static/'+path.name)
        replace(path,blob(commit,relative),item['old_mode'])
    new_release=old_release.parent/(commit[:12]+'-report-reliability')
    shutil.copytree(old_release,new_release,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    replace(new_release/facebook,blob(commit,facebook))
    manifest['collector_release']=str(new_release)
    nxt=LINK.with_name('current.report-repair-next');nxt.symlink_to(new_release);os.replace(nxt,LINK)
    (BACKUP/'deployment.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'commit':commit,'backup':str(BACKUP),'collector_release':str(new_release)}))


if __name__=='__main__':main()
