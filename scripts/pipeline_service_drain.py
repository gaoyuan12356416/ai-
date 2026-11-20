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
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.maintenance import DIRECTORY, UNITS, matches
from features.pipeline_dispatch import restart_handoff

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


def planned_restart(unit):
    """A stop (including shutdown) must still drain every dependent task."""
    if unit != restart_handoff.COORDINATOR:
        return False
    try:
        result = subprocess.run(['systemctl', 'list-jobs', '--no-legend', '--no-pager', '--plain'],
                                capture_output=True, text=True, check=True, timeout=3)
        return any(len(parts) == 4 and parts[1] == unit and parts[2] == 'restart'
                   for parts in (line.split() for line in result.stdout.splitlines()))
    except (OSError, subprocess.SubprocessError):
        return False


def drain_state(unit, db=DB, *, restarting=False):
    # Old callers of pending() retain their full-stop semantics.
    with contextlib.closing(sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True, timeout=5)) as c:
        c.row_factory = sqlite3.Row
        return restart_handoff.assess(c, unit, restarting=restarting)


def save_handoff(unit, renders, directory=DIRECTORY):
    path = directory / (unit + '.json')
    hold = json.loads(path.read_text())
    if hold.get('pid') != os.getpid():
        raise RuntimeError('drain_owner_changed')
    hold['continuing_renders'] = renders
    hold['handoff_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    temporary = path.with_suffix('.handoff.tmp')
    temporary.write_text(json.dumps(hold) + '\n')
    temporary.chmod(0o644)
    temporary.replace(path)


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


def release(unit, directory=DIRECTORY, db=DB):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    path = directory / (unit + '.json')
    hold = json.loads(path.read_text()) if path.exists() else {}
    handed = hold.get('continuing_renders', [])
    ready_at = None
    for attempt in range(120):
        try:
            with opener.open(HEALTH[unit], timeout=2) as response:
                if response.status == 200:
                    ready_at = ready_at or dt.datetime.now(dt.timezone.utc)
                    verification = {'waiting': [], 'attention': []}
                    if handed:
                        with contextlib.closing(sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True, timeout=5)) as c:
                            c.row_factory = sqlite3.Row
                            verification = restart_handoff.resumed(c, handed, ready_at)
                    if verification['waiting']:
                        time.sleep(.5)
                        continue
                    path.unlink(missing_ok=True)
                    print(json.dumps({'service': unit, 'drain_released': True,
                                      'continued_renders': len(handed),
                                      'attention': verification['attention']}), flush=True)
                    return
        except (OSError, ValueError, sqlite3.Error):
            pass
        time.sleep(.5)
    raise RuntimeError('service_or_render_handoff_not_ready_drain_preserved:' + unit)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('stop', 'start', 'status'))
    parser.add_argument('--unit', choices=UNITS, required=True)
    parser.add_argument('--preview-restart', action='store_true',
                        help='read-only status preview; never changes a stop into a restart')
    args = parser.parse_args()
    if args.preview_restart and args.action != 'status':
        parser.error('--preview-restart requires status')
    if args.action == 'start':
        return release(args.unit)
    if args.action == 'status':
        print(json.dumps({'service': args.unit, 'held': (DIRECTORY / (args.unit + '.json')).exists(),
                          **drain_state(args.unit, restarting=args.preview_restart or planned_restart(args.unit))}))
        return
    last = None
    installed = False
    while True:
        try:
            if not installed:
                write_hold(args.unit)
                installed = True
            restarting = planned_restart(args.unit)
            state = drain_state(args.unit, restarting=restarting)
            jobs, renders = state['pending'], state['continuing_renders']
            signature = ([(j['id'], j['generation']) for j in jobs],
                         [(j['id'], j['generation']) for j in renders])
            if signature != last:
                print(json.dumps({'service': args.unit, 'draining_jobs': jobs,
                                  'continuing_renders': renders}), flush=True)
                last = signature
            if not jobs:
                if renders:
                    # Recheck the systemd operation: a cancelled restart must
                    # never turn into an indefinite stop with live renderers.
                    if not planned_restart(args.unit):
                        continue
                    save_handoff(args.unit, renders)
                # The coordinator reads the hold inside its serialized claim
                # transaction. An empty snapshot after installing it cannot
                # race a new dependent claim. Only identified local renders
                # survive the short restart; no publishing permit is replayed.
                return
        except Exception as exc:
            print(json.dumps({'service': args.unit, 'drain_check_error': type(exc).__name__}), flush=True)
        time.sleep(2)


if __name__ == '__main__':
    main()
