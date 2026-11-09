"""Hourly derived snapshot: verified local revenue + bounded read-only drama labels."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path

from .core import build
from features.social_analytics.warehouse_reader import current, connect, DEFAULT_ROOT as WAREHOUSE_ROOT
from features.social_analytics.warehouse_refresh import Reader, environment, atomic_json

DEFAULT_ROOT = '/mnt/data-disk/x-channel-tag-revenue'


def collect_labels(reader, ids):
    rows = []
    for offset in range(0, len(ids), 100):
        values = ','.join('0x' + value.encode('utf-8').hex() for value in ids[offset:offset + 100])
        statement = """SELECT DISTINCT HEX(JSON_OBJECT('content_id',content_id,'app_id',app_id,
          'language',language,'series_code',series_code,'labels',labels))
          FROM kunlunads_dev.ads_drama_resource FORCE INDEX(content_id)
          WHERE app_id=1479 AND content_id IN (%s) LIMIT 50001""" % values
        batch = reader.query([statement], timeout=60)
        if len(batch) >= 50001:
            raise RuntimeError('label_query_capacity_exceeded')
        rows.extend(batch)
    return rows


def refresh(root, warehouse_root, env_path):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'refresh.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'already_running'}
        try:
            manifest, path = current(warehouse_root)
            if manifest.get('catalog_errors', {}).get('x:2116'):
                raise RuntimeError('x_catalog_unavailable')
            now = datetime.now(timezone.utc)
            end = min(manifest['end'], (now.date() - timedelta(days=1)).isoformat())
            with closing(connect(path)) as db:
                catalog = [json.loads(r[0]) for r in db.execute("SELECT payload FROM catalog WHERE json_extract(payload,'$.platform')='x'")]
                metrics = [json.loads(r[0]) for r in db.execute("SELECT payload FROM effects WHERE platform='x' AND dt<=?", (end,))]
            ids = sorted({r['drama'] for r in catalog if r.get('drama')})
            resources = collect_labels(Reader(environment(env_path)), ids)
            result = build(catalog, metrics, resources, end)
            covered = set(manifest.get('coverage', {}).get('x:2116', []))
            first = datetime.fromisoformat(manifest['start']).date()
            last = datetime.fromisoformat(end).date()
            missing = [(first + timedelta(days=n)).isoformat() for n in range((last-first).days+1)
                       if (first + timedelta(days=n)).isoformat() not in covered]
            version = now.strftime('%Y%m%dT%H%M%S%fZ')
            result['meta'] = dict(version=version, generated_at=now.isoformat(),
                                  source_version=manifest['version'], source_generated_at=manifest['generated_at'],
                                  start=manifest['start'], end=end, timezone='UTC', currency='USD',
                                  app_id='1479', allocation='equal_distinct_labels',
                                  label_basis='current_catalog', label_updated_at=now.isoformat(),
                                  missing_dates=missing, source='ai_backend_confirmed_x_publications')
            directory = root / 'snapshots'
            directory.mkdir(exist_ok=True)
            filename = 'snapshots/' + version + '.json'
            atomic_json(root / filename, result)
            old = json.loads((root / 'current.json').read_text()) if (root / 'current.json').exists() else {}
            atomic_json(root / 'current.json', dict(file=filename, version=version, previous=old.get('file')))
            atomic_json(root / 'status.json', dict(status='ok', attempted_at=now.isoformat()))
            keep = {filename, old.get('file')}
            for snapshot in directory.glob('*.json'):
                if str(snapshot.relative_to(root)) not in keep:
                    snapshot.unlink()
            return dict(status='ok', version=version, accounts=len(result['accounts']), audit=result['audit'],
                        missing_days=len(missing), start=manifest['start'], end=end)
        except Exception as exc:
            atomic_json(root / 'status.json', dict(status='error', attempted_at=datetime.now(timezone.utc).isoformat(),
                                                  message='标签画像更新失败，保留上次成功结果'))
            # Do not print SQL, credentials, connection details or exception text.
            raise RuntimeError('portrait_refresh_failed:' + type(exc).__name__) from None


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=DEFAULT_ROOT)
    parser.add_argument('--warehouse-root', default=WAREHOUSE_ROOT)
    parser.add_argument('--env', default='/root/drama_material_service/.env')
    args = parser.parse_args()
    print(json.dumps(refresh(args.root, args.warehouse_root, args.env), ensure_ascii=False))
