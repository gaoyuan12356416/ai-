"""Replay observed arrivals from the publisher. The HTTP target must advertise an authenticated QA sink."""
import argparse
import asyncio
import base64
from collections import Counter, defaultdict
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid
import zlib


ROUTE = "/api/integrations/v2/drama-page-assignments"


def read_trace(profile):
    entry = json.loads(Path(__file__).with_name("qa_trace.json").read_text(encoding="utf8"))["profiles"][profile]
    raw = zlib.decompress(base64.b64decode(entry["rows_z"]))
    events = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != entry["sha256"] or len(events) != entry["requests"]
            or sum(e[1] for e in events) != entry["items"]):
        raise ValueError("invalid arrival trace")
    return {k:v for k,v in entry.items() if k != "rows_z"}, events


def source_kind(f):
    key = f["external_task_id"]
    if key.isdecimal():
        return 1
    if not re.fullmatch(r"(?:cross|native):[1-9]\d{0,17}(?::.+)?", key):
        raise ValueError("unverifiable fixture task")
    return int(key.startswith("cross:"))


def make_plan(trace, fixtures, representative, run_id):
    if not fixtures:
        raise ValueError("missing real publishing inputs")
    for f in fixtures:
        if (type(f["page_group_id"]) is not int or f["page_group_id"] <= 0
                or not 1 <= len(f["drama_ids"]) <= 100
                or not all(isinstance(x,str) and x.strip() == x and x for x in f["drama_ids"])):
            raise ValueError("invalid fixture")
    def signature(f):
        return (int(f["offset"]), len(f["drama_ids"]), int(f["source_product"]), source_kind(f))
    exact = defaultdict(list)
    for f in fixtures:
        exact[signature(f)].append(f)
    by_size = Counter((s, n, p, c) for s, n, p, g, c in trace)
    exact_mode = {key:len(fs) for key,fs in exact.items()} == dict(by_size)
    if not exact_mode and not representative:
        raise ValueError("historical time/product/task-type/batch-size shape differs; supply complete historical inputs")
    stats = Counter()
    plans = []
    cursor = Counter()
    indexed = {}
    for index, (sec, n, product, group, cross) in enumerate(trace):
        stratum = (n, product, group, cross)
        if exact_mode:
            fs = exact[sec,n,product,cross]
            # Every row in this stratum is a real request from that second. The old
            # parent-pool field is descriptive; current permissions decide the pool.
            # If the extraction includes the original pool, preserve that association.
            pos = next((i for i,f in enumerate(fs) if f.get("historical_pool")==group), 0)
            chosen = fs.pop(pos)
        else:
            if stratum not in indexed:
                options = [f for f in fixtures if int(f["source_product"]) == product and len(f["drama_ids"]) >= n]
                if not options:
                    raise ValueError("missing authorized fixture for product %d of at least %d items" % (product,n))
                def rank(f):
                    return (int(bool(group) and f["page_group_id"] != group), int(source_kind(f) != cross),
                            len(f["drama_ids"]) - n)
                best = min(map(rank, options))
                indexed[stratum] = [f for f in options if rank(f) == best]
            options = indexed[stratum]
            chosen = options[cursor[stratum] % len(options)]
            cursor[stratum] += 1
        source = chosen["external_task_id"]
        if source.isdecimal():
            source = "cross:" + source
        ids = chosen["drama_ids"][:n]
        if group == 0:
            stats["historical_pool_unspecified"] += 1
        elif chosen["page_group_id"] != group:
            stats["different_authorized_pool"] += 1
        if source_kind(chosen) != cross:
            stats["different_publishing_task_type"] += 1
        if len(chosen["drama_ids"]) > n:
            stats["subset_of_real_drama_ids"] += 1
        external = source.split(":",2)[:2]
        key = ":".join(external) + "::qa." + run_id + "." + str(index)
        payload = {"external_task_id":key, "page_group_id":chosen["page_group_id"], "drama_ids":ids}
        plans.append((sec, payload))
    return plans, {"exact_historical_arrivals_and_inputs": exact_mode, "differences": dict(stats),
                   "fixture_tasks": len({tuple(f["external_task_id"].split(":")[:2]) for f in fixtures}),
                   "replayed_distinct_source_tasks": len({tuple(p["external_task_id"].split(":")[:2]) for _,p in plans})}


