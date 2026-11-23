"""Prepare service-specific releases from reviewed changes and the active dependencies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from features.fb_page_current.common import ROOT,atomic,encoded,stamp
from operations import disk

SOURCE=Path(__file__).resolve().parents[2]
BASES=dict(writer=Path('/opt/meta-page-recovery/current'),api=Path('/opt/fb-page-portraits/current'),
           assignment=Path('/opt/fb-page-assignment/current'))
PACKAGES=dict(writer='fb_page_portrait',api='fb_page_portrait',assignment='fb_page_assignment')
FILES=dict(writer={'refresh.py':'096233b8cebf1a8db3a87da8f2ef81f8301b8483','storage.py':'858a82ee1f42f12a3f6e863bfd9b262a615c0934'},
           api={'server.py':'e51707b750bd332cbd9c45b46af866cfc8cb367b'},
           assignment={'sources.py':'baf127f8bd947d85e0b88ee7ec9d81aa12e5a745','server.py':'431fc3be8b75f0fbb8c17362a77e16e6d66aaf0a'})
DROP=Path('/etc/systemd/system/fb-page-portrait-refresh.service.d/zz-page-current.conf')


def blob(path):
    data=path.read_bytes()
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def build(role):
    disk();original=BASES[role].resolve()
    rev=SOURCE.name
    if len(rev)!=40 or SOURCE.parent!=ROOT/'code':raise RuntimeError('verified release directory required')
    package=PACKAGES[role];base=original/'features'
    for name,expected in FILES[role].items():
        if blob(base/package/name)!=expected:raise RuntimeError('concurrent deployment: '+role+'/'+name)
    target=ROOT/'runtimes'/rev/role
    if (target/'complete.json').is_file():return target
    dest=target/'features';dest.mkdir(parents=True,exist_ok=True)
    for p in base.iterdir():
        if p.name in (package,'fb_page_current','__pycache__'):continue
        out=dest/p.name
        if not out.exists():out.symlink_to(p)
    shutil.copytree(base/package,dest/package,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(SOURCE/'features/fb_page_current',dest/'fb_page_current',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    for name in FILES[role]:shutil.copy2(SOURCE/'features'/package/name,dest/package/name)
    atomic(target/'complete.json',dict(role=role,revision=rev,original=str(original),files=FILES[role],at=stamp()))
    return target


def backup(path,name):
    store=ROOT/'backup';store.mkdir(exist_ok=True)
    receipt=store/(name+'.json')
    if not receipt.exists():
        if path.exists():shutil.copy2(path,store/name)
        atomic(receipt,dict(path=str(path),existed=path.exists(),at=stamp()))


def put(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name('.'+path.name+'.page-current')
    temp.write_text(data)
    temp.chmod(0o644);temp.replace(path)


def writer_release():
    target=build('writer')
    backup(DROP,'refresh-drop-in')
    put(DROP,'[Service]\nWorkingDirectory='+str(target)+'\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n')
    subprocess.run(['systemctl','daemon-reload'],check=True)
    actual=subprocess.check_output(['systemctl','show','fb-page-portrait-refresh.service','-p','WorkingDirectory','--value'],text=True).strip()
    if actual!=str(target):raise RuntimeError('refresh target not applied')
    return dict(scope='future natural refresh invocations',working_directory=actual)


if __name__=='__main__':
    ps=argparse.ArgumentParser();ps.add_argument('action',choices=['writer','build-api','build-assignment'])
    cmd=ps.parse_args().action
    print(encoded(writer_release() if cmd=='writer' else str(build(cmd[6:]))))

