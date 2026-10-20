"""Private, read-only acceptance snapshot; never copy credential/token columns.

Run locally with --output under an ignored private directory. The remote half
only reads approved SQLite columns/short-link files and the 63350 replica. It
streams compressed JSON over SSH without creating any file on the server.
"""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


DATABASES = {
    "main": "/root/drama_material_service/data/drama_material_jobs.sqlite3",
    "x": "/var/lib/x-post-automation/accounts.sqlite3",
    "auto": "/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3",
}
TABLES = {
    "main": {
        "drama_admin_user": "user_id tenant_key email name",
        "drama_material_short_link": "id job_id material_kind content_id long_url created_at_utc published_at_utc publish_state",
        "youtube_link_attribution": "task_id link_id context created_at",
        "youtube_manual_short_link": "tenant owner operation_id link_id context_json created_at",
        "drama_youtube_publish": "id job_id source_kind operator_user_id operator_name channel_id preparation_id content_id "
                                 "video_id privacy_status video_state video_published_at_utc workflow public_status status "
                                 "unknown_outcome created_at_utc",
        "youtube_auto_preparation": "id tenant owner body",
        "drama_material_job": "job_id drama_name language",
    },
    "x": {
        "x_post_queue": "id idempotency_key account_id account_username source_date material_id content_id material_name "
                        "material_language drama_name tag description page_name page_id status created_at updated_at "
                        "run_id run_date material_key candidate_rank pool_item_id catchup_run_id schedule_run_id source_type "
                        "episode_key drama_pool_item_id episode_number drama_replay_generation manual_run_id delivery_mode "
                        "relay_account_id relay_account_username account_drama_language account_drama_language_frozen",
        "x_post_publish_log": "id queue_id account_id status attempt_count long_url short_url post_text x_media_id "
                              "x_post_id x_post_url error_code unknown_outcome started_at published_at created_at updated_at",
        "x_post_manual_run": "id idempotency_key actor_user_id actor_name trigger_source external_task_key template_ref "
                             "template_version status created_at updated_at",
        "x_post_repost_ledger": "id queue_id run_date target_account_id relay_account_id source_post_id source_post_url "
                                "repost_id status source_attempt_count repost_attempt_count error_code unknown_outcome "
                                "created_at updated_at source_published_at reposted_at",
    },
    "auto": {
        "x_auto_template": "id name created_by_user_id created_by_name updated_by_user_id updated_by_name created_at updated_at",
        "x_auto_task": "id run_id template_id template_version account_id status execution_run_id execution_queue_id "
                       "execution_log_id unknown_outcome created_at published_at_utc",
    },
}
CONTEXT_FIELDS = {
    "tenant", "owner", "owner_name", "channel_id", "channel_name", "content_id", "drama_name",
    "language", "excluded", "sub_user_id", "publisher_sub_user_id", "af_c_id", "c",
    "task_id", "operation_id", "version",
}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def collect_database(path, tables):
    result = {}
    with closing(sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for table, wanted in tables.items():
            metadata = {row["name"]: row["type"] for row in connection.execute("PRAGMA table_info(" + table + ")")}
            columns = wanted.split()
            if set(columns) - set(metadata):
                raise ValueError("snapshot_schema_missing:" + table)
            records = [dict(row) for row in connection.execute("SELECT " + ",".join(columns) + " FROM " + table)]
            if len(records) > 200000:
                raise ValueError("snapshot_capacity:" + table)
            if table in {"youtube_link_attribution", "youtube_manual_short_link"}:
                field = "context" if table == "youtube_link_attribution" else "context_json"
                for row in records:
                    original = json.loads(row[field])
                    row[field] = json.dumps({k: v for k, v in original.items() if k in CONTEXT_FIELDS}, ensure_ascii=False)
            if table == "youtube_auto_preparation":
                for row in records:
                    body = json.loads(row["body"])
                    row["body"] = json.dumps({
                        "material": {key: body.get("material", {}).get(key) for key in ("macro_name", "language")},
                        "channel": {"name": body.get("channel", {}).get("name")},
                    }, ensure_ascii=False)
            result[table] = {"columns": {key: metadata[key] for key in columns}, "rows": records}
        connection.rollback()
    return result


def settings():
    result = {}
    allowed = {"ADMIN_MAPPING_MYSQL_HOST", "ADMIN_MAPPING_MYSQL_PORT", "ADMIN_MAPPING_MYSQL_USER",
               "ADMIN_MAPPING_MYSQL_PASSWORD", "ADMIN_MAPPING_MYSQL_DATABASE"}
    for raw in Path("/root/drama_material_service/.env").read_text().splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in allowed:
            continue
        pieces = shlex.split(value, comments=True)
        result[key] = pieces[0] if len(pieces) == 1 else value
    if result.get("ADMIN_MAPPING_MYSQL_HOST") != "101.32.56.53" or result.get("ADMIN_MAPPING_MYSQL_PORT") != "63350":
        raise ValueError("snapshot_replica_configuration_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_]+", result.get("ADMIN_MAPPING_MYSQL_DATABASE", "")):
        raise ValueError("snapshot_mapping_database_invalid")
    return result


def query(config, statement):
    env = dict(os.environ)
    env["MYSQL_PWD"] = config["ADMIN_MAPPING_MYSQL_PASSWORD"]
    # This one-off manual acceptance read uses the reserved manual pool; the
    # deployed report continues to use the normal gated /usr/bin/mysql.
    command = ["/usr/bin/mysql.real", "-h", "101.32.56.53", "-P", "63350",
               "-u", config["ADMIN_MAPPING_MYSQL_USER"], "-N", "-B", "-r", "--skip-reconnect",
               "--default-character-set=utf8mb4", "--connect-timeout=3"]
    statement = ("SET SESSION time_zone='+00:00'; SET SESSION MAX_EXECUTION_TIME=15000; "
                 "START TRANSACTION READ ONLY; SELECT @@read_only; " + statement + "; ROLLBACK;")
    result = subprocess.run(command, input=statement, env=env, capture_output=True, text=True, timeout=35)
    lines = result.stdout.splitlines()
    if result.returncode or not lines or lines.pop(0) != "1":
        raise ValueError("snapshot_replica_query_failed")
    return [json.loads(bytes.fromhex(line).decode("utf-8")) for line in lines if line.strip()]


def collect_remote():
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "databases": {}}
    for label, path in DATABASES.items():
        result["databases"][label] = collect_database(path, TABLES[label])
    config = settings()
    metrics = []
    for platform, site in (("youtube", "2284"), ("x", "2116")):
        statement = """SELECT HEX(JSON_OBJECT('date',CAST(dt AS CHAR),'campaign_id',MIN(campaign_id),
            'campaign',MIN(campaign),'clicks',SUM(COALESCE(clicks,0)),'views',SUM(COALESCE(views,0)),
            'installs',SUM(COALESCE(installs,0)),'conversions',SUM(COALESCE(recharge,0)),
            'revenue_cents',CAST(ROUND(SUM(COALESCE(revenue,0))*100) AS SIGNED),
            'refund_cents',CAST(ROUND(SUM(COALESCE(refund_revenue,0))*100) AS SIGNED),
            'updated_at',CAST(MAX(updated_at) AS CHAR)))
            FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(sd)
            WHERE site_id='%s' AND dt='2026-10-19'
            GROUP BY dt,BINARY campaign_id LIMIT 50001""" % site
        values = query(config, statement)
        if len(values) > 50000:
            raise ValueError("snapshot_metric_capacity")
        metrics.extend(dict(row, platform=platform) for row in values)
    result["metrics"] = metrics
    emails = sorted({str(row.get("email") or "").strip().lower()
                     for row in result["databases"]["main"]["drama_admin_user"]["rows"] if row.get("email")})
    email_literals = ",".join("CONVERT(0x" + email.encode().hex() + " USING utf8mb4)" for email in emails)
    result["user_mapping"] = query(config, (
        "SELECT DISTINCT HEX(JSON_OBJECT('email',LOWER(TRIM(email)),'sub_user_id',CAST(sub_user_id AS CHAR))) "
        "FROM `" + config["ADMIN_MAPPING_MYSQL_DATABASE"] + "`.admin_user_group "
        "WHERE sub_user_id=248 OR BINARY LOWER(TRIM(email)) IN (" + email_literals + ") LIMIT 10001"))
    if len(result["user_mapping"]) > 10000:
        raise ValueError("snapshot_mapping_capacity")
    root = Path("/mnt/data-disk/x-post-automation/s2l").resolve()
    files, missing = {}, []
    for row in result["databases"]["x"]["x_post_publish_log"]["rows"]:
        if not row["long_url"]:
            continue
        name = str(int(row["id"])) + ".html"
        path = root / name
        if path.resolve().parent != root:
            raise ValueError("snapshot_link_path_invalid")
        if not path.is_file():
            missing.append(name)
            continue
        data = path.read_bytes()
        if len(data) > 65536:
            raise ValueError("snapshot_html_size_invalid")
        files[name] = {"text": data.decode("utf-8"), "sha256": digest(data)}
    result["short_links"] = files
    result["missing_short_links"] = missing
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    sys.stdout.buffer.write(gzip.compress(canonical(result)))


