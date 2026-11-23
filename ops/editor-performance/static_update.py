"""Publish or roll back only the editor HTML/JS, without restarting services."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path('/mnt/data-disk/editor-performance-deploy')
DESTINATIONS=[Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html')]
NAMES=['video-editing-ai-studio.js','video-editing-ai.html']


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args])


def digest(data):return hashlib.sha256(data).hexdigest()


def atomic(path,data):
    temp=path.with_name('.editor-next-'+path.name)
    temp.write_bytes(data);temp.replace(path)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['install','rollback'])
    p.add_argument('--repo',type=Path);p.add_argument('--base');p.add_argument('--version')
    p.add_argument('--manifest',type=Path)
    args=p.parse_args()
    if args.action=='install':
        if git(args.repo,'rev-parse','HEAD').decode().strip()!=args.version or git(args.repo,'status','--porcelain').strip():
            raise RuntimeError('Need a clean exact GitHub checkout')
        if not git(args.repo,'branch','-r','--contains',args.version).strip():
            raise RuntimeError('Version has not been fetched from GitHub')
        folder=ROOT/'backups'/('static-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        prepared=[]
        for name in NAMES:
            old=git(args.repo,'show',args.base+':static/'+name)
            new=git(args.repo,'show',args.version+':static/'+name)
            for destination in DESTINATIONS:
                path=destination/name
                if path.read_bytes()!=old:raise RuntimeError('Concurrent static change: '+str(path))
                prepared.append((path,old,new))
        folder.mkdir(parents=True,mode=0o700)
        record={'base':args.base,'version':args.version,'state':'prepared','files':[]}
        for i,(path,old,new) in enumerate(prepared):
            (folder/f'{i}.before').write_bytes(old);(folder/f'{i}.after').write_bytes(new)
            record['files'].append({'path':str(path),'before':f'{i}.before','after':f'{i}.after','old_sha256':digest(old),'new_sha256':digest(new)})
        manifest=folder/'manifest.json'
        atomic(manifest,json.dumps(record,indent=2).encode())
    else:
        manifest=args.manifest;folder=manifest.parent;record=json.loads(manifest.read_text())
    changes=[]
    for item in record['files']:
        path=Path(item['path']);install=args.action=='install'
        before=(folder/item['before' if install else 'after']).read_bytes()
        after=(folder/item['after' if install else 'before']).read_bytes()
        if path.read_bytes()!=before:raise RuntimeError('Concurrent static change: '+str(path))
        changes.append((path,before,after))
    completed=[]
    try:
        for path,before,after in changes:
            atomic(path,after);completed.append((path,before))
    except Exception:
        for path,before in reversed(completed):atomic(path,before)
        raise
    record['state']='active' if args.action=='install' else 'rolled_back'
    atomic(manifest,json.dumps(record,indent=2).encode())
    print(json.dumps({'state':record['state'],'manifest':str(manifest),'version':record['version']}))


if __name__=='__main__':main()
