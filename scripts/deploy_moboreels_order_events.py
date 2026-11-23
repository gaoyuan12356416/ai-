"""One-table, row-preserving migration for recharge/refund event identity."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.moboreels_library.settings import Settings
import deploy_moboreels_clicks as switcher
import pymysql

TABLE = 'ads_ai.cdreader_koc_order'
SQL = ROOT / 'deploy/sql/moboreels-order-events-20261123.sql'


def connect(settings, write=False):
    return pymysql.connect(host=settings.db_host, port=settings.write_port if write else settings.read_port,
                           user=settings.db_user, password=settings.db_password, database='ads_ai',
                           connect_timeout=5, read_timeout=20, write_timeout=5, autocommit=True,
                           charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)


def read_table(settings):
    with closing(connect(settings)) as conn, conn.cursor() as cur:
        cur.execute('SELECT @@read_only AS ro')
        if cur.fetchone()['ro'] != 1:
            raise RuntimeError('Expected read-only replica')
        cur.execute('SELECT * FROM ' + TABLE + ' ORDER BY row_key LIMIT 1001')
        rows = cur.fetchall()
        if len(rows) > 1000:
            raise RuntimeError('Migration backup exceeds the reviewed small-table scope')
        cur.execute('SHOW CREATE TABLE ' + TABLE)
        schema = cur.fetchone()['Create Table']
        cur.execute('SHOW INDEX FROM ' + TABLE)
        indexes = cur.fetchall()
    return {'rows': rows, 'schema': schema, 'indexes': indexes}


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def migrate(folder):
    settings = Settings.from_env()
    folder = folder.resolve()
    if not (folder / 'manifest.json').exists():
        raise RuntimeError('Stage the exact GitHub source first')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest['profile'] != 'order-events' or manifest['commit'] != switcher.run('git', '-C', ROOT, 'rev-parse', 'HEAD'):
        raise RuntimeError('Migration and staged code versions differ')
    mounted = switcher.run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk')
    if mounted != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Required data disk unavailable')
    with switcher.drained():
        before = read_table(settings)
        names = {r['Key_name'] for r in before['indexes']}
        if 'idx_order_ref' in names and 'uk_order' not in names:
            print(json.dumps({'migration': 'already_applied'}))
            return
        if 'uk_order' not in names or 'PRIMARY' not in names:
            raise RuntimeError('Unexpected order indexes; preserve existing state')
        # The verified deployment has only legacy recharge rows. Existing refunds
        # would need their legacy key mapped explicitly before changing ingestion.
        if any(r['classification'] != 0 for r in before['rows']):
            raise RuntimeError('Existing non-recharge rows need explicit legacy-key reconciliation')
        backup = folder / 'order-table-before.json'
        with backup.open('x', encoding='utf-8') as out:
            json.dump(before, out, ensure_ascii=False, default=str, indent=2)
        backup.chmod(0o600)
        with closing(sqlite3.connect('file:' + str(switcher.DATA / 'reports.sqlite3') + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(folder / 'reports-before.sqlite3')) as target:
                source.backup(target)
        with closing(connect(settings, write=True)) as conn, conn.cursor() as cur:
            cur.execute('SELECT DATABASE() AS db, @@read_only AS ro')
            state = cur.fetchone()
            if state != {'db': 'ads_ai', 'ro': 0}:
                raise RuntimeError('Expected ads_ai deployment writer')
            cur.execute('SET SESSION lock_wait_timeout=5')
            cur.execute(SQL.read_text(encoding='utf-8'))
        after = read_table(settings)
        if fingerprint(before['rows']) != fingerprint(after['rows']):
            raise RuntimeError('Order rows changed during schema migration')
        indexes = {r['Key_name']: r['Non_unique'] for r in after['indexes']}
        if indexes.get('PRIMARY') != 0 or indexes.get('idx_order_ref') != 1 or 'uk_order' in indexes:
            raise RuntimeError('Replica index verification failed')
        result = {'migration': 'verified', 'rows_preserved': len(before['rows']),
                  'rows_sha256': fingerprint(before['rows']), 'commit': manifest['commit'],
                  'finished_at': datetime.now(timezone.utc).isoformat()}
        (folder / 'migration.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


def rollback(folder):
    # Preserve the new financial facts and their supporting index. The old collector
    # cannot ingest separate events, so pause only its timer until a forward fix.
    switcher.run('systemctl', 'stop', 'moboreels-report-sync.timer')
    switcher.switch(folder, True)
    print('Order rows, schema, raw snapshots and ledgers retained. Resume the report timer only after a forward event-compatible fix.')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('migrate', 'rollback'))
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    (migrate if args.action == 'migrate' else rollback)(args.folder)