class Client:
    def __init__(self, url, token, expect_mysql_table=None):
        u = urlsplit(url)
        if (u.scheme != "http" or u.username or u.password or u.query or u.fragment
                or u.path not in ("", "/") or not u.hostname):
            raise ValueError("use the private QA server origin without a path")
        addr = ipaddress.ip_address(u.hostname)
        if addr.version != 4 or not addr.is_private:
            raise ValueError("QA only supports a private IPv4 origin")
        self.host, self.port = str(addr), u.port or 80
        self.expect_mysql_table = expect_mysql_table
        if expect_mysql_table and not re.fullmatch(r"fb_page_assignment_qa_[0-9]{8}(?:_[a-z0-9]{1,16})?",expect_mysql_table):
            raise ValueError("invalid expected QA table")
        self.token = token.strip().encode("ascii")
        if len(self.token)<32 or any(b<33 or b>126 for b in self.token):
            raise ValueError("invalid QA credential")

    async def http(self, method, path, payload=None):
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            raw = b"" if payload is None else json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode()
            head = ("%s %s HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\nContent-Type: application/json\r\n"
                    "Content-Length: %d\r\nAuthorization: Bearer " % (method,path,self.host,self.port,len(raw))).encode()
            writer.write(head+self.token+b"\r\n\r\n"+raw)
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header[:-4].split(b"\r\n")
            status = int(lines[0].split()[1])
            headers = {k.lower():v.strip() for k,v in (line.split(b":",1) for line in lines[1:])}
            size = int(headers[b"content-length"])
            if not 0 <= size <= 512*1024:
                raise ValueError("invalid HTTP response size")
            return status, json.loads(await reader.readexactly(size))
        finally:
            writer.close()
            if hasattr(writer, "wait_closed"):
                await writer.wait_closed()

    async def status(self):
        status, body = await asyncio.wait_for(self.http("GET", "/qa/status"), 8)
        target = body.get("audit_target")
        identity = body.get("audit_identity") or {}
        mysql_ok = (target=="hk-mysql-qa-table" and identity.get("schema")=="ads_ai"
                    and identity.get("instance")==body.get("instance") and identity.get("server_uuid")
                    and re.fullmatch(r"fb_page_assignment_qa_[0-9]{8}(?:_[a-z0-9]{1,16})?",identity.get("table",""))
                    and re.fullmatch(r"[a-f0-9]{64}",identity.get("schema_sha256","")))
        if (status != 200 or body.get("qa_only") is not True or (target!="isolated-sqlite" and not mysql_ok)
                or body.get("feishu") != "disabled" or not body.get("instance")):
            raise ValueError("the endpoint has not authenticated as an isolated QA instance")
        if self.expect_mysql_table and (not mysql_ok or identity["table"] != self.expect_mysql_table):
            raise ValueError("the endpoint does not point to the requested isolated MySQL table")
        return body


def percentile(values, q):
    return round(sorted(values)[max(0, math.ceil(q*len(values))-1)], 3) if values else None


