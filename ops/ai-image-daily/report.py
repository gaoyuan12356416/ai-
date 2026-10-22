#!/usr/bin/env python3
"""Read-only collection and durable, frozen multi-object publication."""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.request import Request, urlopen

from collector import Catalog, TZ, collect_days, collect_lineage
from analysis import build_report
from render import build_bundle

PREFIX = 'codex-artifacts/html/ai-image-daily'
LEGACY_INDEX = 'codex-artifacts/html/dramawave-ai-image-daily/index.html'
DOMAIN = 'https://advertising-1306474899.cos.ap-hongkong.myqcloud.com'
STATE = '/mnt/data-disk/ai-image-daily-v3'
DISK_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
NO_CACHE = 'no-cache, max-age=0, must-revalidate'
IMMUTABLE_CACHE = 'public, max-age=31536000, immutable'
HEADER_FIELDS = {'content-type': 'ContentType', 'cache-control': 'CacheControl',
                 'content-encoding': 'ContentEncoding', 'content-disposition': 'ContentDisposition',
                 'content-language': 'ContentLanguage', 'expires': 'Expires'}


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def write_json(path, value):
    atomic_bytes(path, json_bytes(value))


def load_json(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def sha(data):
    return hashlib.sha256(data).hexdigest()


def config(path):
    env = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            env[key.strip()] = value.strip().strip(chr(34)).strip(chr(39))
    return env


def check_key(key):
    if not isinstance(key, str) or not key or '\\' in key or '?' in key or '#' in key or '%' in key:
        raise ValueError('cos_key_out_of_scope')
    if any(part in ('..', '.') for part in PurePosixPath(key).parts) or str(PurePosixPath(key)) != key:
        raise ValueError('cos_key_out_of_scope')
    if key != LEGACY_INDEX and not key.startswith(PREFIX + '/'):
        raise ValueError('cos_key_out_of_scope')


def metadata(headers):
    result = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in HEADER_FIELDS and value is not None:
            result[HEADER_FIELDS[lower]] = str(value)
        elif lower.startswith('x-cos-meta-'):
            result.setdefault('Metadata', {})[lower] = str(value)
    return result


def fingerprint(obj):
    return None if obj is None else dict(sha256=sha(obj['data']), metadata=obj['metadata'])


def same(obj, data, meta):
    return obj is not None and obj['data'] == data and obj['metadata'] == meta


class Publisher:
    def __init__(self, env):
        from qcloud_cos import CosConfig, CosS3Client
        if env['COS_BUCKET'] != 'advertising-1306474899' or env['COS_REGION'] != 'ap-hongkong':
            raise ValueError('unexpected_cos_bucket_or_region')
        self.client = CosS3Client(CosConfig(Region=env['COS_REGION'], SecretId=env['COS_SECRET_ID'], SecretKey=env['COS_SECRET_KEY']))
        self.bucket = env['COS_BUCKET']

    def url(self, key):
        check_key(key)
        return DOMAIN + '/' + key

    def get(self, key):
        from qcloud_cos.cos_exception import CosServiceError
        check_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            stream = response['Body'].get_raw_stream()
            try:
                data = stream.read()
            finally:
                stream.close()
            return dict(data=data, metadata=metadata(response))
        except CosServiceError as error:
            if error.get_status_code() == 404:
                return None
            raise

    def put(self, key, data, meta):
        check_key(key)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **meta)

    def verify(self, key, data, meta, decoded=None):
        expected = sha(data)
        request = Request(self.url(key) + '?verify=' + expected[:20],
                          headers={'Cache-Control': 'no-cache', 'Accept-Encoding': 'gzip'})
        with urlopen(request, timeout=35) as response:
            actual = response.read()
            actual_meta = metadata(dict(response.headers.items()))
            if response.status != 200 or sha(actual) != expected or actual_meta != meta:
                raise RuntimeError('public_cos_readback_failed')
        raw = gzip.decompress(actual) if meta.get('ContentEncoding') == 'gzip' else actual
        if decoded is not None and raw != decoded:
            raise RuntimeError('public_decoded_readback_failed')
        return dict(url=self.url(key), stored_sha256=expected, sha256=sha(raw),
                    bytes=len(raw), stored_bytes=len(data), http_status=200)


