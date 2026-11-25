"""Maintain an atomic local copy of the catalog columns used by FB unrestricted plans."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import fcntl
import itertools
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_auto_posts.metric_mysql import GatedMetricReader, literal
from features.fb_auto_posts.core import StoreError

DIRECTORY = Path('/mnt/data-disk/fb-planner-catalog')
LEDGER = Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
SOURCE = 'kunlunads_dev.ads_custom_source'
RESOURCE = 'kunlunads_dev.ads_drama_resource'
MATERIAL_FILTER = """data_source=6 AND product IN ('Dramawave','DramawaveSource') AND type=2
 AND is_delete=0 AND video_duration>0 AND data_source_id=TRIM(data_source_id) AND TRIM(data_source_id)<>''"""
MATERIAL_FIELDS = """id,TRIM(data_source_id),LOWER(TRIM(COALESCE(language,''))),product,
 CAST(video_duration AS CHAR),url,name,tag_name"""
RESOURCE_FIELDS = """content_id,CAST(app_id AS CHAR),language,series_code,labels,
 CASE WHEN type=2 THEN CONVERT(BINARY TRIM(`desc`) USING utf8mb4) ELSE NULL END"""
RESOURCE_KEYS = ('content_id','app_id','language','series_code','labels','description')


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start+size]


def read_parallel(callback, jobs):
    # Bound both outstanding SQL connections and buffered result bytes.
    jobs = iter(jobs)
    with ThreadPoolExecutor(max_workers=2) as pool:
        while True:
            group = list(itertools.islice(jobs, 4))
            if not group:
                return
            yield from pool.map(callback, group)


def select(fields, tail):
    return 'SELECT HEX(JSON_ARRAY(' + fields + ')) ' + tail


def source_rows(reader, lower, upper, before):
    span = f'id BETWEEN {lower} AND {upper}'
    if before is None:
        return [], reader.query([select(MATERIAL_FIELDS, f'FROM {SOURCE} FORCE INDEX(PRIMARY) WHERE {span} AND {MATERIAL_FILTER}')])
    # Each group has <=64 numeric IDs; group_concat fits the server default of 1024 bytes.
    # The full current membership catches physical deletions and moves out of the filter.
    covered = select("""GROUP_CONCAT(id ORDER BY id),GROUP_CONCAT(CASE WHEN updated_at IS NULL OR
       updated_at>=FROM_UNIXTIME(%d) THEN id END ORDER BY id)""" % before,
       f'FROM {SOURCE} FORCE INDEX(PRIMARY) WHERE {span} AND {MATERIAL_FILTER} GROUP BY id DIV 64')
    return reader.query([covered]), []


def refresh_materials(db, reader, before):
    high = int(reader.query([select('MAX(id)', 'FROM '+SOURCE)])[0][0] or 0)
    # Reconcile the old tail as well if upstream hard-deleted its largest IDs.
    high = max(high, db.execute('SELECT COALESCE(MAX(id),0) FROM material').fetchone()[0])
    width = 32768 if before is not None else 8192
    spans = [(lo, min(high,lo+width-1)) for lo in range(0,high+1,width)]
    def load(span):
        return span, source_rows(reader, *span, before)
    ins = 'INSERT OR REPLACE INTO material VALUES(?,?,?,?,?,?,?,?)'
    changed = removed = 0
    for index, ((lo,hi),(groups,rows)) in enumerate(read_parallel(load,spans),1):
        if before is not None:
            old = {r[0] for r in db.execute('SELECT id FROM material WHERE id BETWEEN ? AND ?', (lo,hi))}
            present, dirty = set(), set()
            for members, touched in groups:
                present.update(map(int,members.split(',')))
                if touched:
                    dirty.update(map(int,touched.split(',')))
            gone = old - present
            removed += len(gone)
            db.executemany('DELETE FROM material WHERE id=?', ((i,) for i in gone))
            for ids in chunks(sorted(dirty | (present-old)), 1500):
                wanted = ','.join(map(str,ids))
                new = reader.query([select(MATERIAL_FIELDS, f'FROM {SOURCE} FORCE INDEX(PRIMARY) WHERE id IN ({wanted}) AND {MATERIAL_FILTER}')])
                # Rows may have been withdrawn between the two bounded reads.
                db.executemany('DELETE FROM material WHERE id=?', ((i,) for i in ids))
                db.executemany(ins,new)
                changed += len(new)
        else:
            db.executemany(ins, rows)
            changed += len(rows)
        if index % 128 == 0:
            print('material',index,'/',len(spans),changed,flush=True)
    print('material_complete changed',changed,'removed',removed,flush=True)


def resource_apps(reader):
    apps = []
    while True:
        after = ' AND app>'+literal(apps[-1]) if apps else ''
        rows = reader.query([select('app', f'FROM {RESOURCE} FORCE INDEX(aacn) WHERE app_id=1479{after} ORDER BY app LIMIT 1')])
        if not rows:
            return apps
        apps.append(rows[0][0])


def refresh_resources(db, reader, before):
    ids = [r[0] for r in db.execute('SELECT DISTINCT cid FROM material ORDER BY cid')]
    apps = resource_apps(reader)
    old = dict(db.execute('SELECT cid,n FROM resource'))
    app_sql = ','.join(map(literal,apps)) or 'NULL'
    # Check counts using the covering index. Deleted/moved resource rows must
    # invalidate the cached descriptions and portrait just as updates do.
    changed = set()
    if before is not None:
        statuses = reader.query([select('drama_status',
            f'FROM (SELECT DISTINCT drama_status FROM {RESOURCE} FORCE INDEX(du)) status_keys')])
        for (status,) in statuses:
            cond = 'drama_status IS NULL' if status is None else 'drama_status='+str(int(status))
            # Chunk a long interval if the updater was stopped. If the last
            # snapshot is very old use the full refresh instead.
            for start in range(before,int(time.time())+1,1800):
                def changes(lo,hi):
                    sql=f"""SELECT DISTINCT HEX(JSON_ARRAY(content_id)) FROM {RESOURCE} FORCE INDEX(du)
                        WHERE {cond} AND updated_at>=FROM_UNIXTIME({lo}) AND updated_at<FROM_UNIXTIME({hi})
                          AND app_id=1479"""
                    try:
                        return reader.query([sql])
                    except StoreError as exc:
                        if exc.code!='fb_auto_metric_query_mysql_3024' or hi-lo<=1:
                            raise
                        mid=(lo+hi)//2
                        return changes(lo,mid)+changes(mid,hi)
                changed.update(r[0] for r in changes(start,start+1800))
    def load(batch):
        keys = ','.join(map(literal,batch))
        where = f'app_id=1479 AND app IN ({app_sql}) AND content_id IN ({keys})'
        counts = dict(reader.query([select('content_id,COUNT(*)',f'FROM {RESOURCE} FORCE INDEX(aacn) WHERE {where} GROUP BY content_id')]))
        dirty = [cid for cid in batch if before is None or cid in changed or old.get(cid)!=counts.get(cid,0)]
        if not dirty:
            return []
        def details(part):
            key_sql = ','.join(map(literal,part))
            sql = f"""SELECT DISTINCT HEX(JSON_ARRAY({RESOURCE_FIELDS})) FROM {RESOURCE} FORCE INDEX(actcld)
                WHERE app_id=1479 AND app IN ({app_sql}) AND content_id IN ({key_sql})"""
            try:
                return reader.query([sql],execution_ms=8000)
            except StoreError as exc:
                if len(part)<2 or exc.code not in {'fb_auto_metric_query_mysql_3024','fb_auto_metric_query_timeout'}:
                    raise
                half=len(part)//2
                return details(part[:half])+details(part[half:])
        entries = defaultdict(list)
        for part in chunks(dirty,144):
            for row in details(part):
                entries[row[0]].append(dict(zip(RESOURCE_KEYS,row)))
        return [(cid,counts.get(cid,0),json.dumps(entries[cid],separators=(',',':'),ensure_ascii=False)) for cid in dirty]
    total = 0
    for i,rows in enumerate(read_parallel(load,chunks(ids,576)),1):
        db.executemany('INSERT OR REPLACE INTO resource VALUES(?,?,?)',rows)
        total += len(rows)
        if i%20==0: print('resources',i,total,flush=True)
    # A content that disappears from the material catalog may re-enter later.
    db.execute('DELETE FROM resource WHERE cid NOT IN (SELECT cid FROM material)')
    print('resources_complete',len(ids),total,flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force',action='store_true')
    args=parser.parse_args()
    mounted=subprocess.check_output(['findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk'],text=True).strip()
    if mounted!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('catalog data mount unavailable')
    directory=DIRECTORY
    directory.mkdir(exist_ok=True)
    with (directory/'refresh.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        active=directory/'current.sqlite3'
        previous=0
        if active.exists():
            with closing(sqlite3.connect(active.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
                previous=db.execute('SELECT started FROM generation').fetchone()[0]
        now=int(time.time())
        if not args.force:
            with closing(sqlite3.connect(LEDGER.as_uri()+'?mode=ro',uri=True)) as db:
                due=db.execute("""SELECT MAX(d.created_at_utc) FROM fb_auto_due_slot d JOIN fb_auto_template t
                    ON t.id=d.template_id JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=d.template_version
                    WHERE d.status IN ('pending','preparing') AND t.status='enabled'
                    AND json_extract(v.config_json,'$.drama_launch_window_days')=0
                    AND json_extract(v.config_json,'$.app_id')='1479'""").fetchone()[0]
            evening=time.localtime(now).tm_hour==23 and time.localtime(now).tm_min>=5 and now-previous>7200
            if not evening and (not due or (int(datetime.fromisoformat(due).timestamp())<=previous and now-previous<7200)):
                return
        reader=GatedMetricReader(dict(user=os.environ['FB_AUTO_MYSQL_USER'],password=os.environ['FB_AUTO_MYSQL_PASSWORD']))
        remote,concat_limit=reader.query([select('UNIX_TIMESTAMP(),@@group_concat_max_len', '')])[0]
        remote=int(remote)
        if int(concat_limit)<1024:
            raise ValueError('source group_concat limit is too low for complete membership')
        tmp=directory/'building.sqlite3'
        tmp.unlink(missing_ok=True)
        if active.exists(): shutil.copyfile(active,tmp)
        with closing(sqlite3.connect(tmp)) as db:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA synchronous=OFF')
            db.executescript("""CREATE TABLE IF NOT EXISTS material(id INTEGER PRIMARY KEY,cid TEXT,lang TEXT,product TEXT,
                duration NUMERIC,url TEXT,name TEXT,tag TEXT);
                CREATE TABLE IF NOT EXISTS resource(cid TEXT PRIMARY KEY,n INTEGER,items TEXT);
                CREATE TABLE IF NOT EXISTS generation(started INTEGER,source_epoch INTEGER,finished INTEGER);""")
            prev=db.execute('SELECT source_epoch FROM generation').fetchone()
            before=prev[0]-2 if prev else None
            refresh_materials(db,reader,before)
            db.execute('CREATE INDEX IF NOT EXISTS material_cid ON material(cid)')
            refresh_resources(db,reader,before if before and remote-before<86400 else None)
            db.execute('DELETE FROM generation')
            db.execute('INSERT INTO generation VALUES(?,?,?)',(now,remote,int(time.time())))
            db.commit()
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('invalid catalog')
        tmp.chmod(0o640)
        import grp
        os.chown(tmp,0,grp.getgrnam('fb-auto-post').gr_gid)
        with tmp.open('rb') as file:os.fsync(file.fileno())
        tmp.replace(active)
        print('catalog_ready seconds',int(time.time())-now,flush=True)


if __name__=='__main__':
    main()
