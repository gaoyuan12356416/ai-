"""Stage the exact delta on the installed FB owner, including concurrent overlays."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import closing
import sqlite3

SRC=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SRC))
from scripts import deploy_fb_catalog_plan as predecessor
from scripts import deploy_fb_skipped_recheck as fb
from scripts import deploy_hit_drama as release
from scripts.deploy_fb_profile_priority import owner_active

FILES=['features/fb_auto_posts/'+name+'.py' for name in ('core','catalog_plan','planning_catalog')]
BASE='33d0b25157d1381f16697bffcf6b47e08584ef4a'
CACHE=Path('/mnt/data-disk/fb-planner-catalog')
INSTALL=Path('/opt/fb-planner-catalog/current')
TIMER='fb-planner-catalog.timer'


def install_reader():
    INSTALL.parent.mkdir(parents=True,exist_ok=True)
    if INSTALL.exists() and INSTALL.resolve()!=SRC:
        raise RuntimeError('shared catalog installation changed')
    if not INSTALL.exists():
        INSTALL.symlink_to(SRC)
    for name in (TIMER,'fb-planner-catalog.service'):
        dest=Path('/etc/systemd/system')/name
        data=(SRC/'deploy'/name).read_bytes()
        if dest.exists() and dest.read_bytes()!=data:
            raise RuntimeError('catalog unit changed: '+name)
        if not dest.exists():release.atomic(dest,data)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now',TIMER],check=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','apply','rollback'])
    p.add_argument('--folder',type=Path,required=True)
    a=p.parse_args()
    folder=a.folder.resolve()
    if not folder.is_relative_to('/mnt/data-disk') or release.run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise ValueError('deployment disk unavailable')
    if a.action=='prepare':
        predecessor.BASE=BASE
        predecessor.FILES=FILES
        predecessor.TEST='tests/test_fb_planning_catalog.py'
        predecessor.prepare(folder)
        subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_fb_*catalog*.py'],
                       cwd=folder/'candidate',check=True)
        return
    fb.native_active=owner_active
    flag=CACHE/'enabled'
    if a.action=='rollback':
        flag.unlink(missing_ok=True)
        if INSTALL.exists() and INSTALL.resolve()==SRC:
            subprocess.run(['systemctl','disable','--now',TIMER],check=True)
        release.activate('fb',folder,True,defer_plan_read=False)
        return
    catalog=CACHE/'current.sqlite3'
    with closing(sqlite3.connect(catalog.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        assert db.execute('SELECT count(*) FROM generation').fetchone()[0]==1
    install_reader()
    release.activate('fb',folder,False,defer_plan_read=False)
    flag.touch(mode=0o644)
    print('shared catalog enabled',flush=True)


if __name__=='__main__':
    main()
