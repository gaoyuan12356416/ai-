"""Force a commit between two SQL reads using complete representative rows from two real releases."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import tempfile
from unittest.mock import patch
import observe_reader
from operations import *
from history_archive import private_mysql


def sandbox(_=None):
    db=private_mysql()
    with db.cursor() as c:c.execute('USE portrait_direct_verify')
    return db


def prepare():
    disk()
    v=current();base=json.loads((ROOT/'receipts'/(v+'.json')).read_text())['base']
    older=receipt(base)['payload_hashes'];recent=receipt(v)['payload_hashes']
    parts=[sorted(p for p in older.keys()&recent.keys() if (older[p]!=recent[p])==different) for different in (True,False)]
    ids=[p for group in parts if group for p in group[::max(1,len(group)//6)][:6]]
    if not ids:raise RuntimeError('no shared Pages for direct replay')
    with closing(private_mysql()) as private,private.cursor() as lab,closing(connect_reader(env())) as replica,replica.cursor() as c:
        lab.execute('CREATE DATABASE IF NOT EXISTS portrait_direct_verify')
        lab.execute('USE portrait_direct_verify')
        for name,version in [(NEXT,base),('arrivals',v)]:
            lab.execute('DROP TABLE IF EXISTS '+name)
            lab.execute(f'CREATE TABLE {name} LIKE ads_ai.{NEXT}')
            c.execute(f'SELECT profile_version,page_id,window_start,window_end,paid_cents,social_cents,spend_cents,summary_json,'
                      f'COMPRESS(detail_json),meta_json,payload_sha256,created_at FROM {PUBLIC} WHERE profile_version=%s AND page_id IN ('+
                      ','.join(['%s']*len(ids))+')',(version,*ids))
            rows=c.fetchall()
            if {r[1] for r in rows}!=set(ids) or any(sha(unpack(r[8]))!=r[10] for r in rows):
                raise RuntimeError('source sample differs from its data')
            for row in rows:
                if row[10]!=(older if version==base else recent)[row[1]]:raise RuntimeError('published receipt mismatch')
                lab.execute(f'INSERT INTO {name} ('+','.join(COLUMNS)+') VALUES (%s,%s,%s,%s,%s,%s,%s,%s,UNCOMPRESS(%s),%s,%s,%s)',row)
    out=dict(at=stamp(),base=base,version=v,rows=len(ids))
    atomic(ROOT/'qa/direct-replay-input.json',out);return out


def run():
    disk()
    source=json.loads((ROOT/'qa/direct-replay-input.json').read_text())
    with tempfile.TemporaryDirectory(dir=ROOT,prefix='qa-sql-') as name,closing(sandbox()) as db,db.cursor() as c:
        private=Path(name);job=dict(target=NEXT,base=source['base'],meta=dict(version=source['version'],page_count=source['rows']))
        atomic(private/'building.json',job)
        with patch.object(observe_reader,'ROOT',private),patch.object(observe_reader,'connect_reader',sandbox),ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(observe_reader.observe_build,{},NEXT,job,time.monotonic()+40)
            limit=time.monotonic()+20
            while not (private/'qa'/('direct-watch-'+NEXT+'.json')).is_file():
                if pending.done():raise RuntimeError('reader failed to arm') from pending.exception()
                if time.monotonic()>limit:raise RuntimeError('private reader not armed')
                time.sleep(.05)
            atomic(private/'pending.json',job)
            db.begin()
            c.execute(f'UPDATE {NEXT} AS t JOIN arrivals AS s ON t.page_id=s.page_id SET '+
                      ','.join('t.'+k+'=s.'+k for k in COLUMNS if k!='page_id'))
            db.commit()
            result=pending.result(timeout=20)
        if result is None or result['pinned_rows']!=source['rows'] or result['fresh_rows']!=source['rows']:
            raise RuntimeError('private SQL race not observed')
        result.update(source='two complete real replica rowsets, private MySQL socket',columns=12,at=stamp())
        atomic(ROOT/'qa/direct-replay-verified.json',result);return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','run'));args=parser.parse_args()
    print(encoded(prepare() if args.action=='prepare' else run()),flush=True)
