import argparse
import fcntl
import json
import os
from pathlib import Path
import time
from .core import ROOT, update


def main():
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');args=p.parse_args()
    Path(ROOT).mkdir(parents=True,exist_ok=True)
    with (Path(ROOT)/'refresh.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        token=os.environ.get('A_GROUP_TASK_CREATE_TOKEN','')
        if not token:raise RuntimeError('rank_token_not_configured')
        for attempt in range(1 if args.once else 4):
            result=update(token);print(json.dumps(result),flush=True)
            if result['status']=='ok':
                # Metadata is auxiliary; a failed enqueue never invalidates a rank.
                try:
                    from .core import database
                    from contextlib import closing
                    from features.drama_audience.store import initialize,enqueue
                    with closing(database()) as db:
                        payload=json.loads(db.execute('SELECT payload FROM snapshots WHERE date=?',(result['date'],)).fetchone()[0])
                    initialize();enqueue([r['series_key'] for r in payload['list']],priority=50)
                except Exception as exc:
                    print(json.dumps(dict(event='rank_metadata_pending',error=type(exc).__name__)),flush=True)
                return
            if attempt < 3 and not args.once:time.sleep(600)
        raise SystemExit(1)


if __name__=='__main__':main()
