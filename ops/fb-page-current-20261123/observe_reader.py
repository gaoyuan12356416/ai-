"""Observe two genuine direct SQL reads spanning one natural replica commit."""
import argparse
from contextlib import closing
import time
from operations import *


def observe(table, timeout):
    target = checked_table(table)
    if target not in (NEXT, PUBLIC):
        raise ValueError('current-rows target required')
    deadline = time.monotonic() + timeout
    pending = ROOT / 'pending.json'
    cfg = env()
    while time.monotonic() < deadline:
        try:
            job = json.loads(pending.read_text())
        except FileNotFoundError:
            time.sleep(.4)
            continue
        if job['target'] != target:
            time.sleep(1)
            continue
        with closing(connect_reader(cfg)) as pinned, closing(connect_reader(cfg)) as fresh:
            with pinned.cursor() as a, fresh.cursor() as b:
                a.execute("SET SESSION tx_isolation='REPEATABLE-READ'")
                a.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                before = one(a, f'SELECT MAX(profile_version) FROM {target}')[0]
                if before != job['base']:
                    time.sleep(.5)
                    continue
                n = one(a, f'SELECT COUNT(*) FROM {target} WHERE profile_version=%s', (before,))[0]
                earliest = time.monotonic()
                while time.monotonic() - earliest < 8:
                    observed = one(b, f'SELECT MAX(profile_version) FROM {target}')[0]
                    if observed != before:
                        again = one(a, f'SELECT COUNT(*) FROM {target} WHERE profile_version=%s', (before,))[0]
                        now = one(b, f'SELECT COUNT(*) FROM {target} WHERE profile_version=%s', (observed,))[0]
                        if not n or again != n or observed != job['meta']['version'] or now != len(job['expected']):
                            raise RuntimeError('direct repeatable-read queries crossed database snapshots')
                        result = dict(at=stamp(), table=target, before=before, after=observed, pinned_rows=again,
                                      fresh_rows=now, held_seconds=round(time.monotonic()-earliest, 3))
                        atomic(ROOT / 'qa' / ('direct-' + target + '.json'), result)
                        return result
                    time.sleep(.18)
                pinned.rollback()
    raise RuntimeError('no natural commit observed before the observation deadline')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('table',choices=(NEXT,PUBLIC))
    parser.add_argument('--timeout',type=int,default=7200)
    args=parser.parse_args()
    disk()
    print(encoded(observe(args.table,args.timeout)),flush=True)
