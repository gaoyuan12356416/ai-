"""Verify direct roles and optional full admission using SELECT-only traffic."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from features.hk_sql_budget import Ledger,budget_connection


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--saturation",action="store_true",help="run when this real HK pool is idle")
    args=parser.parse_args()
    ledger=Ledger()
    before=ledger.status()
    roles=["read","write"] if not args.saturation else ["read"]*28+["write"]*2
    if before["available"]<len(roles):
        raise SystemExit("concurrent work present; run full admission check after it drains")
    admitted=threading.Barrier(len(roles)+1,timeout=12)
    permit=[threading.Event() for _ in roles]
    results=[]

    def query(role, index=None):
        with budget_connection(role, time.monotonic()+18) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT @@server_uuid,CONNECTION_ID(),@@session.tx_read_only,@@global.read_only")
                instance,connection_id,transaction,server=cursor.fetchone()
                if int(transaction)!=int(role=="read") or (role=="write" and int(server)!=0):
                    raise AssertionError("transaction role differs")
            if index is not None:
                admitted.wait()
                if not permit[index].wait(12):
                    raise TimeoutError("probe release")
            return instance,int(connection_id)

    with ThreadPoolExecutor(max_workers=len(roles)+1) as pool:
        batch=[pool.submit(query,r,i) for i,r in enumerate(roles)]
        try:
            admitted.wait()
            high=ledger.status()
            queued=False
            if args.saturation:
                waiter=pool.submit(query,"read")
                end=time.monotonic()+2
                while not ledger.status()["queued"] and time.monotonic()<end:
                    time.sleep(.01)
                queued=ledger.status()["queued"]>=1 and not waiter.done()
                if not queued:
                    raise AssertionError("31st request did not wait")
                permit[0].set()
                waiter.result(timeout=3)
        finally:
            for event in permit:event.set()
        results=[f.result() for f in batch]
    after=ledger.status()
    if len(results)!=len(set(results)) or high["in_use"]>30:
        raise AssertionError("session identity or budget mismatch")
    print(json.dumps(dict(opened=len(results),states=high["states"],roles=high["roles"],
                          overflow_queued=queued,data_writes=0,after=after),sort_keys=True))


if __name__=="__main__":main()
