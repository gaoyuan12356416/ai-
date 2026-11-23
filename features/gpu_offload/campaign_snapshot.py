"""Index frozen Campaign Copy wire data and read its existing cohort contract."""
from contextlib import closing
import datetime as dt
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
from urllib.parse import parse_qs

from .campaign import service as original


def index_snapshot(seed,root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    raw=Path(seed).read_bytes();version=hashlib.sha256(raw).hexdigest()
    wire=json.loads(raw);del raw
    if wire.get('v')!=2 or not wire.get('m',{}).get('read_only_verified'):
        raise ValueError('verified_campaign_seed_required')
    fields=wire['cf'];keep=[i for i,f in enumerate(fields) if f not in original.PRIVATE_CAMPAIGN_FIELDS]
    wire['cf']=[fields[i] for i in keep];wire['c']=[[r[i] for i in keep] for r in wire['c']]
    wire['m'].pop('mapping_metadata_next_bucket',None)
    target=root/(version+'.sqlite3');temporary=root/(version+'.incoming')
    if temporary.exists():temporary.unlink()
    counts={}
    try:
        with closing(sqlite3.connect(str(temporary))) as db:
            db.execute('PRAGMA journal_mode=OFF');db.execute('PRAGMA synchronous=OFF');db.execute('PRAGMA cache_size=-16384')
            db.execute('CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)')
            for k,v in wire.items():
                if k not in ('c','d','x','xd'):db.execute('INSERT INTO meta VALUES(?,?)',(k,json.dumps(v,ensure_ascii=False,separators=(',',':'))))
            for table,fields_key in (('c','cf'),('d','df'),('x','xf'),('xd','xdf')):
                fields=wire[fields_key];rows=wire.pop(table)
                db.execute('CREATE TABLE '+table+'(seq INTEGER PRIMARY KEY,entity TEXT,copy_date TEXT,level INTEGER,payload TEXT NOT NULL)')
                positions={name:i for i,name in enumerate(fields)}
                def encoded():
                    for seq,row in enumerate(rows):
                        if table in ('c','d'):entity=str(int(row[positions['platform']] or 0))+':'+str(row[positions['campaign_id']])
                        else:entity=str(row[positions['entity_key']])
                        day=str(row[positions['copy_date']] or '') if 'copy_date' in positions else ''
                        level=int(row[positions['level']]) if 'level' in positions else 0
                        yield seq,entity,day,level,json.dumps(row,ensure_ascii=False,separators=(',',':'),default=original.json_default)
                db.executemany('INSERT INTO '+table+' VALUES(?,?,?,?,?)',encoded())
                counts[table]=len(rows);del rows
                db.execute('CREATE INDEX '+table+'_entity ON '+table+'(entity)')
                if table in ('c','x'):db.execute('CREATE INDEX '+table+'_cohort ON '+table+'(level,copy_date)')
            db.commit()
            if db.execute('PRAGMA quick_check').fetchone()!=('ok',):raise ValueError('campaign_index_invalid')
        with temporary.open('r+b') as f:os.fsync(f.fileno())
        temporary.chmod(0o400);temporary.replace(target)
        # A compressed immutable index is also the CPU fallback's source. The
        # fallback never repeats business SQL or rebuilds the full JSON index.
        packed=root/(version+'.sqlite3.gz');pending=root/(version+'.gzip.incoming')
        with target.open('rb') as source,pending.open('wb') as sink:
            with gzip.GzipFile(fileobj=sink,mode='wb',compresslevel=1,mtime=0) as compressed:
                shutil.copyfileobj(source,compressed,1024*1024)
            sink.flush();os.fsync(sink.fileno())
        packed_digest=hashlib.sha256()
        with pending.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):packed_digest.update(block)
        pending.chmod(0o400);pending.replace(packed)
        manifest=dict(version=version,database=target.name,generated_at=wire['m']['generated_at'],rows=counts,
                      archive=packed.name,archive_bytes=packed.stat().st_size,archive_sha256=packed_digest.hexdigest(),database_bytes=target.stat().st_size)
        next_file=root/'current.next';next_file.write_text(json.dumps(manifest));next_file.replace(root/'current.json')
        # Retain the current and two previous immutable indexes.
        versions=sorted(root.glob('*.sqlite3'),key=lambda p:p.stat().st_mtime,reverse=True)
        for p in versions[3:]:
            if p!=target:
                p.unlink();p.with_suffix('.sqlite3.gz').unlink(missing_ok=True)
        return manifest
    finally:
        if temporary.exists():temporary.unlink()


def current(root):
    root=Path(root);manifest=json.loads((root/'current.json').read_text())
    path=(root/manifest['database']).resolve()
    if path.parent!=root.resolve() or path.name!=manifest['version']+'.sqlite3':raise ValueError('invalid_campaign_snapshot')
    return manifest,path


def write_view(root,query,output,snapshot=None):
    manifest,path=snapshot or current(root)
    params=parse_qs(query,keep_blank_values=True,max_num_fields=32)
    cohort=params.get('view')==['cohort']
    level=int(params.get('level',['0'])[0])
    if level not in (0,1,2):raise ValueError('invalid_level')
    today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    start=params.get('copy_start',[(today-dt.timedelta(days=6)).isoformat()])[0]
    end=params.get('copy_end',[today.isoformat()])[0]
    for date in (start,end):
        if date:dt.date.fromisoformat(date)
    if start and end and start>end:raise ValueError('invalid_date_range')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-16384')
        result={k:json.loads(v) for k,v in db.execute('SELECT k,v FROM meta')}
        queries={table:('SELECT payload FROM '+table+' ORDER BY seq',()) for table in ('c','d','x','xd')}
        if cohort:
            result['m'].update(view_level=level,view_start=start,view_end=end,view_scope='按复制日期加载，效果保留该范围全部统计日')
            table,facts=('c','d') if level==0 else ('x','xd')
            where='level=? AND (?=\'\' OR copy_date>=?) AND (?=\'\' OR copy_date<=?)'
            args=(level,start,start,end,end)
            queries={key:None for key in queries}
            queries[table]=('SELECT payload FROM '+table+' WHERE '+where+' ORDER BY seq',args)
            queries[facts]=('SELECT payload FROM '+facts+' WHERE entity IN (SELECT entity FROM '+table+' WHERE '+where+') ORDER BY seq',args)
        header=json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()
        output.write(header[:-1])
        for table,query_args in queries.items():
            output.write((',"'+table+'":[').encode());first=True
            if query_args:
                for (payload,) in db.execute(*query_args):
                    if not first:output.write(b',')
                    output.write(payload.encode());first=False
            output.write(b']')
        output.write(b'}')
    return manifest


def render(root,query):
    output=io.BytesIO();manifest=write_view(root,query,output)
    return manifest,output.getvalue()


def compressed_view(root,query,destination,snapshot=None):
    destination=Path(destination);temporary=destination.with_suffix('.incoming')
    try:
        with temporary.open('wb') as raw:
            with gzip.GzipFile(fileobj=raw,mode='wb',compresslevel=1,mtime=0) as output:
                manifest=write_view(root,query,output,snapshot=snapshot)
            raw.flush();os.fsync(raw.fileno())
        temporary.chmod(0o600);temporary.replace(destination)
        return manifest
    finally:
        if temporary.exists():temporary.unlink()
