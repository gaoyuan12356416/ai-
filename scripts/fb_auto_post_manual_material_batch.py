"""Root-only operator CLI: preview, reserve, inspect and verify a frozen batch."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.manual_batch import BatchError, build_manifest, canonical, digest, prepare_source_url, reserve_batch, summary, task_policy
from features.fb_auto_posts.repositories import PagePoolRepository, ReadOnlyMySQL
from features.fb_auto_posts.strategy import daily_capacity


def service_environment():
    pid = subprocess.check_output(["systemctl", "show", "fb-auto-post-service.service", "--property=MainPID", "--value"], text=True).strip()
    if not pid.isdigit() or pid == "0":
        raise BatchError("FB sidecar must be running")
    return dict(x.split("=", 1) for x in Path("/proc", pid, "environ").read_bytes().decode().split("\0") if "=" in x)


@contextmanager
def read_db(env):
    conn = sqlite3.connect("file:" + env["FB_AUTO_POST_DB_PATH"] + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def mysql_repository(env):
    if env.get("FB_AUTO_MYSQL_PORT") != "63350":
        raise BatchError("Manual reads require the read-only 63350 endpoint")
    def connect():
        import pymysql
        conn = pymysql.connect(host=env["FB_AUTO_MYSQL_HOST"], port=63350, user=env["FB_AUTO_MYSQL_USER"], password=env["FB_AUTO_MYSQL_PASSWORD"], database=env["FB_AUTO_MYSQL_DATABASE"], charset="utf8mb4", autocommit=True, connect_timeout=5, read_timeout=20, write_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT @@read_only")
            if cur.fetchone()[0] != 1:
                conn.close()
                raise BatchError("Source server is not read-only")
            cur.execute("SET SESSION MAX_EXECUTION_TIME=15000")
        return conn
    return ReadOnlyMySQL(connect, env["FB_AUTO_MYSQL_DATABASE"], env.get("FB_AUTO_BLACKLIST_MYSQL_DATABASE", "ads_setting"))


def load_materials(repo, input_ids, app_id, pages, waived_series):
    import requests
    marks = ",".join("%s" for _ in input_ids)
    rows = repo.select(f"SELECT CAST(id AS CHAR) material_id,data_source,product,type,is_delete,TRIM(data_source_id) content_id,url media_url,LOWER(TRIM(language)) language,COALESCE(name,'') material_name,video_duration FROM `{repo.schema}`.ads_custom_source WHERE id IN ({marks})", input_ids)
    if {r["material_id"] for r in rows} != set(input_ids):
        raise BatchError("At least one exact material ID is missing")
    contents = sorted({r["content_id"] for r in rows})
    cm = ",".join("%s" for _ in contents)
    dramas = repo.select(f"SELECT id,content_id,COALESCE(name,'') drama_name,LOWER(TRIM(language)) language,release_status,deploy_time,CAST(series_code AS CHAR) series_code FROM `{repo.schema}`.ads_drama_info WHERE app_id=%s AND content_id IN ({cm}) ORDER BY id", [app_id, *contents])
    descriptions = repo.select(f"SELECT content_id,LOWER(TRIM(language)) language,MAX(TRIM(`desc`)) description,COUNT(DISTINCT BINARY TRIM(`desc`)) variants FROM `{repo.schema}`.ads_drama_resource WHERE app_id=%s AND type=2 AND content_id IN ({cm}) GROUP BY content_id,LOWER(TRIM(language))", [app_id, *contents])
    dm = {}
    for d in dramas:
        dm[(d["content_id"], d["language"])] = d
    desc = {(d["content_id"], d["language"]): d for d in descriptions}
    blacklist = repo.select(f"SELECT id,type,content_id FROM `{repo.blacklist_schema}`.ads_facebook_post_blacklist WHERE is_delete=0 AND ((type=1 AND content_id IN ({cm})) OR (type=0 AND content_id=%s))", [*contents, waived_series])
    if any(int(b["type"]) != 0 or str(b["content_id"]) != waived_series for b in blacklist):
        raise BatchError("Material is blocked by a non-waived blacklist entry")
    languages = {p.language for p in pages}
    result = {}
    for row in rows:
        d = dm.get((row["content_id"], row["language"]))
        detail = desc.get((row["content_id"], row["language"]))
        if (int(row["type"]) != 2 or int(row["data_source"]) != 6 or int(row["is_delete"]) != 0
                or not d or d["series_code"] != waived_series or int(d["release_status"]) != 1
                or not 0 < int(d["deploy_time"]) <= int(datetime.now(timezone.utc).timestamp())
                or not detail or int(detail["variants"]) != 1 or not str(detail["description"] or "").strip()):
            raise BatchError("Material/drama identity or description failed validation: " + row["material_id"])
        duration = str(row["video_duration"])
        media_info = {}
        if row["language"] in languages:
            normalized_source = prepare_source_url(row["media_url"])
            r = requests.head(normalized_source, timeout=(5, 20), allow_redirects=False)
            if r.status_code != 200 or "video/mp4" not in r.headers.get("Content-Type", "").lower() or int(r.headers.get("Content-Length", "0")) <= 0:
                raise BatchError("Material media HEAD failed: " + row["material_id"])
            media_info = {"size_bytes": int(r.headers["Content-Length"]), "etag": r.headers.get("ETag", ""), "prepare_source_url": normalized_source}
            if float(duration) <= 0:
                probe = subprocess.run(["ffprobe", "-v", "error", "-rw_timeout", "10000000", "-show_entries", "format=duration", "-of", "json", normalized_source], capture_output=True, text=True, timeout=40)
                if probe.returncode != 0:
                    raise BatchError("Material duration probe failed: " + row["material_id"])
                duration = str(json.loads(probe.stdout)["format"]["duration"])
        result[row["material_id"]] = {"material_id": row["material_id"], "content_id": row["content_id"], "media_url": row["media_url"], "language": row["language"], "material_name": row["material_name"], "material_tag": "FBmanual", "source_product": row["product"], "series_code": d["series_code"], "drama_name": d["drama_name"], "drama_description": str(detail["description"]).strip(), "duration_seconds": duration, "media_info": media_info, "waived_blacklist_entries": blacklist}
    return result


def preview(env, *, template_id, input_ids, operation_id, waived_series, waive_drama_cooldown):
    repo = mysql_repository(env)
    pool = PagePoolRepository(repo)
    with read_db(env) as conn:
        template = conn.execute("SELECT t.*,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.id=?", (template_id,)).fetchone()
    if not template:
        raise BatchError("Template not found")
    template = dict(template)
    config = json.loads(template["config_json"])
    pages = pool.list_pages(config["group_ids"], is_admin=bool(template["scope_is_admin"]), owner_user_id=template["owner_user_id"])
    if pool.legacy_conflicts(config["group_ids"]):
        raise BatchError("Legacy publishing queue overlaps this group")
    materials = load_materials(repo, input_ids, config["app_id"], pages, waived_series)
    with read_db(env) as conn:
        manifest = build_manifest(conn, template, pages, materials, input_ids, operation_id, waived_series=waived_series, waive_drama_cooldown=waive_drama_cooldown, now=datetime.now(timezone.utc))
        enabled = [dict(r) for r in conn.execute("SELECT t.id,t.owner_user_id,t.scope_is_admin,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.status='enabled'")]
    automatic_daily_jobs = 0
    for other in enabled:
        cfg = json.loads(other["config_json"])
        other_pages = pages if other["id"] == template_id else pool.list_pages(cfg["group_ids"], is_admin=bool(other["scope_is_admin"]), owner_user_id=other["owner_user_id"])
        if other["id"] != template_id and {p.page_id for p in pages}.intersection(p.page_id for p in other_pages):
            raise BatchError("Another enabled template overlaps the target Pages")
        automatic_daily_jobs += daily_capacity(cfg, other_pages)
    return manifest, automatic_daily_jobs


def existing_manifest(env, operation_id):
    with read_db(env) as conn:
        rows = conn.execute("SELECT id,config_json FROM fb_auto_run WHERE slot_key=? AND trigger_type='manual'", ("manual:materials:" + operation_id,)).fetchall()
    if len(rows) > 1:
        raise BatchError("Ambiguous batch operation")
    return dict(rows[0]) if rows else None


def status(env, operation_id):
    with read_db(env) as conn:
        run = conn.execute("SELECT * FROM fb_auto_run WHERE slot_key=? AND trigger_type='manual'", ("manual:materials:" + operation_id,)).fetchone()
        if not run:
            raise BatchError("Batch not found")
        manifest = json.loads(run["config_json"])["operator_material_batch"]["manifest"]
        tasks = [dict(r) for r in conn.execute("SELECT id,page_id,material_id,content_id,status,graph_post_id,error_code,error_message,unknown_outcome,started_at_utc,completed_at_utc,gpu_job_id,source_media_url,prepared_media_url,prepared_sha256,prepared_size_bytes,prepared_duration_seconds,short_url,long_url,message_text,selection_json FROM fb_auto_task WHERE run_id=? ORDER BY id", (run["id"],))]
        for row in tasks:
            row["sequence"] = json.loads(row.pop("selection_json"))["sequence"]
        attempted = [dict(r) for r in conn.execute("SELECT x.id,MIN(a.created_at_utc) submitted_at_utc,MIN(a.id) attempt_id FROM fb_auto_task x JOIN fb_auto_publish_attempt a ON a.task_id=x.id WHERE x.run_id=? GROUP BY x.id ORDER BY submitted_at_utc,attempt_id", (run["id"],))]
    positions = {x["id"]: x["sequence"] for x in tasks}
    actual = [positions[x["id"]] for x in attempted]
    return {"run_id": run["id"], "operation_id": operation_id, "frozen_sha256": digest(manifest), "counts": dict(Counter(x["status"] for x in tasks)), "total": len(tasks), "unique_pages": len({x["page_id"] for x in tasks}), "request_sequence": actual, "request_order_valid": actual == sorted(actual), "skipped_pages": manifest["skipped_pages"], "unmatched_material_ids": manifest["unmatched_material_ids"], "tasks": tasks}


def verify(env, report):
    import requests
    pool = PagePoolRepository(mysql_repository(env))
    session = requests.Session()
    session.trust_env = False
    version = env.get("FB_GRAPH_API_VERSION", "v22.0")
    for task in report["tasks"]:
        if task["status"] != "published" or not task["graph_post_id"]:
            continue
        result = {"verified": False}
        for credential in pool.eligible_credentials(task["page_id"]):
            try:
                response = session.get(f"https://graph.facebook.com/{version}/{task['graph_post_id']}", params={"fields": "id,published,status,permalink_url", "access_token": credential.token}, timeout=(5, 20), allow_redirects=False)
                body = response.json()
            except (requests.RequestException, ValueError):
                result = {"verified": False, "error": "readback_transport"}
                continue
            if response.status_code != 200:
                error = body.get("error", {})
                result = {"verified": False, "error_code": error.get("code"), "error_subcode": error.get("error_subcode")}
                continue
            link = str(body.get("permalink_url") or "")
            if link.startswith("/"):
                link = "https://www.facebook.com" + link
            result = {"verified": body.get("published") is True and bool(link), "published": body.get("published"), "status": body.get("status"), "permalink_url": link, "object_id": body.get("id", "")}
            break
        task["meta_readback"] = result
    session.close()
    report["meta_verified_published"] = sum(x.get("meta_readback", {}).get("verified") is True for x in report["tasks"])
    return report


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(canonical(value) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def recover_unattempted_preparation(env, operation_id, output):
    """Audited in-place retry only for failed preparation with no Graph attempt."""
    import requests
    from features.fb_auto_posts.core import FBAutoPostStore
    report = status(env, operation_id)
    original = existing_manifest(env, operation_id)
    receipt = json.loads(original["config_json"])["operator_material_batch"]
    manifest = receipt["manifest"]
    if digest(manifest) != receipt["sha256"]:
        raise BatchError("Frozen batch receipt is invalid")
    candidates = [t for t in report["tasks"] if t["status"] == "failed" and t["error_code"] == "fb_auto_prepared_response_invalid"]
    for mid in sorted({t["material_id"] for t in candidates}):
        raw = manifest["materials"][mid]
        r = requests.head(prepare_source_url(raw["media_url"]), timeout=(5, 20), allow_redirects=False)
        frozen = raw["media_info"]
        if (r.status_code != 200 or "video/mp4" not in r.headers.get("Content-Type", "").lower()
                or int(r.headers.get("Content-Length", "0")) != frozen["size_bytes"]
                or r.headers.get("ETag", "") != frozen["etag"]):
            raise BatchError("HTTPS source differs from the frozen COS object")
    write_json(output, {"before": report, "recovered_task_ids": [t["id"] for t in candidates]})
    store = FBAutoPostStore(env["FB_AUTO_POST_DB_PATH"])
    with store._lock, store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for task in candidates:
            row = dict(conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (task["id"],)).fetchone())
            if (row["status"] != "failed" or row["error_code"] != "fb_auto_prepared_response_invalid"
                    or row["attempt_count"] > 1 or row["prepared_at_utc"] or row["prepared_media_url"]
                    or row["unknown_outcome"] or row["graph_post_id"]
                    or conn.execute("SELECT 1 FROM fb_auto_publish_attempt WHERE task_id=?", (row["id"],)).fetchone()
                    or conn.execute("SELECT 1 FROM fb_auto_publish_ledger WHERE task_id=?", (row["id"],)).fetchone()):
                raise BatchError("Recovery target has changed or has a publication attempt")
            if task_policy(conn, row) is None:
                raise BatchError("Recovery target is not in the frozen manual batch")
            conn.execute("UPDATE fb_auto_task SET status='planned',completed_at_utc='',lease_owner='',lease_expires_at_utc='',next_prepare_at_utc='' WHERE id=?", (row["id"],))
        if candidates:
            conn.execute("UPDATE fb_auto_run SET status='queued',completed_at_utc='' WHERE id=?", (report["run_id"],))
        conn.commit()
    return {"run_id": report["run_id"], "operation_id": operation_id, "recovered": len(candidates), "receipt": output, "frozen_sha256": receipt["sha256"]}


def main():
    parser = argparse.ArgumentParser(__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preview", action="store_true")
    modes.add_argument("--apply", metavar="MANIFEST")
    modes.add_argument("--status", metavar="OPERATION_ID")
    modes.add_argument("--verify", metavar="OPERATION_ID")
    modes.add_argument("--recover-unattempted-preparation", metavar="OPERATION_ID")
    parser.add_argument("--template-id", type=int, default=1)
    parser.add_argument("--material-ids", nargs="+")
    parser.add_argument("--operation-id")
    parser.add_argument("--waive-series-blacklist")
    parser.add_argument("--waive-drama-cooldown", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    env = service_environment()
    if args.preview:
        if not all([args.material_ids, args.operation_id, args.waive_series_blacklist, args.output]):
            parser.error("Preview requires material IDs, operation ID, explicit series waiver and output")
        manifest, capacity = preview(env, template_id=args.template_id, input_ids=args.material_ids, operation_id=args.operation_id, waived_series=args.waive_series_blacklist, waive_drama_cooldown=args.waive_drama_cooldown)
        write_json(args.output, manifest)
        result = {**summary(manifest), "automatic_daily_jobs": capacity, "manifest_path": args.output}
    elif args.apply:
        manifest = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        existing = existing_manifest(env, manifest["operation_id"])
        if existing:
            receipt = json.loads(existing["config_json"])["operator_material_batch"]
            if receipt["sha256"] != digest(manifest):
                raise BatchError("Existing operation has a different scope")
            result = {"run_id": existing["id"], "idempotent": True, **summary(manifest)}
        else:
            fresh, capacity = preview(env, template_id=manifest["template_id"], input_ids=manifest["input_ids"], operation_id=manifest["operation_id"], waived_series=manifest["waived_series"], waive_drama_cooldown=manifest["waive_drama_cooldown"])
            if digest(fresh) != digest(manifest):
                raise BatchError("Live scope changed; generate a fresh preview before submitting")
            from features.fb_auto_posts.core import FBAutoPostStore
            result = reserve_batch(FBAutoPostStore(env["FB_AUTO_POST_DB_PATH"]), manifest, max_jobs=int(env["FB_AUTO_MAX_JOBS_PER_SLOT"]), max_daily_jobs=int(env["FB_AUTO_MAX_DAILY_JOBS"]), automatic_daily_jobs=capacity)
        if args.output:
            write_json(args.output, result)
    elif args.recover_unattempted_preparation:
        if not args.output:
            parser.error("Recovery requires an audit output file")
        result = recover_unattempted_preparation(env, args.recover_unattempted_preparation, args.output)
    else:
        result = status(env, args.status or args.verify)
        if args.verify:
            result = verify(env, result)
        if args.output:
            write_json(args.output, result)
        result = {k: v for k, v in result.items() if k != "tasks"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Transport errors may contain credential-bearing request URLs.
        message = str(exc) if isinstance(exc, BatchError) else type(exc).__name__
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        raise SystemExit(1)
