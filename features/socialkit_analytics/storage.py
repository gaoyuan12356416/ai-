"""Versioned ads_ai staging and atomic, immutable local snapshots."""
import contextlib
import json
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

from .collector import CollectionError, mysql_settings
from .schema import (EFFECT_FIELDS, EFFECT_METRICS, PUBLICATION_FIELDS, PUBLICATION_METRICS,
                     TABLES, create_snapshot_schema, row_key)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))
    if hasattr(os, 'O_DIRECTORY'):
        descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def read_manifest(root):
    path = Path(root) / 'current.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def snapshot_path(root, manifest):
    root = Path(root).resolve()
    value = manifest.get('database') or manifest.get('db_path')
    path = (root / value).resolve()
    if root not in path.parents or path.suffix != '.sqlite3':
        raise CollectionError('snapshot path escapes the configured data root')
    return path


class SnapshotBuilder:
    def __init__(self, root, version, old_manifest=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.folder = self.root / 'snapshots'
        self.folder.mkdir(exist_ok=True)
        self.path = self.folder / (version + '.sqlite3')
        if self.path.exists():
            raise CollectionError('snapshot version already exists')
        self.connection = sqlite3.connect(str(self.path))
        if old_manifest:
            source_path = snapshot_path(self.root, old_manifest)
            source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
            try:
                source.backup(self.connection)
            finally:
                source.close()
        self.connection.execute('PRAGMA journal_mode=DELETE')
        self.connection.execute('PRAGMA synchronous=FULL')
        create_snapshot_schema(self.connection)

    def write_day(self, day):
        with self.connection:
            for kind, fields in [('effects', EFFECT_FIELDS), ('publications', PUBLICATION_FIELDS)]:
                self.connection.execute('DELETE FROM %s WHERE date=?' % kind, (day['date'],))
                columns = ('row_key',) + fields
                self.connection.executemany('INSERT INTO %s (%s) VALUES (%s)' % (
                    kind, ','.join('"%s"' % f for f in columns), ','.join('?' for _ in columns)),
                    [(row_key(kind, row),) + tuple(row[f] for f in fields) for row in day[kind]])

    def finish(self, manifest):
        coverage = manifest['coverage']
        with self.connection:
            for key, value in [('manifest', manifest), ('version', manifest['version']), ('coverage', coverage)]:
                self.connection.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',
                                        (key, json.dumps(value, ensure_ascii=False)))
            self.connection.execute('ANALYZE')
        if self.connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise CollectionError('local snapshot integrity check failed')
        self.connection.close()
        self.connection = None
        with self.path.open('r+b') as handle:
            os.fsync(handle.fileno())

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class WarehouseWriter:
    """Only the three explicitly deployed report tables; no runtime DDL or source writes."""
    def __init__(self, env):
        self.config = mysql_settings(env, 'writer')
        self.connection = None
        self.version = None

    def __enter__(self):
        self.connection = self._connect()
        return self

    def _connect(self):
        import pymysql
        connection = pymysql.connect(**self.config, charset='utf8mb4', connect_timeout=5,
                                     read_timeout=60, write_timeout=15, autocommit=False,
                                     cursorclass=pymysql.cursors.DictCursor)
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT DATABASE() AS db,@@read_only AS ro')
                identity = cursor.fetchone()
                if identity['db'] != 'ads_ai' or int(identity['ro']) != 0:
                    raise CollectionError('dedicated writer did not prove ads_ai/read_only=0')
        except Exception:
            connection.close()
            raise
        return connection

    def __exit__(self, exc_type, exc, traceback):
        if self.connection:
            with contextlib.suppress(Exception):
                self.connection.rollback()
            if exc_type is not None and self.version:
                with contextlib.suppress(Exception):
                    self.failed(self.version, exc_type.__name__)
            self.connection.close()

    def start(self, version, start, end, started_at):
        self.version = version
        with self.connection.cursor() as cursor:
            cursor.execute('INSERT INTO ads_ai.socialkit_refresh_runs '
                           '(version,status,range_start,range_end,started_at,manifest_json,error_code) '
                           'VALUES(%s,%s,%s,%s,%s,%s,%s)',
                           (version, 'collecting', start, end, started_at, '{}', ''))
        self.connection.commit()

    def write_day(self, version, day):
        for kind, fields in [('effects', EFFECT_FIELDS), ('publications', PUBLICATION_FIELDS)]:
            columns = ('version', 'row_key') + fields
            sql = 'INSERT INTO ads_ai.%s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s' % (
                TABLES[kind], ','.join('`%s`' % f for f in columns), ','.join(['%s'] * len(columns)),
                ','.join('`%s`=VALUES(`%s`)' % (f, f) for f in fields))
            rows = day[kind]
            for offset in range(0, len(rows), 100):
                values = [(version, row_key(kind, row)) + tuple(row[f] for f in fields)
                          for row in rows[offset:offset + 100]]
                self._write_fact_batch(sql, values)

    def _write_fact_batch(self, sql, values):
        import pymysql
        for attempt in range(3):
            try:
                if self.connection is None:
                    self.connection = self._connect()
                with self.connection.cursor() as cursor:
                    cursor.executemany(sql, values)
                self.connection.commit()
                return
            except pymysql.err.OperationalError as exc:
                code = exc.args[0] if exc.args else None
                if code not in (2006, 2013) or attempt == 2:
                    raise
                # Only replay the identical version/row-key upsert. A lost COMMIT
                # reply may already be durable; absolute values cannot double-count.
                with contextlib.suppress(Exception):
                    if self.connection is not None:
                        self.connection.close()
                self.connection = None
                print(json.dumps(dict(event='warehouse_fact_write_retry', attempt=attempt + 2,
                                      error_code=code, rows=len(values))), flush=True)
                time.sleep((2, 5)[attempt])

    def finish(self, version, manifest):
        with self.connection.cursor() as cursor:
            cursor.execute('UPDATE ads_ai.socialkit_refresh_runs SET status=%s,finished_at=%s,'
                           'manifest_json=%s,error_code=%s WHERE version=%s',
                           ('verified', manifest['generated_at'], json.dumps(manifest, ensure_ascii=False), '', version))
        self.connection.commit()

    def failed(self, version, error_code):
        with self.connection.cursor() as cursor:
            cursor.execute('UPDATE ads_ai.socialkit_refresh_runs SET status=%s,error_code=%s WHERE version=%s',
                           ('failed', error_code, version))
        self.connection.commit()

    def delete_version_rows(self, table, version):
        if table not in tuple(TABLES.values()) + ('socialkit_refresh_runs',):
            raise CollectionError('cleanup attempted an unapproved table')
        limit = 1 if table == 'socialkit_refresh_runs' else 1000
        with self.connection.cursor() as cursor:
            cursor.execute('DELETE FROM ads_ai.%s WHERE version=%%s LIMIT %d' % (table, limit), (version,))
            affected = cursor.rowcount
        self.connection.commit()
        return affected

    def delete_fact_date(self, table, version, day):
        if table not in TABLES.values():
            raise CollectionError('date cleanup attempted an unapproved fact table')
        day = date.fromisoformat(day).isoformat()
        with self.connection.cursor() as cursor:
            cursor.execute('DELETE FROM ads_ai.%s WHERE version=%%s AND `date`=%%s LIMIT 1000' % table,
                           (version, day))
            affected = cursor.rowcount
        self.connection.commit()
        return affected


