"""Reconstruct only priority IDs from a completed, matching source collection during handover."""
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


def main(writer, verify_only=False):
    os.chdir(writer)
    sys.path.insert(0,str(writer))
    from features.fb_page_portrait.collect import paid, social_input
    from features.fb_page_portrait.recover_committed import financial_input
    from features.fb_page_current.common import MONEY, PORTRAITS, PUBLIC, checked, encoded, literal
    from features.fb_page_current.read import Gate
    root=PORTRAITS
    head=json.loads((root/'current.json').read_text())
    status=json.loads((root/'status.json').read_text())
    v=checked(head['version'])
    if status.get('status')!='ok' or status['version']!=v:
        raise RuntimeError('await complete version')
    proof=json.loads((root/(v+'.receipt.json')).read_text())
    ids=sorted(proof['payload_hashes'])
    matches=[]
    for f in (root/'collect-cache').glob('*/manifest.json'):
        state=json.loads(f.read_text())
        if state.get('complete') and state['started_at']==status['collection_started_at']:
            matches.append(f.parent)
    if len(matches)!=1:
        raise RuntimeError('expected one complete original collection')

    class Frozen:
        folder=matches[0]
        def query(self,unused,statements,timeout=45):
            name=hashlib.sha256(encoded(statements).encode()).hexdigest()+'.json'
            try:
                return json.loads((self.folder/name).read_text())['rows']
            except FileNotFoundError:
                # Original collector recursively split timed-out partitions.
                raise RuntimeError('3024 missing frozen partition; resolve children') from None
    cache=Frozen()
    if not (cache.folder/'social-input.json').is_file():
        raise RuntimeError('missing frozen social source')
    with open(os.devnull,'w') as silent, redirect_stdout(silent):
        scope=dict(start=status['start'],end=status['end'],page_ids=ids)
        frozen=social_input(cache,ids,scope['start'],scope['end'])
        if frozen['version']!=status['social_version']:
            raise RuntimeError('social version mismatch')
        scope['social']=frozen['rows']
        scope['paid']=paid(None,ids,scope['start'],scope['end'],cache)
    expected,totals=financial_input(scope)
    rows=Gate().query(["SELECT HEX(JSON_OBJECT('p',page_id,'paid_cents',paid_cents,'social_cents',social_cents,"
                       "'spend_cents',spend_cents)) FROM "+PUBLIC+' WHERE profile_version='+literal(v)])
    if {r['p']:{k:r[k] for k in MONEY} for r in rows}!=expected or any(totals[k]!=proof[k] for k in MONEY):
        raise RuntimeError('per-Page frozen source and receipt differ')
    priority=sorted({str(r['content_id']) for k in ('paid','social') for r in scope[k]
                     if r.get('content_id') and int(r.get('app_id',1479))==1479})
    output=root/'audience-priority.json'
    report=dict(version=v,priority_ids=len(priority),verified_pages=len(expected),
                paid_rows=len(scope['paid']),social_rows=len(scope['social']))
    if verify_only:
        if json.loads(output.read_text()) != dict(version=v,ids=priority):
            raise ValueError('published priority list differs from original facts')
        print(json.dumps(dict(report,verified=True)))
        return
    with tempfile.NamedTemporaryFile(dir=root,delete=False) as f:
        temp=Path(f.name)
        f.write(encoded(dict(version=v,ids=priority)).encode())
        f.flush()
        os.fsync(f.fileno())
    try:
        if json.loads((root/'current.json').read_text())!=head:
            raise RuntimeError('newer pointer; rerun for that collection')
        # Ordinary refresh replaces its output atomically. Never overwrite a newer manifest.
        if not output.exists():
            os.link(temp,output)
        else:
            import fcntl
            with (root/'refresh.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                if json.loads((root/'current.json').read_text())!=head:
                    raise RuntimeError('newer pointer')
                if json.loads(output.read_text())['version']<=v:
                    os.replace(temp,output)
    finally:
        temp.unlink(missing_ok=True)
    print(json.dumps(report))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('writer',type=Path)
    p.add_argument('--verify-only',action='store_true')
    args=p.parse_args()
    main(args.writer.resolve(),args.verify_only)
