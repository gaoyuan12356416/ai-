"""Copy one verified immutable HK index to the CPU's read-only fallback."""
from contextlib import closing
import fcntl
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time

from .sql_reconciliation import atomic_json


def synchronize(root,port,token,rate=2*1024*1024):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with (root/'sync.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'already_running'}
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=180)
        archive=root/'download.incoming';temporary=root/'database.incoming'
        try:
            headers={'Authorization':'Bearer '+token,'Connection':'close'}
            connection.request('GET','/v1/status',headers=headers)
            response=connection.getresponse();body=response.read(65537)
            if response.status!=200 or len(body)>65536:raise RuntimeError('snapshot_status_unavailable')
            manifest=json.loads(body);version=manifest['version']
            if not re.fullmatch('[a-f0-9]{64}',version) or manifest['database']!=version+'.sqlite3':raise ValueError('invalid_index_manifest')
            if (root/'current.json').exists() and json.loads((root/'current.json').read_text())['version']==version:
                return {'status':'unchanged','version':version}
            if not 0<int(manifest['archive_bytes'])<=1024**3 or not 0<int(manifest['database_bytes'])<=4*1024**3:raise ValueError('index_exceeds_cache_storage_budget')
            if shutil.disk_usage(root).free<int(manifest['archive_bytes'])+int(manifest['database_bytes'])+1024**3:raise OSError('cache_disk_space_insufficient')
            connection.close();connection=http.client.HTTPConnection('127.0.0.1',port,timeout=180)
            connection.request('GET','/v1/index?version='+version,headers=headers);response=connection.getresponse()
            if response.status!=200:raise RuntimeError('snapshot_export_unavailable')
            digest=hashlib.sha256();received=0;started=time.monotonic()
            with archive.open('wb') as out:
                while True:
                    block=response.read(65536)
                    if not block:break
                    received+=len(block)
                    if received>manifest['archive_bytes']:raise ValueError('archive_size_mismatch')
                    digest.update(block);out.write(block)
                    delay=received/rate-(time.monotonic()-started)
                    if delay>0:time.sleep(delay)
                out.flush();os.fsync(out.fileno())
            if received!=manifest['archive_bytes'] or digest.hexdigest()!=manifest['archive_sha256']:raise ValueError('archive_checksum_failed')
            size=0
            with gzip.open(archive,'rb') as source,temporary.open('wb') as out:
                while True:
                    block=source.read(1024*1024)
                    if not block:break
                    size+=len(block)
                    if size>manifest['database_bytes']:raise ValueError('database_size_mismatch')
                    out.write(block)
                out.flush();os.fsync(out.fileno())
            if size!=manifest['database_bytes']:raise ValueError('database_size_mismatch')
            with closing(sqlite3.connect(temporary.as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute('PRAGMA quick_check').fetchone()!=('ok',):raise ValueError('index_check_failed')
            destination=root/manifest['database'];temporary.chmod(0o400);temporary.replace(destination)
            atomic_json(root/'current.json',manifest)
            versions=sorted(root.glob('*.sqlite3'),key=lambda p:p.stat().st_mtime,reverse=True)
            for old in versions[3:]:
                if old!=destination:old.unlink()
            return dict(status='copied',version=version,bytes=received,seconds=round(time.monotonic()-started,2))
        finally:
            connection.close()
            for path in (archive,temporary):path.unlink(missing_ok=True)


if __name__=='__main__':
    print(json.dumps(synchronize(os.environ['CAMPAIGN_SNAPSHOT_ROOT'],int(os.environ.get('CAMPAIGN_SOURCE_PORT','19973')),os.environ['OFFLOAD_INTERNAL_TOKEN'])),flush=True)
