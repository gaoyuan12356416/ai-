"""Narrow, hash-guarded worker repair; rollback preserves all publication data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

ROOT=Path('/root/drama_material_service')
BASE=Path('/mnt/data-disk/deploy/youtube-auto-publish')
UNIT='youtube-auto-publish-worker.service'
FILES={'features/youtube_auto_publish/worker_runtime.py':None,
       'features/youtube_auto_publish/runtime.py':'913e6b79d395d84891f7005ba032e3a9b2d1f9ff81f5d0b046ee4c79fb496bf3',
       'scripts/youtube_auto_publish_worker.py':'409f7196bbc6193c0c712dbda32d59bcf2bafe37ee72c40a77d0f8234a0632d4'}
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
def run(*args):return subprocess.check_output(args,text=True).strip()
def copy(src,dst):
    tmp=dst.with_name(dst.name+'.repair-new');shutil.copy2(src,tmp);os.replace(tmp,dst)
def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('--rollback',type=Path);args=parser.parse_args()
    assert run('findmnt','-n','-o','UUID','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    if args.rollback:
        backup=args.rollback.resolve();assert (BASE/'backups').resolve() in backup.parents
        manifest=json.loads((backup/'manifest.json').read_text())
        assert set(manifest['files'])==set(FILES)
        for rel,entry in manifest['files'].items():
            assert sha(ROOT/rel)==entry['new']
            if entry['old']:assert sha(backup/rel)==entry['old']
        run('systemctl','stop',UNIT)
        try:
            for rel,entry in reversed(list(manifest['files'].items())):
                if entry['old']:copy(backup/rel,ROOT/rel)
                else:(ROOT/rel).unlink()
        finally:run('systemctl','start',UNIT)
        print('Code rolled back; database and assets retained');return
    stage=Path(__file__).resolve().parents[1]
    commit=run('git','-C',str(stage),'rev-parse','HEAD')
    assert not run('git','-C',str(stage),'status','--porcelain')
    for rel,expected in FILES.items():assert sha(ROOT/rel)==expected,rel
    db=ROOT/'data/drama_material_jobs.sqlite3'
    with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as c:
        c.execute('pragma query_only=on')
        assert c.execute("select count(*) from drama_youtube_publish where lease_owner<>'' and lease_expires_at_utc>strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()[0]==0,'Active upload; retry when idle'
    backup=BASE/'backups'/('worker-stall-'+time.strftime('%Y%m%d-%H%M%S')+'-'+commit[:12]);backup.mkdir(parents=True)
    manifest={'commit':commit,'files':{}}
    for rel,expected in FILES.items():
        if expected:
            target=backup/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/rel,target)
            assert sha(target)==expected
        manifest['files'][rel]={'old':expected,'new':sha(stage/rel)}
    (backup/'manifest.json').write_text(json.dumps(manifest,indent=2))
    with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as src, sqlite3.connect(str(backup/'before.sqlite3')) as dst:src.backup(dst)
    run('systemctl','stop',UNIT)
    try:
        for rel in FILES:copy(stage/rel,ROOT/rel)
        for rel,entry in manifest['files'].items():assert sha(ROOT/rel)==entry['new']
    finally:run('systemctl','start',UNIT)
    print(json.dumps({'backup':str(backup),'commit':commit,'state':run('systemctl','is-active',UNIT)}))
if __name__=='__main__':main()
