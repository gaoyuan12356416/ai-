"""Keep overlapping short read-only views while observing one natural replica commit."""
import argparse
from contextlib import closing,ExitStack
import time
from operations import *


def begin(db, table):
    db.rollback()
    with db.cursor() as c:
        c.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
        v=one(c,f'SELECT MAX(profile_version) FROM {table}')[0]
        n=one(c,f'SELECT COUNT(*) FROM {table} WHERE profile_version=%s',(v,))[0]
    return v,n,time.monotonic()


def confirm(old_db,old,fresh,target,job):
    before,n,started=old
    with old_db.cursor() as a,fresh.cursor() as b:
        again=one(a,f'SELECT COUNT(*) FROM {target} WHERE profile_version=%s',(before,))[0]
        after=one(b,f'SELECT MAX(profile_version) FROM {target}')[0]
        now=one(b,f'SELECT COUNT(*) FROM {target} WHERE profile_version=%s',(after,))[0]
    if before!=job['base'] or not n or again!=n or after!=job['meta']['version'] or now!=job['meta']['page_count']:
        raise RuntimeError('direct repeatable-read queries crossed database snapshots')
    fact=dict(at=stamp(),table=target,before=before,after=after,pinned_rows=again,fresh_rows=now,
              held_seconds=round(time.monotonic()-started,3))
    atomic(ROOT/'qa'/('direct-'+target+'.json'),fact)
    return fact


def observe_build(cfg,target,job,deadline):
    info=ROOT/'qa'/('direct-watch-'+target+'.json')
    version=job['meta']['version']
    with ExitStack() as stack:
        peers=[stack.enter_context(closing(connect_reader(cfg))) for _ in range(3)]
        for db in peers[:2]:
            with db.cursor() as c:c.execute("SET SESSION tx_isolation='REPEATABLE-READ'")
        foreground,spare,fresh=peers
        first=begin(foreground,target)
        if first[0]!=job['base']:return None
        atomic(info,dict(at=stamp(),phase='armed',before=first[0],after=version,rows=first[1]))
        while time.monotonic()<deadline:
            # Prepare the replacement on another connection while the old transaction is still held.
            if time.monotonic()-first[2]>=5:
                successor=begin(spare,target)
                if successor[0]==version:return confirm(foreground,first,spare,target,job)
                if successor[0]!=job['base']:raise RuntimeError('unexpected replica version')
                foreground.rollback()
                foreground,spare=spare,foreground
                first=successor
            if (ROOT/'pending.json').is_file():
                with fresh.cursor() as c:visible=one(c,f'SELECT MAX(profile_version) FROM {target}')[0]
                if visible==version:return confirm(foreground,first,fresh,target,job)
            elif not (ROOT/'building.json').is_file():
                # A fast commit may already have cleaned its stage. Open a new view before releasing the old one.
                successor=begin(spare,target)
                if successor[0]==version:return confirm(foreground,first,spare,target,job)
                return None
            time.sleep(.35)
    return None


def observe(target,timeout):
    checked_table(target)
    deadline=time.monotonic()+timeout
    cfg=env()
    while time.monotonic()<deadline:
        try:job=json.loads((ROOT/'building.json').read_text())
        except FileNotFoundError:job=None
        if job and job['target']==target:
            result=observe_build(cfg,target,job,deadline)
            if result:return result
        time.sleep(.6)
    raise RuntimeError('no natural commit observed before the observation deadline')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('table',choices=(NEXT,PUBLIC))
    parser.add_argument('--timeout',type=int,default=10800)
    args=parser.parse_args();disk()
    print(encoded(observe(args.table,args.timeout)),flush=True)
