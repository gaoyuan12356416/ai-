"""Stream frozen versions into compressed chunks, then restore every row on an isolated MySQL."""
import gzip
import hashlib
import math
import os
import sqlite3
import time
from operations import *
from features.fb_page_current.common import insert_batch,pack,read_config
from features.fb_page_current.history import HISTORY,blob_path,lookup,rows as archived_rows
from cutover import layout,AFTER

SOCKET=ROOT/'sandbox-socket/mysql.sock'
FROZEN=ROOT/'history-frozen.json'
PAGE_SQL=('page_id,window_start,window_end,paid_cents,social_cents,spend_cents,summary_json,meta_json,'
          'payload_sha256,created_at,SHA2(detail_json,256)')


def private_mysql():
    db=pymysql.connect(unix_socket=str(SOCKET),user='root',autocommit=True,charset='utf8mb4',
                       binary_prefix=True,read_timeout=100,write_timeout=30)
    with db.cursor() as c:
        c.execute('SELECT @@socket,@@version')
        sock,ver=c.fetchone()
        if sock!='/run/portrait-qa/mysql.sock' or not ver.startswith('5.7.'):raise RuntimeError('private restore host required')
        c.execute('CREATE DATABASE IF NOT EXISTS portrait_archive_verify')
        c.execute('USE portrait_archive_verify')
        c.execute("SET SESSION time_zone='+00:00'")
    return db


def normalize(v,r):
    pid,start,end,a,b,s,summary,meta,h,ts,calc=r
    if calc!=h:raise RuntimeError('source detail does not match stored checksum')
    return [v,pid,str(start),str(end),a,b,s,summary,meta,h,str(ts)]


