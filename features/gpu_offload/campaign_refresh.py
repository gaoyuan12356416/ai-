"""One bounded refresh using the existing arithmetic, without a resident cache."""
import json
import gc
import hashlib
import os
from pathlib import Path
import time

from .campaign import service as original
from .campaign.descriptions import open_descriptions
from .sql_client import Broker,read_session
from .sql_reconciliation import atomic_json
from .campaign_snapshot import current
from .campaign_history import write_merged_seed


def refresh(seed):
    seed=Path(seed)
    # An existing verified seed is mandatory: migration never starts a fresh
    # all-history business query because a cache file went missing.
    previous=json.loads(seed.read_bytes())
    if previous.get('v')!=2 or not previous.get('m',{}).get('read_only_verified'):
        raise ValueError('verified_campaign_seed_required')
    manifest,database=current(seed.parent/'index')
    digest=hashlib.sha256()
    with seed.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    if digest.hexdigest()!=manifest['version']:
        raise ValueError('campaign_index_must_match_refresh_seed')
    # Queries only require Campaign/entity metadata. Historical facts are read
    # from the same immutable local index after the SQL lease has closed.
    previous.pop('d',None);previous.pop('xd',None);gc.collect()
    broker=Broker(int(os.environ['SQL_OFFLOAD_PORT']),os.environ['SQL_OFFLOAD_TOKEN'],os.environ['SQL_OFFLOAD_RECEIPTS'])
    started=time.monotonic()
    with open_descriptions(seed.parent/'refresh-descriptions.sqlite3',manifest['version']) as descriptions:
        with read_session(broker,'campaign-copy-report',user=os.environ['SQL_OFFLOAD_USER'],
                          password=os.environ['SQL_OFFLOAD_PASSWORD'],database='ads_business',
                          budget_seconds=840,statement_timeout_ms=15000,report_options=True,compressed=True) as session:
            raw=original.query_raw_connection(session,previous,descriptions=descriptions)
    prior_meta=previous.get('m',{})
    del previous;gc.collect()
    payload=original.build_payload(raw)
    dates,next_history=raw['refresh_dates'],raw['history_next_date']
    freshness=dict(prior_meta.get('date_refreshed_at',{}))
    if not freshness:freshness.update({day:prior_meta.get('generated_at','') for day in prior_meta.get('refreshed_dates',[])})
    freshness.update({day:payload['meta']['generated_at'] for day in raw['refresh_dates']})
    payload['meta'].update(rolling_refresh=True,pending_refresh_dates=raw.get('pending_refresh_dates',[]),date_refreshed_at=freshness)
    del raw;gc.collect()
    # Stream the encoder to disk: avoid retaining a second full JSON string and
    # its encoded bytes in addition to the complete historical wire arrays.
    wire=original.encode_wire_payload(payload);del payload
    temporary=seed.with_suffix('.next')
    try:
        with temporary.open('w',encoding='utf-8') as out:
            os.chmod(str(temporary),0o600)
            metadata=write_merged_seed(wire,database,prior_meta,dates,next_history,out)
            out.flush();os.fsync(out.fileno())
        temporary.replace(seed)
    finally:
        if temporary.exists():temporary.unlink()
    return dict(generated_at=metadata['generated_at'],refreshed_dates=metadata['refreshed_dates'],
                seconds=round(time.monotonic()-started,3),bytes=seed.stat().st_size)


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('seed',type=Path);args=parser.parse_args()
    result=refresh(args.seed);print(json.dumps(result),flush=True)


if __name__=='__main__':main()
