"""Rehearse archive, per-column recovery, and post-archive latest-service restoration on a Unix socket."""
from contextlib import closing,ExitStack
from functools import partial
import tempfile
from unittest.mock import patch
from history_archive import *
import operations
import recovery
import cutover
from features.fb_page_current import common
from sandbox_check import connect
from features.fb_page_current.mirror import legacy
from cutover import swap_sql


def main():
    disk()
    test_root=Path(tempfile.mkdtemp(dir=ROOT,prefix='qa-history-'))
    base=test_root/'history';base.mkdir()
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
        (base/'schema.sql').write_text(ddl+';\n')
        (base/'task-references.jsonl.gz').write_bytes(gzip.compress(b'',mtime=0))
        revision,tools_hash=package_tools(base)
        manifest=dict(rows=len(pids),schema_sha256=file_hash(base/'schema.sql'),recovery_revision=revision,restore_tools_sha256=tools_hash,
            task_reference_sha256=file_hash(base/'task-references.jsonl.gz'),
            versions=[dict(version=v,manifest_sha256=got['manifest_sha256'],restore_sha256=file_hash(base/'versions'/v/'restored.json'))])
        atomic(base/'manifest.json',manifest)
        atomic(test_root/'history-ready.json',dict(rows=len(pids),manifest_sha256=file_hash(base/'manifest.json')))
        portraits=test_root/'portraits'
        atomic(portraits/'current.json',dict(version=v))
        atomic(portraits/(v+'.receipt.json'),proof)
        # All SQL goes to the private socket. The systemd restoration step only records the private target.
        with ExitStack() as overrides:
            for mod in (recovery,cutover):
                for name,value in dict(ROOT=test_root,env=lambda:{},connect_reader=connect,writer=connect).items():
                    overrides.enter_context(patch.object(mod,name,value))
            overrides.enter_context(patch.object(common,'ROOT',test_root))
            overrides.enter_context(patch.object(operations,'PORTRAITS',portraits))
            overrides.enter_context(patch.object(cutover,'PORTRAITS',portraits))
            overrides.enter_context(patch.object(recovery,'HISTORY',base))
            overrides.enter_context(patch.object(recovery,'legacy',partial(legacy,connector=connect)))
            overrides.enter_context(patch.object(recovery,'restore_writer',lambda:atomic(test_root/'restored-unit.json',dict(sandbox=True))))
            for missing in (False,True):
                swap_sql(c,True)
                cutover.mode('live' if missing else 'live-mirror')
                if missing:c.execute('DROP TABLE '+LEGACY)
                if not missing:
                    def lost_ack(cursor,forward):
                        swap_sql(cursor,forward)
                        raise pymysql.OperationalError(2013,'private post-rename disconnect')
                    with patch.object(cutover,'swap_sql',lost_ack):
                        try:recovery.rollback()
                        except pymysql.OperationalError as exc:
                            if exc.args[0]!=2013:raise
                        else:raise RuntimeError('unexercised DDL disconnect')
                    if not (test_root/'switch-intent.json').is_file():raise RuntimeError('DDL intent lost')
                outcome=recovery.rollback()
                if outcome['version']!=v or dict_all(c,v)!=pids or cutover.layout(source)!=cutover.BEFORE or (test_root/'switch-intent.json').exists():
                    raise RuntimeError('executable rollback did not restore the complete legacy layout')
        if not (test_root/'restored-unit.json').is_file():raise RuntimeError('sandbox unit handoff missing')
        result=dict(at=stamp(),mysql_socket='/run/portrait-qa/mysql.sock',version=v,restored_rows=got['rows'],
                    references=len(pids),all_12_columns_verified=True,existing_legacy_rollback=True,postarchive_latest_rollback=True,
                    command='recovery.py rollback',isolated_unit_restore=True,rename_disconnect_reconciled=True)
        atomic(ROOT/'qa/postarchive-rollback.json',result);return result


def dict_all(c,v):
    c.execute(f'SELECT page_id,payload_sha256 FROM {PUBLIC} WHERE profile_version=%s ORDER BY page_id',(v,))
    return dict(c.fetchall())


if __name__=='__main__':print(encoded(main()))
