"""Upgrade only independent catalog code and its two UI files, retaining all work."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3

import deploy as release


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['install','rollback'])
    p.add_argument('--repo',type=Path);p.add_argument('--version');p.add_argument('--base');p.add_argument('--static-base')
    p.add_argument('--manifest',type=Path)
    args=p.parse_args()
    if args.manifest:
        return release.switch(json.loads(args.manifest.read_text()),args.manifest.parent,args.action=='rollback')
    if args.action!='install':raise RuntimeError('Rollback requires its manifest')
    if release.cmd('git','-C',args.repo,'rev-parse','HEAD')!=args.version or release.cmd('git','-C',args.repo,'status','--porcelain'):
        raise RuntimeError('Need a clean exact GitHub checkout')
    if not release.cmd('git','-C',args.repo,'branch','-r','--contains',args.version):
        raise RuntimeError('Version was not fetched from GitHub')
    previous=release.CURRENT.resolve()
    if previous.name!=args.base:raise RuntimeError('Concurrent API release change')
    changes=[]
    for name in ['video-editing-ai-studio.js','video-editing-ai.html']:
        before=release.blob(args.repo,args.static_base,'static/'+name)
        after=release.blob(args.repo,args.version,'static/'+name)
        for root in release.STATIC:
            path=root/name
            if path.read_bytes()!=before:raise RuntimeError('Concurrent static change: '+str(path))
            changes.append((path,before,after))
    folder=release.ROOT/'backups'/('catalog-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    folder.mkdir(parents=True,mode=0o700)
    with closing(sqlite3.connect(release.DB.as_uri()+'?mode=ro',uri=True)) as src:
        with closing(sqlite3.connect(folder/'before-editor.sqlite3')) as dst:src.backup(dst)
    (folder/'before-editor.sqlite3').chmod(0o600)
    shutil.copytree(previous/'ai_video_editing',folder/'ai_video_editing',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    target=release.prepare_release(args.repo,args.base,args.version,previous)
    record={'version':args.version,'base':args.base,'static_base':args.static_base,'previous':str(previous),
            'release':str(target),'compatibility':str(previous),'state':'prepared','files':[]}
    for i,(path,before,after) in enumerate(changes):
        (folder/f'{i}.before').write_bytes(before);(folder/f'{i}.after').write_bytes(after)
        record['files'].append({'path':str(path),'kind':'static','before':f'{i}.before','after':f'{i}.after',
                                'old_sha256':release.sha(before),'new_sha256':release.sha(after)})
    release.atomic(folder/'manifest.json',json.dumps(record,indent=2).encode())
    release.switch(record,folder)


if __name__=='__main__':main()
