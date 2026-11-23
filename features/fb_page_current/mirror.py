"""During migration only, compare the same frozen model outside the hourly service's slot."""
from contextlib import closing,nullcontext
import datetime as dt
import fcntl
import gzip
import json
import os
from pathlib import Path
import subprocess

from .common import (LEGACY,MONEY,NEXT,PORTRAITS,PUBLIC,ROOT,atomic,checked,encoded,insert_batch,literal,pack,query,
                     read_config,sha,stamp,writer)
from .publish import promote,verify,wait_visible

QUEUE=ROOT/'mirror-queue'


def seal(data,proof,mode,resumed):
    version=checked(proof['version']);directory=QUEUE/version
    mount=subprocess.check_output(['findmnt','-no','UUID','-T',str(ROOT)],text=True).strip()
    if mount!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':raise RuntimeError('missing portrait data disk')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    ready=directory/'ready.json'
    if not ready.exists():
        temp=directory/'pages.gz.tmp'
        with temp.open('wb') as handle:
            with gzip.GzipFile(fileobj=handle,mode='wb',compresslevel=1,mtime=0) as out:
                for pid,page in data['pages'].items():
                    raw=encoded(page).encode()
                    if sha(raw)!=proof['payload_hashes'][pid]:raise RuntimeError('frozen mirror payload mismatch')
                    out.write(raw+b'\n')
            handle.flush();os.fsync(handle.fileno())
        temp.replace(directory/'pages.gz')
        atomic(ready,dict(meta=data['meta'],proof=proof,mode=mode,resumed=resumed,
                          invocation=os.environ.get('INVOCATION_ID',''),sealed=stamp()))
    unit='fb-page-current-mirror-'+version.lower()
    subprocess.run(['systemd-run','--quiet','--collect','--unit='+unit,
                    '--property=Nice=10','--property=MemoryMax=640M','--property=RequiresMountsFor=/mnt/data-disk',
                    '--property=WorkingDirectory='+str(Path.cwd()),'/usr/bin/python3','-B','-m','features.fb_page_current.mirror'],check=True)


def items(directory,meta,row_times,proof=None):
    from . import selection
    from features.fb_page_portrait.storage import projections
    generated=dt.datetime.fromisoformat(meta['generated_at']).astimezone(dt.timezone.utc).replace(tzinfo=None,microsecond=0)
    fresh=proof is not None and not selection.available(meta['version'])
    cache=selection.alongside(meta,proof,selection.FILE) if fresh else nullcontext(None)
    with gzip.open(directory/'pages.gz','rb') as data,cache as observe:
        for line in data:
            raw=line[:-1] if line.endswith(b'\n') else line
            page=json.loads(raw);summary,_=projections(page)
            ts=generated if row_times is None else dt.datetime.fromtimestamp(row_times[page['page_id']],dt.timezone.utc).replace(tzinfo=None)
            digest=sha(raw)
            if observe:observe(page['page_id'],page,digest)
            yield page['page_id'],encoded(summary),raw,digest,[page[k] for k in MONEY],ts


def legacy(reader,env,meta,proof,source,*,connector=writer):
    v=meta['version'];where=' WHERE profile_version='+literal(v)
    countsql=f"SELECT HEX(JSON_OBJECT('n',COUNT(*))) FROM {LEGACY}"+where
    count=query(reader,countsql)[0]['n'];expected={}
    if count not in (0,proof['verified_rows']):raise RuntimeError('partial rollback table version')
    sql=f'INSERT INTO {LEGACY}(profile_version,page_id,window_start,window_end,paid_cents,social_cents,spend_cents,summary_json,detail_json,meta_json,payload_sha256,created_at) VALUES '
    shape='(%s,%s,%s,%s,%s,%s,%s,%s,UNCOMPRESS(%s),%s,%s,%s)'
    small_meta=encoded(meta)
    with closing(connector(env)) as db,db.cursor() as c:
        if not count:db.begin()
        try:
            batch=[];size=0
            for pid,summary,raw,h,amounts,ts in source:
                if pid in expected or proof['payload_hashes'].get(pid)!=h:raise RuntimeError('mirror source differs')
                expected[pid]=[h,sha(summary),*amounts]
                if count:continue
                body=pack(raw);batch.append((v,pid,meta['start'],meta['end'],*amounts,summary,body,small_meta,h,ts))
                size+=len(body)+len(summary)
                if size>=900000 or len(batch)>=100:insert_batch(c,sql,shape,batch);batch=[];size=0
            if set(expected)!=set(proof['payload_hashes']) or any(sum(x[i+2] for x in expected.values())!=proof[k] for i,k in enumerate(MONEY)):
                raise RuntimeError('mirror input scope or financial totals differ')
            if not count:insert_batch(c,sql,shape,batch);db.commit()
        except BaseException:
            if not count:db.rollback()
            raise
    wait_visible(reader,countsql,lambda r:r[0]['n']==len(expected))
    verify(reader,LEGACY,v,expected)
    return proof


