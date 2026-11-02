"""Daily complete-window refresh; python -m features.socialkit_analytics.refresh."""
import argparse
import contextlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collector import CollectionError, GatedReader, SocialKitSource, collect_days, days_between
from .schema import EFFECT_METRICS, PUBLICATION_METRICS
from .storage import (SnapshotBuilder, WarehouseWriter, atomic_json, cleanup_warehouse, publish_snapshot,
                      read_manifest, verify_warehouse)

DEFAULT_ROOT = '/mnt/data-disk/socialkit-analytics'
BEIJING = timezone(timedelta(hours=8))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def default_range(now=None):
    now = now or datetime.now(timezone.utc)
    yesterday = now.astimezone(BEIJING).date() - timedelta(days=1)
    return (yesterday - timedelta(days=13)).isoformat(), yesterday.isoformat()


def load_environment(files):
    env = dict(os.environ)
    for filename in files:
        for raw in Path(filename).read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env


def ensure_data_mount(root):
    root = Path(root).resolve()
    mount = Path('/mnt/data-disk')
    if os.name == 'posix' and (root == mount or mount in root.parents):
        check = subprocess.run(['findmnt', '-n', '-o', 'UUID', '--target', str(mount)],
                               capture_output=True, text=True, timeout=5)
        if check.returncode or check.stdout.strip() != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8' or not os.path.ismount(str(mount)):
            raise CollectionError('required data disk is not mounted at /mnt/data-disk')


@contextlib.contextmanager
def refresh_lock(root):
    path = Path(root) / 'refresh.lock'
    with path.open('a+b') as handle:
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CollectionError('another SocialKit refresh is already running') from None
        else:
            import msvcrt
            handle.write(b'0')
            handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise CollectionError('another SocialKit refresh is already running') from None
        yield


def day_control(day):
    result = dict(date=day['date'])
    for kind, fields in [('effects', EFFECT_METRICS), ('publications', PUBLICATION_METRICS)]:
        result[kind] = dict(rows=len(day[kind]), **{field: sum(row[field] for row in day[kind]) for field in fields})
    return result


def run_refresh(env, root, start, end, *, reader=None, source=None, writer=None,
                collect=collect_days, verify=verify_warehouse):
    days = days_between(start, end)
    ensure_data_mount(root)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with refresh_lock(root):
        version = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
        started = utc_now()
        old = read_manifest(root)
        status = dict(version=version, refresh_status='running', last_attempt=started,
                      last_success=old.get('generated_at') if old else None, range=dict(start=start, end=end), completed_dates=[])
        atomic_json(root / 'status.json', status)
        builder = None
        active_writer = None
        try:
            with contextlib.ExitStack() as stack:
                active_reader = reader or GatedReader(env)
                active_source = source or stack.enter_context(SocialKitSource(env))
                active_writer = writer or stack.enter_context(WarehouseWriter(env))
                active_writer.start(version, start, end, started)
                builder = SnapshotBuilder(root, version, old)
                date_manifest = dict((old or {}).get('date_manifest', {}))
                controls = []
                for day in collect(active_reader, active_source, start, end):
                    if day['date'] not in days or day['date'] in status['completed_dates']:
                        raise CollectionError('refresh returned an unexpected or duplicate day')
                    builder.write_day(day)
                    active_writer.write_day(version, day)
                    checked_control = day_control(day)
                    controls.append(checked_control)
                    date_manifest[day['date']] = dict(version=version, refreshed_at=started, state='complete',
                                                     effect_rows=len(day['effects']), publication_rows=len(day['publications']),
                                                     source_rows=day['source_rows'], source_updated_at=day['source_updated_at'],
                                                     quality=day['quality'], controls=checked_control)
                    status['completed_dates'].append(day['date'])
                    atomic_json(root / 'status.json', status)
                    print(json.dumps(dict(event='day_staged', date=day['date'], effect_rows=len(day['effects']),
                                          publication_rows=len(day['publications']))), flush=True)
                if status['completed_dates'] != days:
                    raise CollectionError('refresh did not complete every requested date')
                verify(active_reader, version, controls)
                completed = utc_now()
                covered = sorted(date_manifest)
                quality = dict(effect_rows=sum(v['effect_rows'] for v in date_manifest.values()),
                               publication_rows=sum(v['publication_rows'] for v in date_manifest.values()),
                               unmatched_rows=sum(v['quality']['unmatched_rows'] for v in date_manifest.values()),
                               unmatched={field: sum(v['quality']['unmatched'][field] for v in date_manifest.values())
                                          for field in EFFECT_METRICS})
                source_updated = max((v.get('source_updated_at', '') for v in date_manifest.values()), default='')
                manifest = dict(version=version, database=str(builder.path.relative_to(root)).replace('\\', '/'),
                                db_path=str(builder.path), generated_at=completed, refreshed_at=completed,
                                range=dict(start=covered[0], end=covered[-1]), range_start=covered[0], range_end=covered[-1],
                                refresh_range=dict(start=start, end=end), date_manifest=date_manifest,
                                coverage={kind: {day: 'complete' for day in covered} for kind in ['effects', 'publications']},
                                quality=quality, source_updated_at=source_updated, source_status='available',
                                refresh_status='complete', effect_timezone='UTC', publication_timezone='account_local')
                builder.finish(manifest)
                active_writer.finish(version, manifest)
                publish_snapshot(root, manifest, old)
                status.update(refresh_status='complete', last_success=completed, finished_at=completed)
                with contextlib.suppress(OSError):
                    atomic_json(root / 'status.json', status)
                try:
                    from .query import warm_snapshot
                    status['precompute'] = warm_snapshot(root, manifest['version'])
                except Exception as precompute_error:
                    status['precompute_warning'] = type(precompute_error).__name__
                try:
                    status['cleanup'] = cleanup_warehouse(active_reader, active_writer, [manifest, old])
                    if status['cleanup']['incomplete']:
                        status['cleanup_warning'] = 'cleanup_budget_exhausted'
                        print(json.dumps(dict(event='cleanup_incomplete', **status['cleanup'])), flush=True)
                except Exception as cleanup_error:
                    status['cleanup_warning'] = type(cleanup_error).__name__
                # current.json is the serving commit point; later housekeeping must not undo success.
                with contextlib.suppress(OSError):
                    atomic_json(root / 'status.json', status)
                return manifest
        except Exception as exc:
            # Exception text can contain SQL data or connector details; publish a stable code only.
            error_code = type(exc).__name__
            if builder:
                builder.close()
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.failed(version, error_code)
            status.update(refresh_status='failed', finished_at=utc_now(), error_code=error_code)
            atomic_json(root / 'status.json', status)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Refresh SocialKit effect/publication facts and publish a verified cache')
    parser.add_argument('--root', default=DEFAULT_ROOT)
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--env-file', action='append', default=[])
    args = parser.parse_args(argv)
    if bool(args.start) != bool(args.end):
        parser.error('--start and --end must be provided together')
    start, end = (args.start, args.end) if args.start else default_range()
    try:
        result = run_refresh(load_environment(args.env_file), args.root, start, end)
    except Exception as exc:
        print(json.dumps(dict(event='refresh_failed', error_code=type(exc).__name__)), flush=True)
        return 1
    print(json.dumps(dict(event='refresh_complete', version=result['version'], range=result['refresh_range'],
                          quality=result['quality'])), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