def legacy_redirect():
    target = DOMAIN + '/' + PREFIX + '/index.html'
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>三产品 AI 图片日报</title><p>日报已升级：'
            '<a href="' + target + '">打开三产品最新日报</a></p>'
            '<script>location.replace(' + json.dumps(target) + '+location.hash);</script></html>').encode('utf-8')


def phase(key):
    if key.startswith(PREFIX + '/assets/'):
        return 0
    if key.startswith(PREFIX + '/daily/'):
        return 1
    if key == PREFIX + '/history.json':
        return 2
    if key == PREFIX + '/status.json':
        return 3
    if key == PREFIX + '/index.html':
        return 4
    if key == LEGACY_INDEX:
        return 5
    raise ValueError('unexpected_bundle_key')


def freeze_bundle(state, run, report, history_entry, history, bundle, version):
    """Persist both byte representations and their manifest before any COS write."""
    state, run = Path(state).resolve(), Path(run).resolve()
    if (state / 'runs').resolve() not in run.parents or (state / 'pending.json').exists():
        raise ValueError('unsafe_freeze_state')
    date = report['report_date']
    dt.date.fromisoformat(date)
    if report.get('schema_version') != 3 or not re.fullmatch(r'[A-Za-z0-9_-]+', version):
        raise ValueError('invalid_bundle_version')
    required = {'index.html', 'daily/' + date + '/index.html', 'history.json'}
    if not required.issubset(bundle):
        raise ValueError('incomplete_static_bundle')
    objects = {PREFIX + '/' + key: value for key, value in bundle.items()}
    # Live health is intentionally outside the frozen content transaction. A
    # failed attempt may update it while all report bytes stay recoverable.
    objects.pop(PREFIX + '/status.json', None)
    objects[LEGACY_INDEX] = dict(data=legacy_redirect(), content_type='text/html; charset=utf-8',
                               immutable=False, compress=True)
    artifacts = []
    for index, (key, artifact) in enumerate(sorted(objects.items(), key=lambda pair: (phase(pair[0]), pair[0]))):
        check_key(key)
        rank = phase(key)
        immutable = rank in (0, 1)
        if bool(artifact['immutable']) != immutable:
            raise ValueError('incorrect_bundle_immutability')
        if rank == 1 and key != PREFIX + '/daily/' + date + '/index.html':
            raise ValueError('unexpected_daily_snapshot')
        raw = artifact['data']
        if not isinstance(raw, bytes):
            raise ValueError('bundle_bytes_required')
        compress = bool(artifact.get('compress'))
        if key.endswith(('.html', '.json', '.js')) and not compress:
            raise ValueError('text_artifact_requires_gzip')
        encoded = gzip.compress(raw, compresslevel=6, mtime=0) if compress else raw
        meta = dict(ContentType=artifact['content_type'], ContentDisposition='inline',
                    CacheControl=IMMUTABLE_CACHE if immutable else NO_CACHE)
        if compress:
            meta['ContentEncoding'] = 'gzip'
        raw_file, stored_file = 'objects/%04d.raw' % index, 'objects/%04d.stored' % index
        atomic_bytes(run / raw_file, raw)
        atomic_bytes(run / stored_file, encoded)
        artifacts.append(dict(key=key, phase=rank, immutable=immutable, raw_file=raw_file,
            stored_file=stored_file, sha256=sha(raw), stored_sha256=sha(encoded), bytes=len(raw),
            stored_bytes=len(encoded), metadata=meta))
    write_json(run / 'report.json', report)
    manifest = dict(schema_version=3, report_date=date, version=version, history_entry=history_entry,
        history=history, report_sha256=sha((run / 'report.json').read_bytes()), objects=artifacts)
    write_json(run / 'manifest.json', manifest)
    write_json(state / 'pending.json', dict(schema_version=3, run=str(run), report_date=date,
                                           manifest_sha256=sha((run / 'manifest.json').read_bytes())))
    return manifest


