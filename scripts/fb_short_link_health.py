"""Read-only stage progress; observation never claims, releases or retries work."""
import contextlib
import datetime as dt
from pathlib import Path
import sqlite3
import time

DATABASE = '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'
THRESHOLD = 300


def instant(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def summarize(rows, now, previous=None):
    previous = previous or {}
    sampled = instant(previous.get('observed_at'))
    # A gap is unknown observation time, not evidence of a continuous stall.
    prior = previous.get('pending_since', {}) if sampled and 0 <= now - sampled <= 150 else {}
    counts = dict(due_pending=0, overdue_pending=0, future_scheduled=0,
                  platform_processing=0, outcome_unknown=0, executing=0,
                  due_ready=0, preparing=0, schedule_unknown=0, held=0)
    pending = {}
    oldest = 0
    for row in rows:
        state = row['status']
        if state == 'unknown' or row['unknown_outcome']:
            counts['outcome_unknown'] += 1
        elif row['held']:
            counts['held'] += 1
        elif state == 'submitted':
            counts['platform_processing'] += 1
        elif state == 'running':
            counts['executing'] += 1
        elif state == 'queued':
            planned = instant(row['planned_at_utc'])
            if planned is None:
                counts['schedule_unknown'] += 1
            elif planned > now:
                counts['future_scheduled'] += 1
            elif row['prepare_state'] != 'ready':
                counts['preparing'] += 1
            elif row['link_state'] == 'ready':
                counts['due_ready'] += 1
            else:
                counts['due_pending'] += 1
                since = prior.get(row['task_id'], now)
                if not isinstance(since, (int, float)) or since > now:
                    since = now
                pending[row['task_id']] = since
                age = now - since
                oldest = max(oldest, age)
                counts['overdue_pending'] += age >= THRESHOLD
    return dict(counts, available=True, ok=counts['overdue_pending'] == 0,
                observed_at=dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                oldest_pending_seconds=round(oldest), threshold_seconds=THRESHOLD,
                pending_since=pending,
                code='short_link_backlog' if counts['overdue_pending'] else 'ok')


def collect_progress(database=DATABASE, now=None, previous=None):
    now = time.time() if now is None else now
    try:
        with contextlib.closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + 2
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = conn.execute("""SELECT task_id,status,unknown_outcome,planned_at_utc,prepare_state,link_state,
                EXISTS(SELECT 1 FROM pipeline_canonical_hold h WHERE h.source_key='fb:video:'||t.task_id) AS held
                FROM fb_video_task t WHERE status IN ('queued','submitted','running','unknown')""").fetchall()
        return summarize(rows, now, previous)
    except (OSError, sqlite3.Error):
        return {'available': False, 'ok': False, 'code': 'short_link_data_unavailable',
                'observed_at': dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                'pending_since': {}}
