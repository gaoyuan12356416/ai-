"""Batch lookups share facts. Decisions and their tie-breaking counters are per sub-batch."""
from __future__ import annotations

from collections import Counter, defaultdict, OrderedDict
from heapq import nsmallest
import hashlib
import sqlite3
import time
import traceback

from features.fb_page_assignment.runtime import log
from features.fb_page_assignment.selector import KNOWN_AUDIENCES, _load_order, _match, _number, _profile
from .catalog import Reads, SelectionError, TABLE, page_states, pool_products, dramas_for_pools
from .snapshot import Catalog, canon, topics

POLICY = "positive-profile-v2.2-direct"
STATES = ("page_id", "language", "status", "forbidden_status", "is_permission",
          "active_ad_count", "fan_ccount", "updated_at")


def log_dependency_failure(step, exc, requests):
    # Never log a driver message, SQL, request body or frame locals.
    stack = []
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        module = frame.f_globals.get("__name__", "")
        if module.startswith(("features.fb_page_assignment_v2.", "features.hk_sql_budget.", "pymysql.")):
            stack.append(f"{module}.{frame.f_code.co_name}:{lineno}")
    cause = exc.__cause__ or exc
    code = cause.args[0] if cause.args and type(cause.args[0]) is int else None
    busy = isinstance(cause, sqlite3.OperationalError) and str(cause) in ("database is locked", "database table is locked")
    log("v2_selection_dependency_failure", step=step, category=type(cause).__name__,
        driver_code=code, sqlite_busy=busy, frames=stack,
        request_ids=[r["request_id"] for r in requests if r.get("request_id")])


def eligible_pages(raw, profiles):
    lanes = defaultdict(list)
    for row in raw:
        pid, lang, status, forbidden, permission, active, fans, _ = row
        if (_number(status), _number(forbidden), _number(permission)) != (0, 0, 1):
            continue
        p = profiles.get(pid)
        if p is not None and p["kind"] == "profiled":
            lanes[lang].append(dict(page_id=pid, profile=p, active_ad_count=_number(active),
                                    fan_ccount=_number(fans)))
    return lanes


def decide(ids, dramas, lanes, score_cache=None):
    allocations, output = Counter(), []
    score_cache = {} if score_cache is None else score_cache
    for idx, cid in enumerate(ids, 1):
        d = dramas[cid]
        row = dict(drama_id=cid, item_index=idx, page_id=None, status="unmatched",
                   selection_stage="none", error_code=d.get("error") or "no_profile_match",
                   error_message="", diagnostics={})
        output.append(row)
        if d.get("error"):
            continue
        audience, themes = d["audience"], tuple(sorted(set(d["themes"])))
        if audience not in KNOWN_AUDIENCES or not themes:
            row["error_code"] = "drama_profile_missing"
            continue
        stages = [("", "fallback_profile")]
        if d["language"]:
            stages.insert(0, (d["language"], "language_profile"))
        for lang, stage in stages:
            key = (lang, audience, themes)
            if key not in score_cache:
                scored = []
                for page in lanes.get(lang, ()):
                    score = _match(page, audience, set(themes))[0]
                    if score > 0:
                        scored.append((score, page))
                score_cache[key] = scored
            if not score_cache[key]:
                continue
            ranked = nsmallest(3, score_cache[key],
                               key=lambda pair: (-pair[0], _load_order(pair[1], allocations)))
            score, page = min(ranked, key=lambda pair: _load_order(pair[1], allocations))
            pid = page["page_id"]
            row.update(page_id=pid, selection_stage=stage, status="assigned", error_code="",
                       diagnostics={"match_score_bp": score, "batch_assigned_count": allocations[pid]})
            allocations[pid] += 1
            break
    return output


