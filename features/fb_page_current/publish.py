"""Build one bounded delta and publish it atomically with the database version."""
from contextlib import closing,contextmanager
import datetime as dt
import fcntl
import json
import time

from .common import (MONEY,PAGE,ROOT,STATE,STAGE,NEXT,PUBLIC,atomic,checked,checked_table,
                     current_state,digest_rows,encoded,insert_batch,literal,pack,query,scope_hash,sha,stamp,writer)


@contextmanager
def owner(root=ROOT):
    root.mkdir(parents=True,exist_ok=True)
    with (root/'writer.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def expected_hash(expected):
    return sha(encoded(expected))


def clear_build(root,version):
    marker=root/'building.json'
    if marker.exists() and json.loads(marker.read_text())['version']==version:
        marker.unlink()
    (root/(checked(version)+'.delta.gz')).unlink(missing_ok=True)


def verify(reader,table,version,expected,*,base_hashes=None):
    staged=base_hashes is not None
    condition=" WHERE profile_version="+literal(checked(version))
    count=query(reader,"SELECT HEX(JSON_OBJECT('n',COUNT(*))) FROM "+table+condition)[0]['n']
    if count!=len(expected):raise RuntimeError('portrait row count mismatch')
    observed=digest_rows(reader,table,version,expected,stage=staged)
    if set(observed)!=set(expected):raise RuntimeError('portrait scope mismatch')
    for pid,(digest,summary,*amounts) in expected.items():
        row=observed[pid]
        calc=row['calc']
        if staged and calc is None:calc=base_hashes.get(pid)
        if digest!=row['stored'] or digest!=calc or summary!=row['summary'] or amounts!=[row['a'],row['b'],row['c']]:
            raise RuntimeError('portrait value mismatch for '+pid)


def wait_visible(reader,sql,predicate):
    deadline=time.monotonic()+100
    while True:
        rows=query(reader,sql)
        if predicate(rows):return
        if time.monotonic()>=deadline:raise RuntimeError('replica confirmation pending')
        time.sleep(2)


def stage_rows(db,version,items,prior,meta,matching=None):
    expected={}
    totals=dict.fromkeys(MONEY,0)
    changed=skipped=0
    sql=(f'INSERT INTO {STAGE}(profile_version,page_id,paid_cents,social_cents,spend_cents,summary_json,'
         'detail_json,payload_sha256,row_timestamp) VALUES ')
    placeholders='(%s,%s,%s,%s,%s,%s,UNCOMPRESS(%s),%s,%s)'
    batch=[]
    byte_count=0
    with db.cursor() as c:
        c.execute(f'DELETE FROM {STAGE} WHERE profile_version=%s',(version,))
        for record in items:
            pid,summary,raw,digest,amounts,row_time=record[:6]
            length=record[6] if len(record)>6 else len(raw)
            checked(pid,PAGE)
            if pid in expected or len(amounts)!=3:raise ValueError('duplicate page or malformed money')
            if matching is not None and matching.get(pid)!=digest:raise ValueError('source receipt mismatch')
            if raw is None:
                if prior.get(pid)!=digest:raise RuntimeError('base of unchanged Page is no longer available')
            elif sha(raw)!=digest:raise ValueError('input detail hash mismatch')
            if json.loads(summary).get('page_id')!=pid:raise ValueError('input page identity mismatch')
            expected[pid]=[digest,sha(summary),*amounts]
            for key,value in zip(MONEY,amounts):totals[key]+=value
            payload=None
            if prior.get(pid)!=digest:
                payload=pack(raw)
                changed+=length
            else:skipped+=length
            batch.append((version,pid,*amounts,summary,payload,digest,row_time))
            byte_count+=len(payload or b'')+len(summary)
            if byte_count>=900000 or len(batch)>=100:
                insert_batch(c,sql,placeholders,batch);batch=[];byte_count=0
        insert_batch(c,sql,placeholders,batch)
    if not expected or len(expected)!=meta['page_count'] or totals!=meta['audit']:
        raise RuntimeError('source count or financial totals mismatch')
    if matching is not None and set(expected)!=set(matching):raise RuntimeError('source receipt scope mismatch')
    return expected,dict(detail_bytes_written=changed,detail_bytes_reused=skipped,
                         unchanged=sum(prior.get(k)==v[0] for k,v in expected.items()),
                         added=sum(k not in prior for k in expected),removed=len(set(prior)-set(expected)))


def commit(db,target,version,baseline,meta,expected):
    checked_table(target)
    if target not in (NEXT,PUBLIC):raise ValueError('invalid current destination')
    data=encoded(meta)
    common='profile_version,window_start,window_end,paid_cents,social_cents,spend_cents,summary_json,meta_json,payload_sha256,created_at'
    assign=','.join(f'{k}=VALUES({k})' for k in common.split(','))
    db.begin()
    try:
        with db.cursor() as c:
            c.execute(f'SELECT active_version FROM {STATE} WHERE singleton=1 FOR UPDATE')
            if c.fetchone()!=(baseline,) or version<=baseline:raise RuntimeError('stale promotion')
            c.execute(f'INSERT INTO {target}(page_id,detail_json,{common}) '
                      f'SELECT s.page_id,s.detail_json,s.profile_version,%s,%s,s.paid_cents,s.social_cents,s.spend_cents,'
                      f's.summary_json,%s,s.payload_sha256,s.row_timestamp FROM {STAGE} s '
                      'WHERE s.profile_version=%s AND s.detail_json IS NOT NULL '
                      f'ON DUPLICATE KEY UPDATE detail_json=VALUES(detail_json),{assign}',
                      (meta['start'],meta['end'],data,version))
            c.execute(f'UPDATE {target} t JOIN {STAGE} s ON t.page_id=s.page_id AND s.profile_version=%s '
                      'SET t.profile_version=s.profile_version,t.window_start=%s,t.window_end=%s,t.paid_cents=s.paid_cents,'
                      't.social_cents=s.social_cents,t.spend_cents=s.spend_cents,t.summary_json=s.summary_json,'
                      't.meta_json=%s,t.created_at=s.row_timestamp WHERE s.detail_json IS NULL AND t.payload_sha256=s.payload_sha256',
                      (version,meta['start'],meta['end'],data))
            c.execute(f'DELETE t FROM {target} t LEFT JOIN {STAGE} s ON s.page_id=t.page_id '
                      'AND s.profile_version=%s WHERE s.page_id IS NULL',(version,))
            c.execute(f'UPDATE {STATE} SET active_version=%s,window_start=%s,window_end=%s,page_count=%s,'
                      'scope_sha256=%s,manifest_sha256=%s,paid_cents=%s,social_cents=%s,spend_cents=%s,meta_json=%s '
                      'WHERE singleton=1 AND active_version=%s',(version,meta['start'],meta['end'],len(expected),
                      scope_hash(expected),expected_hash(expected),*(meta['audit'][k] for k in MONEY),data,baseline))
            if c.rowcount!=1:raise RuntimeError('head compare-and-swap failed')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def finalize(db,reader,target,pending,root,ack=None):
    meta=pending['meta'];version=meta['version'];expected=pending['expected']
    wait_visible(reader,f"SELECT HEX(JSON_OBJECT('v',active_version)) FROM {STATE} WHERE singleton=1",lambda r:r[0]['v']==version)
    visible=stamp()
    seen_ms=round((time.monotonic()-ack[1])*1000,2) if ack else None
    head=current_state(reader)
    if head['manifest']!=expected_hash(expected) or head['scope']!=scope_hash(expected) or head['pages']!=len(expected) or json.loads(head['meta'])!=meta:
        raise RuntimeError('published head and input differ')
    verify(reader,target,version,expected)
    counts=query(reader,f"SELECT HEX(JSON_OBJECT('n',COUNT(*),'d',COUNT(DISTINCT page_id),'v',COUNT(DISTINCT profile_version),"
                 f"'a',SUM(paid_cents),'b',SUM(social_cents),'c',SUM(spend_cents))) FROM {target}")[0]
    if [counts[k] for k in ('n','d','v','a','b','c')]!=[len(expected),len(expected),1,*[meta['audit'][k] for k in MONEY]]:
        raise RuntimeError('published row uniqueness or money mismatch')
    receipt=dict(version=version,table='ads_ai.'+PUBLIC,verified_rows=len(expected),
                 payload_hashes={k:v[0] for k,v in expected.items()},**meta['audit'])
    file=root/'receipts'/(version+'.json')
    record=dict(version=version,base=pending['base'],target=target,
           checked=stamp(),commit_ack=ack[0] if ack else None,replica_head_visible=visible,
           replica_observed_after_ack_ms=seen_ms,facts=counts,scope=head['scope'],manifest=head['manifest'],**pending['stats'])
    if file.exists():
        previous=json.loads(file.read_text())
        if previous['manifest']!=head['manifest']:raise RuntimeError('conflicting published receipt')
        if previous.get('commit_ack'):
            for name in ('commit_ack','replica_head_visible','replica_observed_after_ack_ms'):record[name]=previous[name]
    atomic(file,record)
    # The final proof precedes removal of transient data, including after a commit-disconnect.
    with db.cursor() as c:c.execute(f'DELETE FROM {STAGE} WHERE profile_version=%s',(version,))
    return receipt


def promote(reader,env,target,meta,items,*,matching=None,root=ROOT,connector=writer,durable_source=False):
    version=checked(meta['version'])
    with owner(root), closing(connector(env)) as db:
        head=current_state(reader);baseline=head['version']
        if version<baseline:raise RuntimeError('newer published version exists')
        file=root/'pending.json'
        if file.exists():
            pending=json.loads(file.read_text())
            pv=pending['meta']['version']
            if pv<=baseline and pv==baseline:
                recovered=finalize(db,reader,target,pending,root)
                # Keep the pointer handoff available even if the service died after this write.
                atomic(root/'last-current-proof.json',recovered)
                file.unlink()
                clear_build(root,pv)
                if pv==version:return recovered
            elif pv!=version:
                raise RuntimeError('unresolved candidate '+pv)
        if version==baseline:
            proof=json.loads((root/'last-current-proof.json').read_text())
            if proof['version']!=version:raise RuntimeError('verified proof missing')
            if matching is not None and proof['payload_hashes']!=matching:raise RuntimeError('receipt conflicts')
            if matching is None:
                replay={pid:[digest,sha(summary),*amounts] for pid,summary,raw,digest,amounts,ts in items
                        if sha(raw)==digest}
                if expected_hash(replay)!=head['manifest'] or json.loads(head['meta'])!=meta:
                    raise RuntimeError('replayed input differs from published payload')
            clear_build(root,version)
            return proof
        marker=root/'building.json'
        existing=json.loads(marker.read_text()) if marker.exists() else None
        if existing and (existing['version']!=version or existing['base']!=baseline or existing['meta']!=meta):
            raise RuntimeError('resume the original staged version first')
        prior={r['p']:r['h'] for r in query(reader,
            f"SELECT HEX(JSON_OBJECT('p',page_id,'h',payload_sha256)) FROM {target}")}
        if durable_source:
            from . import delta
            source=root/(version+'.delta.gz')
            if not existing:delta.freeze(source,items,prior)
            items=delta.read(source)
        atomic(marker,dict(version=version,base=baseline,target=target,meta=meta,durable=durable_source))
        expected,stats=stage_rows(db,version,items,prior,meta,matching)
        wait_visible(reader,f"SELECT HEX(JSON_OBJECT('n',COUNT(*))) FROM {STAGE} WHERE profile_version="+literal(version),
                     lambda r:r[0]['n']==len(expected))
        verify(reader,STAGE,version,expected,base_hashes=prior)
        pending=dict(base=baseline,meta=meta,expected=expected,stats=stats,target=target)
        atomic(file,pending)
        commit(db,target,version,baseline,meta,expected)
        proof=finalize(db,reader,target,pending,root,ack=(stamp(),time.monotonic()))
        atomic(root/'last-current-proof.json',proof)
        file.unlink()
        clear_build(root,version)
        return proof


def model_items(data,projections):
    ts=dt.datetime.fromisoformat(data['meta']['generated_at']).astimezone(dt.timezone.utc).replace(tzinfo=None,microsecond=0)
    for pid,page in data['pages'].items():
        summary,_=projections(page)
        raw=encoded(page).encode()
        yield pid,encoded(summary),raw,sha(raw),[page[k] for k in MONEY],ts
