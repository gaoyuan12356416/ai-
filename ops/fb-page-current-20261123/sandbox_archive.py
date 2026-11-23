"""Rehearse archive, per-column recovery, and post-archive latest-service restoration on a Unix socket."""
from contextlib import closing
import tempfile
from history_archive import *
from recovery import create_legacy
from sandbox_check import connect
from features.fb_page_current.mirror import legacy
from cutover import swap_sql


def main():
    disk()
    base=Path(tempfile.mkdtemp(dir=ROOT,prefix='qa-history-'))
    with closing(connect()) as source,source.cursor() as c,closing(private_mysql()) as restore:
        if one(c,'SELECT @@socket')[0]!='/run/portrait-qa/mysql.sock':raise RuntimeError('sandbox required')
        if shape(c,PUBLIC)!=('profile_version','page_id') or shape(c,NEXT)!=('page_id',):raise RuntimeError('run sandbox_check first')
        v=one(c,f'SELECT MAX(profile_version) FROM {PUBLIC}')[0]
        pids=dict_all(c,v)
        proof=dict(version=v,verified_rows=len(pids),payload_hashes=pids)
        totals=one(c,f'SELECT SUM(paid_cents),SUM(social_cents),SUM(spend_cents) FROM {PUBLIC} WHERE profile_version=%s',(v,))
        proof.update(dict(zip(MONEY,map(int,totals))))
        ddl=one(c,'SHOW CREATE TABLE '+PUBLIC)[1]
        m=export_version(source,PUBLIC,v,base,proof=proof,rate=0)
        got=restore_version(restore,m,base,ddl)
        for pid,h in pids.items():
            raw,meta=lookup(v,pid,base)
            if sha(raw)!=h or meta['version']!=v:raise RuntimeError('reference recovery failed')
        # Simulate loss of the old table and hydrate it from the current, verified physical table.
        swap_sql(c,True)
        c.execute('DROP TABLE '+LEGACY)
        create_legacy(source,ddl)
        legacy(ManualReader(source),{},meta,proof,source_items(source,PUBLIC,v,pids),connector=connect)
        swap_sql(c,False)
        if dict_all(c,v)!=pids:raise RuntimeError('restored active version differs')
        result=dict(at=stamp(),mysql_socket='/run/portrait-qa/mysql.sock',version=v,restored_rows=got['rows'],
                    references=len(pids),all_12_columns_verified=True,postarchive_latest_rollback=True)
        atomic(ROOT/'qa/postarchive-rollback.json',result);print(encoded(result))


def dict_all(c,v):
    c.execute(f'SELECT page_id,payload_sha256 FROM {PUBLIC} WHERE profile_version=%s ORDER BY page_id',(v,))
    return dict(c.fetchall())


if __name__=='__main__':main()
