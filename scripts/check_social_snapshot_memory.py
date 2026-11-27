"""Replay a published snapshot locally; never query or write the source warehouse.

Run in a memory-limited transient service, with the release root on PYTHONPATH.
The output directory must be separate from the serving warehouse directory.
"""
import argparse
import gc
import hashlib
import json
from pathlib import Path
import resource
import sqlite3
from contextlib import closing

from features.social_analytics import warehouse_refresh as refresh


def fingerprint(db, table, order):
    checksum = hashlib.sha256()
    count = 0
    for row in db.execute('SELECT * FROM ' + table + ' ORDER BY ' + order):
        checksum.update(refresh.encoded(row).encode('utf-8'))
        checksum.update(b'\n')
        count += 1
    return dict(rows=count, sha256=checksum.hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if output == source.parent or output in source.parents or source in output.parents:
        raise ValueError('output must be separate from the serving warehouse')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        manifest = json.loads(db.execute("SELECT payload FROM metadata WHERE key='manifest'").fetchone()[0])
        data = dict(catalog=[json.loads(row[0]) for row in db.execute('SELECT payload FROM catalog')],
                    metrics=[json.loads(row[0]) for row in db.execute('SELECT payload FROM effects')],
                    titles={row[0]: json.loads(row[1]) for row in db.execute('SELECT content_id,payload FROM titles')})
    print(refresh.encoded(dict(event='loaded', max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)), flush=True)
    values = refresh.table_rows(data, manifest['version'])
    counts = {key: len(rows) for key, rows in values.items()}
    if counts != manifest['tables']:
        raise AssertionError('warehouse row counts changed')
    print(refresh.encoded(dict(event='warehouse_rows_built', tables=counts,
                              max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)), flush=True)
    del values
    destination = refresh.snapshot(output, manifest['version'], data, manifest)
    del data
    gc.collect()
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as before, \
            closing(sqlite3.connect(destination.as_uri() + '?mode=ro', uri=True)) as after:
        checks = {}
        for table, order in (('catalog', 'row_key'), ('effects', 'platform,dt,row_key'),
                             ('titles', 'content_id'), ('metadata', 'key')):
            original = fingerprint(before, table, order)
            actual = fingerprint(after, table, order)
            if original != actual:
                raise AssertionError('snapshot mismatch: ' + table)
            checks[table] = actual
    result = dict(status='passed', version=manifest['version'], tables=checks,
                  max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    refresh.atomic_json(output / 'verification.json', result)
    print(refresh.encoded(result), flush=True)


if __name__ == '__main__':
    main()