def collect_contract_supplement():
    tables = {key: TABLES["main"][key] for key in ("youtube_link_attribution", "youtube_manual_short_link")}
    result = {"started_at": datetime.now(timezone.utc).isoformat(),
              "tables": collect_database(DATABASES["main"], tables)}
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    sys.stdout.buffer.write(gzip.compress(canonical(result)))


def apply_contract_supplement(source, output, parsed, payload):
    source = Path(source).resolve()
    for name in ("main.sqlite3", "x.sqlite3", "auto.sqlite3", "metrics.json", "user_mapping.json"):
        shutil.copy2(source / name, output / name)
    with closing(sqlite3.connect(output / "main.sqlite3")) as connection, connection:
        connection.row_factory = sqlite3.Row
        manual_columns = {row["name"] for row in connection.execute("PRAGMA table_info(youtube_manual_short_link)")}
        if "operation_id" not in manual_columns:
            connection.execute("ALTER TABLE youtube_manual_short_link ADD COLUMN operation_id TEXT")
        for table, data in parsed["tables"].items():
            field = "context" if table == "youtube_link_attribution" else "context_json"
            current = {row["link_id"]: row for row in data["rows"]}
            for old in connection.execute("SELECT * FROM " + table).fetchall():
                fresh = current[old["link_id"]]
                old_context, fresh_context = json.loads(old[field]), json.loads(fresh[field])
                if any(fresh_context.get(key) != value for key, value in old_context.items()):
                    raise ValueError("frozen_context_changed_since_snapshot")
                if table == "youtube_link_attribution":
                    if old["task_id"] != fresh["task_id"]:
                        raise ValueError("frozen_task_changed_since_snapshot")
                    connection.execute("UPDATE youtube_link_attribution SET context=? WHERE link_id=?",
                                       (fresh[field], old["link_id"]))
                else:
                    if old["tenant"] != fresh["tenant"] or old["owner"] != fresh["owner"]:
                        raise ValueError("frozen_owner_changed_since_snapshot")
                    connection.execute("UPDATE youtube_manual_short_link SET context_json=?,operation_id=? WHERE link_id=?",
                                       (fresh[field], fresh["operation_id"], old["link_id"]))
    (output / "contract-supplement.json.gz").write_bytes(payload)
    manifest = {
        "base_snapshot": str(source), "base_manifest_sha256": digest((source / "manifest.json").read_bytes()),
        "supplement_sha256": digest(payload), "started_at": parsed["started_at"], "finished_at": parsed["finished_at"],
        "main_sha256": digest((output / "main.sqlite3").read_bytes()),
        "metrics_sha256": digest((output / "metrics.json").read_bytes()),
        "short_links_root": str(source / "s2l"), "immutable_metrics_preserved": True,
    }
    (output / "manifest.json").write_bytes(canonical(manifest))
    print(json.dumps({"output": str(output), "manifest": manifest}, ensure_ascii=False))