def verification_statements(version, days):
    # Both values originate in the worker; encode as hex rather than interpolate SQL literals.
    literal = '0x' + version.encode('ascii').hex()
    statements = []
    for kind, metrics in [('effects', EFFECT_METRICS), ('publications', PUBLICATION_METRICS)]:
        totals = ','.join("'%s',COALESCE(SUM(`%s`),0)" % (field, field) for field in metrics)
        statements.append("SELECT HEX(JSON_OBJECT('kind','%s','date',CAST(`date` AS CHAR),"
                          "'rows',COUNT(*),%s)) FROM ads_ai.%s WHERE version=%s GROUP BY `date`" %
                          (kind, totals, TABLES[kind], literal))
    return statements


def verify_warehouse(reader, version, expected_days, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        results = reader.query(verification_statements(version, expected_days))
        actual = {(row['kind'], row['date']): row for row in results}
        allowed = {(kind, day['date']) for kind in ('effects', 'publications') for day in expected_days}
        matched = not (set(actual) - allowed)
        for day in expected_days:
            for kind, metrics in [('effects', EFFECT_METRICS), ('publications', PUBLICATION_METRICS)]:
                expected = day[kind]
                row = actual.get((kind, day['date']), {})
                if int(row.get('rows', 0)) != expected['rows'] or any(
                        int(row.get(field, 0)) != expected[field] for field in metrics):
                    matched = False
        if matched:
            return
        if time.monotonic() >= deadline:
            raise CollectionError('ads_ai read replica verification did not match staged facts')
        time.sleep(3)


def publish_snapshot(root, manifest, old_manifest):
    root = Path(root)
    if old_manifest:
        atomic_json(root / 'previous.json', old_manifest)
    atomic_json(root / 'current.json', manifest)
    keep = {snapshot_path(root, manifest)}
    if old_manifest:
        keep.add(snapshot_path(root, old_manifest))
    # Every new SQLite file contains retained earlier dates, so only current/previous files are needed.
    for candidate in (root / 'snapshots').glob('*.sqlite3'):
        resolved = candidate.resolve()
        if resolved not in keep and resolved.parent == (root / 'snapshots').resolve():
            with contextlib.suppress(OSError):
                candidate.unlink()
    versions = {manifest['version']}
    if old_manifest:
        versions.add(old_manifest['version'])
    retained_precomputed = {version + suffix for version in versions for suffix in ('-overview.json', '-options.json')}
    folder = (root / 'precomputed').resolve()
    for candidate in folder.glob('*.json'):
        if candidate.resolve().parent == folder and candidate.name not in retained_precomputed:
            with contextlib.suppress(OSError):
                candidate.unlink()


def cleanup_warehouse(reader, writer, manifests, max_batches=600, budget_seconds=120):
    """Prune replica-discovered stale versions/dates; retain both published manifests."""
    keep = set()
    allowed_pairs = set()
    unknown_date_versions = set()
    for manifest in manifests:
        if not manifest:
            continue
        keep.add(manifest['version'])
        keep.update(day['version'] for day in manifest.get('date_manifest', {}).values())
        allowed_pairs.update((value['version'], day) for day, value in manifest.get('date_manifest', {}).items())
        if not manifest.get('date_manifest'):
            # Compatibility: a manifest lacking day references cannot authorize partial deletion.
            unknown_date_versions.add(manifest['version'])
    literals = ','.join('0x' + value.encode('ascii').hex() for value in sorted(keep))
    started = time.monotonic()
    deadline = started + budget_seconds
    statements = ["SELECT HEX(JSON_OBJECT('kind','obsolete_version','version',version)) FROM ads_ai.socialkit_refresh_runs "
                  "WHERE version NOT IN (%s) ORDER BY started_at LIMIT 6" % literals]
    for kind, table in TABLES.items():
        index = 'idx_socialkit_effect_day' if kind == 'effects' else 'idx_socialkit_publish_day'
        statements.append("SELECT HEX(JSON_OBJECT('kind','%s','version',version,'date',CAST(`date` AS CHAR))) "
                          "FROM ads_ai.%s FORCE INDEX(%s) WHERE version IN (%s) GROUP BY `date`,version" %
                          (kind, table, index, literals))
    discovered = reader.query(statements)
    rows = [row for row in discovered if row.get('kind', 'obsolete_version') == 'obsolete_version']
    stale_pairs = []
    for row in discovered:
        if row.get('kind') not in TABLES:
            continue
        version, day = row['version'], date.fromisoformat(row['date']).isoformat()
        if version not in keep:
            raise CollectionError('date cleanup returned an unexpected version')
        if version not in unknown_date_versions and (version, day) not in allowed_pairs:
            stale_pairs.append((TABLES[row['kind']], version, day))
    batches, deleted = 0, 0
    def outcome(incomplete):
        return dict(deleted_rows=deleted, batches=batches, elapsed_seconds=round(time.monotonic() - started, 3),
                    incomplete=incomplete)
    for table, version, day in sorted(set(stale_pairs)):
        while batches < max_batches and time.monotonic() < deadline:
            count = writer.delete_fact_date(table, version, day)
            deleted += count
            batches += 1
            if count == 0:
                break
        else:
            return outcome(True)
    for row in rows[:5]:
        version = row['version']
        if not isinstance(version, str) or version in keep:
            raise CollectionError('cleanup candidate overlaps a published version')
        for table in TABLES.values():
            while batches < max_batches and time.monotonic() < deadline:
                count = writer.delete_version_rows(table, version)
                deleted += count
                batches += 1
                if count == 0:
                    break
            else:
                return outcome(True)
        if batches >= max_batches or time.monotonic() >= deadline:
            return outcome(True)
        writer.delete_version_rows('socialkit_refresh_runs', version)
        batches += 1
    return outcome(len(rows) > 5)
