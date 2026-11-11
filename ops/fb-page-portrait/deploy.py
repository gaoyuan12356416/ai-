"""Deploy from an exact fetched GitHub commit, without restarting publishers."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

FILES=['fb-pages.html','fb-video-publish.js','fb-page-portraits.js','fb-page-portraits.css']
BASE={'fb-video-publish.js':'573e067fcc4b054de21ebe8d956ce2b97a26359d50bf9083a1494c137338f875',
      'fb-pages.html':'0d7ea715d00419f167ae72ca64ff1199a432fca0528584ed32006426a43c1de9'}


def run(*args):return subprocess.check_output(args,text=True).strip()
def digest(path):return hashlib.sha256(Path(path).read_text().encode()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--source');p.add_argument('--commit');p.add_argument('--deployment',required=True);p.add_argument('--rollback',action='store_true');a=p.parse_args()
    deployment=Path(a.deployment).resolve()
    assert deployment.is_relative_to(Path('/mnt/data-disk'))
    if a.rollback:
        m=json.loads((deployment/'manifest.json').read_text())
        for name in ('fb-page-portrait-refresh.timer','fb-page-portrait.service'):
            subprocess.run(['systemctl','disable','--now',name],check=True)
        for item in reversed(m['files']):
            target=Path(item['target'])
            if item['before'] is None:target.unlink(missing_ok=True)
            else:shutil.copy2(deployment/item['before'],target)
        run('systemctl','daemon-reload');run('nginx','-t');run('systemctl','reload','nginx')
        print('Code rolled back; imported Pages, income snapshots and publication ledgers preserved.');return
    source=Path(a.source).resolve();assert run('git','-C',str(source),'rev-parse','HEAD')==a.commit
    assert not run('git','-C',str(source),'status','--porcelain')
    assert run('findmnt','-no','UUID','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not deployment.exists()
    # Only the live public assets and main static fallback are replaced.
    roots=[Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html')]
    for root in roots:
        for name,expected in BASE.items():assert digest(root/name)==expected,'live_static_drift:'+str(root/name)
    candidate=deployment/'candidate';candidate.mkdir(parents=True)
    (candidate/'features').mkdir();(candidate/'features/__init__.py').write_text('')
    shutil.copytree(source/'features/fb_page_portrait',candidate/'features/fb_page_portrait',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    manifest=dict(commit=a.commit,source=str(source),candidate=str(candidate),at=datetime.now(timezone.utc).isoformat(),files=[])
    for root in roots:
        for name in FILES:
            target=root/name;backup='backup/'+str(target).lstrip('/')
            if target.exists():
                (deployment/backup).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,deployment/backup)
            else:backup=None
            manifest['files'].append(dict(target=str(target),before=backup))
    ops=source/'ops/fb-page-portrait'
    for name in ['fb-page-portrait.service','fb-page-portrait-refresh.service','fb-page-portrait-refresh.timer','fb-page-portrait.conf']:
        target=Path('/etc/nginx/default.d' if name.endswith('.conf') else '/etc/systemd/system')/name
        assert not target.exists(),'existing_feature_unit_requires_upgrade_plan'
        manifest['files'].append(dict(target=str(target),before=None))
    (deployment/'manifest.json').write_text(json.dumps(manifest,indent=2))
    pointer=Path('/opt/fb-page-portraits/current');pointer.parent.mkdir(exist_ok=True)
    assert not pointer.exists();pointer.symlink_to(candidate)
    for name in ['fb-page-portrait.service','fb-page-portrait-refresh.service','fb-page-portrait-refresh.timer']:
        shutil.copy2(ops/name,Path('/etc/systemd/system')/name)
    run('systemctl','daemon-reload');run('systemctl','enable','--now','fb-page-portrait.service')
    ready=False
    for attempt in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:18852/health',timeout=2) as r:ready=json.load(r)['ok']
            if ready:break
        except OSError:time.sleep(.25)
    assert ready,'portrait_read_service_not_ready'
    shutil.copy2(ops/'fb-page-portrait.conf','/etc/nginx/default.d/fb-page-portrait.conf')
    run('nginx','-t');run('systemctl','reload','nginx')
    for root in roots:
        for name in FILES:shutil.copy2(source/'static'/name,root/name)
    # Initial refresh/verification runs explicitly before the timer is enabled.
    print(json.dumps(dict(commit=a.commit,deployment=str(deployment),service=run('systemctl','is-active','fb-page-portrait.service'))))


if __name__=='__main__':main()
