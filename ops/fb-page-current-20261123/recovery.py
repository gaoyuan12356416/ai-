"""Executable rollback and guarded removal of the already restored frozen table."""
import argparse
from contextlib import closing
import json
import time
from operations import *
from cutover import AFTER,BEFORE,HOT,ORIG,layout,transition
from history_archive import HISTORY,file_hash,insert_archived,archived_rows
from features.fb_page_current.mirror import legacy
from features.fb_page_current.common import read_config
from verify import fields


def allocated(c):
    c.execute('SELECT table_name,data_length,index_length FROM information_schema.tables WHERE table_schema=DATABASE()')
    return {r[0]:int(r[1] or 0)+int(r[2] or 0) for r in c.fetchall()}


def shared_allocation(c):
    per_table=bool(one(c,'SELECT @@innodb_file_per_table')[0])
    c.execute("SELECT SUM(TOTAL_EXTENTS*EXTENT_SIZE),SUM(FREE_EXTENTS*EXTENT_SIZE) FROM information_schema.FILES "
              "WHERE ENGINE='InnoDB' AND TABLESPACE_NAME='innodb_system'")
    total,available=c.fetchone()
    return dict(file_per_table=per_table,system_bytes=None if total is None else int(total),
                system_free_bytes=None if available is None else int(available),at=stamp())


def manifest():
    marker=json.loads((ROOT/'history-ready.json').read_text())
    if file_hash(HISTORY/'manifest.json')!=marker['manifest_sha256']:raise RuntimeError('backup manifest checksum mismatch')
    m=json.loads((HISTORY/'manifest.json').read_text())
    if file_hash(HISTORY/'schema.sql')!=m['schema_sha256'] or marker['rows']!=m['rows']:
        raise RuntimeError('backup schema or count mismatch')
    if file_hash(HISTORY/'task-references.jsonl.gz')!=m['task_reference_sha256']:
        raise RuntimeError('backup task index checksum mismatch')
    if file_hash(HISTORY/'restore-tools.tar.gz')!=m['restore_tools_sha256']:
        raise RuntimeError('backup recovery tools checksum mismatch')
    for v in m['versions']:
        folder=HISTORY/'versions'/checked(v['version'])
        if file_hash(folder/'snapshot.json')!=v['manifest_sha256'] or file_hash(folder/'restored.json')!=v['restore_sha256']:
            raise RuntimeError('per-version recovery proof mismatch')
    return marker,m


