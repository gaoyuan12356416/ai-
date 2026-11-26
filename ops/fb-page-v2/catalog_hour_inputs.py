"""Rebuild the complete dated peak-hour demand from its original cross/native task records."""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
import time
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from features.fb_page_assignment_v2.catalog import Reads, TABLE, Task, permissions
from features.fb_page_assignment_v2.snapshot import atomic, canon


START = datetime(2026,11,23,12,39,32)
STOP = START + timedelta(hours=1)


def batches(items,size=200):
    values=list(items)
    return [values[i:i+size] for i in range(0,len(values),size)]


def material_order(raw):
    if not raw:
        return ()
    if isinstance(raw,(bytes,bytearray)):
        raw=raw.decode()
    if isinstance(raw,str):
        try:
            parsed=json.loads(raw)
        except ValueError:
            parsed=raw
    else:
        parsed=raw
    if isinstance(parsed,dict):
        values=list(parsed)
    elif isinstance(parsed,list):
        values=parsed
    else:
        values=str(parsed).split(",")
    return tuple(dict.fromkeys(int(s) for v in values for s in str(v).split(",") if s.strip().isdecimal() and int(s)>0))


def children(raw):
    if not raw:
        return []
    try:
        obj=json.loads(raw) if isinstance(raw,(str,bytes,bytearray)) else raw
    except ValueError:
        return []
    pairs=[]
    for key,entries in (obj.items() if isinstance(obj,dict) else ()):
        bits=str(key).split(":")
        if len(bits)==2 and bits[0] in ("0","1") and bits[1].isdecimal() and isinstance(entries,list):
            pairs.extend((int(v),int(bits[1])) for v in entries if str(v).isdecimal() and int(v)>0)
    return list(dict.fromkeys(pairs))


def signature(events):
    return Counter((int(e["offset"]), len(e["drama_ids"]), e["source_product"],e["historical_pool"],
                    int(e["external_task_id"].startswith("cross:"))) for e in events)


def sample_counts(counter):
    return [dict(signature=list(k),count=v) for k,v in sorted(counter.items())]


