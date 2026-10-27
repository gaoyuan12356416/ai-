#!/usr/bin/env python3
"""Read-only real-source acceptance; evidence belongs in a private data-disk directory."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.social_analytics import adapters, report, routes
from features.youtube_daily_report.collector import read_settings, DB_PATH, ENV_PATH


def fingerprint(campaign):
    with sqlite3.connect(Path(DB_PATH).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        values = [list(db.execute('SELECT * FROM ' + table + ' WHERE ' + column + '=?', (campaign,)))
                  for table, column in [('drama_youtube_publish', 'preparation_id'),
                                        ('youtube_link_attribution', 'task_id'),
                                        ('drama_material_short_link', 'job_id')]]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign-id', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if not output.is_dir() or not str(output.resolve()).startswith('/mnt/data-disk/social-analytics/'):
        raise ValueError('existing_private_output_directory_required')
    settings = read_settings(ENV_PATH)
    settings['JOB_DB_PATH'] = DB_PATH
    catalog, errors = adapters.read_catalogs(settings, ['youtube'])
    assert not errors
    matching = [row for row in catalog if row['key'][0] == args.campaign_id]
    assert len(matching) == 1, 'exact_unique_campaign_required'
    selected = matching[0]
    actor = dict(tenant_key=selected['tenant'], user_id=selected['owner'], role='admin')
    query = dict(start=[args.start], end=[args.end], platform=['youtube'], owner=[selected['owner']],
                 search=[selected['drama_label'].removesuffix('(吹き替え)')],
                 group_by=['owner,drama,channel'], sort=['clicks'], direction=['desc'],
                 page=['1'], page_size=['50'], ranking_metric=['clicks'])
    report.parameters(query)
    before = fingerprint(args.campaign_id)
    traces = []
    original = routes.query_source_rows

    def traced(settings, statement, limit):
        started = time.monotonic()
        rows = original(settings, statement, limit)
        traces.append(dict(seconds=round(time.monotonic()-started, 3), rows=len(rows), sql=statement))
        return rows

    routes.query_source_rows = traced
    routes._cache.clear()
    started = time.monotonic()
    value = routes.build_report(settings, actor, query, ai_backend_only=True, include_titles=True)
    cold = round(time.monotonic() - started, 3)
    assert value['quality']['platforms']['youtube']['error'] == '', 'source_not_available'
    assert len(value['rows']) == 1
    # Independent all-period exact-ID aggregate; no shared batch/coverage logic.
    literal = '0x' + args.campaign_id.encode().hex()
    sql = """SELECT HEX(JSON_OBJECT('clicks',SUM(clicks),'views',SUM(views),'installs',SUM(installs),
        'revenue_cents',CAST(ROUND(SUM(revenue)*100) AS SIGNED),
        'refund_cents',CAST(ROUND(SUM(refund_revenue)*100) AS SIGNED),'conversions',SUM(recharge)))
        FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(c_id_dt)
        WHERE campaign_id=CONVERT(%s USING utf8mb4) AND BINARY campaign_id=%s
        AND site_id='2284' AND dt BETWEEN '%s' AND '%s'""" % (literal,literal,args.start,args.end)
    expected = original(settings, sql, 1)[0]
    for field in report.FIELDS:
        assert value['totals'][field] == expected[field], ('independent_sql_mismatch', field)
    started = time.monotonic()
    warm = routes.build_report(settings, actor, query, ai_backend_only=True)
    warm_seconds = round(time.monotonic() - started, 3)
    assert warm['totals'] == value['totals']
    csv_bytes = report.csv_export(value)
    exported = list(csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig'))))
    assert len(exported) == 1 and int(exported[0]['落地页点击']) == expected['clicks']
    assert int(exported[0]['落地页访问']) == expected['views']
    assert fingerprint(args.campaign_id) == before, 'frozen_publication_changed'
    (output/'query.json').write_text(json.dumps(query, ensure_ascii=False), encoding='utf-8')
    (output/'report.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'report.csv').write_bytes(csv_bytes)
    evidence = dict(cold_seconds=cold, warm_seconds=warm_seconds, totals=value['totals'],
                    missing_dates=value['quality']['missing_dates'], independent_sql=expected,
                    publisher_fingerprint=before, queries=traces)
    (output/'acceptance.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: val for key, val in evidence.items() if key not in {'queries'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
