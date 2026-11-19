"""Import the bounded Nginx prefix captured between the old and new redirect handlers."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.moboreels_library.clicks import BEIJING, ClickStats, UPSERT, publication, traffic_bucket
from features.moboreels_library.settings import Settings
from features.moboreels_library.short_links import safe_target


PREFIX = rb'^\S+ - \S+ \[([^\]]+)\] '
STAMP = re.compile(PREFIX)
REDIRECT = re.compile(PREFIX + rb'"GET /m/([A-Za-z0-9_-]{16})(?:\?[^ "]*)? HTTP/[\d.]+" '
                      rb'302 \d+ "([^"]*)" "([^"]*)"')


def capture(logdir):
    entries = []
    for path in sorted(logdir.glob('access.log*')):
        if not re.fullmatch(r'access\.log(?:-\d{8}(?:\.gz)?)?', path.name):
            continue
        stat = path.stat()
        entries.append({'path': str(path), 'inode': stat.st_ino, 'size': stat.st_size})
    if not any(Path(row['path']).name == 'access.log' for row in entries):
        raise ValueError('active Nginx access log missing')
    return {'captured_at': datetime.now(timezone.utc).isoformat(), 'files': entries}


def lines(entry):
    path = Path(entry['path'])
    stat = path.stat()
    if stat.st_ino != entry['inode'] or stat.st_size < entry['size']:
        raise ValueError('captured Nginx log changed: '+path.name)
    if path.suffix == '.gz':
        if stat.st_size != entry['size']:
            raise ValueError('captured archive size changed: '+path.name)
        with gzip.open(path, 'rb') as stream:
            yield from stream
    else:
        with path.open('rb') as stream:
            left = entry['size']
            while left:
                line = stream.readline(left)
                if not line:
                    raise ValueError('captured Nginx log was truncated')
                left -= len(line)
                if line.endswith(b'\n'):
                    yield line


def log_time(value):
    return datetime.strptime(value.decode('ascii'), '%d/%b/%Y:%H:%M:%S %z')


def import_history(stats, snapshot):
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    def receipt(conn):
        row = conn.execute("SELECT value FROM metadata WHERE key='nginx_history'").fetchone()
        if row:
            saved = json.loads(row[0])
            if saved['snapshot'] != digest:
                raise ValueError('a different historical prefix is already imported')
            return saved
    with stats.connect() as conn:
        previous = receipt(conn)
        if previous:
            return previous
    uri = (stats.directory / 'short-links.sqlite3').as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as links:
        saved = [json.loads(row[0]) for row in links.execute('SELECT record_json FROM links')]
    valid = {r['short_code']: r for r in saved if r.get('status') == 'succeeded'
             and r.get('institution_user_id') == stats.institution and publication(r) and safe_target(r.get('target_url'))}
    counts = Counter()
    earliest_log = None
    for entry in snapshot['files']:
        first = True
        for line in lines(entry):
            if first:
                stamp = STAMP.match(line)
                if stamp:
                    first = False
                    when = log_time(stamp[1])
                    earliest_log = min(earliest_log, when) if earliest_log else when
            if b'"GET /m/' not in line:
                continue
            match = REDIRECT.match(line)
            if not match:
                continue
            code = match[2].decode('ascii')
            if code in valid:
                day, source, clicks, automated = traffic_bucket(
                    log_time(match[1]), match[3].decode('latin1'), match[4].decode('latin1'))
                counts[(day, code, source, clicks, automated)] += 1
    if earliest_log is None:
        raise ValueError('no dated access log in the captured prefix')
    first_issue = min((r['created_at'] for r in valid.values()), default=snapshot['captured_at'])
    coverage = max(earliest_log, datetime.fromisoformat(first_issue.replace('Z', '+00:00'))).astimezone(BEIJING)
    result = {'snapshot': digest, 'captured_at': snapshot['captured_at'], 'coverage_from': coverage.date().isoformat(),
              'files': len(snapshot['files']), 'clicks': 0, 'automated': 0}
    batch = []
    for (day, code, source, clicks, automated), count in counts.items():
        batch.append((day, code, source, clicks*count, automated*count))
        result['clicks'] += clicks*count
        result['automated'] += automated*count
    with stats.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = receipt(conn)
        if previous:
            return previous
        conn.executemany(UPSERT, batch)
        conn.execute("UPDATE metadata SET value=MIN(value,?) WHERE key='coverage_from'", (result['coverage_from'],))
        conn.execute("INSERT INTO metadata VALUES('nginx_history',?)", (json.dumps(result),))
        conn.commit()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capture', 'import'))
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--logs', type=Path, default=Path('/var/log/nginx'))
    args = parser.parse_args()
    if args.action == 'capture':
        with args.manifest.open('x', encoding='utf-8') as out:
            json.dump(capture(args.logs), out)
    else:
        snapshot = json.loads(args.manifest.read_text(encoding='utf-8'))
        print(json.dumps(import_history(ClickStats(Settings.from_env()), snapshot)))


if __name__ == '__main__':
    main()
