"""Scoped pool-policy overlays, ledger-preserving migration and safe HTTP switch."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_publish_queue_isolation import (
    backup_db, copy_file, preserve_owners, run, save, sha, switch, DBS,
)
from scripts.publish_scheduler_health import deployment_guard

BASE = 'f44b3b47dda1fff4d2dae2910ad81a6554f74f50'
VIDEO = ['features/fb_video_publish/'+n+'.py' for n in ('sources','pool','pool_scheduler','worker','core','guards')]
DISPATCH = ['features/pipeline_dispatch/'+n+'.py' for n in ('adapters','fb_isolation','core')]
FB = VIDEO + DISPATCH + ['features/fb_auto_posts/gpu.py','features/pipeline_dispatch/fb_native.py']
STATIC = ['static/'+n for n in ('fb-pages.html','fb-video-publish.html','fb-video-publish.js')]
STATIC_BASE = {'static/fb-pages.html': '7ee927121d752f078732dbf3837db6c5e1eaf38c7af895eb647cb30610234014', 'static/fb-video-publish.html': 'e9bda56780b2c948a42ad5d14e93a507d5f122d2ab30e4ea8f51523033ad4393', 'static/fb-video-publish.js': '76da1afb723b95faab04c9a3c25769626ae95122062e8c1a9c2e7c6b3f6cb1e1'}


def prepare(source, folder):
    assert run(['findmnt','-n','-o','UUID','/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk'))
    commit = run(['git','rev-parse','HEAD'],cwd=source)
    assert not run(['git','status','--porcelain'],cwd=source)
    assert run(['git','ls-remote','origin','refs/heads/codex/fb-page-pool-any-product-20261106'],cwd=source).split()[0] == commit
    folder.mkdir(parents=True,exist_ok=False)
    manifest = dict(commit=commit, source=str(source), lanes={})
    for lane, root, files in [('fb','/opt/fb-auto-post/current',FB),
                              ('coordinator','/opt/pipeline-dispatch/current',DISPATCH),
                              ('app','/root/drama_material_service',STATIC),
                              ('public','/usr/share/nginx/html',STATIC)]:
        live=Path(root).resolve(); candidate=folder/'candidates'/lane
        candidate.parent.mkdir(exist_ok=True)
        if lane in ('fb','coordinator'):
            shutil.copytree(live,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
            preserve_owners(live,candidate)
        else: candidate.mkdir()
        item=dict(before_root=str(live),candidate=str(candidate),state='prepared',files={})
        manifest['lanes'][lane]=item
        for relative in files:
            actual=relative.removeprefix('static/') if lane=='public' else relative
            before=live/actual;target=candidate/actual;backup=folder/'before'/lane/actual
            target.parent.mkdir(parents=True,exist_ok=True);backup.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(before,backup);copy_file(before,target)
            if relative in STATIC:
                assert sha(before)==STATIC_BASE[relative],'static_drift:'+str(before)
                copy_file(source/relative,target)
            else:
                patch=run(['git','diff',BASE,commit,'--',relative],cwd=source)+'\n'
                run(['git','apply','--check','-'],cwd=candidate,input=patch)
                run(['git','apply','-'],cwd=candidate,input=patch)
                compile(target.read_bytes(),relative,'exec')
            item['files'][actual]=dict(before=sha(before),after=sha(target))
    # Online SQLite backups; never restore these over current publishing facts.
    for lane in ('fb','dispatch'): backup_db(Path(DBS[lane]),folder/(lane+'-before.sqlite3'))
    shutil.copy2(folder/'fb-before.sqlite3',folder/'fb-rehearsal.sqlite3')
    code='''import sqlite3,hashlib,sys
from features.fb_video_publish.core import Store
p=sys.argv[1]
def facts():
 c=sqlite3.connect(p)
 try:return {t:hashlib.sha256(repr(c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall()).encode()).hexdigest() for t in ['fb_video_task','fb_video_pool_item','fb_video_pool_slot','fb_video_attempt','fb_video_link']}
 finally:c.close()
before=facts();Store(p);Store(p);assert before==facts();print('pool_tasks_attempts_links_preserved')
'''
    proof=run([sys.executable,'-c',code,folder/'fb-rehearsal.sqlite3'],cwd=folder/'candidates/fb')
    (folder/'rehearsal.log').write_text(proof+'\n')
    save(folder/'manifest.json',manifest)
    print(json.dumps(dict(prepared=True,commit=commit,rehearsal=proof)),flush=True)


def static_switch(folder,lane,rollback=False):
    path=folder/'manifest.json';m=json.loads(path.read_text());item=m['lanes'][lane]
    for rel,h in item['files'].items():
        target=Path(item['before_root'])/rel
        expected=h['after' if rollback else 'before']
        assert sha(target)==expected,'source_drift:'+str(target)
        src=(folder/'before'/lane/rel) if rollback else Path(item['candidate'])/rel
        copy_file(src,target)
    item['state']='rolled_back' if rollback else 'installed';save(path,m)
    print(json.dumps(dict(lane=lane,state=item['state'],restart=False)),flush=True)


def rollback(folder):
    # Old code recreates a unique pending-material index and strict admission.
    # Finish only affected Page-pool queue items first; never delete/reset them.
    with contextlib.closing(sqlite3.connect('file:'+DBS['fb']+'?mode=ro',uri=True)) as c:
        assert not c.execute("SELECT 1 FROM fb_video_pool_item WHERE status IN ('pending','processing') LIMIT 1").fetchone(), 'finish_existing_page_pool_items_before_code_rollback'
        assert not c.execute("SELECT 1 FROM fb_video_task WHERE source_kind='pool' AND status NOT IN ('published','failed','cancelled') LIMIT 1").fetchone(), 'preserve_unresolved_page_pool_tasks'
    m=json.loads((folder/'manifest.json').read_text());reverse=folder/'rollback'
    reverse.mkdir(exist_ok=True)
    r=dict(commit=m['commit'],lanes={})
    for lane in ('fb','coordinator'):
        old=m['lanes'][lane]
        r['lanes'][lane]=dict(before_root=old['candidate'],candidate=old['before_root'],state='prepared',
            files={p:dict(before=h['after'],after=h['before']) for p,h in old['files'].items()})
    save(reverse/'manifest.json',r)
    switch(reverse,'fb')
    with deployment_guard(['coordinator'],reverse/'coordinator-support'):switch(reverse,'coordinator')
    for lane in ('app','public'):static_switch(folder,lane,True)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','activate','rollback']);p.add_argument('--folder',type=Path,required=True)
    a=p.parse_args();folder=a.folder.resolve()
    if a.action=='prepare':prepare(Path(__file__).resolve().parents[1],folder)
    elif a.action=='rollback':rollback(folder)
    else:
        with deployment_guard(['coordinator'],folder/'coordinator-support'):switch(folder,'coordinator')
        switch(folder,'fb')
        for lane in ('app','public'):static_switch(folder,lane)


if __name__=='__main__':main()
