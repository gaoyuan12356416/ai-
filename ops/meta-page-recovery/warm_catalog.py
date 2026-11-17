"""Read the pending info catalog into an existing collection without restarting it."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from features.fb_page_portrait.collect import catalog,PRODUCTS
from features.fb_page_portrait.runtime import atomic_json,encoded
from features.meta_page_recovery.sources import Reader,environment

ROOT=Path('/mnt/data-disk/fb-page-portraits/collect-cache')


class WarmCache:
    def __init__(self,folder):self.folder=folder
    def query(self,reader,statements,timeout=60):
        path=self.folder/(hashlib.sha256(encoded(statements).encode()).hexdigest()+'.json')
        if path.exists():return json.loads(path.read_text())['rows']
        rows=reader.query(statements,timeout=timeout)
        # The active collector can finish the same read concurrently. A unique
        # temp and exclusive final link preserve any already completed result.
        temporary=self.folder/('warm-'+uuid.uuid4().hex+'.json')
        atomic_json(temporary,dict(read_at=datetime.now(timezone.utc).isoformat(),rows=rows))
        try:
            try:os.link(temporary,path)
            except FileExistsError:pass
        finally:temporary.unlink()
        return json.loads(path.read_text())['rows']


def run(folder):
    folder=folder.resolve()
    if folder.parent!=ROOT or not re.fullmatch('[0-9a-f]{32}',folder.name):raise RuntimeError('invalid_collection_path')
    manifest=json.loads((folder/'manifest.json').read_text())
    if manifest.get('complete'):return
    social=json.loads((folder/'social-input.json').read_text())
    ids={app:set() for app in PRODUCTS}
    for path in folder.glob('*.json'):
        if not re.fullmatch('[0-9a-f]{64}\.json',path.name):continue
        for row in json.loads(path.read_text()).get('rows',[]):
            if row.get('app_id') in ids and row.get('content_id') and 'spend' in row and 'revenue' in row:
                ids[row['app_id']].add(str(row['content_id']))
    for row in social['rows']:
        if row.get('content_id') and row.get('app_id',1479) in ids:ids[row.get('app_id',1479)].add(str(row['content_id']))
    reader=Reader(environment());cache=WarmCache(folder)
    for app in PRODUCTS:
        values=catalog(reader,'ads_drama_info','ac',['content_id','app_id','language','series_code','resource_type','name'],app,sorted(ids[app]),cache)
        print(encoded(dict(event='catalog_warm_complete',app_id=app,content_ids=len(ids[app]),rows=len(values))),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--collection',required=True,type=Path);args=parser.parse_args();run(args.collection)
