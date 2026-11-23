"""One bounded refresh using the existing arithmetic, without a resident cache."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time

from .campaign import service as original
from .sql_client import Broker,read_session
from .sql_reconciliation import atomic_json


def refresh(seed):
    seed=Path(seed)
    # An existing verified seed is mandatory: migration never starts a fresh
    # all-history business query because a cache file went missing.
    previous=json.loads(seed.read_bytes())
    if previous.get('v')!=2 or not previous.get('m',{}).get('read_only_verified'):
        raise ValueError('verified_campaign_seed_required')
    broker=Broker(int(os.environ['SQL_OFFLOAD_PORT']),os.environ['SQL_OFFLOAD_TOKEN'],os.environ['SQL_OFFLOAD_RECEIPTS'])
    @contextmanager
    def connection():
        with read_session(broker,'campaign-copy-report',user=os.environ['SQL_OFFLOAD_USER'],
                          password=os.environ['SQL_OFFLOAD_PASSWORD'],database='ads_business',
                          budget_seconds=840,statement_timeout_ms=15000,report_options=True) as session:
            yield session
    original.report_connection=connection
    started=time.monotonic()
    raw=original.query_raw(previous)
    payload=original.merge_history(original.build_payload(raw),previous,raw['refresh_dates'],raw['history_next_date'])
    prior_meta=previous.get('m',{})
    freshness=dict(prior_meta.get('date_refreshed_at',{}))
    if not freshness:freshness.update({day:prior_meta.get('generated_at','') for day in prior_meta.get('refreshed_dates',[])})
    freshness.update({day:payload['meta']['generated_at'] for day in raw['refresh_dates']})
    payload['meta'].update(rolling_refresh=True,pending_refresh_dates=raw.get('pending_refresh_dates',[]),date_refreshed_at=freshness)
    del raw,previous,prior_meta
    # Stream the encoder to disk: avoid retaining a second full JSON string and
    # its encoded bytes in addition to the complete historical wire arrays.
    wire=original.encode_wire_payload(payload);del payload
    temporary=seed.with_suffix('.next')
    try:
        with temporary.open('w',encoding='utf-8') as out:
            os.chmod(str(temporary),0o600)
            json.dump(wire,out,ensure_ascii=False,separators=(',',':'),default=original.json_default)
            out.flush();os.fsync(out.fileno())
        temporary.replace(seed)
    finally:
        if temporary.exists():temporary.unlink()
    return dict(generated_at=wire['m']['generated_at'],refreshed_dates=wire['m']['refreshed_dates'],
                seconds=round(time.monotonic()-started,3),bytes=seed.stat().st_size)


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('seed',type=Path);args=parser.parse_args()
    result=refresh(args.seed);print(json.dumps(result),flush=True)


if __name__=='__main__':main()