def remove_locked():
    disk();cfg=env();ready,m=manifest()
    if not ready.get('archive_flushed'):raise RuntimeError('backup durability acknowledgement missing')
    if m['references']['unexpected_absence']:raise RuntimeError('an expected task reference needs reconciliation')
    if read_config()!='live':raise RuntimeError('legacy mirror not frozen')
    if not (ROOT/'qa/postarchive-rollback.json').is_file():raise RuntimeError('postarchive restoration rehearsal missing')
    frozen=json.loads((ROOT/'history-frozen.json').read_text())
    if frozen['versions']!=[r['version'] for r in m['versions']]:raise RuntimeError('backup scope differs from frozen inventory')
    intent=ROOT/'history-drop-intent.json'
    finished=ROOT/'history-dropped.json'
    with closing(connect_reader(cfg)) as db,db.cursor() as c:
        if intent.exists() and not finished.exists():
            pending=json.loads(intent.read_text())
            if pending['manifest_sha256']!=ready['manifest_sha256']:raise RuntimeError('different outstanding removal intent')
            with closing(writer(cfg)) as w,w.cursor() as wc:actual=shape(wc,LEGACY)
            if actual not in ((),ORIG):raise RuntimeError('unexpected authoritative rollback table')
            deadline=time.monotonic()+100
            while shape(c,LEGACY)!=actual:
                if time.monotonic()>deadline:raise RuntimeError('old removal acknowledgement still in flight')
                time.sleep(1)
        before=allocated(c)
        if finished.exists() and LEGACY not in before and shape(c,PUBLIC)==HOT:
            saved=json.loads(finished.read_text())
            if saved['manifest_sha256']!=ready['manifest_sha256']:raise RuntimeError('prior drop receipt has a different backup')
            return saved
        if LEGACY in before:
            if layout(db)!=AFTER:raise RuntimeError('wrong table namespace')
            c.execute(f'SELECT DISTINCT profile_version FROM {LEGACY} ORDER BY profile_version')
            if [r[0] for r in c.fetchall()]!=frozen['versions']:raise RuntimeError('source changed after archive')
            for snapshot in m['versions']:
                n=one(c,f'SELECT COUNT(*) FROM {LEGACY} WHERE profile_version=%s',(snapshot['version'],))[0]
                if n!=snapshot['rows']:raise RuntimeError('source count changed for '+snapshot['version'])
            if not intent.exists():
                atomic(intent,dict(at=stamp(),name=LEGACY,cutoff=frozen['version'],manifest_sha256=ready['manifest_sha256'],
                    old_bytes=before[LEGACY],schema_before=sum(before.values()),current_bytes=before[PUBLIC],
                    shared=shared_allocation(c)))
            event=json.loads(intent.read_text())
            if event['manifest_sha256']!=ready['manifest_sha256']:raise RuntimeError('drop intent backup differs')
            with closing(writer(cfg)) as w,w.cursor() as wc:wc.execute(f'DROP TABLE {LEGACY}')
        elif not intent.exists():raise RuntimeError('legacy disappeared without a drop receipt')
        limit=time.monotonic()+100
        while shape(c,LEGACY):
            if time.monotonic()>limit:raise RuntimeError('drop acknowledgement pending')
            time.sleep(1)
        if shape(c,PUBLIC)!=HOT:raise RuntimeError('wrong current primary key')
        after=allocated(c);event=json.loads(intent.read_text())
        output=dict(at=stamp(),manifest_sha256=ready['manifest_sha256'],archive_rows=m['rows'],
               archive_versions=len(m['versions']),archive_bytes=ready['compressed_bytes'],
               dropped_table=LEGACY,dropped_table_allocated_bytes=event['old_bytes'],current_table_allocated_bytes=after[PUBLIC],
               schema_before=event['schema_before'],schema_after=sum(after.values()),schema_net_delta=event['schema_before']-sum(after.values()),
               shared_before=event.get('shared'),shared_after=shared_allocation(c),
               current_head=current_state(ManualReader(db))['version'],sizes={k:after.get(k,0) for k in (PUBLIC,NEXT,STATE,STAGE)})
        atomic(ROOT/'history-dropped.json',output)
        return output


