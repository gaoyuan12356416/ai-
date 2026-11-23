"""Online backups and scoped WAL conversion for observed reader/writer locks.

Rollback changes journal mode only; restoring old database contents is forbidden.
"""
import json
import os
from pathlib import Path
import sqlite3
import time
import pwd
import subprocess

ROOT=Path('/mnt/data-disk/production-audit-20261123/native-wal')
PATHS={'fb':'/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3',
       'x':'/var/lib/x-post-automation/accounts.sqlite3',
       'tag':'/mnt/data-disk/x-tag-publisher/posts.sqlite3'}


def main():
    ROOT.mkdir(mode=0o700,exist_ok=True)
    results=[]
    for name,path in PATHS.items():
        source=sqlite3.connect(path,timeout=3,isolation_level=None)
        before=source.execute('PRAGMA journal_mode').fetchone()[0]
        backup=ROOT/(name+'.sqlite3')
        if before!='wal':
            if not backup.exists():
                partial=backup.with_suffix('.partial')
                dest=sqlite3.connect(str(partial));os.chmod(str(partial),0o600)
                try:source.backup(dest,pages=1024,sleep=.05)
                finally:dest.close()
                os.replace(str(partial),str(backup))
            source.close()
            owner=pwd.getpwuid(Path(path).stat().st_uid).pw_name
            code="import sqlite3,sys;c=sqlite3.connect(sys.argv[1],timeout=3,isolation_level=None);print(c.execute('PRAGMA journal_mode=WAL').fetchone()[0]);c.close()"
            for attempt in range(8):
                result=subprocess.run(['runuser','-u',owner,'--','python3','-c',code,path],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
                if result.returncode==0 and result.stdout.strip()=='wal':break
                if attempt==7:raise RuntimeError('WAL conversion could not acquire scoped lock:'+name)
                time.sleep(1)
            source=sqlite3.connect('file:'+path+'?mode=ro',uri=True,timeout=3)
        after=source.execute('PRAGMA journal_mode').fetchone()[0]
        source.close()
        results.append({'name':name,'path':path,'before':before,'after':after,'backup':str(backup)})
        (ROOT/'result.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(results))


if __name__=='__main__':main()