def read_frozen(state):
    pending = load_json(state / 'pending.json')
    if not pending or pending.get('schema_version') != 3:
        raise ValueError('v3_prepared_manifest_required')
    run = Path(pending['run']).resolve()
    if (state / 'runs').resolve() not in run.parents:
        raise ValueError('pending_run_outside_state')
    manifest_data = (run / 'manifest.json').read_bytes()
    if sha(manifest_data) != pending['manifest_sha256']:
        raise ValueError('pending_manifest_changed')
    manifest = json.loads(manifest_data)
    if manifest.get('schema_version') != 3 or manifest['report_date'] != pending['report_date']:
        raise ValueError('pending_manifest_mismatch')
    if sha((run / 'report.json').read_bytes()) != manifest['report_sha256']:
        raise ValueError('pending_report_changed')
    seen = set()
    for item in manifest['objects']:
        key = item['key']
        check_key(key)
        if key in seen or phase(key) != item['phase'] or item['immutable'] != (phase(key) in (0, 1)):
            raise ValueError('invalid_manifest_object')
        seen.add(key)
        for field, digest in [('raw_file', 'sha256'), ('stored_file', 'stored_sha256')]:
            path = (run / item[field]).resolve()
            if run not in path.parents or sha(path.read_bytes()) != item[digest]:
                raise ValueError('pending_artifact_changed')
        raw, encoded = (run / item['raw_file']).read_bytes(), (run / item['stored_file']).read_bytes()
        decoded = gzip.decompress(encoded) if item['metadata'].get('ContentEncoding') == 'gzip' else encoded
        if decoded != raw or len(raw) != item['bytes'] or len(encoded) != item['stored_bytes']:
            raise ValueError('pending_encoding_mismatch')
    if not {PREFIX + '/index.html', PREFIX + '/daily/' + manifest['report_date'] + '/index.html',
            PREFIX + '/history.json', LEGACY_INDEX}.issubset(seen):
        raise ValueError('incomplete_frozen_manifest')
    return run, manifest


def object_json(obj):
    data = obj['data']
    return json.loads(gzip.decompress(data) if obj['metadata'].get('ContentEncoding') == 'gzip' else data)


def backup_mutable(state, run, manifest, observed, previous_health=None):
    backup = state / 'backups' / run.name
    path = backup / 'rollback.json'
    installed = {item['key']: dict(sha256=item['stored_sha256'], metadata=item['metadata'])
                 for item in manifest['objects'] if not item['immutable']}
    if path.exists():
        meta = load_json(path)
        if meta['installed'] != installed:
            raise ValueError('rollback_manifest_changed')
        for record in meta['previous']:
            if record['fingerprint'] is not None:
                previous = (backup / record['file']).read_bytes()
                if sha(previous) != record['fingerprint']['sha256']:
                    raise ValueError('rollback_backup_changed')
        return backup, meta
    previous = []
    for index, key in enumerate(installed):
        obj = observed[key]
        filename = '%04d.previous' % index
        if obj is not None:
            atomic_bytes(backup / filename, obj['data'])
        previous.append(dict(key=key, file=filename, fingerprint=fingerprint(obj)))
    previous_success = None
    if observed.get(PREFIX + '/index.html') is not None and previous_health is not None:
        health = object_json(previous_health)
        prior = health.get('last_success') if health.get('state') == 'failed' else health
        if prior and prior.get('report_date') and prior.get('version') and prior.get('generated_at'):
            previous_success = {key: prior[key] for key in ('report_date', 'generated_at', 'version')}
            dt.date.fromisoformat(previous_success['report_date'])
    meta = dict(schema_version=3, installed=installed, previous=previous,
                previous_success=previous_success,
                manifest_sha256=sha((run / 'manifest.json').read_bytes()))
    write_json(path, meta)
    return backup, meta


