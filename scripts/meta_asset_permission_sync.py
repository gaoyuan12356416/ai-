#!/usr/bin/env python3
"""Audited, one-shot Meta asset permission reconciliation (no application DB writes).

Credentials stay in memory. Reads use the existing read-only database endpoint.
Preview writes an immutable manifest; execute/resume consume that exact manifest.
"""
import argparse
import ast
import collections
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

ACCOUNT_TASKS = {"MANAGE", "ADVERTISE", "ANALYZE", "DRAFT"}
PAGE_TASKS = {"MANAGE", "ADVERTISE", "ANALYZE", "CREATE_CONTENT", "MESSAGING",
              "MODERATE", "MANAGE_LEADS", "VIEW_MONETIZATION_INSIGHTS"}
DEFAULT_BM = "163392938074395"
BLOCK_CODES = {368}
RATE_CODES = {4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def emit(stage, **kw):
    print(json.dumps({"at": now(), "stage": stage, **kw}, ensure_ascii=False, default=str), flush=True)


def normalized_id(value):
    value = str(value)
    return value[4:] if value.startswith("act_") else value


def expected_tasks(kind, existing=()):
    if kind == "page" and any(str(x).startswith("PROFILE_PLUS_") for x in existing):
        return {"PROFILE_PLUS_FULL_CONTROL"}
    return ACCOUNT_TASKS if kind == "account" else PAGE_TASKS


def full_access(kind, tasks):
    tasks = set(tasks or [])
    if kind == "page" and "PROFILE_PLUS_FULL_CONTROL" in tasks:
        return True
    return expected_tasks(kind, tasks).issubset(tasks)


def eligibility(kind, live, db):
    if not db:
        return "unknown", "missing_backend_record"
    relevant = ("account_status", "is_inactive") if kind == "account" else ("status", "forbidden_status")
    if any(db.get(k) is None for k in relevant):
        return "unknown", "missing_backend_status"
    if kind == "account":
        if db["account_status"] != 1 or db["is_inactive"] != 0:
            return "abnormal", "backend_disabled_or_inactive"
        if live.get("account_status") is None:
            return "unknown", "missing_live_account_status"
        if live["account_status"] != 1:
            return "abnormal", "meta_account_not_active"
    else:
        if db["status"] != 0 or db["forbidden_status"] != 0:
            return "abnormal", "backend_page_abnormal_or_forbidden"
        if not isinstance(live.get("is_published"), bool):
            return "unknown", "missing_live_page_status"
        if not live["is_published"] or live.get("is_permanently_closed") is True:
            return "abnormal", "page_unpublished_or_closed"
    return "normal", "eligible"


def error_kind(response):
    error = response.get("error") or {}
    code = error.get("code")
    if code in BLOCK_CODES or error.get("error_subcode") == 1390008:
        return "restricted"
    if code in RATE_CODES or response.get("http") == 429:
        return "rate_limited"
    if code == 190:
        return "credential_invalid"
    if code in (10, 200):
        return "permission_denied"
    if response.get("http", 599) >= 500 or response.get("transport_error"):
        return "unknown"
    return "failed"


class GraphError(RuntimeError):
    def __init__(self, response):
        super().__init__(error_kind(response))
        self.response = response


class Graph:
    def __init__(self, version="v26.0"):
        import requests
        self.session = requests.Session()
        self.version = version

    def call(self, method, path, credential, params=None):
        # No redirects, token in header only, never return exception strings/URLs.
        kwargs = {"headers": {"Authorization": "Bearer " + credential["accessToken"]},
                  "timeout": (6, 40), "allow_redirects": False}
        kwargs["params" if method == "GET" else "data"] = params or {}
        try:
            r = self.session.request(method, "https://graph.facebook.com/" + self.version + "/" + path, **kwargs)
            try:
                data = r.json()
            except ValueError:
                data = {}
            result = {"http": r.status_code, "data": data if isinstance(data, dict) else {},
                      "retry_after": r.headers.get("Retry-After")}
            if isinstance(data, dict) and data.get("error"):
                e = data["error"]
                result["error"] = {k: e.get(k) for k in
                                   ("code", "error_subcode", "message", "type", "fbtrace_id", "is_transient")}
                result["error"]["message"] = re.sub(r"EA[A-Za-z0-9]{30,}", "[REDACTED]",
                                                     str(result["error"].get("message") or ""))
                result["error"]["message"] = result["error"]["message"].replace(credential["accessToken"], "[REDACTED]")
            result["ok"] = 200 <= r.status_code < 300 and not result.get("error") and isinstance(data, dict)
            return result
        except Exception as exc:
            return {"http": 599, "ok": False, "transport_error": type(exc).__name__, "data": {}}

    def get(self, path, credential, params=None):
        response = self.call("GET", path, credential, params)
        if not response["ok"]:
            raise GraphError(response)
        return response["data"]

    def batch_get(self, ids, fields, credential):
        """Graph batch envelope contains GET operations exclusively (25 per call)."""
        result = {}
        for offset in range(0, len(ids), 25):
            keys = ids[offset:offset + 25]
            if any(not re.fullmatch(r"(?:act_)?\d+", k) for k in keys):
                raise ValueError("invalid_batch_object_id")
            batch = [{"method": "GET", "relative_url": key + "?" + urlencode({"fields": fields})} for key in keys]
            try:
                r = self.session.post("https://graph.facebook.com/" + self.version + "/",
                                      headers={"Authorization": "Bearer " + credential["accessToken"]},
                                      data={"batch": json.dumps(batch), "include_headers": "false"},
                                      timeout=(6, 60), allow_redirects=False)
                body = r.json()
            except Exception as exc:
                raise GraphError({"http": 599, "transport_error": type(exc).__name__}) from None
            if not isinstance(body, list) or len(body) != len(keys):
                raise GraphError({"http": r.status_code, "error": {"message": "incomplete_read_batch"}})
            for key, entry in zip(keys, body):
                try:
                    payload = json.loads((entry or {}).get("body", "{}"))
                except ValueError:
                    payload = {}
                if (entry or {}).get("code") == 200 and str(payload.get("id")) == key:
                    result[key] = payload
                else:
                    e = payload.get("error") or {}
                    error = {k: e.get(k) for k in ("code", "error_subcode", "type", "fbtrace_id")}
                    if error_kind({"http": (entry or {}).get("code", 599), "error": error}) in ("restricted", "rate_limited"):
                        raise GraphError({"http": (entry or {}).get("code", 599), "error": error})
                    result[key] = {"id": key, "read_error": error, "http": (entry or {}).get("code")}
            if (offset // 25 + 1) % 10 == 0 or offset + 25 >= len(ids):
                emit("status_read_progress", processed=min(offset + 25, len(ids)), total=len(ids))
            time.sleep(.5)
        return result

    def rows(self, path, credential, fields, params=None, stop_id=None, initial_limit=100):
        out, seen, cursor_seen = {}, set(), set()
        after, limit, retries = None, initial_limit, 0
        while True:
            q = dict(params or {}, fields=fields, limit=limit)
            if after:
                q["after"] = after
            response = self.call("GET", path, credential, q)
            if not response["ok"]:
                code = (response.get("error") or {}).get("code")
                if code == 1 and limit > 5:
                    limit = max(5, limit // 4)
                    continue
                if error_kind(response) == "unknown" and retries < 2:
                    time.sleep(2 ** (retries + 1))
                    retries += 1
                    continue
                raise GraphError(response)
            data = response["data"]
            if not isinstance(data.get("data"), list):
                raise GraphError({"http": 200, "error": {"message": "missing_data_array"}})
            retries = 0
            for item in data["data"]:
                key = str(item.get("id", ""))
                if not key:
                    raise ValueError("missing_asset_id")
                if key in out and out[key] != item:
                    raise ValueError("conflicting_pagination_record")
                out[key] = item
            if stop_id is not None and stop_id in out:
                return out
            paging = data.get("paging") or {}
            if not paging.get("next"):
                break
            after = (paging.get("cursors") or {}).get("after")
            if not after or after in cursor_seen:
                raise ValueError("incomplete_or_looping_pagination")
            cursor_seen.add(after)
            if len(cursor_seen) % 20 == 0:
                emit("read_progress", edge=path, rows=len(out), pages=len(cursor_seen))
            time.sleep(.15)
        emit("read_complete", edge=path, rows=len(out), pages=len(cursor_seen) + 1)
        return out


class Database:
    def __init__(self, config_source):
        import pymysql
        # Compatibility reader for an existing credential-bearing operational file.
        # Parse one literal assignment; do not import or execute the old script.
        tree = ast.parse(Path(config_source).read_text(encoding="utf-8"))
        args = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "MYSQL" for t in n.targets))
        opts = {a[:2]: a[2:] for a in args if a[:2] in ("-h", "-P", "-u", "-p")}
        if opts["-h"] != "101.32.56.53" or opts["-P"] != "63350":
            raise ValueError("only_readonly_endpoint_is_allowed")
        self.conn = pymysql.connect(host=opts["-h"], port=int(opts["-P"]), user=opts["-u"],
                                    password=opts["-p"], database="kunlunads_dev", charset="utf8mb4",
                                    connect_timeout=4, read_timeout=30, write_timeout=5,
                                    autocommit=True, cursorclass=pymysql.cursors.DictCursor)
        if self.query("SELECT @@read_only AS ro")[0]["ro"] != 1:
            raise ValueError("database_is_not_readonly")

    def query(self, sql, args=()):
        if not sql.lstrip().upper().startswith("SELECT "):
            raise ValueError("only_select_allowed")
        with self.conn.cursor() as cursor:
            cursor.execute(sql, args)
            return cursor.fetchall()

    def credentials(self):
        return self.query("SELECT id,user_id,facebookUserID,accessToken,updated_at FROM ads_facebook_info "
                          "WHERE accessToken IS NOT NULL AND accessToken<>'' ORDER BY id")

    def assets(self, kind, ids=None):
        if kind == "account":
            sql = ("SELECT account_id AS asset_id,name,account_status,is_inactive,updated_at "
                   "FROM ads_accounts_setting WHERE platform_id=0")
        else:
            sql = ("SELECT page_id AS asset_id,page_name AS name,status,forbidden_status,bm_id,updated_at "
                   "FROM ads_facebook_page_ins WHERE 1=1")
        if ids is not None:
            key = "account_id" if kind == "account" else "page_id"
            sql += " AND " + key + " IN (" + ",".join(["%s"] * len(ids)) + ")"
        rows = self.query(sql, tuple(ids or []))
        groups = collections.defaultdict(list)
        for row in rows:
            groups[normalized_id(row["asset_id"])].append(row)
        return {key: value[0] if len(value) == 1 else None for key, value in groups.items()}


def public_credential(c):
    return {"credential_id": c["id"], "local_user_id": c["user_id"],
            "fingerprint": hashlib.sha256(c["accessToken"].encode()).hexdigest()[:12]}


def single_credential(credentials, local_user_id):
    matches = [c for c in credentials if c["user_id"] == local_user_id]
    if len(matches) != 1:
        raise ValueError("ambiguous_local_user_credential")
    return matches[0]


def verify_identity(graph, credential, system_users):
    me = graph.get("me", credential, {"fields": "id,name"})
    match = system_users.get(me.get("id"))
    if not match or match.get("name") != me.get("name"):
        raise ValueError("token_identity_not_in_expected_bm")
    return {"local_user_id": credential["user_id"], "meta_id": me["id"], "name": me["name"],
            "bm_role": match["role"], "stored_meta_id": credential["facebookUserID"],
            "stored_id_differs": credential["facebookUserID"] != me["id"]}


def verify_admin(graph, credential, bm, system_users=None):
    roles = graph.rows("me/business_users", credential, "id,role,business")
    matches = [r for r in roles.values() if (r.get("business") or {}).get("id") == bm and r.get("role") == "ADMIN"]
    if matches:
        return {**public_credential(credential), "bm_user_id": matches[0]["id"], "proof": "business_users_ADMIN"}
    if system_users:
        me = graph.get("me", credential, {"fields": "id,name"})
        row = system_users.get(me.get("id"))
        if row and row.get("role") == "ADMIN" and row.get("name") == me.get("name"):
            return {**public_credential(credential), "bm_user_id": me["id"], "proof": "system_users_ADMIN"}
    return None


def primary_admin(graph, credentials, bm, cache_path):
    preferred = []
    path = Path(cache_path)
    if path.exists():
        cache = json.loads(path.read_text())
        preferred = [str((r.get("credential_source") or {}).get("primary_key_value"))
                     for r in (cache.get("bms", {}).get(bm, {}).get("admins", []))]
    order = sorted(credentials, key=lambda c: (str(c["id"]) not in preferred, c["id"]))
    for credential in order:
        try:
            proof = verify_admin(graph, credential, bm)
            if proof:
                return credential, proof
        except GraphError:
            continue
    raise ValueError("no_verified_admin_for_reads")


def mapping(rows):
    return {normalized_id(k): v for k, v in rows.items()}


def preview(args, db, graph, credentials):
    directory = Path(args.run_dir)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        raise ValueError("manifest_already_exists_use_new_run_directory")
    directory.mkdir(parents=True, exist_ok=True)
    admin, admin_proof = primary_admin(graph, credentials, args.business, args.admin_cache)
    users = graph.rows(args.business + "/system_users", admin, "id,name,role")
    source = single_credential(credentials, args.source)
    target = single_credential(credentials, args.target)
    identities = {"source": verify_identity(graph, source, users), "target": verify_identity(graph, target, users)}
    previous_identities = directory / "identities.json"
    if args.reuse_complete_snapshots:
        old = json.loads(previous_identities.read_text())
        if any(old[s]["meta_id"] != identities[s]["meta_id"] for s in ("source", "target")):
            raise ValueError("cached_snapshot_identity_mismatch")
    def snapshot(name, fn):
        path = directory / (name + ".json")
        if args.reuse_complete_snapshots and path.exists() and time.time() - path.stat().st_mtime < 3600:
            emit("complete_snapshot_reused", name=name)
            return json.loads(path.read_text())
        value = fn()
        write_json(path, value)
        return value
    write_json(directory / "identities.json", identities)
    emit("identities_verified", **identities)
    snapshots = {}
    for kind, edge, fields, direct in [
            ("account", "assigned_ad_accounts", "id,name,account_status,tasks,business", "adaccounts"),
            ("page", "assigned_pages", "id,name,tasks", "accounts")]:
        part = {}
        for side, cred in [("source", source), ("target", target)]:
            if kind == "page" and side == "source":
                # Meta's administrator-facing source assigned_pages edge fails
                # even with one field and limit 5. The authenticated source's
                # /me/accounts returns its actual tasks and paginates correctly.
                part[side] = snapshot("page_source_assignments", lambda: mapping(graph.rows(
                    "me/accounts", source, "id,name,tasks", initial_limit=100)))
                part[side + "_visible"] = {k: {"id": k} for k in part[side]}
                write_json(directory / "page_source_visible.json", part[side + "_visible"])
                continue
            part[side] = snapshot(kind + "_" + side + "_assignments", lambda: mapping(graph.rows(
                identities[side]["meta_id"] + "/" + edge, admin, fields, {"business": args.business})))
            part[side + "_visible"] = snapshot(kind + "_" + side + "_visible", lambda: mapping(graph.rows("me/" + direct, cred, "id")))
        snapshots[kind] = part
    # Ownership and the reader's explicit tasks establish the authority to delegate.
    owned_pages = snapshot("owned_pages", lambda: mapping(graph.rows(args.business + "/owned_pages", admin, "id")))
    client_accounts = snapshot("client_accounts", lambda: mapping(graph.rows(args.business + "/client_ad_accounts", admin, "id,user_tasks")))
    assets = []
    for kind, part in snapshots.items():
        backend = db.assets(kind)
        if kind == "page":
            normal_ids = [k for k in part["source"] if k in part["source_visible"] and backend.get(k)
                          and backend[k].get("status") == 0 and backend[k].get("forbidden_status") == 0]
            states = snapshot("page_live_status", lambda: graph.batch_get(normal_ids, "id,is_published,is_permanently_closed", source))
            for key, state in states.items():
                part["source"][key] = dict(part["source"][key], **state)
        for key in sorted(set(part["source"]) | set(part["source_visible"])):
            source_row = part["source"].get(key)
            target_row = part["target"].get(key)
            row = {"kind": kind, "asset_id": key, "name": (source_row or {}).get("name", ""),
                   "business_id": args.business, "source_tasks": (source_row or {}).get("tasks"),
                   "before_tasks": (target_row or {}).get("tasks", []), "backend": backend.get(key),
                   "source_live": source_row, "target_visible": key in part["target_visible"],
                   "target_assigned": target_row is not None}
            row["desired_tasks"] = sorted(set(row["before_tasks"]) | expected_tasks(kind, row["before_tasks"]))
            if not source_row or key not in part["source_visible"]:
                row.update(category="unknown", reason="source_assignment_visibility_disagree")
            elif not isinstance(source_row.get("tasks"), list):
                row.update(category="unknown", reason="source_tasks_missing")
            else:
                status, reason = eligibility(kind, source_row, backend.get(key))
                if status != "normal":
                    row.update(category=status, reason=reason)
                elif (target_row is not None) != row["target_visible"]:
                    row.update(category="unknown", reason="target_assignment_visibility_disagree")
                elif target_row is not None and not isinstance(target_row.get("tasks"), list):
                    row.update(category="unknown", reason="target_tasks_missing")
                elif target_row and full_access(kind, target_row.get("tasks")):
                    row.update(category="already_sufficient", reason="verified_full_access")
                else:
                    row.update(category="missing" if not target_row else "insufficient", reason="needs_full_access")
                    if kind == "page":
                        delegable = key in owned_pages or full_access("page", source_row.get("tasks"))
                        row["delegation_proof"] = "bm_owned_page" if key in owned_pages else "bm_source_system_user_full_tasks"
                    else:
                        owner = (source_row.get("business") or {}).get("id")
                        delegable = owner == args.business
                        row["delegation_proof"] = "bm_owned_account" if delegable else "admin_account_tasks"
                        if not delegable:
                            detail = client_accounts.get(key) or {}
                            row["admin_tasks"] = detail.get("user_tasks")
                            delegable = "MANAGE" in (detail.get("user_tasks") or [])
                    row["delegable"] = delegable
                    if not delegable:
                        row["difference"] = row["category"]
                        row.update(category="not_delegable", reason="full_management_not_proven_for_bm")
            assets.append(row)
    manifest = {"format_version": 1, "created_at": now(), "code_version": args.code_version,
                "graph_version": graph.version, "business_id": args.business, "identities": identities,
                "read_admin": admin_proof, "complete": True, "assets": assets,
                "source_page_assignment_evidence": "source_token_me_accounts_tasks",
                "source_page_edge_limitation": "admin_assigned_pages_code1_even_minimal_fields",
                "counts": {kind: dict(collections.Counter(r["category"] for r in assets if r["kind"] == kind))
                           for kind in ("account", "page")}}
    write_json(manifest_path, manifest)
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (directory / "manifest.sha256").write_text(digest + "\n")
    export_csv(directory / "comparison.csv", assets)
    emit("preview_complete", counts=manifest["counts"], manifest_sha256=digest)


def safe_response(response):
    return {k: response[k] for k in ("http", "error", "transport_error", "retry_after") if k in response}


def export_csv(path, assets, results=None):
    results = results or {}
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = ["kind", "asset_id", "name", "category", "reason", "source_tasks", "before_tasks",
                  "desired_tasks", "result", "result_reason", "after_tasks"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for a in assets:
            row = {k: a.get(k, "") for k in fields}
            result = results.get(a["kind"] + ":" + a["asset_id"], {})
            row.update(result=result.get("status", ""), result_reason=result.get("reason", ""),
                       after_tasks=result.get("after_tasks", []))
            for key, value in row.items():
                if isinstance(value, list):
                    row[key] = ",".join(value)
                elif isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
                    row[key] = "'" + value
            writer.writerow(row)


class Journal:
    def __init__(self, directory, digest):
        self.path = directory / "journal.jsonl"
        self.events = [json.loads(s) for s in self.path.read_text().splitlines()] if self.path.exists() else []
        if any(e.get("manifest_sha256") != digest for e in self.events):
            raise ValueError("journal_manifest_mismatch")
        self.digest = digest

    def append(self, event, **kw):
        value = {"at": now(), "manifest_sha256": self.digest, "event": event, **kw}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.events.append(value)
        return value

    def results(self):
        return {e["key"]: e["result"] for e in self.events if e["event"] == "result"}

    def unresolved(self):
        intents = {e["key"] for e in self.events if e["event"] == "intent"}
        finished = {e["key"] for e in self.events if e["event"] == "result"}
        return intents - finished


class Rotation:
    def __init__(self, candidates, clock=time.monotonic, sleep=time.sleep):
        self.candidates = list(candidates)
        self.clock, self.sleep = clock, sleep
        self.last_global, self.last_token = None, {}
        self.index, self.disabled = 0, set()

    def next(self, tried):
        for _ in self.candidates:
            c = self.candidates[self.index % len(self.candidates)]
            self.index += 1
            if c["id"] not in self.disabled and c["id"] not in tried:
                return c
        return None

    def before_write(self, c):
        t = self.clock()
        wait = max(0, (self.last_global + 5 - t) if self.last_global is not None else 0,
                   (self.last_token[c["id"]] + 30 - t) if c["id"] in self.last_token else 0)
        if wait:
            self.sleep(wait)
        self.last_global = self.clock()
        self.last_token[c["id"]] = self.last_global


def read_assignment(graph, credential, asset, target_id, bm):
    if asset["kind"] == "page":
        # Page /assigned_users GET requires pages_manage_metadata even for BM
        # administrators. The business-management user edge exposes the same
        # assignment tasks without requiring an unrelated OAuth scope.
        rows = graph.rows(target_id + "/assigned_pages", credential, "id,name,tasks",
                          {"business": bm}, stop_id=asset["asset_id"])
        return rows.get(asset["asset_id"])
    path = ("act_" if asset["kind"] == "account" else "") + asset["asset_id"]
    rows = graph.rows(path + "/assigned_users", credential, "id,name,tasks", {"business": bm})
    return rows.get(target_id)


def live_asset(graph, credential, asset):
    prefix = "act_" if asset["kind"] == "account" else ""
    fields = "id,account_status,business,user_tasks" if prefix else "id,is_published,is_permanently_closed"
    return graph.get(prefix + asset["asset_id"], credential, {"fields": fields})


def verify_after(graph, admin, target, asset, target_id, bm):
    assignment = read_assignment(graph, admin, asset, target_id, bm)
    if not assignment or not full_access(asset["kind"], assignment.get("tasks")):
        return {"status": "pending_verification", "reason": "full_tasks_not_observed",
                "after_tasks": (assignment or {}).get("tasks", [])}
    live = live_asset(graph, target, asset)
    if asset["kind"] == "account" and not full_access("account", live.get("user_tasks")):
        return {"status": "pending_verification", "reason": "target_token_tasks_not_full",
                "after_tasks": assignment["tasks"]}
    if asset["kind"] == "page":
        # The target's own system-user edge verifies tasks in addition to a public Page GET.
        rows = graph.rows(target_id + "/assigned_pages", target, "id,tasks", {"business": bm},
                          stop_id=asset["asset_id"])
        direct = rows.get(asset["asset_id"])
        if not direct or not full_access("page", direct.get("tasks")):
            return {"status": "pending_verification", "reason": "target_token_page_tasks_not_full",
                    "after_tasks": assignment["tasks"]}
    return {"status": "verified", "reason": "admin_and_target_readback_passed", "after_tasks": assignment["tasks"]}


def discover_admins(graph, credentials, bm, system_users, directory):
    # All saved unique tokens, not only historically cached administrator IDs.
    checks, admins, seen = [], [], set()
    cache_path = directory / "admin_checks.json"
    prior = json.loads(cache_path.read_text()) if cache_path.exists() else []
    reusable = {str(x["credential_id"]): x for x in prior
                if x.get("business_id") == bm and x.get("checked_at")
                and (datetime.now(timezone.utc) - datetime.fromisoformat(x["checked_at"])).total_seconds() < 1800}
    for i, c in enumerate(credentials):
        fingerprint = hashlib.sha256(c["accessToken"].encode()).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        check = {**public_credential(c), "business_id": bm, "checked_at": now()}
        cached = reusable.get(str(c["id"]))
        if cached and cached.get("fingerprint") == check["fingerprint"]:
            checks.append(cached)
            if cached.get("verified_admin"):
                admins.append(c)
            continue
        try:
            proof = verify_admin(graph, c, bm, system_users)
            check.update(verified_admin=bool(proof), proof=proof)
            if proof:
                admins.append(c)
        except GraphError as exc:
            check.update(verified_admin=False, error=safe_response(exc.response))
        checks.append(check)
        if (i + 1) % 20 == 0:
            write_json(directory / "admin_checks.json", checks)
            emit("admin_discovery", scanned=i + 1, total=len(credentials), verified=len(admins))
        time.sleep(.1)
    write_json(directory / "admin_checks.json", checks)
    emit("admin_discovery_complete", checked=len(checks), verified=len(admins))
    return admins


def execute(args, db, graph, credentials):
    directory = Path(args.run_dir)
    manifest_path = directory / "manifest.json"
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if digest != (directory / "manifest.sha256").read_text().strip():
        raise ValueError("manifest_digest_mismatch")
    manifest = json.loads(manifest_path.read_text())
    if not manifest["complete"] or manifest["business_id"] != args.business:
        raise ValueError("incomplete_or_wrong_business_manifest")
    journal = Journal(directory, digest)
    if args.command == "execute" and journal.events:
        raise ValueError("existing_journal_use_resume")
    if any(e["event"] == "bm_paused" for e in journal.events):
        emit("execution_paused", reason="existing_bm_restriction_requires_platform_resolution")
        return
    journal.append("execution_start", code_version=args.code_version, command=args.command,
                   manifest_code_version=manifest["code_version"])
    admin, _ = primary_admin(graph, credentials, args.business, args.admin_cache)
    users = graph.rows(args.business + "/system_users", admin, "id,name,role")
    source = single_credential(credentials, manifest["identities"]["source"]["local_user_id"])
    target = single_credential(credentials, manifest["identities"]["target"]["local_user_id"])
    for side, c in [("source", source), ("target", target)]:
        identity = verify_identity(graph, c, users)
        if identity["meta_id"] != manifest["identities"][side]["meta_id"]:
            raise ValueError("identity_changed_since_preview")
    target_id = manifest["identities"]["target"]["meta_id"]
    admins = discover_admins(graph, credentials, args.business, users, directory)
    if not admins:
        raise ValueError("no_current_verified_administrators")
    rotation = Rotation(admins)
    checked_actors = set()
    assets = [a for a in manifest["assets"] if a["category"] in ("missing", "insufficient")]
    # One canary per kind before proceeding with that asset class.
    assets.sort(key=lambda a: (a["kind"], a["asset_id"]))
    firsts = [next((a for a in assets if a["kind"] == kind), None) for kind in ("account", "page")]
    assets = [a for a in firsts if a] + [a for a in assets if a not in firsts]
    blocked_kinds = set()
    canary_done = set()
    results = journal.results()
    pending = journal.unresolved()
    try:
        for a in assets:
            key = a["kind"] + ":" + a["asset_id"]
            if key in results and results[key]["status"] not in ("pending_verification", "unknown"):
                if results[key]["status"] == "verified":
                    canary_done.add(a["kind"])
                elif a["kind"] not in canary_done:
                    blocked_kinds.add(a["kind"])
                continue
            if a["kind"] in blocked_kinds:
                continue
            if key in pending or key in results:
                # Resume of unknown writes performs readback only; no automatic rewrite.
                try:
                    result = verify_after(graph, admin, target, a, target_id, args.business)
                except GraphError as exc:
                    result = {"status": "unknown", "reason": "resume_readback_failed", "error": safe_response(exc.response)}
                journal.append("result", key=key, result=result)
                results[key] = result
                if result["status"] == "verified":
                    canary_done.add(a["kind"])
                elif a["kind"] not in canary_done:
                    blocked_kinds.add(a["kind"])
                continue
            tried = set()
            result = None
            while True:
                c = rotation.next(tried)
                if not c:
                    result = {"status": "failed", "reason": "no_eligible_admin_remaining"}
                    break
                tried.add(c["id"])
                try:
                    if c["id"] not in checked_actors:
                        proof = verify_admin(graph, c, args.business, users)
                        if not proof:
                            rotation.disabled.add(c["id"])
                            journal.append("admin_role_removed", actor=public_credential(c))
                            continue
                        checked_actors.add(c["id"])
                    before = read_assignment(graph, c, a, target_id, args.business)
                    live = live_asset(graph, source, a)
                    raw_id = ("act_" if a["kind"] == "account" else "") + a["asset_id"]
                    backend = db.assets(a["kind"], [raw_id]).get(a["asset_id"])
                    state, reason = eligibility(a["kind"], live, backend)
                    if state != "normal":
                        result = {"status": "skipped", "reason": reason}
                        break
                    if before and full_access(a["kind"], before.get("tasks")):
                        result = verify_after(graph, c, target, a, target_id, args.business)
                        result["reason"] = "already_full_before_write"
                        break
                    # Shared assets must still be delegable by this acting administrator.
                    if a["kind"] == "account":
                        actor = live_asset(graph, c, a)
                        if "MANAGE" not in (actor.get("user_tasks") or []):
                            journal.append("admin_not_delegable", key=key, actor=public_credential(c))
                            continue
                    if a["kind"] == "page":
                        source_assignment = graph.rows("me/accounts", source, "id,tasks", stop_id=a["asset_id"], initial_limit=100).get(a["asset_id"])
                        if source_assignment and not full_access("page", source_assignment.get("tasks")):
                            owned_now = graph.rows(args.business + "/owned_pages", c, "id", stop_id=a["asset_id"])
                            if a["asset_id"] not in owned_now:
                                result = {"status": "skipped", "reason": "page_delegation_no_longer_proven"}
                                break
                    else:
                        source_assignment = read_assignment(graph, c, a, manifest["identities"]["source"]["meta_id"], args.business)
                    if not source_assignment:
                        result = {"status": "skipped", "reason": "source_permission_removed"}
                        break
                except GraphError as exc:
                    category = error_kind(exc.response)
                    journal.append("preflight_error", key=key, actor=public_credential(c), response=safe_response(exc.response))
                    if category in ("restricted", "rate_limited"):
                        result = {"status": category, "reason": "preflight_platform_restriction", "error": safe_response(exc.response)}
                        journal.append("bm_paused", business_id=args.business, key=key, reason=category, response=safe_response(exc.response))
                        break
                    if category in ("credential_invalid", "permission_denied"):
                        if category == "credential_invalid":
                            rotation.disabled.add(c["id"])
                        continue
                    result = {"status": "unknown", "reason": "preflight_read_failed", "error": safe_response(exc.response)}
                    break
                current = set((before or {}).get("tasks") or [])
                desired = sorted(current | set(a["desired_tasks"]))
                rotation.before_write(c)
                journal.append("intent", key=key, actor=public_credential(c), target_id=target_id,
                               before_assigned=before is not None, before_tasks=sorted(current), desired_tasks=desired,
                               source_live=live, backend=backend)
                response = graph.call("POST", raw_id + "/assigned_users", c,
                                      {"user": target_id, "tasks": json.dumps(desired)})
                journal.append("response", key=key, actor=public_credential(c), response=safe_response(response),
                               acknowledged=response.get("ok") and response["data"].get("success") is True)
                category = error_kind(response) if not response.get("ok") else "submitted"
                if category == "restricted":
                    result = {"status": "restricted", "reason": "platform_restriction_no_token_bypass",
                              "error": safe_response(response)}
                    journal.append("bm_paused", business_id=args.business, key=key, reason="368_or_1390008", response=safe_response(response))
                    break
                if category == "rate_limited":
                    # Respect Retry-After; explicit resume is required, never rotate to evade it.
                    result = {"status": "rate_limited", "reason": "platform_rate_limit", "error": safe_response(response)}
                    journal.append("bm_paused", business_id=args.business, key=key, reason="rate_limit", response=safe_response(response))
                    break
                if category in ("credential_invalid", "permission_denied"):
                    if category == "credential_invalid":
                        rotation.disabled.add(c["id"])
                    continue
                if category == "failed":
                    result = {"status": "failed", "reason": "non_retryable_api_error", "error": safe_response(response)}
                    break
                try:
                    time.sleep(2)
                    result = verify_after(graph, c, target, a, target_id, args.business)
                    if category == "unknown" and result["status"] != "verified":
                        result["status"] = "unknown"
                        result["reason"] = "ambiguous_write_readback_not_confirmed"
                except GraphError as exc:
                    result = {"status": "unknown" if category == "unknown" else "pending_verification",
                              "reason": "write_readback_failed", "error": safe_response(exc.response)}
                    if error_kind(exc.response) in ("restricted", "rate_limited"):
                        journal.append("bm_paused", business_id=args.business, key=key,
                                       reason="readback_platform_restriction", response=safe_response(exc.response))
                break
            journal.append("result", key=key, result=result)
            results[key] = result
            emit("asset_result", key=key, **result)
            write_json(directory / "results.json", results)
            export_csv(directory / "results.csv", manifest["assets"], results)
            if any(e["event"] == "bm_paused" for e in journal.events):
                break
            if a["kind"] not in canary_done:
                if result["status"] == "verified":
                    canary_done.add(a["kind"])
                else:
                    blocked_kinds.add(a["kind"])
                    journal.append("canary_paused", kind=a["kind"], key=key, result=result)
    finally:
        results = journal.results()
        write_json(directory / "results.json", results)
        export_csv(directory / "results.csv", manifest["assets"], results)
        counts = dict(collections.Counter(r["status"] for r in results.values()))
        summary = {"at": now(), "manifest_sha256": digest, "counts": counts,
                   "candidates": len(assets), "unattempted": len(assets) - len(results),
                   "bm_paused": any(e["event"] == "bm_paused" for e in journal.events)}
        write_json(directory / "execution_summary.json", summary)
        emit("execution_complete", **summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preview", "execute", "resume"])
    parser.add_argument("--source", type=int, default=803)
    parser.add_argument("--target", type=int, default=916)
    parser.add_argument("--business", default=DEFAULT_BM)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--graph-version", default="v26.0")
    parser.add_argument("--code-version", required=True)
    parser.add_argument("--reuse-complete-snapshots", action="store_true", help="Reuse this run's complete <1h read checkpoints after verifying identities")
    parser.add_argument("--credential-config", default="/root/codex_test/authorize_fb_accounts_to_user913_20260819.py")
    parser.add_argument("--admin-cache", default="/root/codex_test/output/meta_bm_admin_token_cache.json")
    args = parser.parse_args()
    directory = Path(args.run_dir).resolve()
    if os.name != "nt":
        if not str(directory).startswith("/mnt/data-disk/"):
            parser.error("run directory must be on data disk")
        uuid = subprocess.check_output(["findmnt", "-no", "UUID", "/mnt/data-disk"], text=True).strip()
        if uuid != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
            parser.error("data disk mount does not match")
    directory.mkdir(parents=True, exist_ok=True)
    # Cross-process lock prevents simultaneous execute/resume of the same ledger.
    import fcntl
    with (directory / ".run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db, graph = Database(args.credential_config), Graph(args.graph_version)
        try:
            credentials = db.credentials()
            (preview if args.command == "preview" else execute)(args, db, graph, credentials)
        except GraphError as exc:
            emit("stopped", reason="graph_read_failed", response=safe_response(exc.response))
            raise SystemExit(2)
        finally:
            db.conn.close()


if __name__ == "__main__":
    main()
