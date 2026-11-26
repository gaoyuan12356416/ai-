"""Build replay requests from historical FB queues using only current permitted pools."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.fb_page_assignment_v2.catalog import Reads, TABLE, Task, permissions
from features.fb_page_assignment_v2.snapshot import atomic, canon


def collect(open_reader, start, stop, *, representative=False):
    """Burst uses every independent native row; hour samples real native sub-batches by size/pool."""
    all_events, ignored, strata = [], Counter(), Counter()
    products = {1479:1479, 3086:1479, 979:979, 3302:3302}
    with open_reader() as r:
        for row in r.query("SELECT id,ads_app_id FROM " + TABLE + "ads_apps_setting WHERE platform_id=0 "
                           "AND app_type IN (10000,10001) AND ads_app_id IN (1479,979,3302)"):
            products[int(row["id"])] = int(row["ads_app_id"])
    begin = start
    while begin < stop:
        end = min(stop, begin + timedelta(minutes=2))
        with open_reader() as r:
            native = r.query("SELECT id,product,created_at FROM " + TABLE +
                             "ads_template_make_queue FORCE INDEX(idx_created_at) WHERE created_at>=%s "
                             "AND created_at<%s ORDER BY created_at,id", (begin.isoformat(" "), end.isoformat(" ")))
            native = [row for row in native if str(row["product"]).isdecimal() and int(row["product"]) in products]
            if not representative:
                # The observed 720-request burst consists of standalone tasks. Native
                # children of a cross queued earlier are outside that arrival window.
                keys = {int(row["id"]) for row in native}
                children = {int(row["publish_queue_id"]) for row in r.many(
                    "SELECT DISTINCT a.publish_queue_id FROM " + TABLE +
                    "ads_cross_sub_template_accounts a FORCE INDEX(idx_pqid) JOIN " + TABLE +
                    "ads_template_make_queue q ON q.id=a.publish_queue_id AND q.template_make_id=a.template_id "
                    "WHERE a.publish_queue_id IN ({ids}) AND a.template_type IN (0,1) AND a.super_queue_id>0", keys)}
                native = [row for row in native if int(row["id"]) not in children]
            refs = {Task("native", int(row["id"])) for row in native}
            grants, materials = permissions(r, refs)
            source_ids = {sid for ids in materials.values() for sid in ids}
            titles = {int(row["id"]): str(row["data_source_id"]).strip() for row in r.many(
                "SELECT s.id,c.data_source_id FROM " + TABLE + "ads_source s JOIN " + TABLE +
                "ads_custom_source c ON s.source_type=3 AND c.id=CAST(s.source_id AS UNSIGNED) WHERE "
                "s.id IN ({ids})", source_ids) if row["data_source_id"] and str(row["data_source_id"]).strip() != "0"}
        for row in native:
            ref = Task("native", int(row["id"]))
            pools = sorted(grants.get(ref, ()))
            if not pools:
                ignored["no_current_permission"] += 1
                continue
            source = materials[ref]
            ids = sorted({titles[sid] for sid in source if sid in titles})
            if not ids or source - titles.keys():
                ignored["source_unresolved"] += 1
                continue
            product = products[int(row["product"])]
            ts = datetime.fromisoformat(str(row["created_at"]))
            for offset in range(0, len(ids), 100):
                part = ids[offset:offset+100]
                # Multiple currently permitted account pools: first in numeric
                # order is deterministic. Report the number of alternative grants.
                pool = pools[0]
                size = next(n for n in (1, 2, 5, 10, 20, 50, 100) if len(part) <= n)
                stratum = (product, pool, size)
                strata[stratum] += 1
                if representative and strata[stratum] > 4:
                    continue
                all_events.append(dict(offset=(ts-start).total_seconds(), source_product=product,
                                       available_pools=len(pools), external_task_id="native:%d:%d" % (ref.key, offset),
                                       page_group_id=pool, drama_ids=part))
        begin = end
    return dict(start=start.isoformat(" "), stop_exclusive=stop.isoformat(" "), ignored=dict(ignored),
                mode="stratified_native_samples" if representative else "standalone_native_burst",
                request_count=len(all_events), item_count=sum(len(v["drama_ids"]) for v in all_events),
                events=all_events, strata=[dict(product=p, pool=g, size_max=b, available=n)
                                           for (p,g,b),n in sorted(strata.items())])


def main():
    from features.hk_sql_budget import budget_connection
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--window", choices=("burst", "hour"), required=True)
    args = parser.parse_args()
    @contextmanager
    def reader():
        deadline = time.monotonic()+80
        with budget_connection(role="read", deadline=deadline) as conn, conn.cursor() as cursor:
            yield Reads(cursor, deadline)
    if args.window == "burst":
        start, stop = datetime(2026,11,21,15,40,56), datetime(2026,11,21,15,41,6)
    else:
        start, stop = datetime(2026,11,23,12,39,32), datetime(2026,11,23,13,39,32)
    data = collect(reader, start, stop, representative=args.window == "hour")
    atomic(args.out, canon(data))
    print(json.dumps({k:v for k,v in data.items() if k not in ("events","strata")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