def collect(open_reader, trace, workers=2):
    with open_reader() as reader:
        dims={1479:1479,3086:1479,979:979,3302:3302}
        for row in reader.query("SELECT id,ads_app_id FROM " + TABLE + "ads_apps_setting WHERE platform_id=0 "
                                "AND app_type IN (10000,10001) AND ads_app_id IN (1479,979,3302)"):
            dims[int(row["id"])]=int(row["ads_app_id"])

    def minute(minute_index):
        begin=START+timedelta(minutes=minute_index)
        end=begin+timedelta(minutes=1)
        wh=" FORCE INDEX(idx_created_at) WHERE created_at>=%s AND created_at<%s ORDER BY created_at,id"
        args=(begin.isoformat(" "),end.isoformat(" "))
        with open_reader() as r:
            cross=r.query("SELECT id,created_at,page_group_id,source_id,publish_queue FROM "+TABLE+
                          "ads_cross_template_queue"+wh,args)
            native=r.query("SELECT id,created_at,product,template_make_id,target_ids FROM "+TABLE+
                           "ads_template_make_queue"+wh,args)
        return cross,native

    with ThreadPoolExecutor(max_workers=workers) as pool:
        def parallel(fn, values):
            return list(pool.map(fn,values))
        cross_rows,native_inside={},{}
        for cross,native in parallel(minute,range(60)):
            cross_rows.update({int(row["id"]):row for row in cross})
            native_inside.update({int(row["id"]):row for row in native
                                  if str(row["product"]).isdecimal() and int(row["product"]) in dims})
        parent_pairs={}
        for key,row in cross_rows.items():
            for pair in children(row["publish_queue"]):
                if pair in parent_pairs and parent_pairs[pair]!=key:
                    raise ValueError("historical_native_child_collision")
                parent_pairs[pair]=key
        missing_native={n for n,_ in parent_pairs if n not in native_inside}
        all_native=dict(native_inside)
        def more_natives(keys):
            with open_reader() as r:
                return r.many("SELECT id,created_at,product,template_make_id,target_ids FROM "+TABLE+
                              "ads_template_make_queue WHERE id IN ({ids})",keys)
        for rows in parallel(more_natives,batches(sorted(missing_native),350)):
            all_native.update({int(row["id"]):row for row in rows if str(row["product"]).isdecimal()
                               and int(row["product"]) in dims})

        missing_relations=[key for key,row in native_inside.items()
                           if (key,int(row["template_make_id"] or 0)) not in parent_pairs]
        def recover(keys):
            with open_reader() as r:
                return r.many("SELECT a.publish_queue_id,a.super_queue_id,a.template_id FROM "+TABLE+
                              "ads_cross_sub_template_accounts a FORCE INDEX(idx_pqid) JOIN "+TABLE+
                              "ads_template_make_queue q ON q.id=a.publish_queue_id AND q.template_make_id=a.template_id "
                              "WHERE a.publish_queue_id IN ({ids}) AND a.template_type IN (0,1) "
                              "AND a.super_queue_id>0",keys)
        for entries in parallel(recover,batches(missing_relations,300)):
            for entry in entries:
                pair=int(entry["publish_queue_id"]),int(entry["template_id"])
                parent=int(entry["super_queue_id"])
                if pair in parent_pairs and parent_pairs[pair]!=parent:
                    raise ValueError("historical_native_parent_conflict")
                parent_pairs[pair]=parent

        # The original study deduplicated by destination product and parent,
        # using persisted FB child IDs and exact template IDs for attribution.
        original={}
        for key,row in all_native.items():
            xid=parent_pairs.get((key,int(row["template_make_id"] or 0)))
            product=dims[int(row["product"])]
            ref=Task("cross",xid) if xid else Task("native",key)
            if xid:
                if xid not in cross_rows:
                    continue
                parent=cross_rows[xid]
                entry=original.setdefault((ref,product),[parent["created_at"],int(parent["page_group_id"] or 0),
                                                         material_order(parent["source_id"]),[]])
                if not entry[2]:
                    entry[3].extend(material_order(row["target_ids"]))
            elif key in native_inside:
                original[ref,product]=[row["created_at"],0,material_order(row["target_ids"]),[]]
        unique_refs={ref for ref,_ in original}
        grants={}
        def current(keys):
            with open_reader() as r: return permissions(r,keys)[0]
        for result in parallel(current,batches(sorted(unique_refs,key=lambda t:(t.kind,t.key)),180)):
            grants.update(result)
        pool_ids={gid for gs in grants.values() for gid in gs}
        with open_reader() as r:
            available={int(row["id"]) for row in r.many("SELECT id,app_id FROM "+TABLE+
                       "ads_facebook_page_group WHERE id IN ({ids}) AND is_delete=0 "
                       "AND app_id IN (1479,979,3302)",pool_ids)}
        material_ids={k for _,_,ids,extra in original.values() for k in (ids or extra)}
        def mapping(keys):
            with open_reader() as r:
                return r.many("SELECT s.id,c.data_source_id FROM "+TABLE+"ads_source s JOIN "+TABLE+
                              "ads_custom_source c ON s.source_type=3 AND c.id=CAST(s.source_id AS UNSIGNED) "
                              "WHERE s.id IN ({ids})",keys)
        titles={}
        for mapped in parallel(mapping,batches(sorted(material_ids),350)):
            titles.update({int(row["id"]):str(row["data_source_id"]).strip() for row in mapped
                           if row["data_source_id"] and str(row["data_source_id"]).strip() not in ("","0")})

    events,unresolved=[],[]
    pool_pairs=Counter()
    reconstructed=Counter()
    for (ref,product),(ts,historical,source,extra) in original.items():
        sec=int((datetime.fromisoformat(str(ts))-START).total_seconds())
        raw=list(dict.fromkeys(source or extra))
        ids=list(dict.fromkeys(titles[k] for k in raw if k in titles))
        scope=sorted(grants.get(ref,set()) & available)
        external=f"{ref.kind}:{ref.key}"
        for i in range(0,len(ids),100):
            part=ids[i:i+100]
            reconstructed[sec,len(part),product,historical,int(ref.kind=="cross")]+=1
        reason=("material_unresolved" if not ids or any(k not in titles for k in raw)
                else "current_task_pool_unavailable_or_ambiguous" if not scope else None)
        if reason:
            unresolved.append(dict(external_task_id=external,source_product=product,offset=sec,
                                   historical_pool=historical,items=len(ids),reason=reason))
            continue
        # Cross permissions return one proven pool. Direct tasks can have two
        # real account pools, matching the deterministic choice in the burst fixture.
        selected=scope[0]
        for i in range(0,len(ids),100):
            part=ids[i:i+100]
            events.append(dict(external_task_id=f"{external}:{product}.{i}",drama_ids=part,page_group_id=selected,
                               historical_pool=historical,source_product=product,offset=sec,available_pools=len(scope)))
            pool_pairs[historical,selected]+=1
    events.sort(key=lambda e:(e["offset"],e["external_task_id"]))
    count=signature(events)
    return dict(mode="historical_tasks",start=START.isoformat(" "),stop_exclusive=STOP.isoformat(" "),
                request_count=len(events),item_count=sum(len(e["drama_ids"]) for e in events),events=events,
                distinct_tasks=len({v["external_task_id"].split(":",2)[0]+":"+v["external_task_id"].split(":",2)[1] for v in events}),
                historical_source_tasks=len(original),
                pool_transitions=[dict(historical=h,current=g,requests=n) for (h,g),n in sorted(pool_pairs.items())],
                order_basis="first occurrence in stored material list; V1 HTTP item order is unavailable for global traffic",
                historical_missing=sample_counts(trace-reconstructed),historical_extra=sample_counts(reconstructed-trace),
                missing=sample_counts(trace-count),extra=sample_counts(count-trace),unresolved=unresolved,
                exact_arrivals=(trace==count))


def expected(path):
    head=json.loads(path.read_bytes())["profiles"]["hour"]
    raw=zlib.decompress(base64.b64decode(head["rows_z"]))
    if hashlib.sha256(raw).hexdigest()!=head["sha256"]:
        raise ValueError("trace_hash_mismatch")
    values=Counter(map(tuple,json.loads(raw)))
    if sum(values.values())!=6165 or sum(key[1]*n for key,n in values.items())!=11269:
        raise ValueError("unexpected_historical_trace")
    return values


def main():
    from features.hk_sql_budget import budget_connection
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--trace",type=Path,default=Path(__file__).with_name("qa_trace.json"))
    p.add_argument("--workers",type=int,choices=(1,2,3,4),default=2)
    a=p.parse_args()
    @contextmanager
    def reader():
        deadline=time.monotonic()+50
        with budget_connection(role="read",deadline=deadline) as con,con.cursor() as cur:
            yield Reads(cur,deadline)
    data=collect(reader,expected(a.trace),a.workers)
    atomic(a.out,canon(data))
    print(json.dumps({k:v for k,v in data.items() if k not in ("events","missing","extra","historical_missing",
                    "historical_extra","unresolved","order_basis")} | {"unresolved_tasks":len(data["unresolved"]),
                      "missing_requests":sum(v["count"] for v in data["missing"]),
                      "extra_requests":sum(v["count"] for v in data["extra"])}))


if __name__=="__main__":
    main()
