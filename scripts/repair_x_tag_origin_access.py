"""Grant the tag publisher only traversal/read access to its frozen Auto origin.

No process restart, database mutation, scheduler change or credential access.
Run from a verified GitHub checkout; keep the private ACL rollback record.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess

AUTO = Path('/mnt/data-disk/x-tag-publisher/auto')
DB = AUTO / 'x-auto-post.sqlite3'
USER = 'x-post-automation'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def check():
    # Test as the actual service user, not root. SQLite enforces mode=ro.
    code = "import sqlite3; c=sqlite3.connect(%r,uri=True); c.execute('SELECT id FROM x_auto_task LIMIT 1').fetchone(); print('readonly-origin-ok')" % (DB.as_uri()+'?mode=ro')
    return run('runuser','-u',USER,'--','python3','-c',code).stdout.strip()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('apply','rollback','check'))
    p.add_argument('--directory',type=Path)
    a=p.parse_args()
    if a.action=='check': print(check());return
    folder=a.directory
    if folder is None: p.error('--directory required')
    if a.action=='rollback':
        run('setfacl','--restore='+str(folder/'before.acl'))
        print('original ACL restored; business facts unchanged');return
    folder.mkdir(mode=0o700,parents=True,exist_ok=False)
    before=run('getfacl','-p',str(AUTO),str(DB)).stdout
    (folder/'before.acl').write_text(before)
    revision=run('git','-C',str(Path(__file__).resolve().parents[1]),'rev-parse','HEAD').stdout.strip()
    record={'revision':revision,'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'paths':[str(AUTO),str(DB)],'state':'prepared','restart':False}
    (folder/'record.json').write_text(json.dumps(record,indent=2)+'\n')
    # No listing or writes in the private Auto directory; no group membership.
    run('setfacl','-m','u:'+USER+':--x',str(AUTO))
    run('setfacl','-m','u:'+USER+':r--',str(DB))
    record.update(state='active',verification=check())
    (folder/'after.acl').write_text(run('getfacl','-p',str(AUTO),str(DB)).stdout)
    (folder/'record.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record))


if __name__=='__main__':main()