def remove():
    with (HISTORY/'archive.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return remove_locked()


def create_legacy(destination,ddl):
    prefix='CREATE TABLE `'+PUBLIC+'`'
    if not ddl.startswith(prefix):raise RuntimeError('legacy DDL identity')
    with destination.cursor() as c:c.execute('CREATE TABLE `'+LEGACY+'`'+ddl[len(prefix):].rstrip(';\n'))


def ensure_latest():
    cfg=env()
    with closing(connect_reader(cfg)) as db,db.cursor() as c:
        if shape(c,PUBLIC)==ORIG:return current()
        if shape(c,PUBLIC)!=HOT:raise RuntimeError('unrecognized source')
        if not shape(c,LEGACY):
            manifest()
            with closing(writer(cfg)) as w:create_legacy(w,(HISTORY/'schema.sql').read_text())
            limit=time.monotonic()+90
            while not shape(c,LEGACY):
                if time.monotonic()>limit:raise RuntimeError('new rollback table not visible')
                time.sleep(1)
        if shape(c,LEGACY)!=ORIG:raise RuntimeError('rollback index mismatch')
        # All source pages are read from the same consistent version on the replica.
        c.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
        v=current_state(ManualReader(db))['version']
        p=receipt(v);meta=json.loads(one(c,f'SELECT meta_json FROM {PUBLIC} WHERE profile_version=%s LIMIT 1',(v,))[0])
        with closing(connect_reader(cfg)) as checks:
            legacy(ManualReader(checks),cfg,meta,p,source_items(db,PUBLIC,v,p['payload_hashes']))
        db.rollback()
        return v


def rollback():
    cfg=env()
    from cutover import confirm_shape,refresh_lock,mode
    outstanding=ROOT/'switch-intent.json'
    if outstanding.exists():
        intent=json.loads(outstanding.read_text())
        if intent['action']=='rollback':
            result=transition(False)
            restore_writer()
            return result
        if intent['action']!='cutover':raise RuntimeError('unknown outstanding namespace operation')
        with refresh_lock(),closing(writer(cfg)) as w,closing(connect_reader(cfg)) as replica:
            actual=layout(w)
            if actual not in (BEFORE,AFTER):raise RuntimeError('cannot identify interrupted forward switch')
            confirm_shape(replica,actual)
            if actual==BEFORE:
                atomic(ROOT/'cutover-cancelled.json',dict(at=stamp(),intent=intent))
                mode('shadow');outstanding.unlink()
        if actual==AFTER:transition(True)
    with closing(connect_reader(cfg)) as db:
        before=layout(db)==BEFORE
    if before:
        with refresh_lock():mode('legacy')
        if (ROOT/'readers-live.json').exists() and not (ROOT/'readers-rollback.json').exists():
            from readers_deploy import candidates,handoff
            candidates();handoff(True)
        restore_writer()
        return dict(legacy=True,version=current())
    # Prevent history removal while we repopulate the verified latest version.
    with (HISTORY/'archive.lock').open('a') if HISTORY.exists() else (ROOT/'rollback-archive.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        v=ensure_latest()
        if current()!=v:raise RuntimeError('newer version published, rerun to add it before rename')
        with closing(connect_reader(cfg)) as db:
            proof=receipt(v)
            a=fields(db,PUBLIC,v,proof['payload_hashes'])
            b=fields(db,LEGACY,v,proof['payload_hashes'])
            if a!=b or set(a)!=set(proof['payload_hashes']):raise RuntimeError('rollback rows or sources differ')
            atomic(ROOT/'qa/rollback-latest.json',dict(at=stamp(),version=v,rows=len(a)))
        result=transition(False)
    # The compatible readers can serve the append-only table and still have the sessions' frozen Page pages.
    restore_writer()
    return result


def restore_writer():
    from deploy import DROP,put
    record=json.loads((ROOT/'backup/refresh-drop-in.json').read_text())
    if record['existed']:put(DROP,(ROOT/'backup/refresh-drop-in').read_text())
    else:DROP.unlink(missing_ok=True)
    subprocess.run(['systemctl','daemon-reload'],check=True)


def restore_reference(v):
    checked(v);disk();cfg=env();manifest()
    if read_config()!='legacy':raise RuntimeError('requires restored append-only format')
    directory=HISTORY/'versions'/v;m=json.loads((directory/'snapshot.json').read_text())
    with closing(connect_reader(cfg)) as rc,rc.cursor() as c:
        if shape(c,PUBLIC)!=ORIG:raise RuntimeError('wrong restore target')
        existing=one(c,f'SELECT COUNT(*) FROM {PUBLIC} WHERE profile_version=%s',(v,))[0]
        if existing not in (0,m['rows']):raise RuntimeError('preexisting partial version')
        if not existing:
            with closing(writer(cfg)) as w,w.cursor() as wc:
                w.begin()
                try:
                    for part in m['parts']:
                        f=directory/part['name']
                        if file_hash(f)!=part['sha256']:raise RuntimeError('archive part checksum')
                        insert_archived(wc,PUBLIC,list(archived_rows(f)),HISTORY)
                    w.commit()
                except BaseException:w.rollback();raise
        deadline=time.monotonic()+100
        while one(c,f'SELECT COUNT(*) FROM {PUBLIC} WHERE profile_version=%s',(v,))[0]!=m['rows']:
            if time.monotonic()>deadline:raise RuntimeError('restore readback pending')
            time.sleep(1)
        expected={}
        for part in m['parts']:
            for r in archived_rows(directory/part['name']):expected[r[1]]=[r[9],sha(r[7]),*r[4:7]]
        from features.fb_page_current.publish import verify
        verify(ManualReader(rc),PUBLIC,v,expected)
        return dict(version=v,verified=m['rows'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['remove','rollback','restore-version']);p.add_argument('--version')
    p.add_argument('--sandbox',action='store_true',help='exercise rollback on the isolated MySQL Unix socket')
    a=p.parse_args();disk()
    if a.sandbox:
        if a.action!='rollback':p.error('--sandbox exercises the rollback command')
        from sandbox_archive import main
        print(encoded(main()),flush=True)
    else:print(encoded(remove() if a.action=='remove' else rollback() if a.action=='rollback' else restore_reference(a.version)),flush=True)