def file_hash(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for buf in iter(lambda:f.read(1024*1024),b''):h.update(buf)
    return h.hexdigest()


def package_tools(base):
    revision=Path(__file__).resolve().parents[2].name
    if len(revision)!=40 or any(c not in '0123456789abcdef' for c in revision):raise RuntimeError('unversioned recovery tools')
    target=base/'restore-tools.tar.gz';temp=base/'restore-tools.partial'
    subprocess.run(['git','--git-dir='+str(ROOT/'source.git'),'archive','--format=tar.gz','--prefix=fb-page-current-restore/',
                    '-o',str(temp),revision,'features/__init__.py','features/fb_page_current','features/fb_page_portrait',
                    'features/fb_page_assignment','ops/fb-page-current-20261123'],check=True)
    temp.replace(target)
    return revision,file_hash(target)


def write_blob(path,packet):
    path.parent.mkdir(exist_ok=True,parents=True)
    tmp=path.with_suffix('.partial')
    tmp.write_bytes(packet)
    tmp.replace(path)


def missing_bodies(db,source,v,records,base):
    missing={r[1]:r[9] for r in records if not blob_path(r[9],base).is_file()}
    stored=0
    with db.cursor() as c:
        for group in chunks(list(missing),20):
            c.execute(f'SELECT page_id,payload_sha256,COMPRESS(detail_json) FROM {source} WHERE profile_version=%s '
                      'AND page_id IN ('+','.join(['%s']*len(group))+')',(v,*group))
            got=c.fetchall()
            if len(got)!=len(group):raise RuntimeError('frozen payload count mismatch')
            for pid,h,packed in got:
                if missing[pid]!=h or sha(unpack(packed))!=h:raise RuntimeError('historical blob mismatch')
                dest=blob_path(h,base)
                if not dest.exists():write_blob(dest,packed);stored+=len(packed)
    return stored


def export_version(db,source,v,base,*,proof=None,rate=4*1024**2):
    checked_table(source);checked(v)
    dest=base/'versions'/v;dest.mkdir(parents=True,exist_ok=True)
    marker=dest/'snapshot.json'
    if marker.exists():return json.loads(marker.read_text())
    parts=[];amount=[0,0,0];published_hashes={};meta=None;n=0;last='';limit=128
    with db.cursor() as c:
        while True:
            started=time.monotonic()
            try:
                c.execute(f'SELECT {PAGE_SQL} FROM {source} WHERE profile_version=%s AND page_id>%s '
                          f'ORDER BY page_id LIMIT {limit}',(v,last))
                result=c.fetchall()
            except pymysql.MySQLError as e:
                if e.args[0]==3024 and limit>4:limit=max(4,limit//2);continue
                raise
            if not result:break
            batch=[normalize(v,row) for row in result]
            if batch[0][1]<=last:raise RuntimeError('frozen cursor did not advance')
            saved=missing_bodies(db,source,v,batch,base)
            filename='part-%05d.jsonl.gz'%len(parts);out=dest/filename;temp=dest/(filename+'.partial')
            with temp.open('wb') as fp:
                with gzip.GzipFile(fileobj=fp,mode='wb',compresslevel=3,mtime=0) as gz:
                    for row in batch:
                        if row[1] in published_hashes:raise RuntimeError('duplicate archival page')
                        published_hashes[row[1]]=row[9]
                        for i in range(3):amount[i]+=row[i+4]
                        if meta is None:meta=row[8]
                        gz.write(encoded(row).encode()+b'\n')
                fp.flush();os.fsync(fp.fileno())
            temp.replace(out)
            parts.append(dict(name=filename,first=batch[0][1],last=batch[-1][1],rows=len(batch),sha256=file_hash(out),bytes=out.stat().st_size))
            n+=len(batch);last=batch[-1][1]
            atomic(base/'progress.json',dict(phase='export',version=v,rows=n,at=stamp()))
            if rate:time.sleep(max(.01,(saved+sum(len(r[7])+len(r[8]) for r in batch))/rate-(time.monotonic()-started)))
        count=one(c,f'SELECT COUNT(*) FROM {source} WHERE profile_version=%s',(v,))[0]
        if count!=n or not n:raise RuntimeError('frozen row count mismatch')
    if proof and (proof['payload_hashes']!=published_hashes or amount!=[proof[k] for k in MONEY]):
        raise RuntimeError('stored historical publication proof differs')
    receipt_hash=None
    if proof:
        original=dest/'publication-receipt.json.gz'
        temp=dest/'publication-receipt.partial'
        temp.write_bytes(gzip.compress(encoded(proof).encode(),compresslevel=4,mtime=0))
        temp.replace(original)
        receipt_hash=file_hash(original)
    manifest=dict(version=v,rows=n,amounts=amount,meta=meta,parts=parts,
                  source_receipt_checked=bool(proof),source_receipt_sha256=receipt_hash,at=stamp())
    atomic(marker,manifest)
    return manifest


def identity(row):
    return [*row[:7],sha(row[7]),sha(row[8]),row[9],row[10]]


def insert_archived(c,table,records,base):
    sql=f'INSERT INTO {table}(profile_version,page_id,window_start,window_end,paid_cents,social_cents,spend_cents,summary_json,meta_json,payload_sha256,created_at,detail_json) VALUES '
    shape='(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UNCOMPRESS(%s))'
    batch=[];size=0
    for r in records:
        packed=blob_path(r[9],base).read_bytes()
        if sha(unpack(packed))!=r[9]:raise RuntimeError('archived detail verification failed')
        batch.append((*r,packed));size+=len(packed)+len(r[7])+len(r[8])
        if size>=1400000 or len(batch)>=80:insert_batch(c,sql,shape,batch);batch=[];size=0
    insert_batch(c,sql,shape,batch)


def restore_version(db,manifest,base,ddl):
    v=manifest['version'];dest=base/'versions'/v;done=dest/'restored.json'
    if manifest.get('source_receipt_sha256'):
        if file_hash(dest/'publication-receipt.json.gz')!=manifest['source_receipt_sha256']:
            raise RuntimeError('archived publication receipt checksum mismatch')
    if done.exists():
        prior=json.loads(done.read_text())
        if prior['manifest_sha256']!=file_hash(dest/'snapshot.json'):raise RuntimeError('restored manifest changed')
        return prior
    with db.cursor() as c:
        if one(c,'SELECT DATABASE()')[0]!='portrait_archive_verify':raise RuntimeError('private restore schema required')
        c.execute('DROP TABLE IF EXISTS '+PUBLIC)
        c.execute(ddl)
        total=0;sums=[0,0,0]
        for part in manifest['parts']:
            file=dest/part['name']
            if file_hash(file)!=part['sha256']:raise RuntimeError('archive chunk checksum failed')
            records=list(archived_rows(file))
            if len(records)!=part['rows']:raise RuntimeError('archive part length mismatch')
            insert_archived(c,PUBLIC,records,base)
            c.execute(f'SELECT profile_version,page_id,window_start,window_end,paid_cents,social_cents,spend_cents,'
                      f'SHA2(summary_json,256),SHA2(meta_json,256),SHA2(detail_json,256),created_at,payload_sha256 FROM {PUBLIC} '
                      'WHERE profile_version=%s AND page_id BETWEEN %s AND %s ORDER BY page_id',(v,part['first'],part['last']))
            observed=[]
            for rv,p,a,b,x,y,z,ss,ms,ds,t,stored in c.fetchall():
                if ds!=stored:raise RuntimeError('restored detail and stored checksum differ')
                observed.append([rv,p,str(a),str(b),x,y,z,ss,ms,ds,str(t)])
            if observed!=[identity(row) for row in records]:raise RuntimeError('restored Page columns differ')
            total+=len(records)
            for row in records:
                for i in range(3):sums[i]+=row[4+i]
        c.execute(f'SELECT COUNT(*),COUNT(DISTINCT page_id),SUM(paid_cents),SUM(social_cents),SUM(spend_cents) FROM {PUBLIC}')
        if list(c.fetchone())!=[total,total,*sums] or sums!=manifest['amounts'] or total!=manifest['rows']:
            raise RuntimeError('restored totals differ')
    result=dict(version=v,rows=total,amounts=sums,at=stamp(),manifest_sha256=file_hash(dest/'snapshot.json'))
    atomic(done,result);return result


def references(db,versions,index):
    pairs=set();expected=set();counts={'assignment':0,'recovery':0,'assignment_without_version':0,'outside_table_versions':0}
    last=None
    with db.cursor() as c:
        while True:
            where=' WHERE task_id>%s OR (task_id=%s AND item_index>%s)' if last else ''
            c.execute('SELECT task_id,item_index,page_id,selection_stage,JSON_UNQUOTE(JSON_EXTRACT(NULLIF(diagnostics_json,\'\'),'
                      "'$.profile_snapshot.version')) FROM fb_drama_page_assignment_log"+where+
                      ' ORDER BY task_id,item_index LIMIT 160',(last[0],last[0],last[1]) if last else ())
            rows=c.fetchall()
            if not rows:break
            last=rows[-1][:2]
            for task,item,p,stage,v in rows:
                if not p or not PAGE.fullmatch(str(p)):continue
                if not v or v=='null':
                    counts['assignment_without_version']+=1
                elif v not in versions:
                    counts['outside_table_versions']+=1
                else:
                    pairs.add((v,str(p)));counts['assignment']+=1
                    if not (stage or '').endswith('_unprofiled'):expected.add((v,str(p)))
                    index.write(encoded(dict(source='assignment',task=str(task),item=item,version=v,page=str(p),stage=stage))+'\n')
    with closing(sqlite3.connect('file:/mnt/data-disk/meta-page-recovery/state.sqlite3?mode=ro',uri=True,timeout=3)) as local:
        for task,raw in local.execute('SELECT id,proof FROM jobs WHERE proof IS NOT NULL'):
            try:o=json.loads(raw)
            except (TypeError,ValueError):continue
            v=o.get('profile_version');v=v.get('version') if isinstance(v,dict) else v
            p=str(o.get('new_page_id') or '')
            if v in versions and PAGE.fullmatch(p):
                pairs.add((v,p));counts['recovery']+=1
                if 'profile_snapshot_unavailable' not in (o.get('profile_warnings') or []):expected.add((v,p))
                index.write(encoded(dict(source='recovery',task=str(task),version=v,page=p))+'\n')
    return pairs,counts,expected


def confirm_references(db,versions,base):
    temp=base/'task-references.partial'
    with gzip.open(temp,'wt',encoding='utf-8') as index:pairs,counts,expected=references(db,versions,index)
    temp.replace(base/'task-references.jsonl.gz')
    found=absent=0;unexplained=[]
    by_version={}
    for v,p in pairs:by_version.setdefault(v,[]).append(p)
    with db.cursor() as c:
        for v,ids in by_version.items():
            for part in chunks(sorted(ids),48):
                c.execute(f'SELECT page_id,payload_sha256 FROM {LEGACY} WHERE profile_version=%s AND page_id IN ('+
                          ','.join(['%s']*len(part))+')',(v,*part))
                truth=dict(c.fetchall())
                for p in part:
                    raw,_=lookup(v,p,base)
                    if p in truth:
                        if raw is None or sha(raw)!=truth[p]:raise RuntimeError('referenced historical Page missing')
                        found+=1
                    else:
                        if raw is not None:raise RuntimeError('archived Page absent in source')
                        absent+=1
                        if (v,p) in expected:unexplained.append([v,p])
    return dict(receipts=counts,unique_pairs=len(pairs),found=found,absent_in_source=absent,unexpected_absence=unexplained)


def run():
    disk()
    if read_config()!='live':raise RuntimeError('history still receives mirror writes')
    frozen=json.loads(FROZEN.read_text());base=HISTORY
    base.mkdir(exist_ok=True,mode=0o700)
    with (base/'archive.lock').open('a') as lock,closing(connect_reader(env())) as db,closing(private_mysql()) as lab:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if layout(db)!=AFTER:raise RuntimeError('frozen source identity changed')
        with db.cursor() as c:ddl=one(c,'SHOW CREATE TABLE '+LEGACY)[1]
        prefix='CREATE TABLE `'+LEGACY+'`'
        if not ddl.startswith(prefix):raise RuntimeError('unexpected frozen DDL')
        ddl='CREATE TABLE `'+PUBLIC+'`'+ddl[len(prefix):]
        (base/'schema.sql').write_text(ddl+';\n')
        versions=frozen['versions']
        with db.cursor() as c:
            c.execute(f'SELECT DISTINCT profile_version FROM {LEGACY} ORDER BY profile_version')
            if [r[0] for r in c.fetchall()]!=versions:raise RuntimeError('frozen version inventory changed')
        snapshots=[];total=0
        for idx,v in enumerate(versions):
            original_proof=receipt(v) if (PORTRAITS/(v+'.receipt.json')).is_file() else None
            m=export_version(db,LEGACY,v,base,proof=original_proof)
            restored=restore_version(lab,m,base,ddl)
            snapshots.append(dict(version=v,rows=m['rows'],manifest_sha256=restored['manifest_sha256'],
                                  restore_sha256=file_hash(base/'versions'/v/'restored.json')))
            total+=m['rows']
            print(encoded(dict(completed=idx+1,versions=len(versions),total_rows=total,version=v)),flush=True)
        ref=confirm_references(db,set(versions),base)
        revision,tools_hash=package_tools(base)
        sizes={};size=0;blobs=0
        for root,_,names in os.walk(base):
            for name in names:
                f=Path(root)/name;size+=f.stat().st_size
                if f.parent.parent==base/'blobs':blobs+=1
        out=dict(at=stamp(),cutoff=frozen['version'],versions=snapshots,rows=total,references=ref,
                 schema_sha256=file_hash(base/'schema.sql'),task_reference_sha256=file_hash(base/'task-references.jsonl.gz'),
                 recovery_revision=revision,restore_tools_sha256=tools_hash,
                 compressed_bytes=size,unique_detail_blobs=blobs)
        atomic(base/'manifest.json',out)
        # Flush the archive's mounted filesystem before issuing the external, deletion-eligible receipt.
        subprocess.run(['sync','-f',str(base/'manifest.json')],check=True)
        # The external receipt includes the manifest itself and excludes transient progress and lock files.
        exact_bytes=sum(f.stat().st_size for f in (base/'schema.sql',base/'manifest.json',base/'task-references.jsonl.gz',base/'restore-tools.tar.gz'))
        exact_bytes+=sum(f.stat().st_size for folder in ('versions','blobs') for f in (base/folder).rglob('*') if f.is_file())
        atomic(ROOT/'history-ready.json',dict(at=stamp(),versions=len(versions),rows=total,references=ref,archive_flushed=True,
               compressed_bytes=exact_bytes,manifest_sha256=file_hash(base/'manifest.json')))
        return {k:v for k,v in out.items() if k!='versions'}


if __name__=='__main__':print(encoded(run()),flush=True)
