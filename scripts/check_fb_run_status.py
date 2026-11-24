"""Read-only report verification without invoking storage initialization."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time


def check(runtime, database, output):
    sys.path.insert(0, str(runtime))
    from features.fb_auto_posts.core import FBAutoPostStore, ActorScope, _ClosingConnection
    store = FBAutoPostStore.__new__(FBAutoPostStore)
    store.now_fn = lambda: datetime.now(timezone.utc)

    def connect():
        conn = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5,
                               isolation_level=None, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        return conn

    store.connect = connect
    actor = ActorScope('read-only-verification', 'Verification', True, '')
    start = time.monotonic()
    result = store.list_runs(actor, limit=200)
    elapsed = time.monotonic() - start
    for row in result['items']:
        c = row['task_counts']
        assert c['total'] == sum(c[k] for k in ('published', 'pending', 'active', 'review', 'failed', 'skipped', 'cancelled'))
        if row['display_status'] == 'running':
            assert c['active'] > 0
    historical = [r for r in result['items'] if r['status'] == 'running' and r['created_at_utc'] < '2026-11-23T16:00:00+00:00']
    for row in historical:
        detail = store.get_run(row['id'], actor)['run']
        assert row['task_counts'] == detail['task_counts']
        assert row['display_status'] == detail['display_status']
    proof = dict(checked_at_utc=store.now_fn().isoformat(), elapsed_seconds=round(elapsed, 3),
                 count=len(result['items']), states=dict(Counter(r['display_status'] for r in result['items'])),
                 historical=[{key: r[key] for key in ('id','name','created_at_utc','status','display_status','display_reason','task_counts')} for r in historical])
    output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    output.chmod(0o600)
    print(json.dumps({key: proof[key] for key in ('elapsed_seconds','count','states')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    check(args.runtime.resolve(), args.db.resolve(), args.output.resolve())
