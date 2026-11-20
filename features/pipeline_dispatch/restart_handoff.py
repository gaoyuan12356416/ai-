"""Read-only classification for a short coordinator restart.

The local render executors retain a 120-second lease during transport loss and
persist completion callbacks before delivery. Platform publishing does not have
that contract: it must finish through the normal service-stop barrier.
"""
import datetime as dt
import json

from .maintenance import matches

COORDINATOR = 'pipeline-coordinator.service'
LOCAL_RENDERERS = {'fb_prepare', 'tt_prepare', 'x_random', 'x_repair'}
IDENTITY = ('id', 'generation', 'node_id', 'worker_id')


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def fresh(value, now, seconds=30):
    parsed = timestamp(value)
    return parsed is not None and 0 <= (now - parsed).total_seconds() <= seconds


def render_can_continue(row, now):
    """Unknown executors, leases, observations and external intents stay held."""
    try:
        payload = json.loads(row['payload_json'])
        observed = json.loads(row['observation_json'])
        metrics = json.loads(row['metrics_json'])
        lease = timestamp(row['lease_expires_at'])
        return (row['status'] == 'running' and row['stage'] == 'prepare'
                and row['generation'] > 0 and not row['external_intent']
                and not row['has_operation'] and not row['has_cloud_intent']
                and payload.get('kind') in LOCAL_RENDERERS
                and observed.get('phase') == 'render'
                and metrics.get('backend') in {'cpu', 'gpu'}
                and row['pid'] and row['pid'] > 0
                and fresh(row['worker_seen'], now)
                and fresh(row['node_seen'], now, 90)
                and lease is not None and (lease - now).total_seconds() >= 90)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def assess(conn, unit, *, restarting=False, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = conn.execute("""SELECT j.*, w.pid, w.last_seen_at AS worker_seen,
        w.observation_json, n.last_seen_at AS node_seen, n.metrics_json,
        EXISTS(SELECT 1 FROM pipeline_operation o
            WHERE o.job_id=j.id AND o.generation=j.generation) AS has_operation,
        EXISTS(SELECT 1 FROM pipeline_cloud_intent o
            WHERE o.job_id=j.id AND o.generation=j.generation) AS has_cloud_intent
        FROM pipeline_job j
        LEFT JOIN pipeline_worker w ON w.node_id=j.node_id AND w.worker_id=j.worker_id
        LEFT JOIN pipeline_node n ON n.node_id=j.node_id
        WHERE j.status IN ('running','legacy_running') ORDER BY j.id""").fetchall()
    waiting, continuing = [], []
    for raw in rows:
        row = dict(raw)
        if not matches(unit, row):
            continue
        # Keep credential-bearing payloads and claim hashes out of diagnostics.
        item = {key: row[key] for key in (*IDENTITY, 'source_id', 'channel', 'stage',
                                        'status', 'lease_expires_at')}
        if unit == COORDINATOR and restarting and render_can_continue(row, now):
            continuing.append(item)
        else:
            waiting.append(item)
    return {'pending': waiting, 'continuing_renders': continuing}


def resumed(conn, handed, ready_at, *, now=None):
    """Require a new heartbeat or durable terminal result; never rewrite a job."""
    now = now or dt.datetime.now(dt.timezone.utc)
    waiting, attention = [], []
    for old in handed:
        raw = conn.execute("""SELECT j.*, w.last_seen_at AS worker_seen
            FROM pipeline_job j LEFT JOIN pipeline_worker w
            ON w.node_id=j.node_id AND w.worker_id=j.worker_id WHERE j.id=?""",
                           (old['id'],)).fetchone()
        row = dict(raw) if raw else None
        if not row or any(row[key] != old[key] for key in IDENTITY):
            waiting.append(old['id'])
            continue
        if row['status'] in {'completed', 'failed', 'unknown', 'cancelled'}:
            if row['status'] != 'completed':
                attention.append({'id': row['id'], 'status': row['status']})
            continue  # Existing failure/quarantine facts remain authoritative.
        heartbeat, lease = timestamp(row['worker_seen']), timestamp(row['lease_expires_at'])
        if (row['status'] != 'running' or heartbeat is None or heartbeat < ready_at
                or not fresh(row['worker_seen'], now) or lease is None or lease <= now):
            waiting.append(old['id'])
    return {'waiting': waiting, 'attention': attention}
