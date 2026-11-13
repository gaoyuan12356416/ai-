"""systemd ExecStop barrier for services required by distributed uploads.

The service remains alive while this helper waits. TimeoutStopSec=infinity is
required: uncertainty never authorizes termination of an in-flight task.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.maintenance import DIRECTORY, UNITS, matches

DB = '/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3'
HEALTH = {'pipeline-coordinator.service': 'http://127.0.0.1:18846/health',
          'drama-material-api.service': 'http://127.0.0.1:8787/api/ui/topbar',
          'x-post-automation.service': 'http://127.0.0.1:8810/health',
          'x-tag-post.service': 'http://127.0.0.1:8821/health'}


def pending(unit, db=DB):
    with contextlib.closing(sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True, timeout=5)) as c:
        c.row_factory = sqlite3.Row
        return [dict(row) for row in c.execute("SELECT id,source_id,channel,stage,status,generation "
                    "FROM pipeline_job WHERE status IN ('running','legacy_running') ORDER BY id")
                if matches(unit, row)]


def write_hold(unit, directory=DIRECTORY):
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    path = directory / (unit + '.json')
    temporary = directory / (unit + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps({'unit': unit, 'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                                    'release_condition': 'same service HTTP readiness',
                                    'pid': os.getpid()}) + '\n')
    temporary.chmod(0o644)
    # Serialize against claim admission before publishing the hold. A claim
    # which read the old hold set must commit before we take this writer lock.
    with contextlib.closing(sqlite3.connect('file:' + DB + '?mode=rw', uri=True, timeout=5)) as c:
        c.execute('BEGIN IMMEDIATE')
        temporary.replace(path)
        c.commit()


def release(unit, directory=DIRECTORY):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(120):
        try:
            with opener.open(HEALTH[unit], timeout=2) as response:
                if response.status == 200:
                    (directory / (unit + '.json')).unlink(missing_ok=True)
                    print(json.dumps({'service': unit, 'drain_released': True}), flush=True)
                    return
        except (OSError, ValueError):
            pass
        time.sleep(.5)
    raise RuntimeError('service_not_ready_drain_preserved:' + unit)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('stop', 'start', 'status'))
    parser.add_argument('--unit', choices=UNITS, required=True)
    args = parser.parse_args()
    if args.action == 'start':
        return release(args.unit)
    if args.action == 'status':
        print(json.dumps({'service': args.unit, 'held': (DIRECTORY / (args.unit + '.json')).exists(),
                          'pending': pending(args.unit)}))
        return
    last = None
    installed = False
    while True:
        try:
            if not installed:
                write_hold(args.unit)
                installed = True
            jobs = pending(args.unit)
            signature = [(j['id'], j['generation']) for j in jobs]
            if signature != last:
                print(json.dumps({'service': args.unit, 'draining_jobs': jobs}), flush=True)
                last = signature
            if not jobs:
                # The coordinator reads the hold inside its serialized claim
                # transaction. An empty snapshot after installing it cannot
                # race a new dependent claim. Existing unknown facts are inert.
                return
        except Exception as exc:
            print(json.dumps({'service': args.unit, 'drain_check_error': type(exc).__name__}), flush=True)
        time.sleep(2)


if __name__ == '__main__':
    main()