def publish_pending(state, publisher):
    state = Path(state).resolve()
    run, manifest = read_frozen(state)
    existing_history = load_json(state / 'history.json', [])
    if existing_history and manifest['report_date'] < max(row['date'] for row in existing_history):
        raise ValueError('pending_date_older_than_latest')
    # Complete conflict preflight happens before even the first asset write.
    observed = {}
    for item in manifest['objects']:
        current = publisher.get(item['key'])
        observed[item['key']] = current
        encoded = (run / item['stored_file']).read_bytes()
        if item['immutable'] and current is not None and not same(current, encoded, item['metadata']):
            raise ValueError('immutable_snapshot_or_asset_conflict')
    remote_status = publisher.get(PREFIX + '/status.json')
    if remote_status is not None and (object_json(remote_status).get('report_date') or '') > manifest['report_date']:
        raise ValueError('pending_date_older_than_latest')
    remote_history = observed[PREFIX + '/history.json']
    if remote_history is not None:
        previous_dates = {row['date'] for row in object_json(remote_history)}
        if not previous_dates.issubset({row['date'] for row in manifest['history']}):
            raise ValueError('public_history_regression')
    backup, rollback = backup_mutable(state, run, manifest, observed, remote_status)
    originals = {row['key']: row['fingerprint'] for row in rollback['previous']}
    for key, expected in originals.items():
        if fingerprint(observed[key]) not in (expected, rollback['installed'][key]):
            raise ValueError('public_mutable_object_drift')
    receipts = {}
    for item in sorted(manifest['objects'], key=lambda row: (row['phase'], row['key'])):
        key = item['key']
        raw, encoded = (run / item['raw_file']).read_bytes(), (run / item['stored_file']).read_bytes()
        current = publisher.get(key)
        if item['immutable']:
            if current is not None and not same(current, encoded, item['metadata']):
                raise ValueError('immutable_snapshot_or_asset_conflict')
        elif fingerprint(current) not in (originals[key], rollback['installed'][key]):
            raise ValueError('public_mutable_object_drift')
        if not same(current, encoded, item['metadata']):
            publisher.put(key, encoded, item['metadata'])
        receipts[key] = publisher.verify(key, encoded, item['metadata'], raw)
        write_json(run / 'publish-progress.json', dict(verified=receipts))
    report = load_json(run / 'report.json')
    health_receipt = publish_health(publisher, dict(state='published', schema_version=3,
        report_date=manifest['report_date'], generated_at=report['generated_at'], version=manifest['version']))
    write_json(state / 'history.json', manifest['history'])
    write_json(run / 'publish-receipt.json', dict(objects=receipts, backup=str(backup),
        health=health_receipt, manifest_sha256=sha((run / 'manifest.json').read_bytes())))
    receipt = receipts[PREFIX + '/index.html']
    status = dict(state='published', schema_version=3, report_date=manifest['report_date'],
        generated_at=report['generated_at'], published_at=dt.datetime.now(TZ).isoformat(timespec='seconds'),
        run=str(run), url=receipt['url'], sha256=receipt['sha256'], stored_bytes=receipt['stored_bytes'],
        validation=report['validation'], summary=report['summary'], backup=str(backup),
        object_count=len(receipts), version=manifest['version'])
    write_json(state / 'status.json', status)
    (state / 'pending.json').unlink()
    print(json.dumps(status, ensure_ascii=False), flush=True)
    return status


def publish_health(publisher, value):
    raw = json_bytes(value)
    encoded = gzip.compress(raw, compresslevel=6, mtime=0)
    meta = dict(ContentType='application/json; charset=utf-8', ContentDisposition='inline',
                ContentEncoding='gzip', CacheControl=NO_CACHE)
    key = PREFIX + '/status.json'
    if not same(publisher.get(key), encoded, meta):
        publisher.put(key, encoded, meta)
    return publisher.verify(key, encoded, meta, raw)


