"""Versioned drama metadata, keyed by exact product App ID and content ID."""
import argparse
import collections
import json
import uuid
from datetime import datetime, timezone

from .schema import stable_key


def normalize_language(value):
    value = str(value or '').strip().lower().replace('_', '-')
    parts = value.split('-')
    if parts[0] == 'jp':
        parts[0] = 'ja'
    return '-'.join(parts)


def collect_languages(reader, keys):
    keys = set(keys)
    by_app = collections.defaultdict(list)
    for app_id, content_id in sorted(keys):
        by_app[int(app_id)].append(content_id)
    found = collections.defaultdict(set)
    for app_id, contents in by_app.items():
        for offset in range(0, len(contents), 500):
            literals = ["CONVERT(0x%s USING utf8mb4)" % value.encode('utf-8').hex()
                        for value in contents[offset:offset + 500]]
            sql = ("SELECT HEX(JSON_OBJECT('app_id',app_id,'content_id',MIN(content_id),"
                   "'language',MIN(language))) FROM kunlunads_dev.ads_drama_info FORCE INDEX(ac) "
                   "WHERE app_id=%d AND content_id IN (%s) "
                   "GROUP BY app_id,BINARY content_id,BINARY language" % (app_id, ','.join(literals)))
            for row in reader.records([sql]):
                key = (int(row['app_id']), str(row['content_id']))
                language = normalize_language(row['language'])
                if key in keys and language:
                    found[key].add(language)
    return {stable_key(*key): (next(iter(found[key])) if len(found[key]) == 1
                              else '__conflict__' if found[key] else '') for key in keys}


def update_languages(conn, reader, previous=None):
    """Keep last-known metadata on a source failure; never block income refresh."""
    try:
        keys = conn.execute("SELECT drama_app_id,drama_content_id FROM effects WHERE drama_content_id<>'' "
                            "UNION SELECT drama_app_id,drama_content_id FROM publications WHERE drama_content_id<>''").fetchall()
        values = collect_languages(reader, keys)
    except Exception as exc:
        return dict(previous or {}, status='failed', error_code=type(exc).__name__)
    with conn:
        conn.execute('DELETE FROM drama_languages')
        conn.executemany('INSERT INTO drama_languages(drama,drama_language) VALUES(?,?)', sorted(values.items()))
    return dict(status='complete', source='kunlunads_dev.ads_drama_info',
                updated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                dramas=len(values), missing=sum(value == '' for value in values.values()),
                conflicts=sum(value == '__conflict__' for value in values.values()))


def enrich_snapshot(root, reader):
    """Publish a metadata-only copy, preserving every fact and its source version."""
    from .refresh import ensure_data_mount, refresh_lock
    from .storage import SnapshotBuilder, publish_snapshot, read_manifest
    ensure_data_mount(root)
    with refresh_lock(root):
        old = read_manifest(root)
        if not old:
            raise RuntimeError('No published SocialKit snapshot')
        version = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-lang-' + uuid.uuid4().hex[:8]
        builder = SnapshotBuilder(root, version, old)
        try:
            state = update_languages(builder.connection, reader, old.get('drama_languages'))
            if state['status'] != 'complete':
                raise RuntimeError('Drama language metadata refresh failed: ' + state['error_code'])
            manifest = dict(old, version=version, database=str(builder.path.relative_to(builder.root)).replace('\\', '/'),
                            db_path=str(builder.path), drama_languages=state, metadata_parent_version=old['version'])
            builder.finish(manifest)
            publish_snapshot(root, manifest, old)
            return manifest
        finally:
            builder.close()


def main():
    from .collector import GatedReader
    from .refresh import DEFAULT_ROOT, load_environment
    from .query import warm_snapshot
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=DEFAULT_ROOT)
    parser.add_argument('--env-file', action='append', default=[])
    args = parser.parse_args()
    result = enrich_snapshot(args.root, GatedReader(load_environment(args.env_file), timeout=60))
    warm_snapshot(args.root, result['version'])
    print(json.dumps(dict(version=result['version'], drama_languages=result['drama_languages'])))


if __name__ == '__main__':
    main()
