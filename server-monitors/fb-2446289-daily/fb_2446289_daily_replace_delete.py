#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import collections
import csv
import fcntl
import glob
import importlib.util
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from urllib import error, parse, request


BASE_DIR = Path("/root/codex_test")
OUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"
EXEC_MODULE_PATH = BASE_DIR / "execute_prepared_fb_replace_delete_20260527.py"
FEISHU_CONFIG_PATH = BASE_DIR / "fb_2446289_feishu_config.json"
FEISHU_CONFIG_SOURCE = BASE_DIR / "watch_fb_replace_delete_completion_feishu_20260528.py"
LOCK_PATH = Path("/tmp/fb_2446289_daily_replace_delete.lock")
SUCCESS_HISTORY_PATH = OUT_DIR / "fb_2446289_success_history.json"
PAGE_HEALTH_CACHE_PATH = OUT_DIR / "fb_2446289_page_health_cache.json"
SEC_PORT = "63348"
GRAPH_VERSION = "v22.0"
DEFAULT_WORKERS = 16
DEFAULT_GRAPH_TIMEOUT = 25
MYSQL_RETRY_DELAYS = (1, 3)
MYSQL_RETRYABLE_CODES = frozenset((1040, 1203, 2002, 2003, 2006, 2013, 2055))
SUCCESS_STATUSES = {
    "deleted",
    "already_deleted",
    "already_deleted_or_not_found",
    "delete_retry_success",
    "delete_not_found_treated_success",
    "delete_unverified",
}
PAGE_CACHE_TTL_SECONDS = 7 * 24 * 3600
MATCH_CONDITION = (
    "ads_rules_run_logs: last24h ad/delete status=2 extension contains code=100 "
    "and error_subcode=2446289"
)


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def stamp_str():
    return time.strftime("%Y%m%d_%H%M%S")


def log(message):
    print("%s [fb-2446289-daily] %s" % (now_str(), message), flush=True)


