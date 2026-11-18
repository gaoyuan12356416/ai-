"""Read-only drain diagnostics. No timeout in this module releases a hold."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3

from .maintenance import DIRECTORY, UNITS, matches


def age(value, now):
    try:
        return max(0, int((now - dt.datetime.fromisoformat(value.replace('Z', '+00:00'))).total_seconds()))
    except (TypeError, ValueError):
        return None


def collect(dispatch, native, locks, directory=DIRECTORY):
    from features.x_auto_posts.selection_recovery import execution_available, pure_selection
    now = dt.datetime.now(dt.timezone.utc)
    result = {'checked_at': now.isoformat(), 'holds': []}
    active = []
    for unit in UNITS:
        path = Path(directory) / (unit + '.json')
        try:
            raw = json.loads(path.read_text())
            active.append((unit, raw))
        except FileNotFoundError:
            continue
    if not active:
        return result
    with contextlib.ExitStack() as stack:
        def connect(path):
            c = stack.enter_context(contextlib.closing(sqlite3.connect(
                'file:' + str(path) + '?mode=ro', uri=True, timeout=2)))
            c.row_factory = sqlite3.Row
            return c
        d, a = connect(dispatch), connect(native)
        jobs = d.execute("SELECT * FROM pipeline_job WHERE status IN ('running','legacy_running') ORDER BY id").fetchall()
        workers = {(r['node_id'], r['worker_id']): r for r in d.execute('SELECT * FROM pipeline_worker')}
        for unit, hold in active:
            matched = [r for r in jobs if matches(unit, r)]
            elapsed = age(hold.get('started_at', ''), now)
            item = {'unit': unit, 'started_at': hold.get('started_at'), 'elapsed_seconds': elapsed,
                    'overdue': elapsed is not None and elapsed >= 600,
                    'pending_count': len(matched), 'blockers': []}
            result['holds'].append(item)
            for row in matched[:200]:
                worker = workers.get((row['node_id'], row['worker_id']))
                observed = json.loads(worker['observation_json'] or '{}') if worker else {}
                heartbeat = worker['last_seen_at'] if worker else ''
                phase = observed.get('phase') or row['source_status'] or row['stage']
                checkpoint = d.execute('SELECT phase,received_at FROM pipeline_checkpoint WHERE job_id=? AND generation=? '
                                       'ORDER BY received_at DESC LIMIT 1', (row['id'], row['generation'])).fetchone()
                progress = checkpoint['received_at'] if checkpoint else row['updated_at']
                if checkpoint and phase in ('', 'idle', row['stage']):
                    phase = checkpoint['phase']
                reason = 'execution_in_progress' if heartbeat and (age(heartbeat, now) or 0) <= 90 else 'heartbeat_stale_requires_review'
                if row['source_key'].startswith('x:auto:') and row['source_id'].isdigit():
                    task = a.execute('SELECT * FROM x_auto_task WHERE id=?', (int(row['source_id']),)).fetchone()
                    if task:
                        phase, progress, heartbeat = task['claim_phase'] or task['status'], task['updated_at'], task['updated_at']
                        if task['status'] == 'selecting':
                            reason = ('execution_still_active' if not execution_available(locks, task['id'])
                                      else 'selection_recovery_check_pending' if pure_selection(task)
                                      else 'reservation_or_attempt_requires_review')
                item['blockers'].append({'job_id': row['id'], 'source_key': row['source_key'],
                    'channel': row['channel'], 'stage': row['stage'], 'phase': phase,
                    'started_at': row['started_at'], 'last_progress_at': progress,
                    'last_heartbeat_at': heartbeat, 'reason': reason})
    return result


def save(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False) + '\n')
    temporary.chmod(0o644)
    temporary.replace(path)


def read_snapshot(path='/run/pipeline-service-drain/diagnostics.json'):
    try:
        snapshot = json.loads(Path(path).read_text())
        seconds = age(snapshot.get('checked_at', ''), dt.datetime.now(dt.timezone.utc))
        snapshot['stale'] = seconds is None or seconds > 90
        return snapshot
    except (OSError, ValueError):
        return {'holds': [], 'stale': True, 'error': 'diagnostics_unavailable'}
