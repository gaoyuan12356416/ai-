"""Hourly report refresh. Group summaries run independently at 11:30 Beijing."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .refresh import (BEIJING, DEFAULT_ROOT, SAFE_DIAGNOSTICS, CollectionError,
                      load_environment, run_refresh, utc_now)
from .storage import atomic_json, read_manifest

ALERT_ROOT = '/mnt/data-disk/socialkit-gap-alert'


def scheduled_range(now, manifest=None):
    """Return UTC dates plus the most recent due daily reconciliation, if any."""
    if now.tzinfo is None:
        raise ValueError('scheduled time must have a timezone')
    beijing = now.astimezone(BEIJING)
    today = now.astimezone(timezone.utc).date()
    due = beijing.date() - timedelta(days=int(beijing.hour < 11))
    last_full = (manifest or {}).get('last_full_refresh_beijing_date', '')
    full_day = due.isoformat() if last_full < due.isoformat() else None
    return ((today - timedelta(days=14 if full_day else 2)).isoformat(),
            today.isoformat(), full_day)


def run_hourly(env, root=DEFAULT_ROOT, alert_root=ALERT_ROOT, *, now=None,
               refresh=run_refresh):
    now = now or datetime.now(timezone.utc)
    start, end, full_day = scheduled_range(now, read_manifest(root))
    manifest = refresh(env, root, start, end, full_refresh_beijing_date=full_day)
    state = dict(version=manifest['version'], started_at=now.isoformat(),
                 refresh_status='complete', refresh_range=dict(start=start, end=end),
                 full_refresh_beijing_date=full_day, alert_status='scheduled_daily_1130_beijing')
    status_path = Path(root) / 'hourly-status.json'
    atomic_json(status_path, state)
    state['finished_at'] = utc_now()
    atomic_json(status_path, state)
    print(json.dumps(dict(event='hourly_complete', **state)), flush=True)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=DEFAULT_ROOT)
    parser.add_argument('--alert-root', default=ALERT_ROOT)
    parser.add_argument('--env-file', action='append', default=[])
    args = parser.parse_args(argv)
    try:
        result = run_hourly(load_environment(args.env_file), args.root, args.alert_root)
    except Exception as exc:
        failure = dict(event='hourly_refresh_failed', error_code=type(exc).__name__)
        if isinstance(exc, CollectionError) and str(exc) in SAFE_DIAGNOSTICS:
            failure['error_message'] = str(exc)
        print(json.dumps(failure), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