async def replay(client, plan, speed, duration, drain_timeout, report):
    baseline = await client.status()
    statuses, errors = Counter(), Counter()
    times, start_delays, task_ids = [], [], set()
    matched = null = attempts = 0
    samples = []
    beginning = time.monotonic()
    ending = asyncio.Event()
    def sample(status):
        samples.append({**status, "elapsed": round(time.monotonic()-beginning,2)})
    sample(baseline)

    async def watch():
        while not ending.is_set():
            try:
                await asyncio.wait_for(ending.wait(), 1)
            except asyncio.TimeoutError:
                try:
                    current = await client.status()
                    if current["instance"] != baseline["instance"]:
                        errors["qa_instance_changed"] += 1
                    sample(current)
                except Exception as exc:
                    errors["status_sample." + type(exc).__name__] += 1
    watcher = asyncio.ensure_future(watch())

    async def one(scheduled, payload):
        nonlocal matched, null, attempts
        start = time.monotonic()
        start_delays.append((start-scheduled)*1000)
        attempts += 1
        try:
            status, body = await asyncio.wait_for(client.http("POST", ROUTE, payload),
                                                 max(.001, 50 - (start-scheduled)))
            statuses[status] += 1
            if status != 200:
                errors["http_%d.%s" % (status,str(body.get("error","unknown"))[:80])] += 1
                return
            rows = body.get("results")
            if not isinstance(rows,list) or [r["drama_id"] for r in rows] != payload["drama_ids"]:
                raise ValueError("response_item_order_or_count")
            tids = body.get("task_id")
            if not isinstance(tids,str) or not tids or tids in task_ids:
                raise ValueError("duplicate_or_empty_receipt")
            pages = [r["page_id"] for r in rows]
            if any(p is not None and (not isinstance(p,str) or not p) for p in pages):
                raise ValueError("invalid_page_id")
            task_ids.add(tids)
            null += pages.count(None)
            matched += len(pages)-pages.count(None)
        except Exception as exc:
            code = str(exc) if type(exc) is ValueError and str(exc) in (
                "response_item_order_or_count","duplicate_or_empty_receipt","invalid_page_id") else type(exc).__name__
            errors["request."+code] += 1
        finally:
            times.append((time.monotonic()-scheduled)*1000)

    running = []
    try:
        for offset,payload in plan:
            scheduled = beginning+offset/speed
            delay = scheduled-time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            running.append(asyncio.ensure_future(one(scheduled,payload)))
        await asyncio.gather(*running)
        responses_done = time.monotonic()
        # Preserve idle seconds at the end of the historical window, including an accelerated run.
        await asyncio.sleep(max(0., beginning+duration/speed-time.monotonic()))
        final = await client.status()
        while (final["pending"] or final["unanswered"] or final["active_groups"]) and time.monotonic() < responses_done+drain_timeout:
            await asyncio.sleep(.5)
            final = await client.status()
        sample(final)
    finally:
        ending.set()
        await watcher
    if final["instance"] != baseline["instance"]:
        errors["qa_instance_changed"] += 1
    expected_items = sum(len(p["drama_ids"]) for _,p in plan)
    deltas = {key:final[key]-baseline[key] for key in ("receipts","confirmed","audit_headers","audit_items","audit_retries")}
    load_pass = (not errors and attempts == len(plan) == len(task_ids) == statuses[200] and not final["pending"]
          and not final["unretired_details"] and not final["unretired_snapshots"]
          and max(times)<=50000 and sum(times)/len(times)<=10000
          and matched+null == expected_items and deltas["receipts"] == deltas["confirmed"] == len(plan)
          and deltas["audit_headers"] == len(plan) and deltas["audit_items"] == expected_items)
    null_causes = dict(Counter(final["null_causes"])-Counter(baseline["null_causes"]))
    stages = dict(Counter(final["stages"])-Counter(baseline["stages"]))
    ok = load_pass and matched > 0 and not null_causes.get("drama_not_in_task")
    report.update(ok=ok, load_criteria_met=load_pass, positive_selection_observed=matched>0,
                  audit_target=final["audit_target"], audit_identity=final.get("audit_identity"),
                  sql_during_audit=final.get("sql_during_audit"),
                  stages=stages, null_causes=null_causes,
                  origin=client.host, instance=final["instance"], speed_multiplier=speed,
                  historical_seconds=duration, target_wall_seconds=duration/speed, attempted=attempts,
                  unique_receipts=len(task_ids), items=matched+null, assigned=matched, null=null,
                  errors=dict(errors), http_status=dict(statuses), e2e_latency_ms={
                      "mean":round(sum(times)/len(times),2) if times else None,
                      "p50":percentile(times,.5), "p95":percentile(times,.95), "p99":percentile(times,.99),
                      "max":percentile(times,1)}, start_lag_ms={"p99":percentile(start_delays,.99),"max":percentile(start_delays,1)},
                  wall_to_last_response=round(responses_done-beginning,3),
                  drain_wait_seconds=round(time.monotonic()-responses_done,3), audit_delta=deltas,
                  max_sampled_pending=max(s["pending"] for s in samples),
                  max_sampled_sql_used=max((s["sql_budget"]["in_use"] for s in samples if s.get("sql_budget")),default=None),
                  samples=samples)
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin",default="http://127.0.0.1:8945")
    parser.add_argument("--expect-mysql-table",help="require this exact isolated HK table before sending any requests")
    parser.add_argument("--token-file",type=Path,required=True)
    parser.add_argument("--inputs",type=Path,required=True)
    parser.add_argument("--profile",choices=("burst","hour"),required=True)
    parser.add_argument("--allow-representative",action="store_true")
    parser.add_argument("--speed",type=float,default=1.)
    parser.add_argument("--drain-timeout",type=float,default=300.)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    if not math.isfinite(args.speed) or args.speed <= 0 or args.drain_timeout <= 0:
        parser.error("speed and drain timeout must be positive")
    trace_meta, trace = read_trace(args.profile)
    run_id = uuid.uuid4().hex[:16]
    fixtures = json.loads(args.inputs.read_text(encoding="utf8"))["events"]
    plan, mapping = make_plan(trace, fixtures, args.allow_representative, run_id)
    report = {"run":run_id,"profile":args.profile,"trace":trace_meta,"inputs":mapping}
    client = Client(args.origin,args.token_file.read_text(),args.expect_mysql_table)
    # Reserve the distinct report first, so a repeated invocation cannot destroy previous evidence.
    with args.output.open("x",encoding="utf8") as out:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ok = loop.run_until_complete(replay(client,plan,args.speed,trace_meta["seconds"],args.drain_timeout,report))
        except Exception as exc:
            report.update(ok=False,aborted=type(exc).__name__,detail=str(exc)[:160])
            raise
        finally:
            loop.close()
            asyncio.set_event_loop(None)
            json.dump(report,out,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k!="samples"},ensure_ascii=False))
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
