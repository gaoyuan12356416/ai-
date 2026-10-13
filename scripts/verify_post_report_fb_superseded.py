#!/usr/bin/env python3
"""Compare report collectors in one read-only FB snapshot; never send a report."""
import argparse
from contextlib import nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.post_daily_report import fb
from features.post_daily_report.common import Window, read_db
from features.post_daily_report.report import build_card, build_compact_card, preview_html, validate_channel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--fb-db', default='/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--date', action='append', required=True)
    parser.add_argument('--expect', action='append', default=[], help='DATE=expected,published,blocked,failed')
    args = parser.parse_args()
    baseline = Path(args.baseline).resolve(strict=True)
    output = Path(args.output_dir).resolve()
    if not output.is_relative_to(Path('/mnt/data-disk/post-daily-report/diagnostics')):
        raise RuntimeError('diagnostic_output_outside_data_disk')
    disk = subprocess.check_output(['findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk'], text=True).strip()
    if disk != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('report_data_disk_unavailable')
    # Shared imports must be byte-identical; TT/X and delivery behavior are not part of this repair.
    unchanged = ['common.py', 'tt.py', 'x.py', 'x_cost.py', 'delivery.py', '__init__.py']
    for filename in unchanged:
        rel = Path('features/post_daily_report') / filename
        if (baseline / rel).read_bytes() != (ROOT / rel).read_bytes():
            raise RuntimeError('unrelated_report_code_changed:' + filename)
    spec = importlib.util.spec_from_file_location('features.post_daily_report.fb_baseline', baseline / 'features/post_daily_report/fb.py')
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    expectations = {item.split('=', 1)[0]: tuple(map(int, item.split('=', 1)[1].split(','))) for item in args.expect}
    output.mkdir(parents=True, exist_ok=False)
    original_hashes = {}
    source_reports = Path('/mnt/data-disk/post-daily-report/reports')
    for date in args.date:
        for path in (source_reports / date).glob('*'):
            if path.is_file():
                original_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    summaries = []
    with read_db(args.fb_db) as connection:
        fb.read_db = old.read_db = lambda path: nullcontext(connection)
        for date in args.date:
            window = Window.for_date(date)
            before, after = old.collect({'fb': args.fb_db}, window), fb.collect({'fb': args.fb_db}, window)
            validate_channel(after)
            previous = {r['source']: r for r in before['rows']}
            for row in after['rows']:
                # Published, failed, unresolved and processing facts must all survive.
                for field in ('published', 'late', 'failed', 'unknown', 'pending', 'policy_skipped'):
                    if row[field] != previous[row['source']][field]:
                        raise RuntimeError('historical_outcome_changed:' + date + ':' + field)
            if after['prior_completed'] != before['prior_completed']:
                raise RuntimeError('historical_catchup_changed:' + date)
            auto = after['rows'][0]
            actual = tuple(auto[k] for k in ('expected', 'published', 'blocked', 'failed'))
            if date in expectations and actual != expectations[date]:
                raise RuntimeError('unexpected_recalculation:' + date + ':' + str(actual))
            card_report = {'date': date, 'channels': [after]}
            card = build_card(card_report)
            for content in (card, build_compact_card(card_report)):
                payload = json.dumps(content, ensure_ascii=False, separators=(',', ':'))
                if len(json.dumps({'receive_id': 'x' * 40, 'msg_type': 'interactive', 'content': payload, 'uuid': '0' * 36}, ensure_ascii=False).encode()) > 29000:
                    raise RuntimeError('card_too_large:' + date)
            (output / (date + '.json')).write_text(json.dumps({'before': before, 'after': after}, ensure_ascii=False, indent=2), encoding='utf-8')
            (output / (date + '.html')).write_text(preview_html(card_report, card), encoding='utf-8')
            summaries.append({'date': date, 'before': {k: previous['auto_template'][k] for k in ('expected', 'published', 'blocked', 'failed')},
                              'after': {k: auto[k] for k in ('expected', 'published', 'blocked', 'failed', 'superseded_slots')},
                              'outcomes_preserved': True})
    for filename, expected in original_hashes.items():
        if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
            raise RuntimeError('original_report_changed:' + filename)
    result = {'status': 'verified_no_send', 'baseline': str(baseline), 'release': str(ROOT),
              'summary': summaries, 'original_report_hashes': original_hashes,
              'unchanged_collectors': unchanged, 'publisher_query_only': True}
    (output / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'original_report_hashes'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
