"""Run the real freeze-inventory export, isolated restore, targeted removal and rollback on a private socket."""
from contextlib import closing,ExitStack
from functools import partial
import sqlite3
import tempfile
from unittest.mock import patch
import cutover
import history_archive as archive
import operations as ops
import recovery
from features.fb_page_current import common
from features.fb_page_current.mirror import legacy
from sandbox_check import connect
from sandbox_archive import dict_all


def main():
    ops.disk()
    work=ops.Path(tempfile.mkdtemp(dir=ops.ROOT,prefix='qa-lifecycle-'))
    backup=work/'history';portraits=work/'portraits';ledger=work/'meta.sqlite3'
    (work/'source.git').symlink_to(ops.ROOT/'source.git')
    with closing(connect()) as private,private.cursor() as c:
        if ops.one(c,'SELECT @@socket,DATABASE()')!=('/run/portrait-qa/mysql.sock','ads_ai'):
            raise RuntimeError('full rehearsal requires the private sandbox')
        if cutover.layout(private)!=cutover.BEFORE:raise RuntimeError('rehearsal needs the original private layout')
        v=ops.one(c,'SELECT MAX(profile_version) FROM '+ops.PUBLIC)[0]
        hashes=dict_all(c,v);pid=sorted(hashes)[0]
        amounts=ops.one(c,'SELECT SUM(paid_cents),SUM(social_cents),SUM(spend_cents) FROM '+ops.PUBLIC+' WHERE profile_version=%s',(v,))
        proof=dict(version=v,verified_rows=len(hashes),payload_hashes=hashes,**dict(zip(ops.MONEY,map(int,amounts))))
        ops.atomic(portraits/'current.json',dict(version=v));ops.atomic(portraits/(v+'.receipt.json'),proof)
        ops.atomic(work/'history-frozen.json',dict(version=v,versions=[v]))
        ops.atomic(work/'mode.json',dict(mode='live'))
        prior=ops.json.loads((ops.ROOT/'qa/postarchive-rollback.json').read_text())
        ops.atomic(work/'qa/postarchive-rollback.json',prior)
        with closing(sqlite3.connect(ledger)) as db,db:
            db.execute('CREATE TABLE jobs(id TEXT,proof TEXT)')
            db.execute('INSERT INTO jobs VALUES(?,?)',('test-recovery',ops.encoded(dict(profile_version=v,new_page_id=pid))))
        c.execute('CREATE TABLE fb_drama_page_assignment_log(task_id VARCHAR(32),item_index INT,page_id VARCHAR(32),'
                  'selection_stage VARCHAR(32),diagnostics_json LONGTEXT,PRIMARY KEY(task_id,item_index))')
        try:
            diag=ops.encoded(dict(profile_snapshot=dict(version=v)))
            c.executemany('INSERT INTO fb_drama_page_assignment_log VALUES(%s,%s,%s,%s,%s)',
                          [('match',1,pid,'language_profile',diag),('fallback',1,'999999','language_unprofiled',diag)])
            cutover.swap_sql(c,True)
            with ExitStack() as stack:
                for mod in (archive,recovery,cutover):
                    for key,val in dict(ROOT=work,env=lambda:{},connect_reader=connect).items():
                        stack.enter_context(patch.object(mod,key,val))
                for mod in (recovery,cutover):stack.enter_context(patch.object(mod,'writer',connect))
                for mod in (archive,recovery):stack.enter_context(patch.object(mod,'HISTORY',backup))
                for mod in (archive,cutover,ops):stack.enter_context(patch.object(mod,'PORTRAITS',portraits))
                stack.enter_context(patch.object(archive,'FROZEN',work/'history-frozen.json'))
                stack.enter_context(patch.object(archive,'META_LEDGER',ledger))
                stack.enter_context(patch.object(common,'ROOT',work))
                stack.enter_context(patch.object(recovery,'legacy',partial(legacy,connector=connect)))
                stack.enter_context(patch.object(recovery,'restore_writer',lambda:None))
                receipt=archive.run()
                refs=receipt['references']
                if refs['found']!=1 or refs['absent_in_source']!=1 or refs['unexpected_absence'] or refs['receipts']['assignment']!=2:
                    raise RuntimeError('isolated task lookup differs')
                first=recovery.remove();second=recovery.remove()
                if first!=second or ops.shape(c,ops.LEGACY) or ops.shape(c,ops.PUBLIC)!=('page_id',):
                    raise RuntimeError('wrong or non-idempotent removal')
                restored=recovery.rollback()
                if restored['version']!=v or dict_all(c,v)!=hashes:raise RuntimeError('full rollback failed after removal')
        finally:c.execute('DROP TABLE fb_drama_page_assignment_log')
    outcome=dict(at=ops.stamp(),restored_rows=len(hashes),references=refs['unique_pairs'],archive_flushed=True,
                 targeted_remove=True,idempotent=True,rollback=True,socket='/run/portrait-qa/mysql.sock')
    ops.atomic(ops.ROOT/'qa/complete-archive-rehearsed.json',outcome);return outcome


if __name__=='__main__':print(ops.encoded(main()),flush=True)
