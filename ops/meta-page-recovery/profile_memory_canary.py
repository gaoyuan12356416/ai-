"""Full-size 3-GiB memory check: real encoder/projector, no external SQL writes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from features.fb_page_portrait.build import build
from features.fb_page_portrait import storage
from features.fb_page_portrait.runtime import atomic_json


def verify(data):
    import pymysql
    from pymysql.cursors import Cursor
    from pymysql.connections import Connection
    seen={};stats=dict(sql_batches=0,encoded_sql_bytes=0,external_sql_writes=0)
    class DryCursor(Cursor):
        def execute(self,query,args=None):
            if isinstance(query,(bytes,bytearray)):
                stats['sql_batches']+=1;stats['encoded_sql_bytes']+=len(query)
                self._executed=query
            return 1
        def fetchone(self):return ('ads_ai',0)
        def _escape_args(self,args,conn):
            if len(args)==11:
                seen[args[1]]=dict(page_id=args[1],hash=hashlib.sha256(args[8].encode()).hexdigest(),
                    paid_cents=args[4],social_cents=args[5],spend_cents=args[6])
            return super()._escape_args(args,conn)
    class DryConnection(Connection):
        def __init__(self):
            super().__init__(defer_connect=True,charset='utf8mb4');self.server_status=0
        def cursor(self,*args,**kw):return DryCursor(self)
        def commit(self):pass
    class Reader:
        def query(self,*args,**kw):return list(seen.values())
    # Only the transport is replaced. The installed PyMySQL encoder and its
    # bounded executemany implementation run against the full original input.
    with patch.object(pymysql,'connect',return_value=DryConnection()):
        proof=storage.write(data,dict(ADMIN_MAPPING_MYSQL_USER='unused',ADMIN_MAPPING_MYSQL_PASSWORD='unused'),Reader())
    return proof,stats


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();source=args.input.resolve();root=args.output.resolve()
    if source.parent!=Path('/mnt/data-disk/fb-page-portraits') or not source.name.endswith('.input.json'):raise ValueError('invalid_canary_input')
    if not root.is_relative_to(Path('/mnt/data-disk/meta-page-recovery/verification')) or root.exists():raise ValueError('invalid_canary_output')
    root.mkdir(parents=True,mode=0o700)
    started=time.monotonic();raw=json.loads(source.read_text());data=build(raw)
    proof,stats=verify(data);storage.publish(data,proof,root)
    import resource
    result=dict(status='ok',pages=len(data['pages']),verified_rows=proof['verified_rows'],
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,seconds=round(time.monotonic()-started,3),**stats)
    atomic_json(root/'acceptance.json',result);print(json.dumps(result),flush=True)


if __name__=='__main__':main()