def publish_failure(args, failure):
    """Best-effort health only; never replace a report or change its pending run."""
    if args.prepare_only or args.restore_public:
        return
    try:
        publisher = Publisher(config(args.env_file))
        current = publisher.get(PREFIX + '/status.json')
        health = object_json(current) if current else {}
        last = health.get('last_success') if health.get('state') == 'failed' else health
        last = {key: last.get(key) for key in ('report_date', 'generated_at', 'version')} if last else None
        pending = load_json(Path(args.state) / 'pending.json', {})
        attempted = pending.get('report_date') or args.date or (dt.datetime.now(TZ).date() - dt.timedelta(days=1)).isoformat()
        publish_health(publisher, dict(state='failed', schema_version=3,
            report_date=last.get('report_date') if last else None, last_success=last,
            attempted_date=attempted, failed_at=failure['at'], error_type=failure['error_type']))
    except Exception:
        # A second transport error must not mask the durable original failure.
        pass


def restore_public(state, publisher, backup):
    state, backup = Path(state).resolve(), Path(backup).resolve()
    if (state / 'backups').resolve() not in backup.parents:
        raise ValueError('backup_outside_state')
    meta = load_json(backup / 'rollback.json')
    if not meta or meta.get('schema_version') != 3:
        raise ValueError('v3_backup_required')
    # A drift anywhere prevents a partial rollback.
    for record in meta['previous']:
        key = record['key']
        check_key(key)
        if phase(key) < 2:
            raise ValueError('rollback_immutable_object_forbidden')
        if record['fingerprint'] is not None:
            path = (backup / record['file']).resolve()
            if backup not in path.parents or sha(path.read_bytes()) != record['fingerprint']['sha256']:
                raise ValueError('rollback_backup_changed')
        if fingerprint(publisher.get(key)) not in (meta['installed'][key], record['fingerprint']):
            raise ValueError('rollback_public_object_drift')
    receipts, retained = {}, []
    for record in sorted(meta['previous'], key=lambda row: -phase(row['key'])):
        key, previous = record['key'], record['fingerprint']
        if previous is None:
            retained.append(key)
            continue
        data = (backup / record['file']).read_bytes()
        current = publisher.get(key)
        if fingerprint(current) not in (meta['installed'][key], previous):
            raise ValueError('rollback_public_object_drift')
        if not same(current, data, previous['metadata']):
            publisher.put(key, data, previous['metadata'])
        receipts[key] = publisher.verify(key, data, previous['metadata'])
    # Health is not content and may have changed during a failed attempt. Restore
    # its saved successful state only when the prior v3 homepage was restored.
    # On a first installation there is no prior v3 homepage: the new one remains
    # available, so its current health stays valid even if the old alias reverted.
    previous_home = next((row for row in meta['previous'] if row['key'] == PREFIX + '/index.html'), None)
    health_receipt = None
    if previous_home and previous_home['fingerprint'] is not None and meta.get('previous_success'):
        health_receipt = publish_health(publisher, dict(state='published', schema_version=3,
                                                        **meta['previous_success']))
    result = dict(restored=receipts, retained_new_objects=retained, immutable_objects_preserved=True,
                  health_restored=health_receipt, health_preserved=health_receipt is None)
    write_json(backup / 'restore-receipt.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def run(args):
    import fcntl
    from thumbnails import prepare_thumbnails
    state = Path(args.state).resolve()
    if Path('/mnt/data-disk') not in state.parents:
        raise ValueError('state_must_be_on_data_disk')
    if subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk'], text=True).strip() != DISK_UUID:
        raise ValueError('data_disk_not_mounted')
    args._state_validated = True
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"state":"skipped","reason":"already_running"}')
            return
        if args.restore_public:
            return restore_public(state, Publisher(config(args.env_file)), args.restore_public)
        if (state / 'pending.json').exists():
            frozen_run, manifest = read_frozen(state)
            if args.prepare_only:
                status = dict(state='prepared', report_date=manifest['report_date'], run=str(frozen_run), reused=True)
                print(json.dumps(status), flush=True)
                return status
            return publish_pending(state, Publisher(config(args.env_file)))
        if args.publish_prepared:
            raise ValueError('no_prepared_manifest')
        today = dt.datetime.now(TZ).date()
        date = args.date or (today - dt.timedelta(days=1)).isoformat()
        if dt.date.fromisoformat(date) >= today:
            raise ValueError('complete_natural_day_required')
        history = load_json(state / 'history.json', [])
        if history and date < max(entry['date'] for entry in history):
            raise ValueError('report_date_older_than_latest')
        if any(entry['date'] == date for entry in history):
            print(json.dumps(dict(state='skipped', reason='date_already_published', report_date=date)))
            return
        generated = dt.datetime.now(TZ)
        run_dir = state / 'runs' / generated.strftime('%Y%m%dT%H%M%S%f')
        run_dir.mkdir(parents=True, exist_ok=False)
        env = config(args.env_file)
        catalog = Catalog(env)
        print(json.dumps(dict(phase='lineage', report_date=date)), flush=True)
        mapped, unresolved, scope, counts = collect_lineage(catalog, args.source_db, args.sync_db, multi_product=True)
        write_json(run_dir / 'lineage.json', dict(materials=mapped, unresolved=unresolved, scope=scope, counts=counts))
        print(json.dumps(dict(phase='lineage_complete', counts=counts, unresolved=len(unresolved))), flush=True)
        end = dt.date.fromisoformat(date)
        dates = [(end - dt.timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
        facts = collect_days(catalog, mapped, scope, dates)
        write_json(run_dir / 'facts.json', facts)
        optimizers = sorted({str(row['optimizer']) for row in facts if row['kind'] == 'ai'})
        names = {}
        if optimizers:
            rows = catalog.query("SELECT JSON_OBJECT('id',id,'name',username) FROM admin_users WHERE id IN (" +
                                 ','.join(str(int(i)) for i in optimizers) + ')')
            names = {str(row['id']): row['name'] for row in rows}
        report = build_report(mapped, unresolved, facts, date, generated.isoformat(timespec='seconds'),
                              names, 'scheduled' if history else 'initial', scope=scope)
        report['query_count'] = catalog.queries
        report['home_url'] = DOMAIN + '/' + PREFIX + '/index.html'
        thumbs, thumb_status = prepare_thumbnails(report, state / 'thumbs', DOMAIN + '/' + PREFIX)
        report['thumbnails'] = thumb_status
        version = date.replace('-', '') + '-' + sha(json_bytes(report))[:16]
        entry = dict(date=date, url=DOMAIN + '/' + PREFIX + '/daily/' + date + '/index.html',
                     generated_at=report['generated_at'], snapshot_kind=report['snapshot_kind'])
        new_history = sorted(history + [entry], key=lambda row: row['date'], reverse=True)
        bundle = build_bundle(report, new_history, DOMAIN + '/' + PREFIX, version)
        if set(bundle).intersection(thumbs):
            raise ValueError('thumbnail_bundle_key_collision')
        bundle.update(thumbs)
        manifest = freeze_bundle(state, run_dir, report, entry, new_history, bundle, version)
        write_json(run_dir / 'validation.json', report['validation'])
        if args.prepare_only:
            status = dict(state='prepared', report_date=date, run=str(run_dir),
                          object_count=len(manifest['objects']), version=version)
            print(json.dumps(status), flush=True)
            return status
        return publish_pending(state, Publisher(env))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', default=STATE)
    parser.add_argument('--env-file', default='/root/drama_material_service/.env')
    parser.add_argument('--source-db', default='/root/drama_material_service/data/drama_material_jobs.sqlite3')
    parser.add_argument('--sync-db', default='/mnt/data-disk/youtube-cover-material-sync/sync.sqlite3')
    parser.add_argument('--date')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare-only', action='store_true')
    mode.add_argument('--publish-prepared', action='store_true')
    mode.add_argument('--restore-public')
    args = parser.parse_args()
    try:
        run(args)
    except Exception as error:
        failure = dict(state='failed', at=dt.datetime.now(TZ).isoformat(timespec='seconds'), error_type=type(error).__name__)
        if isinstance(error, (ValueError, RuntimeError)) and re.fullmatch(r'[a-zA-Z0-9_.-]+', str(error)):
            failure['reason'] = str(error)
        state = Path(args.state)
        if getattr(args, '_state_validated', False) and state.exists() and Path('/mnt/data-disk') in state.resolve().parents:
            write_json(state / 'last-failure.json', failure)
            publish_failure(args, failure)
        print(json.dumps(failure), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
