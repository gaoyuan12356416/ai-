#!/usr/bin/env python3
"""One-off acceptance using the already published cache; run under dashboard flock.

No source refresh, database backup, business notification or metric changes.
Writes the new manifest only after every regenerated partition matches the
previously published business content. Requires an existing complete publication.
"""
import argparse
import json
from pathlib import Path
import sys
import time

import tt_minis_storage as storage
import tt_minis_multi_dim_dashboard as dashboard


def business_payload(payload):
    result = dict(payload)
    result['meta'] = dict(payload['meta'])
    result['meta'].pop('generated_at', None)
    result['meta'].pop('source_refreshed_at', None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    receipt = storage.prepare_storage(Path(args.receipt))
    dashboard.CACHE_DB = storage.CACHE_DB
    output = dashboard.WEB_DIR
    previous = dashboard.read_previous_manifest(output)
    start, end = previous['meta']['start_date'], previous['meta']['end_date']
    revisions = dashboard.cache_partition_revisions(start, end)
    entries = previous['data_files']
    count = sum(len(files) for files in entries.values())
    if count != 120:
        raise RuntimeError('Expected audited 60-day / two-level publication')
    for level, files in entries.items():
        for day, info in files.items():
            rev = revisions[(level, day)]
            if (rev['row_count'] != info['row_count']
                    or rev['refreshed_at'] > previous['meta']['generated_at']):
                raise RuntimeError('Cache is newer than publication; wait for a completed normal run')
    checked = []
    original = dashboard.build_payload

    def checked_build(rows, begin, finish, validation, **kwargs):
        result = original(rows, begin, finish, validation, **kwargs)
        level = kwargs['metric_level']
        old_path = dashboard.detail_path(output, entries[level][begin]['path'])
        old = json.loads(old_path.read_text(encoding='utf-8'))
        if business_payload(result) != business_payload(old):
            raise RuntimeError('Business partition mismatch: %s/%s' % (level, begin))
        checked.append([level, begin, len(rows)])
        return result

    dashboard.build_payload = checked_build
    started = time.monotonic()
    dashboard.publish_from_cache(previous, output, start, end)
    first_seconds = time.monotonic() - started
    first = dashboard.read_previous_manifest(output)
    if len(checked) + first['publication']['reused_partitions'] != count:
        raise RuntimeError('Incomplete partition verification')

    def unexpected_fetch(*args, **kwargs):
        raise RuntimeError('Unchanged cache should not be read again')

    dashboard.fetch_rows_from_cache = unexpected_fetch
    started = time.monotonic()
    dashboard.publish_from_cache(first, output, start, end)
    second_seconds = time.monotonic() - started
    second = dashboard.read_previous_manifest(output)
    if second['publication']['written_partitions'] != 0 or second['publication']['reused_partitions'] != count:
        raise RuntimeError('Unexpected incremental publication result')
    if first['data_files'] != second['data_files']:
        raise RuntimeError('Unchanged partition paths changed')
    if previous['totals'] != second['totals'] or previous['daily_totals'] != second['daily_totals']:
        raise RuntimeError('Summary changed')
    # The currently deployed downstream reader, invoked without its send/main path.
    sys.path.insert(0, '/root/codex_test')
    import tt_minis_content_id_roas_broadcast as consumer
    last_rows, generated = consumer.load_published_rows(end, end, report_root=output)
    report = {'ok': True, 'business_partitions_compared': len(checked),
        'rows_compared': sum(row[2] for row in checked),
        'first_publication': first['publication'], 'first_seconds': round(first_seconds, 3),
        'second_publication': second['publication'], 'second_seconds': round(second_seconds, 3),
        'consumer_day': end, 'consumer_rows': len(last_rows), 'generated_at': generated,
        'summary_unchanged': True, 'start_date': start, 'end_date': end}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
