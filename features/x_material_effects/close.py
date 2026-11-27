"""GET-only closing pass; uses the hourly collector's existing lock and cache."""
from contextlib import closing
from datetime import datetime,timezone
import fcntl
import json
from pathlib import Path
from . import collector,store
from features.social_material_effects import sampling


def main():
    if not sampling.closing_start(datetime.now(timezone.utc),23):
        return dict(status='outside_closing_hour')
    root=Path(store.ROOT)
    with (root/'refresh.lock').open('a') as guard:
        try:
            fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(status='hourly_collector_active')
        posts,_=collector.discover()
        return dict(status='completed',**collector.collect_metrics(posts,root/'observations.sqlite3',closing_pass=True))


if __name__=='__main__':
    print(json.dumps(main()),flush=True)