def download(args):
    import paramiko
    helper = Path(args.ssh_helper).resolve()
    spec = importlib.util.spec_from_file_location("private_ssh_helper", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    password = os.environ.get(module.PASSWORD_ENV) or os.environ.get("CODEX_SERVER_PASSWORD") or module.load_local_password()
    if not password:
        raise ValueError("ssh_credentials_unavailable")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / ".gitignore").write_text("*\n", encoding="utf-8")
    with paramiko.SSHClient() as client:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(module.HOST, port=module.PORT, username=module.USERNAME, password=password,
                       timeout=20, auth_timeout=20, banner_timeout=20)
        remote = "python3 - --remote" + (" --supplement-main" if args.supplement_source else "")
        stdin, stdout, stderr = client.exec_command(remote, timeout=120)
        stdin.write(Path(__file__).read_text(encoding="utf-8"))
        stdin.channel.shutdown_write()
        payload = stdout.read()
        diagnostic = stderr.read()
        code = stdout.channel.recv_exit_status()
        if code:
            # The script never prints credentials, but keep diagnostic bytes
            # local anyway rather than forwarding arbitrary server stderr.
            (output / "snapshot-error.txt").write_bytes(diagnostic)
            raise ValueError("snapshot_remote_failed_see_private_diagnostic")
    parsed = json.loads(gzip.decompress(payload))
    if args.supplement_source:
        apply_contract_supplement(args.supplement_source, output, parsed, payload)
        return
    (output / "snapshot.json.gz").write_bytes(payload)
    for label, tables in parsed["databases"].items():
        path = output / (label + ".sqlite3")
        if path.exists():
            raise ValueError("snapshot_output_already_exists")
        with closing(sqlite3.connect(path)) as connection, connection:
            for table, data in tables.items():
                columns = list(data["columns"])
                definitions = ",".join('"%s" %s' % (column, data["columns"][column] or "TEXT") for column in columns)
                connection.execute('CREATE TABLE "%s"(%s)' % (table, definitions))
                if data["rows"]:
                    statement = 'INSERT INTO "%s"(%s) VALUES(%s)' % (
                        table, ",".join('"%s"' % column for column in columns), ",".join("?" for _ in columns))
                    connection.executemany(statement, [tuple(row[column] for column in columns) for row in data["rows"]])
    for key in ("metrics", "user_mapping"):
        (output / (key + ".json")).write_bytes(canonical(parsed[key]))
    html = output / "s2l"
    html.mkdir()
    for name, value in parsed["short_links"].items():
        data = value["text"].encode("utf-8")
        if digest(data) != value["sha256"]:
            raise ValueError("snapshot_html_hash_mismatch")
        (html / name).write_bytes(data)
    manifest = {
        "started_at": parsed["started_at"], "finished_at": parsed["finished_at"],
        "snapshot_sha256": digest(payload),
        "databases": {label: {"sha256": digest((output / (label + ".sqlite3")).read_bytes()),
                              "tables": {table: len(value["rows"]) for table, value in tables.items()}}
                      for label, tables in parsed["databases"].items()},
        "short_links": len(parsed["short_links"]), "missing_short_links": parsed["missing_short_links"],
        "source_revenue_cents": {platform: sum(row["revenue_cents"] for row in parsed["metrics"] if row["platform"] == platform)
                                 for platform in ("youtube", "x")},
    }
    (output / "manifest.json").write_bytes(canonical(manifest))
    print(json.dumps({"output": str(output), "manifest": manifest}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--supplement-main", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--supplement-source", help="Preserve an older fixed snapshot and add only immutable YT contract fields")
    parser.add_argument("--output")
    parser.add_argument("--ssh-helper")
    args = parser.parse_args()
    if args.remote:
        if args.supplement_main:
            collect_contract_supplement()
        else:
            collect_remote()
    else:
        if not args.output or not args.ssh_helper:
            parser.error("--output and --ssh-helper are required")
        download(args)


if __name__ == "__main__":
    main()
