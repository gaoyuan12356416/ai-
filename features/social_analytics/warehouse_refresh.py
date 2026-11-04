"""Hourly full-history report refresh: gated source -> ads_ai -> verified serving copy."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import hashlib
import zlib
from datetime import date, datetime, timedelta, timezone

from .adapters import PLATFORMS, read_catalogs, source_key
from .backend_scope import eligible, restrict
from .report import FIELDS, ReportError, add, empty
from .routes import metric_sql
from .warehouse_reader import DEFAULT_ROOT
from .warehouse_schema import (FACT_TABLES, CATALOG_TABLE, PUBLICATION_TABLE, RUN_TABLE,
                               TABLES, METRICS, digest, encoded, fact_key, ddl)


def now():
    return datetime.now(timezone.utc).isoformat()


def environment(path):
    env = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            env[key] = value.strip().strip('"').strip("'")
    env.update(os.environ)
    env.setdefault('JOB_DB_PATH', '/root/drama_material_service/data/drama_material_jobs.sqlite3')
    return env


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.next')
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write(encoded(value))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class Reader:
    """Several bounded statements share one FIFO-gated, read-only connection."""
    def __init__(self, env):
        self.env = env
        if (str(env.get('ADMIN_MAPPING_MYSQL_HOST')), str(env.get('ADMIN_MAPPING_MYSQL_PORT'))) != ('101.32.56.53', '63350'):
            raise RuntimeError('invalid_reader_endpoint')

    def query(self, statements, timeout=600):
        env = dict(os.environ)
        env.pop('SQL_GATE_BYPASS', None)
        env.update(MYSQL_PWD=self.env['ADMIN_MAPPING_MYSQL_PASSWORD'],
                   SQL_GATE_JOB_NAME='social-analytics-warehouse', SQL_GATE_WAIT_TIMEOUT_SECONDS='60',
                   SQL_GATE_EXEC_TIMEOUT_SECONDS=str(timeout), SQL_GATE_RETRY_DELAYS_SECONDS='')
        sql = ("SET SESSION time_zone='+00:00'; SET SESSION MAX_EXECUTION_TIME=20000; "
               "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ; "
               "START TRANSACTION READ ONLY; SELECT @@read_only;\n" + ';\n'.join(statements) + ';\nROLLBACK;')
        result = subprocess.run(['/usr/bin/mysql', '-h', '101.32.56.53', '-P', '63350',
                                 '-u', self.env['ADMIN_MAPPING_MYSQL_USER'], '--connect-timeout=3',
                                 '--skip-reconnect', '--default-character-set=utf8mb4', '-N', '-B', '-r'],
                                input=sql, env=env, text=True, encoding='utf-8', capture_output=True,
                                timeout=timeout + 70)
        lines = result.stdout.splitlines()
        if result.returncode or not lines or lines.pop(0) != '1':
            codes = re.findall(r'ERROR (\d+)', result.stderr)
            raise RuntimeError('source_query_failed_' + '_'.join(codes))
        return [json.loads(bytes.fromhex(line).decode('utf-8')) for line in lines if line.strip()]


def periods(start, end, days=4):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    while first <= last:
        stop = min(last, first + timedelta(days=days - 1))
        yield first.isoformat(), stop.isoformat()
        first = stop + timedelta(days=1)


def statements(catalog, platform, site, start, end):
    sql = metric_sql(start, end, platform, site).replace("'date',CAST(dt AS CHAR)",
                "'kind','metric','date',CAST(dt AS CHAR)")
    if platform == 'facebook':
        sql = sql.replace("'campaign_id',MIN(campaign_id)",
                          "'adset_id',MIN(adset_id),'ad_id',MIN(ad_id),'campaign_id',MIN(campaign_id)")
        sql = sql.replace('GROUP BY dt,BINARY campaign_id',
                          'GROUP BY dt,BINARY campaign_id,BINARY adset_id,BINARY ad_id')
        pages = sorted({row['effect_source']['adset_id'] for row in catalog
                        if row['platform'] == platform and row['site_id'] == site
                        and row.get('effect_source') and eligible(row, end) and not row.get('excluded')})
        predicate = ('BINARY adset_id IN (' + ','.join('0x' + value.encode().hex() for value in pages) + ')'
                     if pages else '1=0')
        sql = sql.replace('GROUP BY dt,BINARY campaign_id', 'AND ' + predicate + ' GROUP BY dt,BINARY campaign_id')
    coverage = ("SELECT HEX(JSON_OBJECT('kind','coverage','date',CAST(dt AS CHAR),'site_id','%s')) "
                "FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(sd) "
                "WHERE site_id='%s' AND dt BETWEEN '%s' AND '%s' GROUP BY dt" % (site, site, start, end))
    return [sql, coverage]


def collect(env, reader, start=None, end=None):
    catalog, errors = read_catalogs(env, list(PLATFORMS))
    if errors:
        raise RuntimeError('publication_catalog_unavailable')
    end = end or datetime.now(timezone.utc).date().isoformat()
    dates = [row['date'] for row in catalog if row.get('date') and eligible(row, end)]
    start = start or min(dates, default=end)
    if date.fromisoformat(start) > date.fromisoformat(end) or date.fromisoformat(start).year < 2020:
        raise ValueError('invalid_collection_range')
    restricted, _, _, _ = restrict(catalog, [], end)
    accepted, coverage = [], {}
    for platform, spec in PLATFORMS.items():
        for source in spec['sources']:
            site = source['site_id']
            source_dates = [row['date'] for row in restricted if row['platform'] == platform
                            and row['site_id'] == site and row.get('date')]
            first = max(start, min(source_dates, default=start))
            # Coverage includes earlier dates: the source, not publication activity, proves a zero.
            windows = list(periods(start, end))
            source_rows, populated = [], set()
            for offset in range(0, len(windows), 7):
                batch = windows[offset:offset + 7]
                sql = []
                for begin, stop in batch:
                    queries = statements(catalog, platform, site, begin, stop)
                    sql.extend(queries if stop >= first else queries[1:])
                rows = reader.query(sql)
                counts, seen = {}, set()
                for row in rows:
                    kind = row.pop('kind')
                    if row.get('site_id') != site or not batch[0][0] <= row['date'] <= batch[-1][1]:
                        raise RuntimeError('source_range_mismatch')
                    if kind == 'coverage':
                        populated.add(row['date'])
                        continue
                    if kind != 'metric':
                        raise RuntimeError('source_row_kind_invalid')
                    identity = (row['date'], row.get('campaign_id'), row.get('adset_id'), row.get('ad_id'))
                    if identity in seen or any(row.get(field) is None for field in FIELDS):
                        raise RuntimeError('source_metric_invalid')
                    seen.add(identity)
                    add(empty(), row)
                    window = next(begin for begin, stop in batch if begin <= row['date'] <= stop)
                    counts[window] = counts.get(window, 0) + 1
                    if counts[window] > 50000:
                        raise RuntimeError('source_window_overflow')
                    row['platform'] = platform
                    source_rows.append(row)
                print(encoded(dict(event='source_batch', source=source_key(platform, site),
                                   start=batch[0][0], end=batch[-1][1], rows=len(rows))), flush=True)
            _, facts, _, audit = restrict(catalog, source_rows, end)
            accepted.extend(facts)
            coverage[source_key(platform, site)] = sorted(populated)
            print(encoded(dict(event='source_complete', source=source_key(platform, site),
                               rows=len(facts), included=audit['included_totals'])), flush=True)
    from features.youtube_analytics.titles import resolve
    titles = {}
    ids = sorted({row['drama'] for row in restricted if row.get('drama')})
    for offset in range(0, len(ids), 100):
        try:
            titles.update(resolve(ids[offset:offset + 100], lambda statement: reader.query([statement])))
        except Exception:
            # Descriptive names cannot invalidate financial facts.
            titles.update({value: dict(drama_english_name='', drama_english_status='unavailable')
                           for value in ids[offset:offset + 100]})
    return dict(catalog=catalog, metrics=accepted, coverage=coverage, titles=titles,
                start=start, end=end, catalog_errors=dict(errors), source_metadata=errors.source_metadata)


def table_rows(data, version):
    result = {table: [] for table in TABLES[:-1]}
    for row in data['metrics']:
        result[FACT_TABLES[row['platform']]].append((version, fact_key(row), row['date'], row['site_id'],
              row['campaign_id'].encode('utf-8'), *(row[field] for field in METRICS), encoded(row)))
    for row in data['catalog']:
        key = digest(row)
        result[CATALOG_TABLE].append((version, key, row['platform'], row['site_id'], row['tenant'],
                                      row['owner'], row.get('date') or None, encoded(row)))
        for fact in row.get('publications', []):
            value = dict(fact, catalog_key=key)
            result[PUBLICATION_TABLE].append((version, digest([key, fact]), row['platform'], row['site_id'],
                    row['tenant'], row['owner'], fact['date'], encoded(value)))
    # Exact duplicate catalog evidence is one record; distinct/conflicting evidence survives.
    return {table: list({row[1]: row for row in rows}.values()) for table, rows in result.items()}


class Writer:
    def __init__(self, env):
        import pymysql
        prefix = 'SOCIAL_ANALYTICS_WAREHOUSE_WRITE_MYSQL_'
        def value(key, default=''):
            return env.get(prefix + key, env.get('AD_CONTROL_ACTION_LOG_MYSQL_' + key, default))
        if (value('HOST'), str(value('PORT'))) != ('101.32.56.53', '63353'):
            raise RuntimeError('invalid_writer_endpoint')
        self.db = pymysql.connect(host=value('HOST'), port=63353, user=value('USER'), password=value('PASSWORD'),
                                  database='ads_ai', charset='utf8mb4', connect_timeout=3,
                                  read_timeout=15, write_timeout=15, autocommit=False)
        with self.db.cursor() as cursor:
            cursor.execute('SELECT DATABASE(),@@read_only')
            if cursor.fetchone() != ('ads_ai', 0):
                self.db.close()
                raise RuntimeError('writer_identity_invalid')

    def close(self):
        self.db.rollback()
        self.db.close()

    def install_schema(self):
        # Explicit deployment operation only; hourly runtime never calls this.
        with self.db.cursor() as cursor:
            for statement in ddl():
                cursor.execute(statement)
        self.db.commit()

    def start(self, version, started):
        with self.db.cursor() as cursor:
            cursor.execute('INSERT INTO ads_ai.' + RUN_TABLE +
                           '(version,status,started_at,manifest_json) VALUES(%s,%s,%s,%s)',
                           (version, 'collecting', started, '{}'))
        self.db.commit()

    def write(self, values):
        for table, rows in values.items():
            if table not in TABLES[:-1]:
                raise ValueError('table_not_allowed')
            columns = ('version,row_key,dt,site_id,campaign_id,' + ','.join(METRICS) + ',payload_json'
                       if table in FACT_TABLES.values() else
                       'version,row_key,platform,site_id,tenant,owner,dt,payload_json')
            if not rows:
                continue
            sql = 'INSERT INTO ads_ai.' + table + '(' + columns + ') VALUES(' + ','.join(['%s'] * len(rows[0])) + ')'
            for offset in range(0, len(rows), 250):
                with self.db.cursor() as cursor:
                    cursor.executemany(sql, rows[offset:offset + 250])
                self.db.commit()
            print(encoded(dict(event='warehouse_written', table=table, rows=len(rows))), flush=True)

    def finish(self, version, manifest, status='verified'):
        with self.db.cursor() as cursor:
            cursor.execute('UPDATE ads_ai.' + RUN_TABLE +
                           ' SET status=%s,finished_at=%s,manifest_json=%s WHERE version=%s',
                           (status, now(), encoded(manifest), version))
        self.db.commit()

    def cleanup(self, versions):
        deadline = time.monotonic() + 120
        for version in versions:
            for table in TABLES:
                for _ in range(1000):
                    if time.monotonic() > deadline:
                        return
                    with self.db.cursor() as cursor:
                        cursor.execute('DELETE FROM ads_ai.' + table + ' WHERE version=%s LIMIT 1000', (version,))
                        count = cursor.rowcount
                    self.db.commit()
                    if count < 1000:
                        break


def verify(reader, version, values):
    literal = '0x' + version.encode('ascii').hex()
    statements = []
    for table in values:
        sums = (',' + ','.join("'%s',COALESCE(SUM(%s),0)" % (field, field) for field in METRICS)
                if table in FACT_TABLES.values() else '')
        statements.append("SELECT HEX(JSON_OBJECT('table','%s','rows',COUNT(*),'checksum',"
                          "BIT_XOR(CRC32(CONCAT(row_key,':',SHA2(payload_json,256))))%s)) FROM ads_ai.%s WHERE version=%s"
                          % (table, sums, table, literal))
    deadline = time.monotonic() + 90
    while True:
        actual = {row['table']: row for row in reader.query(statements)}
        matched = True
        for table, rows in values.items():
            value = actual.get(table, {})
            matched &= value.get('rows') == len(rows)
            checksum = 0
            for row in rows:
                checksum ^= zlib.crc32((row[1] + ':' + hashlib.sha256(row[-1].encode('utf-8')).hexdigest()).encode('ascii'))
            matched &= int(value.get('checksum', -1)) == checksum
            if table in FACT_TABLES.values():
                matched &= all(int(value.get(field, -1)) == sum(row[5 + index] for row in rows)
                               for index, field in enumerate(METRICS))
        if matched:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('warehouse_replica_verification_failed')
        time.sleep(3)


def snapshot(root, version, data, manifest):
    folder = Path(root) / 'snapshots'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (version + '.sqlite3')
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.executescript('''CREATE TABLE catalog(row_key TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE effects(platform TEXT NOT NULL,dt TEXT NOT NULL,row_key TEXT NOT NULL,payload TEXT NOT NULL,
                               PRIMARY KEY(platform,dt,row_key));
          CREATE INDEX effects_date ON effects(dt,platform);
          CREATE TABLE titles(content_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL);''')
        with db:
            db.executemany('INSERT OR IGNORE INTO catalog VALUES(?,?)', [(digest(row), encoded(row)) for row in data['catalog']])
            db.executemany('INSERT INTO effects VALUES(?,?,?,?)',
                           [(row['platform'], row['date'], fact_key(row), encoded(row)) for row in data['metrics']])
            db.executemany('INSERT INTO titles VALUES(?,?)', [(key, encoded(value)) for key, value in data['titles'].items()])
            db.execute('INSERT INTO metadata VALUES(?,?)', ('manifest', encoded(manifest)))
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('serving_snapshot_integrity_failed')
    with path.open('r+b') as handle:
        os.fsync(handle.fileno())
    path.chmod(0o644)
    return path


def refresh(env, root, start=None, end=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    reader = Reader(env)
    version = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    started = now()
    atomic_json(root / 'status.json', dict(status='collecting', version=version, started_at=started))
    writer = None
    try:
        data = collect(env, reader, start, end)
        values = table_rows(data, version)
        manifest = {key: data[key] for key in ('start', 'end', 'coverage', 'catalog_errors', 'source_metadata')}
        manifest.update(version=version, started_at=started, generated_at=now(),
                        database='snapshots/' + version + '.sqlite3', tables={table: len(rows) for table, rows in values.items()})
        writer = Writer(env)
        writer.start(version, started)
        writer.write(values)
        verify(reader, version, values)
        manifest['generated_at'] = now()
        snapshot(root, version, data, manifest)
        writer.finish(version, manifest)
        previous = json.loads((root / 'current.json').read_text()) if (root / 'current.json').exists() else None
        if previous:
            atomic_json(root / 'previous.json', previous)
        atomic_json(root / 'current.json', manifest)
        atomic_json(root / 'status.json', dict(status='complete', version=version, finished_at=now()))
        print(encoded(dict(event='refresh_complete', version=version, start=data['start'], end=data['end'],
                           tables=manifest['tables'], seconds=round((datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds(), 2))), flush=True)
        # Keep two published generations. Discover obsolete versions only on the reader.
        keep = {version} | ({previous['version']} if previous else set())
        try:
            old = reader.query(["SELECT HEX(JSON_OBJECT('version',version)) FROM ads_ai." + RUN_TABLE +
                                " WHERE version NOT IN (" + ','.join('0x' + v.encode().hex() for v in keep) +
                                ') ORDER BY version LIMIT 3'])
            writer.cleanup([row['version'] for row in old if row['version'] not in keep])
            for path in (root / 'snapshots').glob('*.sqlite3'):
                if path.stem not in keep and time.time() - path.stat().st_mtime > 7200:
                    path.unlink()
        except Exception as exc:
            print(encoded(dict(event='cleanup_deferred', error_code=type(exc).__name__)), flush=True)
        return manifest
    except Exception as exc:
        atomic_json(root / 'status.json', dict(status='failed', version=version, finished_at=now(), error_code=type(exc).__name__))
        if writer:
            with contextlib.suppress(Exception):
                writer.finish(version, {'error_code': type(exc).__name__}, status='failed')
        raise
    finally:
        if writer:
            writer.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=DEFAULT_ROOT)
    parser.add_argument('--env-file', default='/root/drama_material_service/.env')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--install-schema', action='store_true')
    args = parser.parse_args(argv)
    import fcntl
    Path(args.root).mkdir(parents=True, exist_ok=True)
    with (Path(args.root) / 'refresh.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"event":"refresh_already_running"}', flush=True)
            return 0
        env = environment(args.env_file)
        if args.install_schema:
            writer = Writer(env)
            try:
                writer.install_schema()
            finally:
                writer.close()
            print(encoded(dict(event='schema_installed', tables=TABLES)))
        else:
            refresh(env, args.root, args.start, args.end)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