def load_exec_module():
    spec = importlib.util.spec_from_file_location("fb_replace_delete_execmod", str(EXEC_MODULE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_exec_module()
_orig_graph_request = mod.graph_request
_orig_graph_upload_file = mod.graph_upload_file


def patch_graph_timeouts(timeout):
    def fast_graph_request(method, path, token, params=None, timeout=timeout, retries=1):
        return _orig_graph_request(method, path, token, params, timeout=timeout, retries=1)

    def fast_upload_file(path, token, params, field_name="filename", timeout=45, retries=1):
        return _orig_graph_upload_file(path, token, params, field_name=field_name, timeout=45, retries=1)

    mod.graph_request = fast_graph_request
    mod.graph_upload_file = fast_upload_file


def mysql_failure_details(stderr, query):
    """Keep error codes and gate diagnostics, never SQL or credential-bearing text."""
    codes = [int(code) for code in re.findall(r"\bERROR\s+(\d+)\b", stderr or "", re.I)]
    diagnostics = []
    safe_fields = {"event", "job", "pool", "version", "wait_seconds", "attempt", "code"}
    for line in (stderr or "").splitlines():
        if re.fullmatch(r"SQL_GATE_[A-Z_]*(?:FAILED|LOST|TIMEOUT)", line):
            diagnostics.append(line)
            continue
        match = re.match(r"ERROR\s+(\d+)(?:\s+\([A-Z0-9]+\))?", line, re.I)
        if match:
            if int(match.group(1)) in MYSQL_RETRYABLE_CODES:
                # Connection errors carry useful handshake/socket diagnostics.
                line = line.replace(mod.MYSQL_PASSWORD, "[redacted]").replace(query, "[SQL redacted]")
                line = re.sub(r"(?i)\b(access_token|token|password)\s*[=:]\s*\S+", r"\1=[redacted]", line)
                diagnostics.append(line)
            else:
                # Syntax/server errors can echo fragments of the SQL statement.
                diagnostics.append(match.group(0))
            continue
        if not line.startswith("SQL_GATE "):
            continue
        try:
            payload = json.loads(line[len("SQL_GATE "):])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        safe_payload = {key: value for key, value in payload.items() if key in safe_fields}
        diagnostics.append("SQL_GATE " + json.dumps(safe_payload, sort_keys=True))
    return codes, "\n".join(diagnostics)


def mysql_query(port, sql, timeout=180):
    # All callers issue one SELECT. Never expand retries to the Meta mutation loop.
    query = " ".join(sql.split()).rstrip(";")
    if not re.match(r"^SELECT\s", query, re.I) or ";" in query:
        raise ValueError("mysql_query accepts a single SELECT only")
    env = os.environ.copy()
    env["MYSQL_PWD"] = mod.MYSQL_PASSWORD
    env["SQL_GATE_BYPASS"] = "0"
    env["SQL_GATE_RETRY_DELAYS_SECONDS"] = ""
    env["SQL_GATE_JOB_NAME"] = "fb2446289-daily"
    cmd = [
        "mysql",
        "-h%s" % mod.MYSQL_HOST,
        "-P%s" % port,
        "-u%s" % mod.MYSQL_USER,
        "--connect-timeout=5",
        "--default-character-set=utf8mb4",
        "-N",
        "-B",
        "--raw",
        "-e",
        query,
    ]
    attempts = len(MYSQL_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            # TimeoutExpired includes the command/SQL; don't leak it through traceback.
            raise RuntimeError("MySQL SELECT timed out (port=%s, attempt=%s, timeout=%ss); not retried" % (port, attempt, timeout)) from None
        if not proc.returncode:
            return list(csv.reader(proc.stdout.splitlines(), delimiter="\t"))
        codes, diagnostics = mysql_failure_details(proc.stderr, query)
        gate_failed = proc.returncode in (75, 124, 125) or bool(re.search(r"\bSQL_GATE_[A-Z_]*(?:FAILED|LOST|TIMEOUT)\b", proc.stderr or ""))
        retryable = not gate_failed and bool(codes) and set(codes).issubset(MYSQL_RETRYABLE_CODES)
        code_text = ",".join(str(code) for code in codes) or "unknown"
        if retryable and attempt < attempts:
            delay = MYSQL_RETRY_DELAYS[attempt - 1]
            log("MySQL SELECT transient failure port=%s attempt=%s/%s code=%s; retry in %ss" % (port, attempt, attempts, code_text, delay))
            time.sleep(delay)
            continue
        message = "MySQL SELECT failed (port=%s, attempt=%s/%s, code=%s, exit=%s)" % (port, attempt, attempts, code_text, proc.returncode)
        if diagnostics:
            message += "\n" + diagnostics
        raise RuntimeError(message)


def executor_mysql_query(sql, timeout=120):
    return mysql_query(mod.DEV_PORT, sql, timeout=timeout)


# Also protect token/account SELECTs from the imported executor, without changing
# its shared source or introducing retries around any Graph request.
mod.mysql_query = executor_mysql_query


def sql_quote(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def chunks(items, size):
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def normalize_account_id(account_id):
    return mod.normalize_account_id(account_id)


def account_query_values(account_ids):
    values = set()
    for account_id in account_ids:
        account_id = str(account_id or "").strip()
        if not account_id:
            continue
        values.add(account_id)
        if account_id.startswith("act_"):
            values.add(account_id.replace("act_", "", 1))
        else:
            values.add("act_" + account_id)
    return sorted(values)


def load_feishu_config():
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    chat_id = os.environ.get("FEISHU_CHAT_ID")
    if app_id and app_secret and chat_id:
        return {
            "app_id": app_id,
            "app_secret": app_secret,
            "receive_id_type": os.environ.get("FEISHU_RECEIVE_ID_TYPE") or "chat_id",
            "receive_id": os.environ.get("FEISHU_RECEIVE_ID") or chat_id,
            "chat_id": chat_id,
        }

    if FEISHU_CONFIG_PATH.exists():
        payload = json.loads(FEISHU_CONFIG_PATH.read_text(encoding="utf-8"))
        payload.setdefault("receive_id_type", "chat_id")
        payload.setdefault("receive_id", payload.get("chat_id"))
        return payload

    text = FEISHU_CONFIG_SOURCE.read_text(encoding="utf-8")

    def extract(name):
        match = re.search(r'^%s\s*=\s*"([^"]+)"' % re.escape(name), text, re.M)
        if not match:
            raise RuntimeError("missing %s in %s" % (name, FEISHU_CONFIG_SOURCE))
        return match.group(1)

    chat_id = extract("CHAT_ID")
    return {
        "app_id": extract("APP_ID"),
        "app_secret": extract("APP_SECRET"),
        "receive_id_type": "chat_id",
        "receive_id": chat_id,
        "chat_id": chat_id,
    }


def post_json(url, payload, token=None, timeout=30):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tenant_token(app_id, app_secret):
    data = post_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if data.get("code") != 0:
        raise RuntimeError("tenant token failed: " + json.dumps(data, ensure_ascii=False))
    return data["tenant_access_token"]


def send_feishu_text(text, notify=True):
    if not notify:
        log("notify disabled; message follows:\n%s" % text)
        return {"skipped": True}
    cfg = load_feishu_config()
    app_id = cfg["app_id"]
    app_secret = cfg["app_secret"]
    receive_id_type = cfg.get("receive_id_type") or "chat_id"
    receive_id = cfg.get("receive_id") or cfg.get("chat_id")
    last_exc = None
    for attempt in range(1, 4):
        try:
            token = tenant_token(app_id, app_secret)
            url = "https://open.feishu.cn/open-apis/im/v1/messages?" + parse.urlencode({"receive_id_type": receive_id_type})
            payload = {"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
            data = post_json(url, payload, token=token)
            if data.get("code") != 0:
                raise RuntimeError("send failed: " + json.dumps(data, ensure_ascii=False))
            return data
        except Exception as exc:
            last_exc = exc
            log("Feishu send attempt %s failed: %r" % (attempt, exc))
            time.sleep(5 * attempt)
    raise last_exc


def safe_notify(text, notify=True):
    try:
        return send_feishu_text(text, notify=notify)
    except Exception:
        log("Feishu notification failed:\n" + traceback.format_exc())
        return None


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def screen_failed_ads(hours, csv_path, metadata_path):
    log("screening failed ads for last %s hours" % hours)
    fail_sql = """
        SELECT
          CAST(facebook_id AS CHAR) AS ad_id,
          account_id,
          MIN(created_at) AS first_failed_at,
          MAX(created_at) AS last_failed_at,
          COUNT(*) AS fail_count
        FROM kunlunads_sec.ads_rules_run_logs
        WHERE created_at >= NOW() - INTERVAL %s HOUR
          AND type = 'ad'
          AND operation = 'delete'
          AND status = 2
          AND extension LIKE '%%code%%100%%'
          AND extension LIKE '%%error_subcode%%2446289%%'
        GROUP BY facebook_id, account_id
        ORDER BY last_failed_at DESC, ad_id
    """ % int(hours)
    fail_rows = mysql_query(SEC_PORT, fail_sql, timeout=240)
    target_ad_ids = [row[0] for row in fail_rows if row and row[0]]
    log("screened target rows=%s" % len(target_ad_ids))

    target_auto = {}
    target_page = {}
    target_batches = (len(target_ad_ids) + 799) // 800
    for batch, part in enumerate(chunks(target_ad_ids, 800), 1):
        in_list = ",".join(sql_quote(x) for x in part)
        auto_sql = """
            SELECT ad_id, ad_account_id, creative_id, status, local_status, updated_at
            FROM kunlunads_dev.ads_facebook_auto_created_data FORCE INDEX(ad_id)
            WHERE ad_id IN (%s)
        """ % in_list
        for ad_id, account_id, creative_id, status, local_status, updated_at in mysql_query(mod.DEV_PORT, auto_sql, timeout=60):
            target_auto[str(ad_id)] = {
                "ad_account_id": normalize_account_id(account_id),
                "creative_id": creative_id,
                "status": status,
                "local_status": local_status,
                "updated_at": updated_at,
            }

        page_sql = """
            SELECT ad_id, page_id
            FROM kunlunads_dev.ads_facebook_auto_page FORCE INDEX(idx_ad)
            WHERE ad_id IN (%s)
        """ % in_list
        for ad_id, page_id in mysql_query(mod.DEV_PORT, page_sql, timeout=60):
            if page_id and str(ad_id) not in target_page:
                target_page[str(ad_id)] = page_id
        if batch == 1 or batch % 25 == 0 or batch == target_batches:
            log("target metadata batch=%s/%s rows=%s/%s" % (batch, target_batches, min(batch * 800, len(target_ad_ids)), len(target_ad_ids)))

    accounts = set()
    fail_by_ad = {}
    for ad_id, account_id, first_failed_at, last_failed_at, fail_count in fail_rows:
        info = target_auto.get(str(ad_id), {})
        account_id = normalize_account_id(account_id or info.get("ad_account_id"))
        if account_id:
            accounts.add(account_id)
        fail_by_ad[str(ad_id)] = {
            "ad_id": str(ad_id),
            "account_id": account_id,
            "first_failed_at": first_failed_at,
            "last_failed_at": last_failed_at,
            "fail_count": fail_count,
        }

    success_rows = []
    query_accounts = account_query_values(accounts)
    for part in chunks(query_accounts, 300):
        in_accounts = ",".join(sql_quote(x) for x in part)
        success_sql = """
            SELECT account_id, CAST(facebook_id AS CHAR) AS ad_id, MAX(created_at) AS last_success_at, COUNT(*) AS success_count
            FROM kunlunads_sec.ads_rules_run_logs
            WHERE created_at >= NOW() - INTERVAL %s HOUR
              AND type = 'ad'
              AND operation = 'delete'
              AND status = 0
              AND account_id IN (%s)
            GROUP BY account_id, facebook_id
            ORDER BY last_success_at DESC
        """ % (int(hours), in_accounts)
        success_rows.extend(mysql_query(SEC_PORT, success_sql, timeout=180))

    success_ad_ids = [row[1] for row in success_rows if len(row) >= 2 and row[1] and row[1] not in fail_by_ad]
    replacement_auto_by_ad = {}
    replacement_page_by_ad = {}
    replacement_batches = (len(success_ad_ids) + 799) // 800
    for batch, part in enumerate(chunks(success_ad_ids, 800), 1):
        in_list = ",".join(sql_quote(x) for x in part)
        auto_sql = """
            SELECT ad_id, ad_account_id, creative_id, status, local_status
            FROM kunlunads_dev.ads_facebook_auto_created_data FORCE INDEX(ad_id)
            WHERE ad_id IN (%s)
              AND creative_id IS NOT NULL
              AND creative_id <> ''
        """ % in_list
        for ad_id, account_id, creative_id, status, local_status in mysql_query(mod.DEV_PORT, auto_sql, timeout=60):
            replacement_auto_by_ad[str(ad_id)] = {
                "ad_account_id": normalize_account_id(account_id),
                "creative_id": creative_id,
                "status": status,
                "local_status": local_status,
            }

        page_sql = """
            SELECT ad_id, page_id
            FROM kunlunads_dev.ads_facebook_auto_page FORCE INDEX(idx_ad)
            WHERE ad_id IN (%s)
        """ % in_list
        for ad_id, page_id in mysql_query(mod.DEV_PORT, page_sql, timeout=60):
            if page_id and str(ad_id) not in replacement_page_by_ad:
                replacement_page_by_ad[str(ad_id)] = page_id
        if batch == 1 or batch % 25 == 0 or batch == replacement_batches:
            log("replacement metadata batch=%s/%s rows=%s/%s" % (batch, replacement_batches, min(batch * 800, len(success_ad_ids)), len(success_ad_ids)))

    rng = random.Random(str(csv_path.name))
    rng.shuffle(success_rows)
    replacement_by_account = {}
    for account_id, ad_id, last_success_at, success_count in success_rows:
        auto = replacement_auto_by_ad.get(str(ad_id))
        if not auto or not auto.get("creative_id"):
            continue
        normalized = normalize_account_id(account_id or auto.get("ad_account_id"))
        if normalized not in accounts or normalized in replacement_by_account:
            continue
        replacement_by_account[normalized] = {
            "replacement_ad_id": str(ad_id),
            "replacement_creative_id": auto.get("creative_id"),
            "replacement_page_id": replacement_page_by_ad.get(str(ad_id), ""),
            "replacement_last_success_at": last_success_at,
            "replacement_success_count": success_count,
            "replacement_auto_status": auto.get("status", ""),
            "replacement_auto_local_status": auto.get("local_status", ""),
        }

    fields = [
        "ad_id",
        "account_id",
        "creative_id",
        "page_id",
        "replacement_creative_id",
        "replacement_ad_id",
        "replacement_page_id",
        "replacement_last_success_at",
        "replacement_success_count",
        "first_failed_at",
        "last_failed_at",
        "fail_count",
        "target_auto_status",
        "target_auto_local_status",
        "target_auto_updated_at",
        "replacement_auto_status",
        "replacement_auto_local_status",
        "match_condition",
    ]
    rows = []
    seen = set()
    for ad_id, account_id, first_failed_at, last_failed_at, fail_count in fail_rows:
        ad_id = str(ad_id)
        if ad_id in seen:
            continue
        seen.add(ad_id)
        target = target_auto.get(ad_id, {})
        account_id = normalize_account_id(account_id or target.get("ad_account_id"))
        replacement = replacement_by_account.get(account_id, {})
        rows.append(
            {
                "ad_id": ad_id,
                "account_id": account_id,
                "creative_id": target.get("creative_id", ""),
                "page_id": target_page.get(ad_id, ""),
                "replacement_creative_id": replacement.get("replacement_creative_id", ""),
                "replacement_ad_id": replacement.get("replacement_ad_id", ""),
                "replacement_page_id": replacement.get("replacement_page_id", ""),
                "replacement_last_success_at": replacement.get("replacement_last_success_at", ""),
                "replacement_success_count": replacement.get("replacement_success_count", ""),
                "first_failed_at": first_failed_at,
                "last_failed_at": last_failed_at,
                "fail_count": fail_count,
                "target_auto_status": target.get("status", ""),
                "target_auto_local_status": target.get("local_status", ""),
                "target_auto_updated_at": target.get("updated_at", ""),
                "replacement_auto_status": replacement.get("replacement_auto_status", ""),
                "replacement_auto_local_status": replacement.get("replacement_auto_local_status", ""),
                "match_condition": MATCH_CONDITION,
            }
        )

    write_csv(csv_path, rows, fields)
    metadata = {
        "created_at": now_str(),
        "hours": hours,
        "csv_path": str(csv_path),
        "metadata_path": str(metadata_path),
        "target_ad_ids": len(rows),
        "target_accounts": len(set(row["account_id"] for row in rows if row.get("account_id"))),
        "with_replacement_creative_id": sum(1 for row in rows if row.get("replacement_creative_id")),
        "no_replacement_creative_id": sum(1 for row in rows if not row.get("replacement_creative_id")),
        "missing_target_creative_id": sum(1 for row in rows if not row.get("creative_id")),
        "missing_target_page_id": sum(1 for row in rows if not row.get("page_id")),
        "match_condition": MATCH_CONDITION,
    }
    write_json(metadata_path, metadata)
    return rows, metadata


def load_success_history():
    if SUCCESS_HISTORY_PATH.exists():
        try:
            payload = json.loads(SUCCESS_HISTORY_PATH.read_text(encoding="utf-8"))
            return set(str(x) for x in payload.get("ad_ids", []))
        except Exception:
            log("success history read failed, rebuilding")

    success = set()
    patterns = [
        str(OUT_DIR / "fb_replace_delete_803_804_2446289*.jsonl"),
        str(OUT_DIR / "fb_2446289_daily_replace_delete_*.jsonl"),
    ]
    for pattern in patterns:
        for file_name in glob.glob(pattern):
            try:
                with open(file_name, encoding="utf-8") as fp:
                    for line in fp:
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        if item.get("final_status") in SUCCESS_STATUSES:
                            ad_id = str(item.get("ad_id") or "")
                            if ad_id:
                                success.add(ad_id)
            except Exception:
                continue
    write_json(SUCCESS_HISTORY_PATH, {"updated_at": now_str(), "count": len(success), "ad_ids": sorted(success)})
    return success


def update_success_history(success, result_path):
    with result_path.open(encoding="utf-8") as fp:
        for line in fp:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if item.get("final_status") in SUCCESS_STATUSES:
                ad_id = str(item.get("ad_id") or "")
                if ad_id:
                    success.add(ad_id)
    write_json(SUCCESS_HISTORY_PATH, {"updated_at": now_str(), "count": len(success), "ad_ids": sorted(success)})


def load_page_health_cache():
    if not PAGE_HEALTH_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(PAGE_HEALTH_CACHE_PATH.read_text(encoding="utf-8"))
        return payload.get("items", {})
    except Exception:
        return {}


def save_page_health_cache(cache):
    write_json(PAGE_HEALTH_CACHE_PATH, {"updated_at": now_str(), "items": cache})


def load_known_page_choices():
    choices = {}
    patterns = [
        str(OUT_DIR / "fb_no_replacement_normal_page_map_*.json"),
        str(OUT_DIR / "fb_2446289_daily_page_map_*.json"),
    ]
    for pattern in patterns:
        for file_name in sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True):
            try:
                payload = json.loads(Path(file_name).read_text(encoding="utf-8"))
            except Exception:
                continue
            for account_id, item in payload.items():
                account_id = normalize_account_id(account_id)
                chosen = (item.get("chosen") or {}) if isinstance(item, dict) else {}
                page_id = chosen.get("page_id")
                user_id = chosen.get("user_id")
                if account_id and page_id and user_id and account_id not in choices:
                    choices[account_id] = {"page_id": page_id, "user_id": user_id, "source": file_name}
    return choices


def query_candidate_pages(account_id, limit=80):
    rows = mysql_query(
        mod.DEV_PORT,
        """
        SELECT p.page_id, MAX(d.updated_at) AS last_seen_at, COUNT(*) AS cnt
        FROM kunlunads_dev.ads_facebook_auto_created_data d FORCE INDEX(actst)
        JOIN kunlunads_dev.ads_facebook_auto_page p FORCE INDEX(idx_ad) ON p.ad_id = d.ad_id
        WHERE d.ad_account_id = %s
          AND p.page_id IS NOT NULL
          AND p.page_id <> ''
        GROUP BY p.page_id
        ORDER BY last_seen_at DESC
        LIMIT %s
        """
        % (sql_quote(account_id), int(limit)),
        timeout=60,
    )
    return [row[0] for row in rows if row and row[0]]


def build_page_map(source_rows, tokens, page_map_path):
    by_account = collections.defaultdict(lambda: {"target_pages": [], "candidate_pages": []})
    for row in source_rows:
        if row.get("replacement_creative_id"):
            continue
        account_id = row.get("account_id")
        page_id = row.get("page_id")
        if not account_id:
            continue
        if page_id and page_id not in by_account[account_id]["target_pages"]:
            by_account[account_id]["target_pages"].append(page_id)

    page_map = {}
    page_cache = load_page_health_cache()
    known_choices = load_known_page_choices()
    cache_changed = False
    for idx, account_id in enumerate(sorted(by_account), 1):
        log("checking normal page %s/%s account=%s" % (idx, len(by_account), account_id))
        target_pages = by_account[account_id]["target_pages"]
        candidates = []
        known_choice = known_choices.get(account_id) or {}
        for page_id in [known_choice.get("page_id")] + target_pages + query_candidate_pages(account_id):
            if page_id and page_id not in candidates:
                candidates.append(page_id)

        checks = []
        chosen = None
        for page_id in candidates:
            for user_id in ("803", "804"):
                token_info = tokens.get(user_id)
                if not token_info:
                    continue
                cache_key = "%s:%s" % (page_id, user_id)
                cached = page_cache.get(cache_key) or {}
                if cached.get("checked_ts") and time.time() - float(cached.get("checked_ts", 0)) < PAGE_CACHE_TTL_SECONDS:
                    status = cached.get("status")
                    payload = cached.get("payload") or {}
                    from_cache = True
                else:
                    status, payload = mod.graph_request(
                        "GET",
                        "/%s" % page_id,
                        token_info["token"],
                        {"fields": "id,name,is_published"},
                    )
                    page_cache[cache_key] = {
                        "checked_at": now_str(),
                        "checked_ts": time.time(),
                        "status": status,
                        "payload": payload if status == 200 else mod.summarize_error(payload),
                    }
                    cache_changed = True
                    from_cache = False
                record = {
                    "page_id": page_id,
                    "user_id": user_id,
                    "status": status,
                    "payload": payload if status == 200 else mod.summarize_error(payload),
                    "from_cache": from_cache,
                }
                checks.append(record)
                if status == 200 and payload.get("is_published") is True:
                    chosen = {"page_id": page_id, "user_id": user_id, "page_get": payload}
                    break
            if chosen:
                break

        page_map[account_id] = {
            "target_pages": target_pages,
            "candidate_count": len(candidates),
            "chosen": chosen or {},
            "checks": checks[:80],
        }
        if cache_changed and idx % 10 == 0:
            save_page_health_cache(page_cache)

    if cache_changed:
        save_page_health_cache(page_cache)
    write_json(page_map_path, page_map)
    return page_map


def source_to_link_data(source, image_hash):
    source = source if isinstance(source, dict) else {}
    cta = source.get("call_to_action") if isinstance(source.get("call_to_action"), dict) else None
    cta_val = (cta or {}).get("value") or {}
    link = source.get("link") or cta_val.get("link") or source.get("website_url") or "https://www.facebook.com/"
    data = {
        "image_hash": image_hash,
        "link": link,
        "message": source.get("message") or source.get("body") or ".",
        "name": source.get("name") or source.get("title") or ".",
    }
    if cta:
        data["call_to_action"] = cta
    return data


def graph_error(payload):
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return payload["error"]
    if isinstance(payload, dict):
        return payload
    return {"message": str(payload)}


def graph_error_key(payload):
    err = graph_error(payload)
    return err.get("code"), err.get("error_subcode") or err.get("subcode"), err.get("error_user_title") or err.get("message")


def source_link(source):
    source = source if isinstance(source, dict) else {}
    cta = source.get("call_to_action") if isinstance(source.get("call_to_action"), dict) else None
    cta_val = (cta or {}).get("value") or {}
    return source.get("link") or cta_val.get("link") or source.get("website_url") or "https://www.facebook.com/"


def fallback_link_data(mode, source, image_hash):
    source = source if isinstance(source, dict) else {}
    link = source_link(source)
    message = source.get("message") or source.get("body") or "."
    name = source.get("name") or source.get("title") or "."
    if mode == "preserve":
        return source_to_link_data(source, image_hash)
    if mode == "old_link_learn_more":
        return {
            "image_hash": image_hash,
            "link": link,
            "message": message,
            "name": name,
            "call_to_action": {"type": "LEARN_MORE", "value": {"link": link}},
        }
    if mode == "old_link_no_cta":
        return {"image_hash": image_hash, "link": link, "message": message, "name": name}
    return {"image_hash": image_hash, "link": "https://www.facebook.com/", "message": ".", "name": "."}


def fallback_mode_sequence(code, subcode):
    if code == 2 or code is None:
        return ["retry_replacement", "old_link_learn_more", "preserve", "old_link_no_cta", "minimal"]
    if subcode == 1885031:
        return ["old_link_learn_more", "preserve", "old_link_no_cta", "minimal"]
    if subcode == 3858355:
        return ["original_video_recreate", "original_image_recreate"]
    if subcode == 1885029:
        return ["preserve", "old_link_learn_more", "old_link_no_cta", "minimal"]
    if subcode in (1885557, 3858756, 1885499, 2446289):
        return ["minimal", "old_link_no_cta", "old_link_learn_more", "preserve"]
    if subcode in (1487810, 3285008):
        return ["old_link_no_cta", "minimal", "old_link_learn_more"]
    return ["old_link_learn_more", "preserve", "old_link_no_cta", "minimal"]


class DailyExecutor(mod.PreparedExecutor):
    def __init__(self, rows, account_users, tokens, result_path, page_map, status_path, summary_path, meta_path):
        mod.PreparedExecutor.__init__(self, rows, account_users, tokens, result_path, create_placeholder=True, dry_run=False)
        self.page_map = page_map
        self.status_path = status_path
        self.summary_path = summary_path
        self.meta_path = meta_path
        self.known_page_choices = load_known_page_choices()
        self.visible_page_cache = {}

    def token_candidates(self, account_id):
        preferred = ((self.page_map.get(account_id) or {}).get("chosen") or {}).get("user_id")
        order = []
        if preferred:
            order.append(preferred)
        order += ["803", "804"]
        out = []
        seen = set()
        for user_id in order:
            if user_id in seen or user_id not in self.tokens:
                continue
            seen.add(user_id)
            out.append(self.tokens[user_id])
        return out

    def choose_token(self, row):
        account_id = row["account_id"]
        cached = self.account_token_cache.get(account_id)
        candidates = []
        if cached and cached in self.tokens:
            candidates.append(self.tokens[cached])
        candidates.extend(x for x in self.token_candidates(account_id) if x["user_id"] != cached)
        errors = []
        for token_info in candidates:
            status, payload = mod.graph_request(
                "GET",
                "/%s" % row["ad_id"],
                token_info["token"],
                {"fields": "id,account_id,creative{id},configured_status,effective_status,status"},
            )
            if status != 200:
                errors.append({"user_id": token_info["user_id"], "get_ad": mod.summarize_error(payload)})
                continue
            if row.get("prepare_status") == "placeholder_required":
                page_status, page_payload = mod.graph_request(
                    "GET",
                    "/%s" % row["page_id"],
                    token_info["token"],
                    {"fields": "id,name,is_published"},
                )
                if page_status != 200 or page_payload.get("is_published") is not True:
                    errors.append(
                        {
                            "user_id": token_info["user_id"],
                            "get_page": page_payload if page_status == 200 else mod.summarize_error(page_payload),
                        }
                    )
                    continue
            self.account_token_cache[account_id] = token_info["user_id"]
            return token_info, payload, errors
        return None, None, errors

    def choose_visible_page(self, row, token_info):
        account_id = row["account_id"]
        cache_key = (account_id, token_info["user_id"])
        with self.cache_lock:
            cached = self.visible_page_cache.get(cache_key)
        if cached:
            return cached.get("page_id"), cached.get("page_get"), [{"cached": True, "page_id": cached.get("page_id")}]

        candidates = []
        page_map_choice = ((self.page_map.get(account_id) or {}).get("chosen") or {}).get("page_id")
        known_choice = (self.known_page_choices.get(account_id) or {}).get("page_id")
        for page_id in [row.get("page_id"), row.get("selected_page_id"), row.get("original_page_id"), page_map_choice, known_choice]:
            if page_id and page_id not in candidates:
                candidates.append(page_id)
        try:
            for page_id in query_candidate_pages(account_id, limit=80):
                if page_id and page_id not in candidates:
                    candidates.append(page_id)
        except Exception:
            pass

        checks = []
        for page_id in candidates:
            page_status, page_payload = mod.graph_request(
                "GET",
                "/%s" % page_id,
                token_info["token"],
                {"fields": "id,name,is_published"},
            )
            checks.append(
                {
                    "page_id": page_id,
                    "page_status": page_status,
                    "page_get": page_payload if page_status == 200 else mod.summarize_error(page_payload),
                }
            )
            if page_status == 200 and page_payload.get("is_published") is True:
                with self.cache_lock:
                    self.visible_page_cache[cache_key] = {"page_id": page_id, "page_get": page_payload}
                return page_id, page_payload, checks
        return None, None, checks

    def upload_placeholder_image_hash(self, row, token_info):
        upload_status, upload_payload = mod.graph_upload_file(
            mod.ensure_placeholder_jpg(),
            token_info["token"],
            {"account_id": row["account_id"]},
        )
        if upload_status != 200:
            return None, {"ok": False, "final_status": "image_upload_failed", "image_upload": mod.summarize_error(upload_payload)}
        for item in (upload_payload.get("images") or {}).values():
            image_hash = item.get("hash")
            if image_hash:
                return image_hash, {"ok": True, "image_hash": image_hash, "image_upload": upload_payload}
        return None, {"ok": False, "final_status": "image_hash_missing", "image_upload": upload_payload}

    def get_creative_source(self, row, token_info):
        creative_id = row.get("current_creative_id") or row.get("old_creative_id")
        if not creative_id:
            return {}, {"missing_creative_id": True}, 0
        status, payload = mod.graph_request(
            "GET",
            "/%s" % creative_id,
            token_info["token"],
            {"fields": "id,name,object_story_spec,asset_feed_spec,url_tags"},
        )
        if status != 200:
            return {}, mod.summarize_error(payload), status
        spec = payload.get("object_story_spec") or {}
        source = spec.get("link_data") or spec.get("video_data") or {}
        return source if isinstance(source, dict) else {}, payload, status

    def create_fallback_link_creative(self, row, token_info, mode):
        page_id, page_payload, page_checks = self.choose_visible_page(row, token_info)
        if not page_id:
            return {"ok": False, "final_status": "page_not_published_or_visible", "page_checks": page_checks[:20]}
        image_hash, image_result = self.upload_placeholder_image_hash(row, token_info)
        if not image_hash:
            return image_result
        source, old_payload, old_status = self.get_creative_source(row, token_info)
        link_data = fallback_link_data(mode, source, image_hash)
        create_status, create_payload = mod.graph_request(
            "POST",
            "/%s/adcreatives" % row["account_id"],
            token_info["token"],
            {
                "name": "codex_blank_delete_placeholder_daily_2446289_%s" % mode,
                "object_story_spec": json.dumps({"page_id": page_id, "link_data": link_data}, separators=(",", ":")),
            },
        )
        if create_status == 200 and create_payload.get("id"):
            return {
                "ok": True,
                "creative_id": str(create_payload["id"]),
                "mode": mode,
                "selected_page_id": page_id,
                "page_get": page_payload,
                "page_checks": page_checks[:20],
                "image_hash": image_hash,
                "old_creative_get": old_payload if old_status == 200 else mod.summarize_error(old_payload),
                "link_data_used": link_data,
            }
        return {
            "ok": False,
            "final_status": "fallback_link_creative_create_failed",
            "mode": mode,
            "creative_create": mod.summarize_error(create_payload),
            "old_creative_get": old_payload if old_status == 200 else mod.summarize_error(old_payload),
            "link_data_used": link_data,
        }

    def create_original_video_creative(self, row, token_info):
        page_id, page_payload, page_checks = self.choose_visible_page(row, token_info)
        if not page_id:
            return {"ok": False, "final_status": "page_not_published_or_visible", "page_checks": page_checks[:20]}
        source, old_payload, old_status = self.get_creative_source(row, token_info)
        video_id = source.get("video_id") if isinstance(source, dict) else None
        if not video_id:
            return {"ok": False, "final_status": "old_video_id_missing", "old_creative_get": old_payload}
        video_status, video_payload = mod.graph_request(
            "GET",
            "/%s" % video_id,
            token_info["token"],
            {"fields": "id,title,created_time"},
        )
        if video_status != 200:
            return {
                "ok": False,
                "final_status": "old_video_not_visible",
                "video_id": video_id,
                "video_get": mod.summarize_error(video_payload),
                "old_creative_get": old_payload if old_status == 200 else mod.summarize_error(old_payload),
            }
        image_hash, image_result = self.upload_placeholder_image_hash(row, token_info)
        if not image_hash:
            return image_result
        video_data = {
            "video_id": video_id,
            "title": source.get("title") or source.get("name") or ".",
            "message": source.get("message") or source.get("body") or ".",
            "image_hash": image_hash,
        }
        cta = source.get("call_to_action") if isinstance(source.get("call_to_action"), dict) else None
        if cta:
            video_data["call_to_action"] = cta
        create_status, create_payload = mod.graph_request(
            "POST",
            "/%s/adcreatives" % row["account_id"],
            token_info["token"],
            {
                "name": "codex_video_delete_placeholder_daily_3858355",
                "object_story_spec": json.dumps({"page_id": page_id, "video_data": video_data}, separators=(",", ":")),
            },
        )
        if create_status == 200 and create_payload.get("id"):
            return {
                "ok": True,
                "creative_id": str(create_payload["id"]),
                "mode": "original_video_recreate",
                "selected_page_id": page_id,
                "page_get": page_payload,
                "page_checks": page_checks[:20],
                "video_id": video_id,
                "video_get": video_payload,
                "image_hash": image_hash,
                "old_creative_get": old_payload if old_status == 200 else mod.summarize_error(old_payload),
                "video_data_used": video_data,
            }
        return {
            "ok": False,
            "final_status": "video_creative_create_failed",
            "creative_create": mod.summarize_error(create_payload),
            "old_creative_get": old_payload if old_status == 200 else mod.summarize_error(old_payload),
            "video_data_used": video_data,
        }

    def create_original_image_creative(self, row, token_info):
        page_id, page_payload, page_checks = self.choose_visible_page(row, token_info)
        if not page_id:
            return {"ok": False, "final_status": "page_not_published_or_visible", "page_checks": page_checks[:20]}
        source, old_payload, old_status = self.get_creative_source(row, token_info)
        image_hash = source.get("image_hash") if isinstance(source, dict) else None
        if not image_hash:
            return {"ok": False, "final_status": "old_image_hash_missing", "old_creative_get": old_payload}
        link_data = {
            "image_hash": image_hash,
            "link": source_link(source),
            "message": source.get("message") or source.get("body") or ".",
            "name": source.get("name") or source.get("title") or ".",
        }
        cta = source.get("call_to_action") if isinstance(source.get("call_to_action"), dict) else None
        if cta:
            link_data["call_to_action"] = cta
        create_status, create_payload = mod.graph_request(
            "POST",
            "/%s/adcreatives" % row["account_id"],
            token_info["token"],
            {
                "name": "codex_image_delete_placeholder_daily_3858355",
                "object_story_spec": json.dumps({"page_id": page_id, "link_data": link_data}, separators=(",", ":")),
            },
        )
        if create_status == 200 and create_payload.get("id"):
            return {
                "ok": True,
                "creative_id": str(create_payload["id"]),
                "mode": "original_image_recreate",
                "selected_page_id": page_id,
                "page_get": page_payload,
                "page_checks": page_checks[:20],
                "image_hash": image_hash,
                "old_creative_get": old_payload if old_status == 200 else mod.summarize_error(old_payload),
                "link_data_used": link_data,
            }
        return {
            "ok": False,
            "final_status": "image_creative_create_failed",
            "creative_create": mod.summarize_error(create_payload),
            "old_creative_get": old_payload if old_status == 200 else mod.summarize_error(old_payload),
            "link_data_used": link_data,
        }

    def update_then_delete(self, row, token_info, creative_id):
        update_status, update_payload = mod.graph_request(
            "POST",
            "/%s" % row["ad_id"],
            token_info["token"],
            {"creative": json.dumps({"creative_id": creative_id}, separators=(",", ":"))},
        )
        update_result = update_payload if update_status == 200 else mod.summarize_error(update_payload)
        if update_status != 200 or not update_payload.get("success"):
            return {"ok": False, "stage": "update_failed", "update_http_status": update_status, "update_response": update_result}

        delete_status, delete_payload = mod.graph_request("DELETE", "/%s" % row["ad_id"], token_info["token"])
        delete_result = delete_payload if delete_status == 200 else mod.summarize_error(delete_payload)
        if delete_status == 200 and delete_payload.get("success"):
            return {
                "ok": True,
                "stage": "deleted",
                "update_http_status": update_status,
                "update_response": update_result,
                "delete_http_status": delete_status,
                "delete_response": delete_result,
            }
        code, subcode, _title = graph_error_key(delete_payload)
        if code == 100 and subcode == 1815673:
            return {
                "ok": True,
                "stage": "delete_not_found_treated_success",
                "update_http_status": update_status,
                "update_response": update_result,
                "delete_http_status": delete_status,
                "delete_response": delete_result,
            }
        return {
            "ok": False,
            "stage": "delete_failed",
            "update_http_status": update_status,
            "update_response": update_result,
            "delete_http_status": delete_status,
            "delete_response": delete_result,
        }

    def create_placeholder_creative(self, row, token_info):
        cache_key = (row["account_id"], row["page_id"], row.get("old_creative_id", ""), token_info["user_id"])
        with self.cache_lock:
            cached = self.placeholder_cache.get(cache_key)
            if cached:
                return cached
        page_status, page_payload = mod.graph_request(
            "GET",
            "/%s" % row["page_id"],
            token_info["token"],
            {"fields": "id,name,is_published"},
        )
        if page_status != 200 or page_payload.get("is_published") is not True:
            return {
                "ok": False,
                "final_status": "page_not_published_or_visible",
                "page_get": page_payload if page_status == 200 else mod.summarize_error(page_payload),
            }
        upload_status, upload_payload = mod.graph_upload_file(
            mod.ensure_placeholder_jpg(),
            token_info["token"],
            {"account_id": row["account_id"]},
        )
        if upload_status != 200:
            return {"ok": False, "final_status": "image_upload_failed", "image_upload": mod.summarize_error(upload_payload)}
        image_hash = None
        for item in (upload_payload.get("images") or {}).values():
            image_hash = item.get("hash")
            if image_hash:
                break
        if not image_hash:
            return {"ok": False, "final_status": "image_hash_missing", "image_upload": upload_payload}

        old_status, old_payload = mod.graph_request(
            "GET",
            "/%s" % row["old_creative_id"],
            token_info["token"],
            {"fields": "id,name,object_story_spec,asset_feed_spec,url_tags"},
        )
        source = {}
        if old_status == 200:
            spec = old_payload.get("object_story_spec") or {}
            source = spec.get("link_data") or spec.get("video_data") or {}
        link_data = source_to_link_data(source, image_hash)
        object_story_spec = {"page_id": row["page_id"], "link_data": link_data}
        create_status, create_payload = mod.graph_request(
            "POST",
            "/%s/adcreatives" % row["account_id"],
            token_info["token"],
            {
                "name": "codex_blank_delete_placeholder_daily_2446289",
                "object_story_spec": json.dumps(object_story_spec, separators=(",", ":")),
            },
        )
        if create_status == 200 and create_payload.get("id"):
            result = {
                "ok": True,
                "creative_id": str(create_payload["id"]),
                "page_get": page_payload,
                "image_hash": image_hash,
                "old_creative_get": old_payload if old_status == 200 else mod.summarize_error(old_payload),
                "link_data_used": link_data,
            }
            with self.cache_lock:
                self.placeholder_cache[cache_key] = result
            return result
        return {
            "ok": False,
            "final_status": "placeholder_creative_create_failed",
            "creative_create": mod.summarize_error(create_payload),
            "old_creative_get": old_payload if old_status == 200 else mod.summarize_error(old_payload),
            "link_data_used": link_data,
        }

    def process_one(self, row):
        result = {
            "ad_id": row["ad_id"],
            "account_id": row["account_id"],
            "page_id": row["page_id"],
            "original_page_id": row.get("original_page_id", ""),
            "selected_page_id": row.get("page_id", ""),
            "page_selection": row.get("page_selection", ""),
            "old_creative_id": row["old_creative_id"],
            "prepare_status": row["prepare_status"],
            "started_at": now_str(),
        }
        try:
            token_info, before, errors = self.choose_token(row)
            result["token_errors"] = errors[:5]
            if not token_info:
                result["final_status"] = "no_visible_token"
                return result
            result["token_user_id"] = token_info["user_id"]
            result["before_get"] = before
            if before.get("configured_status") == "DELETED" or before.get("status") == "DELETED":
                result["final_status"] = "already_deleted"
                return result
            current_creative_id = (before.get("creative") or {}).get("id") if isinstance(before, dict) else None
            if current_creative_id:
                result["current_creative_id"] = current_creative_id
                if current_creative_id != row.get("old_creative_id"):
                    row = dict(row)
                    row["current_creative_id"] = current_creative_id
            new_id = row["replacement_creative_id"]
            if not new_id:
                placeholder = self.create_placeholder_creative(row, token_info)
                result["placeholder"] = placeholder
                if not placeholder.get("ok"):
                    result["final_status"] = placeholder.get("final_status") or "placeholder_failed"
                    return result
                new_id = placeholder["creative_id"]
            result["new_creative_id"] = new_id
            primary = self.update_then_delete(row, token_info, new_id)
            result["update_http_status"] = primary.get("update_http_status")
            result["update_response"] = primary.get("update_response")
            result["delete_http_status"] = primary.get("delete_http_status")
            result["delete_response"] = primary.get("delete_response")
            if primary.get("ok"):
                result["final_status"] = primary["stage"]
                return result
            if primary.get("stage") == "delete_failed":
                result["final_status"] = "delete_failed"
                return result

            code, subcode, title = graph_error_key(primary.get("update_response"))
            result["update_error_key"] = {"code": code, "subcode": subcode, "title": title}
            fallback_attempts = []
            for mode in fallback_mode_sequence(code, subcode):
                if mode == "retry_replacement":
                    attempt = self.update_then_delete(row, token_info, new_id)
                    attempt["mode"] = mode
                    fallback_attempts.append(attempt)
                    if attempt.get("ok"):
                        result["final_status"] = attempt["stage"]
                        result["successful_fallback_mode"] = mode
                        result["fallback_attempts"] = fallback_attempts
                        return result
                    continue
                if mode == "original_video_recreate":
                    creative = self.create_original_video_creative(row, token_info)
                elif mode == "original_image_recreate":
                    creative = self.create_original_image_creative(row, token_info)
                else:
                    creative = self.create_fallback_link_creative(row, token_info, mode)
                attempt = {"mode": mode, "creative": creative}
                if not creative.get("ok"):
                    fallback_attempts.append(attempt)
                    continue
                retry = self.update_then_delete(row, token_info, creative["creative_id"])
                attempt.update(retry)
                fallback_attempts.append(attempt)
                if retry.get("ok"):
                    result["final_status"] = retry["stage"]
                    result["successful_fallback_mode"] = mode
                    result["new_creative_id"] = creative["creative_id"]
                    result["fallback_attempts"] = fallback_attempts
                    return result
            result["fallback_attempts"] = fallback_attempts
            result["final_status"] = "update_failed"
            return result
        except Exception as exc:
            result["final_status"] = "local_exception"
            result["local_exception"] = repr(exc)
            return result

    def write_result(self, result):
        with self.write_lock:
            with self.result_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.stats[result.get("final_status") or "unknown"] += 1
            total = sum(self.stats.values())
            if total % 100 == 0:
                self.write_status("running")

    def write_status(self, state):
        payload = {
            "state": state,
            "updated_at": now_str(),
            "processed": sum(self.stats.values()),
            "stats": dict(self.stats),
            "result_jsonl": str(self.result_path),
            "summary_csv": str(self.summary_path),
            "meta_json": str(self.meta_path),
        }
        write_json(self.status_path, payload)


def build_execution_rows(source_rows, page_map, success_history, result_path):
    rows = []
    skipped_success = 0
    with result_path.open("a", encoding="utf-8") as fp:
        for row in source_rows:
            ad_id = str(row.get("ad_id") or "").strip()
            if not ad_id:
                continue
            if ad_id in success_history:
                skipped_success += 1
                skipped = {
                    "ad_id": ad_id,
                    "account_id": row.get("account_id", ""),
                    "page_id": row.get("page_id", ""),
                    "old_creative_id": row.get("creative_id", ""),
                    "new_creative_id": row.get("replacement_creative_id", ""),
                    "prepare_status": "history_success_skip",
                    "final_status": "skipped_success_history",
                    "started_at": now_str(),
                }
                fp.write(json.dumps(skipped, ensure_ascii=False, separators=(",", ":")) + "\n")
                continue

            account_id = normalize_account_id(row.get("account_id"))
            replacement = str(row.get("replacement_creative_id") or "").strip()
            page_id = str(row.get("page_id") or "").strip()
            original_page_id = page_id
            prepare_status = "ready"
            page_selection = "original_page_for_existing_replacement"
            if not replacement:
                chosen = ((page_map.get(account_id) or {}).get("chosen") or {})
                page_id = chosen.get("page_id") or page_id
                prepare_status = "placeholder_required"
                if chosen.get("page_id"):
                    page_selection = "normal_published_page_from_same_account_history"
                else:
                    page_selection = "original_page_no_normal_fallback_found"
            rows.append(
                {
                    "ad_id": ad_id,
                    "account_id": account_id,
                    "old_creative_id": str(row.get("creative_id") or "").strip(),
                    "original_page_id": original_page_id,
                    "page_id": page_id,
                    "replacement_creative_id": replacement,
                    "prepare_status": prepare_status,
                    "page_selection": page_selection,
                }
            )
    return rows, skipped_success


def result_counts(result_path):
    counts = collections.Counter()
    if not result_path.exists():
        return counts
    with result_path.open(encoding="utf-8") as fp:
        for line in fp:
            if line.strip():
                try:
                    counts[json.loads(line).get("final_status") or "unknown"] += 1
                except Exception:
                    counts["bad_jsonl"] += 1
    return counts


def format_counts(counts):
    preferred = [
        "deleted",
        "already_deleted",
        "already_deleted_or_not_found",
        "delete_not_found_treated_success",
        "delete_retry_success",
        "delete_unverified",
        "skipped_success_history",
        "update_failed",
        "delete_failed",
        "no_visible_token",
        "placeholder_creative_create_failed",
        "page_not_published_or_visible",
        "image_upload_failed",
        "local_exception",
    ]
    lines = []
    used = set()
    for key in preferred:
        if counts.get(key):
            lines.append("%s：%s" % (key, counts.get(key, 0)))
            used.add(key)
    for key in sorted(counts):
        if key not in used:
            lines.append("%s：%s" % (key, counts[key]))
    return "\n".join(lines) if lines else "无"


def selection_message(metadata, page_map_path, page_map, success_history_count):
    no_replacement = metadata["no_replacement_creative_id"]
    normal_page_found = sum(1 for item in page_map.values() if (item.get("chosen") or {}).get("page_id"))
    normal_page_missing = sum(1 for item in page_map.values() if not (item.get("chosen") or {}).get("page_id"))
    return "\n".join(
        [
            "FB 2446289 自动替换删除：筛选完成",
            "时间：%s" % now_str(),
            "命中 ad_id：%s" % metadata["target_ad_ids"],
            "账户数：%s" % metadata["target_accounts"],
            "有可替换 creative_id：%s" % metadata["with_replacement_creative_id"],
            "无可替换需 placeholder：%s" % no_replacement,
            "placeholder 账户正常 page 已找到：%s" % normal_page_found,
            "placeholder 账户正常 page 未找到：%s" % normal_page_missing,
            "历史成功记录数：%s" % success_history_count,
            "CSV：%s" % metadata["csv_path"],
            "page_map：%s" % page_map_path,
        ]
    )


def final_message(metadata, result_path, summary_path, status_path, meta_path, counts, started_at, finished_at):
    return "\n".join(
        [
            "FB 2446289 自动替换删除：执行完成",
            "开始：%s" % started_at,
            "结束：%s" % finished_at,
            "筛选 ad_id：%s" % metadata["target_ad_ids"],
            "有可替换 creative_id：%s" % metadata["with_replacement_creative_id"],
            "无可替换/placeholder：%s" % metadata["no_replacement_creative_id"],
            "",
            format_counts(counts),
            "",
            "结果 JSONL：%s" % result_path,
            "结果 CSV：%s" % summary_path,
            "status：%s" % status_path,
            "meta：%s" % meta_path,
        ]
    )


def run_self_check(notify):
    host = socket.gethostname()
    cfg = load_feishu_config()
    app_id = cfg["app_id"]
    app_secret = cfg["app_secret"]
    feishu_token_status = "ok"
    try:
        tenant_token(app_id, app_secret)
    except Exception as exc:
        feishu_token_status = repr(exc)
    token_rows = mod.load_tokens(["803", "804"])
    token_checks = {}
    for user_id, token_info in token_rows.items():
        status, payload = mod.graph_request("GET", "/me", token_info["token"], {"fields": "id,name"})
        token_checks[user_id] = {"status": status, "payload": payload if status == 200 else mod.summarize_error(payload)}
    db_probe = mysql_query(SEC_PORT, "SELECT COUNT(*) FROM kunlunads_sec.ads_rules_run_logs WHERE created_at >= NOW() - INTERVAL 1 HOUR", timeout=30)
    result = {
        "checked_at": now_str(),
        "host": host,
        "exec_module": str(EXEC_MODULE_PATH),
        "feishu_app_id": app_id,
        "feishu_receive_id_type": cfg.get("receive_id_type"),
        "feishu_receive_id": cfg.get("receive_id"),
        "feishu_chat_id": cfg.get("chat_id"),
        "feishu_token_status": feishu_token_status,
        "tokens_loaded": sorted(token_rows),
        "token_checks": token_checks,
        "db_probe": db_probe[0][0] if db_probe else None,
        "notify": notify,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def run_once(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    patch_graph_timeouts(args.graph_timeout)

    stamp = stamp_str()
    source_csv = OUT_DIR / ("fb_2446289_daily_source_%s.csv" % stamp)
    source_meta = OUT_DIR / ("fb_2446289_daily_source_%s.json" % stamp)
    page_map_path = OUT_DIR / ("fb_2446289_daily_page_map_%s.json" % stamp)
    result_path = OUT_DIR / ("fb_2446289_daily_replace_delete_%s.jsonl" % stamp)
    summary_path = result_path.with_suffix(".csv")
    status_path = OUT_DIR / ("fb_2446289_daily_replace_delete_%s.status.json" % stamp)
    meta_path = OUT_DIR / ("fb_2446289_daily_replace_delete_%s.meta.json" % stamp)
    result_path.touch()

    started_at = now_str()
    start_message = "\n".join(
        [
            "FB 2446289 自动替换删除：开始执行",
            "时间：%s" % started_at,
            "执行机：%s" % socket.gethostname(),
            "窗口：近 %s 小时" % args.hours,
            "token 用户：803 / 804",
            "workers：%s" % args.workers,
            "dry_run：%s" % args.dry_run,
        ]
    )
    safe_notify(start_message, notify=not args.no_notify)

    meta = {
        "started_at": started_at,
        "hours": args.hours,
        "workers": args.workers,
        "graph_timeout_seconds": args.graph_timeout,
        "source_csv": str(source_csv),
        "source_meta": str(source_meta),
        "page_map": str(page_map_path),
        "result_jsonl": str(result_path),
        "summary_csv": str(summary_path),
        "status_json": str(status_path),
        "meta_json": str(meta_path),
        "dry_run": args.dry_run,
    }
    write_json(meta_path, meta)

    source_rows, metadata = screen_failed_ads(args.hours, source_csv, source_meta)
    tokens = mod.load_tokens(["803", "804"])
    success_history = load_success_history()
    page_source_rows = source_rows[: args.limit] if args.limit else source_rows
    page_map = build_page_map(page_source_rows, tokens, page_map_path)
    safe_notify(selection_message(metadata, page_map_path, page_map, len(success_history)), notify=not args.no_notify)

    if args.limit and len(source_rows) > args.limit:
        source_rows = page_source_rows
        metadata["limited_to"] = args.limit

    if args.dry_run or not source_rows:
        counts = collections.Counter({"dry_run_ready": len(source_rows)}) if args.dry_run else collections.Counter()
        mod.write_summary(result_path, summary_path)
        final = dict(meta)
        final.update({"finished_at": now_str(), "screening": metadata, "counts": dict(counts), "tokens_loaded": sorted(tokens)})
        write_json(meta_path, final)
        write_json(
            status_path,
            {
                "state": "finished",
                "updated_at": now_str(),
                "processed": 0,
                "stats": dict(counts),
                "result_jsonl": str(result_path),
                "summary_csv": str(summary_path),
                "meta_json": str(meta_path),
            },
        )
        safe_notify(final_message(metadata, result_path, summary_path, status_path, meta_path, counts, started_at, now_str()), notify=not args.no_notify)
        return final

    execution_rows, skipped_success = build_execution_rows(source_rows, page_map, success_history, result_path)
    meta.update(
        {
            "screening": metadata,
            "tokens_loaded": sorted(tokens),
            "success_history_count": len(success_history),
            "skipped_success_history": skipped_success,
            "pending_rows": len(execution_rows),
            "pending_with_replacement": sum(1 for row in execution_rows if row.get("replacement_creative_id")),
            "pending_placeholder_required": sum(1 for row in execution_rows if not row.get("replacement_creative_id")),
        }
    )
    write_json(meta_path, meta)

    account_ids = sorted({row["account_id"] for row in execution_rows if row.get("account_id")})
    executor = DailyExecutor(execution_rows, {account_id: [] for account_id in account_ids}, tokens, result_path, page_map, status_path, summary_path, meta_path)
    executor.write_status("running")
    executor.run(max(1, args.workers))
    counts = mod.write_summary(result_path, summary_path)
    update_success_history(success_history, result_path)
    finished_at = now_str()
    final = dict(meta)
    final.update({"finished_at": finished_at, "counts": dict(counts), "success_history_count_after": len(success_history)})
    write_json(meta_path, final)
    executor.write_status("finished")
    safe_notify(final_message(metadata, result_path, summary_path, status_path, meta_path, counts, started_at, finished_at), notify=not args.no_notify)
    return final


def main():
    parser = argparse.ArgumentParser(description="Daily repair for FB ad delete failures matching code=100 subcode=2446289.")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--graph-timeout", type=int, default=DEFAULT_GRAPH_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true", help="Screen and notify without mutating Meta ads.")
    parser.add_argument("--no-notify", action="store_true", help="Do not send Feishu messages.")
    parser.add_argument("--limit", type=int, default=0, help="Limit source rows after screening; intended for manual smoke tests only.")
    parser.add_argument("--self-check", action="store_true", help="Validate DB, Feishu config, and 803/804 tokens without screening or mutation.")
    args = parser.parse_args()

    if args.self_check:
        run_self_check(notify=not args.no_notify)
        return 0

    lock_fp = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        safe_notify("FB 2446289 自动替换删除：已有任务运行中，本次 10 点任务跳过\n时间：%s" % now_str(), notify=not args.no_notify)
        return 0

    try:
        run_once(args)
        return 0
    except Exception:
        tb = traceback.format_exc()
        log(tb)
        safe_notify(
            "FB 2446289 自动替换删除：执行异常\n时间：%s\n\n%s" % (now_str(), tb[-3500:]),
            notify=not args.no_notify,
        )
        return 1
    finally:
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