def run():
    from features.fb_page_portrait.collect import environment,Reader
    from .refresh import catch_up
    env=environment('/root/drama_material_service/.env');reader=Reader(env)
    with (ROOT/'mirror.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        for ready in sorted(QUEUE.glob('*/ready.json')):
            d=ready.parent;job=json.loads(ready.read_text());proof=job['proof'];v=checked(proof['version'])
            ack=ROOT/'comparisons'/(v+'.json')
            if not ack.exists():
                if read_config()!=job['mode']:raise RuntimeError('pending mirror phase requires review')
                try:
                    row_times=None
                    if job['mode']=='shadow':
                        stamps=query(reader,"SELECT HEX(JSON_OBJECT('p',page_id,'s',UNIX_TIMESTAMP(created_at))) "
                                     f"FROM {PUBLIC} WHERE profile_version="+literal(v))
                        row_times={r['p']:r['s'] for r in stamps}
                        if set(row_times)!=set(proof['payload_hashes']):raise RuntimeError('mirror source times incomplete')
                        catch_up(reader,env,NEXT)
                        got=promote(reader,env,NEXT,job['meta'],items(d,job['meta'],row_times,proof),matching=proof['payload_hashes'])
                    elif job['mode']=='live-mirror':
                        got=legacy(reader,env,job['meta'],proof,items(d,job['meta'],None))
                    else:raise RuntimeError('unknown mirror job type')
                    if got['payload_hashes']!=proof['payload_hashes'] or any(got[k]!=proof[k] for k in MONEY):
                        raise RuntimeError('mirror proof mismatch')
                    atomic(ack,dict(version=v,mode=job['mode'],checked=stamp(),pages=proof['verified_rows'],
                                 totals={k:proof[k] for k in MONEY},outcome='matched',
                                 invocation=job['invocation'],resumed=job['resumed']))
                except Exception as exc:
                    atomic(ROOT/'comparisons'/(v+'.error.json'),dict(at=stamp(),version=v,error=type(exc).__name__,message=str(exc)[:230]))
                    raise
            input_file=PORTRAITS/(v+'.input.json')
            if input_file.is_file() and read_config()==job['mode']:
                official=json.loads((PORTRAITS/(v+'.receipt.json')).read_text())
                if official['version']==v and official['payload_hashes']==proof['payload_hashes']:
                    input_file.unlink(missing_ok=True)
            ready.unlink();(d/'pages.gz').unlink();d.rmdir()
            print(encoded(dict(event='natural_portrait_comparison_complete',version=v,mode=job['mode'])),flush=True)


def wait_for_invocation(invocation):
    import re
    import time
    if not re.fullmatch('[0-9a-f]{32}',invocation):raise ValueError('invalid systemd invocation')
    while True:
        for receipt in QUEUE.glob('*/ready.json'):
            try:record=json.loads(receipt.read_text())
            except FileNotFoundError:continue
            if record['invocation']==invocation:return
        for receipt in (ROOT/'comparisons').glob('*.json'):
            if json.loads(receipt.read_text()).get('invocation')==invocation:return
        time.sleep(2)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--await-invocation')
    args=parser.parse_args()
    if args.await_invocation:wait_for_invocation(args.await_invocation)
    run()
