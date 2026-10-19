#!/usr/bin/env python3
"""Submit eligible dramas using separate, explicit per-product cover rules."""

import argparse
import csv
import datetime as dt
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.screenshot_products import AUTO_PROFILES, get_product


DEFAULT_ENV_FILE = "/root/drama_material_service/.env"
DEFAULT_DB_PATH = "/root/drama_material_service/data/drama_material_jobs.sqlite3"
DEFAULT_API_URL = "http://127.0.0.1:8787/api/drama-screenshot-material/jobs"


def load_env_file(path):
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            parsed = shlex.split(value, posix=True)
            value = parsed[0] if parsed else ""
        except ValueError:
            value = value.strip("\"'")
        os.environ.setdefault(key, value)


def mysql_escape(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def mysql_identifier(value):
    return "`" + str(value).replace("`", "``") + "`"


def compact_sql(sql):
    return " ".join(sql.split())


def mysql_cmd():
    host = os.environ.get("DRAMA_DB_HOST") or os.environ.get("ADMIN_MAPPING_MYSQL_HOST") or ""
    port = os.environ.get("DRAMA_DB_PORT") or os.environ.get("ADMIN_MAPPING_MYSQL_PORT") or ""
    user = os.environ.get("DRAMA_DB_USER") or os.environ.get("ADMIN_MAPPING_MYSQL_USER") or ""
    cmd = ["mysql"]
    if host:
        cmd.extend(["-h", host])
    if port:
        cmd.extend(["-P", port])
    if user:
        cmd.extend(["-u", user])
    cmd.extend(["-N", "-B", "--default-character-set=utf8mb4", "-e"])
    return cmd


def run_mysql(sql, timeout, fieldnames):
    password = os.environ.get("DRAMA_DB_PASSWORD") or os.environ.get("ADMIN_MAPPING_MYSQL_PASSWORD") or ""
    env = os.environ.copy()
    if password:
        env["MYSQL_PWD"] = password
    proc = subprocess.run(
        mysql_cmd() + ["SET SESSION TRANSACTION READ ONLY; SET time_zone = '+08:00'; " + compact_sql(sql)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=env,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip() or "mysql query failed")
    return list(csv.DictReader(proc.stdout.splitlines(), delimiter="\t", fieldnames=fieldnames))


def metric_date_expr(date_value):
    if date_value:
        return mysql_escape(date_value)
    return "DATE_SUB(CURDATE(), INTERVAL 1 DAY)"


def fetch_candidates(args):
    database = os.environ.get("DRAMA_DB_NAME") or os.environ.get("ADMIN_MAPPING_MYSQL_DATABASE") or "kunlunads_dev"
    insight_table = os.environ.get("AUTO_COVER_INSIGHT_TABLE", "ads_custom_source_insight")
    source_table = os.environ.get("DRAMA_SOURCE_TABLE", "ads_drama_resource")
    product_values = [item.strip() for item in args.product.split(",") if item.strip()]
    if not product_values:
        raise ValueError("at least one product is required")
    product_sql = "(" + ",".join(mysql_escape(item) for item in product_values) + ")"
    date_sql = metric_date_expr(args.date)
    spend_operator = getattr(args, "spend_operator", ">=")
    if spend_operator not in (">", ">="):
        raise ValueError("invalid spend operator")
    # Preserve DramaWave's existing rounded boundary; Mobo requires positive raw spend.
    spend_expression = "SUM(spend)" if spend_operator == ">" else "spend"
    having_sql = "%s %s %s" % (spend_expression, spend_operator, float(args.min_spend))
    if args.min_roas is not None:
        having_sql += " AND af_roas0 >= %s" % float(args.min_roas)
    metric_sql = """
    SELECT
      data_source_id AS content_id,
      {target_app_id} AS app_id,
      ROUND(SUM(spend), 2) AS spend,
      SUM(af_installs) AS af_installs,
      IF(SUM(af_installs), ROUND(SUM(spend) / SUM(af_installs), 2), 0) AS cpi,
      ROUND(SUM(af_revenue0), 2) AS af_revenue0,
      IF(SUM(spend), ROUND(SUM(af_revenue0) / SUM(spend) * 100, 2), 0) AS af_roas0,
      IF(SUM(spend), ROUND(SUM(revenue) / SUM(spend) * 100, 2), 0) AS fb_roas,
      ROUND(SUM(revenue), 2) AS fb_revenue
    FROM {db}.{insight_table} FORCE INDEX (psdd)
    WHERE product IN {product_sql}
      AND dt = {date_sql}
      AND platform = {platform}
      AND data_source_id <> ''
    GROUP BY data_source_id
    HAVING {having_sql}
    ORDER BY {spend_order} DESC, data_source_id
    """.format(
        db=mysql_identifier(database),
        insight_table=mysql_identifier(insight_table),
        product_sql=product_sql,
        date_sql=date_sql,
        platform=mysql_escape(args.platform),
        target_app_id=mysql_escape(args.target_app_id),
        having_sql=having_sql,
        spend_order="SUM(spend)" if getattr(args, "top_n", 0) else "spend",
    )
    rows = run_mysql(metric_sql, args.mysql_timeout, [
        "content_id",
        "app_id",
        "spend",
        "af_installs",
        "cpi",
        "af_revenue0",
        "af_roas0",
        "fb_roas",
        "fb_revenue",
    ])
    content_ids = [row["content_id"] for row in rows if row.get("content_id")]
    if not content_ids:
        return []

    product = get_product(args.target_app_id)
    episode_filter = "" if product and product["cover_only"] else "AND sub_number > 0 AND sub_url <> ''"
    resource_sql = """
    SELECT
      content_id,
      MAX(name) AS drama_name,
      COUNT(*) AS episode_count
    FROM {db}.{source_table}
    WHERE app_id = {target_app_id}
      AND content_id IN ({content_ids})
      AND type = 2
      {episode_filter}
      AND TRIM(COALESCE(cover, '')) <> ''
    GROUP BY content_id
    """.format(
        db=mysql_identifier(database),
        source_table=mysql_identifier(source_table),
        target_app_id=mysql_escape(args.target_app_id),
        content_ids=",".join(mysql_escape(item) for item in content_ids),
        episode_filter=episode_filter,
    )
    resource_rows = run_mysql(resource_sql, args.mysql_timeout, ["content_id", "drama_name", "episode_count"])
    resource_map = {row["content_id"]: row for row in resource_rows}
    rows = [
        {**row, **resource_map[row["content_id"]]}
        for row in rows
        if row.get("content_id") in resource_map
    ]
    # A ranked profile must see every eligible row before excluding existing jobs.
    if not getattr(args, "top_n", 0) and args.limit and args.limit > 0:
        return rows[:args.limit]
    return rows


def existing_jobs(db_path, app_id, content_ids):
    if not content_ids:
        return {}
    if not Path(db_path).is_file():
        raise RuntimeError("screenshot task database is unavailable; submission stopped")
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        rows = []
        for start in range(0, len(content_ids), 400):
            batch = list(content_ids[start:start + 400])
            placeholders = ",".join(["?"] * len(batch))
            sql = ("SELECT content_id, job_id, status, progress, progress_detail "
                   "FROM drama_screenshot_job WHERE app_id = ? AND content_id IN (%s)") % placeholders
            rows.extend(conn.execute(sql, [str(app_id)] + batch).fetchall())
    finally:
        conn.close()
    result = {}
    for row in rows:
        current = result.get(row["content_id"])
        item = dict(row)
        if not current or str(item.get("job_id", "")) > str(current.get("job_id", "")):
            result[row["content_id"]] = item
    return result


def api_token():
    return (
        os.environ.get("AUTO_COVER_API_TOKEN")
        or os.environ.get("DRAMA_SCREENSHOT_API_TOKEN")
        or os.environ.get("AI_COVER_API_TOKEN")
        or ""
    ).strip()


def submit_job(api_url, token, app_id, content_id, timeout):
    payload = json.dumps({"app_id": str(app_id), "content_id": str(content_id)}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
    }
    req = urllib.request.Request(api_url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            data = json.loads(body) if body else {}
            return resp.status, data
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {"error": body}
        return exc.code, data


def build_parser():
    parser = argparse.ArgumentParser(description="Auto-submit dramas using per-product cover selection rules.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--date", default="", help="Metric date, YYYY-MM-DD. Defaults to CURDATE()-1 in MySQL.")
    parser.add_argument("--product", default=os.environ.get("AUTO_COVER_PRODUCT", "dramawave"))
    parser.add_argument("--platform", default=os.environ.get("AUTO_COVER_PLATFORM", "0"))
    parser.add_argument("--min-roas", type=float, default=float(os.environ.get("AUTO_COVER_MIN_ROAS", "45")))
    parser.add_argument("--min-spend", type=float, default=float(os.environ.get("AUTO_COVER_MIN_SPEND", "1000")))
    parser.add_argument("--target-app-id", default=os.environ.get("AUTO_COVER_TARGET_APP_ID", "1479"))
    parser.add_argument("--api-url", default=os.environ.get("AUTO_COVER_API_URL", DEFAULT_API_URL))
    parser.add_argument("--db-path", default=os.environ.get("DRAMA_JOB_DB_PATH", DEFAULT_DB_PATH))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("AUTO_COVER_LIMIT", "0")))
    parser.add_argument("--submit-delay", type=float, default=float(os.environ.get("AUTO_COVER_SUBMIT_DELAY", "0.5")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("AUTO_COVER_API_TIMEOUT", "30")))
    parser.add_argument("--mysql-timeout", type=int, default=int(os.environ.get("AUTO_COVER_MYSQL_TIMEOUT", "180")))
    parser.add_argument("--dry-run", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all-products", action="store_true", help="Run the three fixed automatic product profiles.")
    selection.add_argument("--profile", choices=[p["name"] for p in AUTO_PROFILES])
    return parser


def run_product(args):
    started_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    candidates = fetch_candidates(args)
    existing = existing_jobs(args.db_path, args.target_app_id, [row["content_id"] for row in candidates])

    skipped = []
    eligible = []
    for row in candidates:
        existing_item = existing.get(row["content_id"])
        item = dict(row)
        if existing_item:
            item.update({
                "existing_job_id": existing_item.get("job_id", ""),
                "existing_status": existing_item.get("status", ""),
            })
            skipped.append(item)
            continue
        eligible.append(item)

    top_n = getattr(args, "top_n", 0)
    selected = eligible[:top_n] if top_n else eligible
    submitted = []
    failed = []
    token = api_token()
    if not args.dry_run and not token:
        raise RuntimeError("missing AUTO_COVER_API_TOKEN or DRAMA_SCREENSHOT_API_TOKEN")

    # Freeze the top selection before POSTs: a failure does not pull in rank six.
    for item in selected:
        if args.dry_run:
            submitted.append({**item, "dry_run": True})
            continue
        try:
            status, data = submit_job(args.api_url, token, args.target_app_id, item["content_id"], args.timeout)
        except (OSError, TimeoutError, ValueError) as exc:
            # Never replay an uncertain POST. A later run rechecks the durable task DB.
            failed.append({**item, "error": type(exc).__name__, "unknown_outcome": True})
            break
        if 200 <= status < 300:
            submitted.append({**item, "job_id": data.get("job_id", ""), "status": data.get("status", "")})
        else:
            failed.append({**item, "http_status": status, "error": data})
        if args.submit_delay > 0:
            time.sleep(args.submit_delay)

    summary = {
        "started_at": started_at,
        "mode": "dry_run" if args.dry_run else "prod",
        "date": args.date or "mysql:CURDATE()-1",
        "product": args.product,
        "platform": args.platform,
        "target_app_id": args.target_app_id,
        "min_roas": args.min_roas,
        "min_spend": args.min_spend,
        "spend_operator": getattr(args, "spend_operator", ">="),
        "top_n": top_n,
        "candidate_count": len(candidates),
        "eligible_new_count": len(eligible),
        "selected_count": len(selected),
        "deferred_by_top_n_count": len(eligible) - len(selected),
        "submitted_count": len(submitted),
        "skipped_existing_count": len(skipped),
        "failed_count": len(failed),
        "submitted": submitted,
        "skipped_existing": skipped,
        "failed": failed,
    }
    return summary


def profile_args(args, profile):
    result = argparse.Namespace(**vars(args))
    result.product = profile["product"]
    result.target_app_id = profile["app_id"]
    result.min_spend = profile["min_spend"]
    result.min_roas = profile["min_roas"]
    result.spend_operator = profile["spend_operator"]
    result.top_n = profile.get("top_n", 0)
    return result


def run_selected_products(args):
    if args.all_products:
        profiles = AUTO_PROFILES
    elif args.profile:
        profiles = [p for p in AUTO_PROFILES if p["name"] == args.profile]
    else:
        # Preserve the original one-product CLI and its environment defaults.
        return [run_product(args)]
    results = []
    for profile in profiles:
        selected = profile_args(args, profile)
        try:
            results.append(run_product(selected))
        except Exception as exc:
            results.append({"product": selected.product, "target_app_id": selected.target_app_id,
                            "date": selected.date, "mode": "dry_run" if args.dry_run else "prod",
                            "min_spend": selected.min_spend, "min_roas": selected.min_roas,
                            "spend_operator": selected.spend_operator,
                            "failed_count": 1, "error_type": type(exc).__name__,
                            "error": "product run failed; no automatic replay"})
    return results


def main(argv=None):
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    pre_args, _ = pre_parser.parse_known_args(argv)
    load_env_file(pre_args.env_file)
    args = build_parser().parse_args(argv)
    if not args.date:
        args.date = (dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date() - dt.timedelta(days=1)).isoformat()
    dt.datetime.strptime(args.date, "%Y-%m-%d")
    results = run_selected_products(args)
    summary = {"date": args.date, "timezone": "Asia/Shanghai", "products": results} if args.all_products else results[0]
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if any(r.get("failed_count", 0) for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