class Selector:
    def __init__(self, catalog=None, connection=None, freeze=None):
        if connection is None:
            from features.hk_sql_budget import budget_connection
            connection = budget_connection
        if freeze is None:
            from .ledger import freeze_candidates
            freeze = freeze_candidates
        self.catalog, self.connection, self.freeze = catalog or Catalog(), connection, freeze
        self.digest = None
        self.profiles, self.cold = {}, OrderedDict()

    def preload(self):
        head, data = self.catalog.current()
        if head["sha256"] != self.digest:
            self.profiles = {pid: _profile(val) for pid, val in data["pages"].items()}
            self.cold.clear()
            self.digest = head["sha256"]
        return head, data

    def hydrate(self, data, facts, groups, requests, viable, deadline):
        """Only exact misses with known authoritative direction need resource rows.
        The normal path uses versioned themes. Misses are deduplicated and read four
        titles at a time; a failed optional read must never become a business null.
        """
        cold_ids = defaultdict(set)
        for i in viable:
            req = requests[i]
            for cid in req["drama_ids"]:
                d = facts[req["page_group_id"], cid]
                if d.get("error"):
                    continue
                app, lang = str(d["app"]), d["language"]
                direction = data["directions"].get(app, {}).get(cid)
                d["audience"] = direction[0] if direction and lang in direction[1] else "__unknown__"
                warm = data["warm"].get(app, {})
                if cid in warm:
                    d["themes"] = warm[cid].get(lang, [])
                    d["theme_source"] = "published"
                elif (app, cid) in self.cold:
                    d["themes"] = self.cold[app, cid].get(lang, [])
                    d["theme_source"] = "indexed_lookup"
                else:
                    d["themes"] = []
                    if d["audience"] in KNOWN_AUDIENCES:
                        lanes = groups[req["page_group_id"]][0]
                        if any(d["audience"] in p["profile"]["branches"]
                               for key in (lang, "") for p in lanes.get(key, ())):
                            cold_ids[app].add(cid)
        failed = set()
        for app, ids in cold_ids.items():
            ids = sorted(ids)
            for offset in range(0, len(ids), 4):
                part = ids[offset:offset+4]
                if time.monotonic() >= deadline:
                    failed.update((app, cid) for cid in ids[offset:])
                    break
                try:
                    with self.connection(role="read", deadline=deadline) as conn, conn.cursor() as cur:
                        rows = Reads(cur, deadline).many(
                            "SELECT DISTINCT content_id,series_code,language,labels FROM " + TABLE +
                            "ads_drama_resource FORCE INDEX(content_id) WHERE app_id=%s AND "
                            "content_id IN ({ids}) LIMIT 20001", part, before=(int(app),), timeout=3000)
                        if len(rows) >= 20001:
                            raise SelectionError(503, "resource_row_limit")
                    classified = defaultdict(list)
                    for row in rows:
                        classified[str(row["content_id"])].append(row)
                    for cid in part:
                        self.cold[app, cid] = topics(classified[cid], data["aliases"], data["themes"])
                    while len(self.cold) > 8192:
                        self.cold.popitem(last=False)
                except Exception:
                    failed.update((app, cid) for cid in part)
        for (_, cid), d in facts.items():
            if d.get("error"):
                continue
            key = (str(d["app"]), cid)
            if key in failed:
                d["lookup_failed"] = True
            elif key in self.cold:
                d["themes"] = self.cold[key].get(d["language"], [])
                d["theme_source"] = "indexed_lookup"

    def select_batch(self, requests, deadline):
        errors = [None] * len(requests)
        if not requests:
            return errors
        step = "load_catalog"
        try:
            head, data = self.preload()
            step = "open_read_connection"
            with self.connection(role="read", deadline=deadline) as conn, conn.cursor() as cur:
                reader = Reads(cur, deadline)
                step = "read_pool_products"
                products = pool_products(reader, {req["page_group_id"] for req in requests})
                viable = set()
                for i, req in enumerate(requests):
                    if req["page_group_id"] not in products:
                        errors[i] = SelectionError(403, "publication_pool_unavailable")
                    else:
                        viable.add(i)
                if not viable:
                    return errors
                step = "read_page_states"
                pools = page_states(reader, set(products), products=products)
                valid_inputs = defaultdict(set)
                for i in viable:
                    valid_inputs[requests[i]["page_group_id"]].update(requests[i]["drama_ids"])
                step = "read_pool_dramas"
                facts = dramas_for_pools(reader, products, valid_inputs)
                step = "close_read_connection"
            step = "prepare_candidates"
            groups = {}
            for group, raw in pools.items():
                frozen = self.freeze(raw)
                groups[group] = (eligible_pages(raw, self.profiles), frozen,
                                 getattr(frozen, "digest", None) or hashlib.sha256(canon(raw)).hexdigest(), {})
            step = "resolve_drama_profiles"
            self.hydrate(data, facts, groups, requests, viable, deadline)
            step = "rank_pages"
            for i in viable:
                req = requests[i]
                ids, gid = req["drama_ids"], req["page_group_id"]
                dramas = {cid: facts[gid, cid] for cid in ids}
                if any(d.get("lookup_failed") for d in dramas.values()):
                    errors[i] = SelectionError(503, "drama_theme_lookup_failed")
                    continue
                lanes, candidates, digest, scores = groups[gid]
                rows = decide(ids, dramas, lanes, scores)
                errors[i] = dict(rows=rows, snapshot=dict(
                    policy=POLICY, catalog_version=head["version"], catalog_sha256=head["sha256"],
                    pool_group_id=gid, pool_app_id=products[gid], candidate_sha256=digest, candidate_fields=STATES,
                    candidates=candidates, dramas=dramas))
        except Exception as exc:
            code = exc.code if isinstance(exc, SelectionError) else "selection_dependency_unavailable"
            if not isinstance(exc, SelectionError):
                log_dependency_failure(step, exc, requests)
            for i in range(len(requests)):
                if errors[i] is None:
                    errors[i] = SelectionError(503, code)
        return errors
