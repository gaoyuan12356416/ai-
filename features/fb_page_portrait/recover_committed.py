"""Project the EXACT already committed payloads; never reclassify or rewrite SQL."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re

from .build import cents
from .storage import TABLE,ROOT,VERSION_RE,publish
from .runtime import encoded,literal,atomic_json
from .collect import environment,Reader


def financial_input(raw):
    fields=('paid_cents','social_cents','spend_cents')
    pages={str(pid):dict.fromkeys(fields,0) for pid in raw['page_ids']}
    for kind in ('paid','social'):
        for row in raw[kind]:
            page=pages[str(row['page_id'])]
            page[kind+'_cents']+=cents(row['revenue']) if kind=='paid' else int(row['revenue_cents'])
            page['spend_cents']+=cents(row.get('spend')) if kind=='paid' else 0
    return pages,{field:sum(row[field] for row in pages.values()) for field in fields}


def stored_items(reader,version,hashes,expected):
    base=f' FROM {TABLE} WHERE profile_version={literal(version)}'
    ids=sorted(hashes);totals=dict.fromkeys(('paid_cents','social_cents','spend_cents'),0)
    def statement(part):
        return "SELECT HEX(JSON_OBJECT('page_id',page_id,'detail',detail_json,'hash',SHA2(detail_json,256),'stored_hash',payload_sha256,'paid_cents',paid_cents,'social_cents',social_cents,'spend_cents',spend_cents))"+base+' AND page_id IN ('+','.join(map(literal,part))+')'
    def read(part):
        try:return reader.query([statement(part)],timeout=60)
        except Exception as exc:
            if '3024' not in str(exc) or len(part)<=1:raise
            middle=len(part)//2
            return read(part[:middle])+read(part[middle:])
    for offset in range(0,len(ids),100):
        selected=ids[offset:offset+100];parts=[selected[i:i+10] for i in range(0,len(selected),10)]
        try:rows=reader.query([statement(part) for part in parts],timeout=180)
        except Exception:
            rows=[]
            for part in parts:rows.extend(read(part))
        if {row['page_id'] for row in rows}!=set(selected) or len(rows)!=len(selected):raise RuntimeError('committed_scope_incomplete')
        for row in rows:
            pid=row['page_id'];digest=hashlib.sha256(row['detail'].encode()).hexdigest()
            if digest!=hashes[pid] or digest!=row['hash'] or digest!=row['stored_hash']:raise RuntimeError('committed_payload_hash_mismatch')
            page=json.loads(row['detail'])
            if page['page_id']!=pid:raise RuntimeError('committed_page_identity_mismatch')
            for field in totals:
                if row[field]!=expected[pid][field] or page[field]!=row[field]:raise RuntimeError('committed_income_mismatch')
                totals[field]+=row[field]
            yield pid,page
        print(encoded(dict(event='committed_projection',pages=offset+len(selected),total=len(ids))),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--version',required=True);parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--root',default=ROOT,type=Path);parser.add_argument('--env',default='/root/drama_material_service/.env');args=parser.parse_args()
    import fcntl
    root=args.root.resolve();source=args.input.resolve();version=args.version
    if not re.fullmatch(VERSION_RE,version) or source.parent!=root or source.name!=version+'.input.json':raise ValueError('invalid_committed_recovery_identity')
    with (root/'refresh.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        current=json.loads((root/'current.json').read_text())['version']
        if current>=version:raise RuntimeError('snapshot_already_published_or_superseded')
        raw=json.loads(source.read_text());expected,audit=financial_input(raw);collection=raw.get('collection');del raw
        reader=Reader(environment(args.env));where=f' FROM {TABLE} WHERE profile_version={literal(version)}'
        rows=reader.query(["SELECT HEX(JSON_OBJECT('meta',meta_json))"+where+' LIMIT 1'])
        if len(rows)!=1:raise RuntimeError('committed_version_missing')
        meta=json.loads(rows[0]['meta'])
        hashes={row['page_id']:row['hash'] for row in reader.query(["SELECT HEX(JSON_OBJECT('page_id',page_id,'hash',payload_sha256))"+where])}
        if meta['version']!=version or meta['audit']!=audit or set(hashes)!=set(expected) or meta['page_count']!=len(expected):raise RuntimeError('committed_version_scope_or_income_mismatch')
        partial=root/(version+'.building')
        if partial.exists():partial.rename(root/(version+'.building-interrupted-'+datetime.now(timezone.utc).strftime('%H%M%S%f')))
        proof=dict(version=version,table=TABLE,verified_rows=len(hashes),payload_hashes=hashes,**audit)
        publish(dict(meta=meta),proof,root,page_items=stored_items(reader,version,hashes,expected))
        atomic_json(root/(version+'.receipt.json'),dict(proof,recovered_from_committed_sql=True))
        if collection:
            try:
                from .partition_cache import complete
                complete(dict(collection=collection))
            except Exception as exc:
                print(encoded(dict(event='partition_cache_maintenance_failed',error=type(exc).__name__)),flush=True)
        atomic_json(root/'status.json',dict(status='ok',**meta))
        import resource
        print(encoded(dict(event='committed_snapshot_published',version=version,pages=len(hashes),max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,external_sql_writes=0)),flush=True)


if __name__=='__main__':main()
