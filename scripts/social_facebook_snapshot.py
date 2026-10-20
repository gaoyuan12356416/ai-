"""Read-only, credential-free private acceptance snapshot for Facebook reporting."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FB_TABLES = {
    "fb_auto_template": "id owner_user_id created_by created_name created_at_utc",
    "fb_auto_template_version": "template_id version created_by created_at_utc",
    "fb_auto_run": "id template_id template_version trigger_type created_at_utc",
    "fb_auto_task": "id run_id template_id template_version page_id material_id content_id status "
                    "long_url short_url selection_json graph_post_id unknown_outcome created_at_utc completed_at_utc",
    "fb_auto_publish_ledger": "task_id page_id material_id status graph_post_id unknown_outcome created_at_utc updated_at_utc",
    "fb_video_task": "task_id batch_id tenant_key owner_user_id page_id material_id content_id status snapshot_json "
                     "ledger_id short_url long_url link_state link_sha256 graph_post_id unknown_outcome created_at_utc updated_at_utc",
    "fb_video_link": "id task_id long_url short_url created_at_utc",
    "fb_video_batch": "batch_id tenant_key owner_user_id owner_name operation_id created_at_utc",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def remote():
    # The maintained prior release supplies bounded, allowlisted readers only.
    sys.path.insert(0, "/mnt/data-disk/social-analytics/source")
    from scripts.social_analytics_snapshot import collect_database, settings, query, DATABASES, TABLES
    now = datetime.now(timezone.utc).isoformat()
    tables = {k: dict(v) for k, v in TABLES.items() if k in ("main", "x")}
    tables["x"]["x_post_link_attribution"] = "log_id queue_id version context_json"
    result = {"captured_at": now, "databases": {
        label: collect_database(DATABASES[label], wanted) for label, wanted in tables.items()
    }}
    result["databases"]["fb"] = collect_database(
        "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3", FB_TABLES)
    config = settings()
    result["metrics"] = []
    for platform, site in (("youtube", "2284"), ("x", "2116"), ("facebook", "2049"), ("facebook", "2310")):
        statement = """SELECT HEX(JSON_OBJECT('date',CAST(dt AS CHAR),'campaign_id',MIN(campaign_id),
            'campaign',MIN(campaign),'clicks',SUM(COALESCE(clicks,0)),'views',SUM(COALESCE(views,0)),
            'installs',SUM(COALESCE(installs,0)),'conversions',SUM(COALESCE(recharge,0)),
            'revenue_cents',CAST(ROUND(SUM(COALESCE(revenue,0))*100) AS SIGNED),
            'refund_cents',CAST(ROUND(SUM(COALESCE(refund_revenue,0))*100) AS SIGNED),
            'updated_at',CAST(MAX(updated_at) AS CHAR)))
            FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(sd)
            WHERE site_id='%s' AND dt='2026-10-19'
            GROUP BY dt,BINARY campaign_id LIMIT 50001""" % site
        records = query(config, statement)
        if len(records) > 50000:
            raise ValueError("snapshot_capacity")
        result["metrics"].extend(dict(row, platform=platform, site_id=site) for row in records)
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    sys.stdout.buffer.write(gzip.compress(canonical(result)))


def local(args):
    import paramiko
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("new_private_output_required")
    output.mkdir(parents=True)
    (output / ".gitignore").write_text("*\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("private_ssh_helper", args.ssh_helper)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    password = os.environ.get(helper.PASSWORD_ENV) or helper.load_local_password()
    if not password:
        raise ValueError("ssh_credentials_unavailable")
    with paramiko.SSHClient() as client:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(helper.HOST, port=helper.PORT, username=helper.USERNAME,
                       password=password, timeout=20, auth_timeout=20, banner_timeout=20)
        stdin, stdout, stderr = client.exec_command("python3 - --remote", timeout=120)
        # stdin execution has no filesystem __file__, so use the verified source root.
        code = Path(__file__).read_text(encoding="utf-8").replace(
            "ROOT = Path(__file__).resolve().parents[1]",
            "ROOT = Path('/mnt/data-disk/social-analytics/source')")
        stdin.write(code)
        stdin.channel.shutdown_write()
        payload = stdout.read()
        diagnostic = stderr.read()
        if stdout.channel.recv_exit_status():
            (output / "private-error.txt").write_bytes(diagnostic)
            raise ValueError("snapshot_failed_see_private_error")
    result = json.loads(gzip.decompress(payload))
    (output / "snapshot.json.gz").write_bytes(payload)
    for label, tables in result["databases"].items():
        with closing(sqlite3.connect(output / (label + ".sqlite3"))) as c, c:
            for table, data in tables.items():
                columns = list(data["columns"])
                c.execute('CREATE TABLE "%s"(%s)' % (table, ",".join(
                    '"%s" %s' % (key, data["columns"][key] or "TEXT") for key in columns)))
                if data["rows"]:
                    c.executemany('INSERT INTO "%s" VALUES(%s)' % (table, ",".join("?" for _ in columns)),
                                  [tuple(row[key] for key in columns) for row in data["rows"]])
                # Fixture-only lookup indexes; source schemas are never modified.
                identifier = next((key for key in ("id", "task_id", "log_id", "user_id") if key in columns), None)
                if identifier:
                    c.execute('CREATE INDEX "%s_lookup" ON "%s"("%s")' % (table, table, identifier))
    (output / "metrics.json").write_bytes(canonical(result["metrics"]))
    manifest = {
        "captured_at": result["captured_at"], "finished_at": result["finished_at"],
        "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
        "tables": {db: {table: len(data["rows"]) for table, data in tables.items()}
                   for db, tables in result["databases"].items()},
        "source_revenue_cents": {site: sum(r["revenue_cents"] for r in result["metrics"] if r["site_id"] == site)
                                 for site in ("2284", "2116", "2049", "2310")},
    }
    (output / "manifest.json").write_bytes(canonical(manifest))
    print(json.dumps(manifest, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--ssh-helper")
    args = parser.parse_args()
    if args.remote:
        remote()
    elif args.output and args.ssh_helper:
        local(args)
    else:
        parser.error("--output and --ssh-helper required")
