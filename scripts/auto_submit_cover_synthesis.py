#!/usr/bin/env python3
"""Submit high-ROAS dramas to the cover/screenshot synthesis backend."""

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
        mysql_cmd() + [compact_sql(sql)],
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
    HAVING af_roas0 >= {min_roas}
       AND spend >= {min_spend}
    ORDER BY spend DESC, data_source_id
    """.format(
        db=mysql_identifier(database),
        insight_table=mysql_identifier(insight_table),
        product_sql=product_sql,
        date_sql=date_sql,
        platform=mysql_escape(args.platform),
        target_app_id=mysql_escape(args.target_app_id),
        min_roas=float(args.min_roas),
        min_spend=float(args.min_spend),
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

    resource_sql = """
    SELECT
      content_id,
      MAX(name) AS drama_name,
      COUNT(*) AS episode_count
    FROM {db}.{source_table}
    WHERE app_id = {target_app_id}
      AND content_id IN ({content_ids})
      AND type = 2
      AND sub_number > 0
      AND sub_url <> ''
      AND cover <> ''
    GROUP BY content_id
    """.format(
        db=mysql_identifier(database),
        source_table=mysql_identifier(source_table),
        target_app_id=mysql_escape(args.target_app_id),
        content_ids=",".join(mysql_escape(item) for item in content_ids),
    )
    resource_rows = run_mysql(resource_sql, args.mysql_timeout, ["content_id", "drama_name", "episode_count"])
    resource_map = {row["content_id"]: row for row in resource_rows}
    rows = [
        {**row, **resource_map[row["content_id"]]}
        for row in rows
        if row.get("content_id") in resource_map
    ]
    if args.limit and args.limit > 0:
        return rows[:args.limit]
    return rows


def existing_jobs(db_path, app_id, content_ids):
    if not content_ids or not Path(db_path).exists():
        return {}
    placeholders = ",".join(["?"] * len(content_ids))
    sql = (
        "SELECT content_id, job_id, status, progress, progress_detail "
        "FROM drama_screenshot_job WHERE app_id = ? AND content_id IN (%s)"
    ) % placeholders
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, [str(app_id)] + list(content_ids)).fetchall()
    except sqlite3.Error:
        return {}
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
    parser = argparse.ArgumentParser(description="Auto-submit high-ROAS dramas to cover synthesis.")
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
    return parser


def main(argv=None):
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    pre_args, _ = pre_parser.parse_known_args(argv)
    load_env_file(pre_args.env_file)

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.date:
        dt.datetime.strptime(args.date, "%Y-%m-%d")

    started_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    candidates = fetch_candidates(args)
    existing = existing_jobs(args.db_path, args.target_app_id, [row["content_id"] for row in candidates])

    submitted = []
    skipped = []
    failed = []
    token = api_token()
    if not args.dry_run and not token:
        raise RuntimeError("missing AUTO_COVER_API_TOKEN or DRAMA_SCREENSHOT_API_TOKEN")

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
        if args.dry_run:
            submitted.append({**item, "dry_run": True})
            continue
        status, data = submit_job(args.api_url, token, args.target_app_id, row["content_id"], args.timeout)
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
        "candidate_count": len(candidates),
        "submitted_count": len(submitted),
        "skipped_existing_count": len(skipped),
        "failed_count": len(failed),
        "submitted": submitted,
        "skipped_existing": skipped,
        "failed": failed,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
