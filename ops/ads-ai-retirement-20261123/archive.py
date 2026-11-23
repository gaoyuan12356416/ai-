"""Archive the approved retired tables from the replica and rehearse restoration."""
import argparse
import concurrent.futures
from collections import defaultdict
from contextlib import closing
from decimal import Decimal
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time

import pymysql


HOST = "101.32.56.53"
ROOT = Path("/mnt/data-disk/ads-ai-retirement-20261123")
BUNDLE = ROOT / "archive"
DATA_MOUNT = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
DEVICE = "ads_facebook_device_insights"
SMALL = (
    "ad_control_v3_rule_group", "ad_control_v3_product_catalog",
    "ad_control_v3_preview", "ad_control_v3_execution", "ad_control_v3_copy_intent",
    "ad_control_v3_rule_group_product", "ad_control_v3_rule_group_optimizer",
    "ad_control_v3_runner_event", "ad_control_v3_preview_target",
    "ad_control_v3_execution_target", "ad_control_copy_lineage",
    "ad_control_action_log", "fb_page_effect_profile",
)
TABLES = (*SMALL, DEVICE)
JSON_TARGETS = {"ad_control_v3_preview_target", "ad_control_v3_execution_target"}
PLACEHOLDERS = ",".join(["%s"] * len(TABLES))
SOCKET = ROOT / "rehearsal-socket/mysql.sock"


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temp.replace(path)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def credentials(writer=False):
    prefix = "AD_CONTROL_ACTION_LOG_" + ("" if writer else "READER_") + "MYSQL_"
    values = {}
    for line in Path("/root/drama_material_service/.env").read_text().splitlines():
        if line.startswith(prefix) and "=" in line:
            key, value = line.split("=", 1)
            values[key[len(prefix):]] = value.strip().strip("\"'")
    port = 63353 if writer else 63350
    if values["HOST"] != HOST or int(values["PORT"]) != port:
        raise RuntimeError("configured endpoint differs from the reviewed endpoint")
    return values["USER"], values["PASSWORD"], port


def connect(*, local=False, writer=False):
    if local:
        settings = dict(unix_socket=str(SOCKET), user="root")
    else:
        user, password, port = credentials(writer)
        settings = dict(host=HOST, port=port, user=user, password=password)
    db = pymysql.connect(**settings, database="ads_ai", charset="utf8mb4",
                         autocommit=True, connect_timeout=5, read_timeout=80, write_timeout=12)
    with db.cursor() as cur:
        cur.execute("SELECT DATABASE(),@@read_only")
        expected = ("ads_ai", 0 if local or writer else 1)
        if cur.fetchone() != expected:
            db.close()
            raise RuntimeError("database or read-only flag mismatch")
        cur.execute("SET SESSION lock_wait_timeout=5")
        if not writer:
            cur.execute("SET SESSION max_execution_time=60000")
            cur.execute("SET SESSION TRANSACTION READ ONLY")
    return db


def disk_check():
    uuid = subprocess.check_output(["findmnt", "-no", "UUID", "-T", "/mnt/data-disk"], text=True).strip()
    if uuid != DATA_MOUNT or not os.access("/mnt/data-disk", os.W_OK):
        raise RuntimeError("expected writable data disk not mounted")


def present(cur):
    cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='ads_ai' "
                "AND TABLE_NAME IN (" + PLACEHOLDERS + ")", TABLES)
    return {row[0] for row in cur.fetchall()}


def schema(cur):
    statements = []
    for name in TABLES:
        cur.execute("SHOW CREATE TABLE `" + name + "`")
        statements.append(cur.fetchone()[1] + ";\n")
    return "SET NAMES utf8mb4;\n" + "\n".join(statements)


def source_facts(cur):
    counts = {}
    for name in TABLES:
        hint = " FORCE INDEX (idx_product_date)" if name == DEVICE else ""
        cur.execute("SELECT COUNT(*) FROM `" + name + "`" + hint)
        counts[name] = cur.fetchone()[0]
    states = {}
    for name, cols in [
        ("ad_control_v3_copy_intent", "status"),
        ("ad_control_v3_runner_event", "status"),
        ("ad_control_copy_lineage", "meta_status, ledger_status, activation_status"),
    ]:
        cur.execute("SELECT " + cols + ",COUNT(*) FROM `" + name + "` GROUP BY " + cols + " ORDER BY " + cols)
        states[name] = [list(row) for row in cur.fetchall()]
    return {"counts": counts, "states": states}


def chunks(cur):
    parts = [{"table": name, "file": name + ".sql.gz", "where": None} for name in SMALL]
    cur.execute("SELECT DISTINCT ad_account_id FROM ads_facebook_device_insights ORDER BY ad_account_id")
    accounts = [row[0] for row in cur.fetchall()]
    if not all(re.fullmatch(r"(?:act_)?[0-9]+", value) for value in accounts):
        raise RuntimeError("unexpected account-key format")
    bounds = [None, *accounts[32::32], None]
    for idx, (low, high) in enumerate(zip(bounds, bounds[1:])):
        clauses = []
        if low is not None:
            clauses.append("ad_account_id >= '" + low + "'")
        if high is not None:
            clauses.append("ad_account_id < '" + high + "'")
        parts.append({"table": DEVICE, "file": "device-%03d.sql.gz" % idx,
                      "where": " AND ".join(clauses) or None})
    return parts


def dump_command(part, local):
    env = os.environ.copy()
    cmd = ["/usr/bin/mysqldump", "--no-defaults", "--no-create-info", "--skip-triggers",
           "--skip-add-locks", "--skip-disable-keys", "--no-tablespaces", "--skip-comments",
           "--skip-dump-date", "--single-transaction", "--quick", "--order-by-primary",
           "--hex-blob", "--tz-utc", "--default-character-set=utf8mb4", "--max-allowed-packet=128M"]
    if local:
        cmd.extend(["--protocol=socket", "--socket=" + str(SOCKET), "--user=root"])
        env.pop("MYSQL_PWD", None)
    else:
        user, pwd, port = credentials()
        cmd.extend(["--protocol=tcp", "--compress", "--host=" + HOST, "--port=" + str(port), "--user=" + user])
        env["MYSQL_PWD"] = pwd
    if part["where"]:
        cmd.append("--where=" + part["where"])
    cmd.extend(["ads_ai", part["table"]])
    return cmd, env


def logical_dump(part, *, local=False):
    cmd, env = dump_command(part, local)
    target = BUNDLE / part["file"]
    temp = target.with_suffix(".partial")
    digest = hashlib.sha256()
    output = None if local else gzip.open(temp, "wb", compresslevel=1)
    try:
        with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            for block in iter(lambda: proc.stdout.read(256 * 1024), b""):
                digest.update(block)
                if output is not None:
                    output.write(block)
            if proc.wait() != 0:
                raise RuntimeError("mysqldump failed: " + part["file"])
    finally:
        if output is not None:
            output.close()
    if local:
        return digest.hexdigest()
    temp.replace(target)
    result = dict(part, sql_sha256=digest.hexdigest(), gzip_sha256=file_hash(target),
                  bytes=target.stat().st_size)
    write_json(BUNDLE / (part["file"] + ".receipt.json"), result)
    print(stamp(), "archived", part["file"], result["bytes"], flush=True)
    return result


def backup():
    disk_check()
    BUNDLE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (BUNDLE / "manifest.json").exists():
        raise RuntimeError("a completed bundle already exists")
    with closing(connect()) as db, db.cursor() as cur:
        if present(cur) != set(TABLES):
            raise RuntimeError("approved table inventory changed")
        ddl = schema(cur)
        facts = source_facts(cur)
        parts = chunks(cur)
    (BUNDLE / "schema.sql").write_text(ddl)
    print(stamp(), "source-counts", json.dumps(facts["counts"]), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(logical_dump, parts))
    manifest = dict(created_at=stamp(), ddl_sha256=file_hash(BUNDLE / "schema.sql"),
                    **facts, parts=results)
    write_json(BUNDLE / "manifest.json", manifest)
    print(stamp(), "backup complete", len(results), "parts", flush=True)


def restore_command(local):
    env = os.environ.copy()
    cmd = ["/usr/bin/mysql.real", "--no-defaults", "--binary-mode", "--default-character-set=utf8mb4",
           "--max-allowed-packet=128M", "--connect-timeout=5"]
    if local:
        cmd.extend(["--protocol=socket", "--socket=" + str(SOCKET), "--user=root"])
        env.pop("MYSQL_PWD", None)
    else:
        user, pwd, port = credentials(writer=True)
        cmd.extend(["--protocol=tcp", "--compress", "--host=" + HOST, "--port=" + str(port), "--user=" + user])
        env["MYSQL_PWD"] = pwd
    return cmd + ["ads_ai"], env


def apply_file(path, local):
    cmd, env = restore_command(local)
    stream = gzip.open(path, "rb") if path.name.endswith(".gz") else path.open("rb")
    with stream, subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        try:
            shutil.copyfileobj(stream, proc.stdin, 256 * 1024)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        if proc.wait() != 0:
            raise RuntimeError("restore failed: " + path.name)


def read_bundle():
    disk_check()
    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    if (set(manifest["counts"]) != set(TABLES) or
            manifest["ddl_sha256"] != file_hash(BUNDLE / "schema.sql")):
        raise RuntimeError("archive manifest or schema mismatch")
    for part in manifest["parts"]:
        if part["table"] not in TABLES or Path(part["file"]).name != part["file"]:
            raise RuntimeError("invalid archive part")
        if file_hash(BUNDLE / part["file"]) != part["gzip_sha256"]:
            raise RuntimeError("archive file checksum mismatch: " + part["file"])
    return manifest


def compare_json(a, b, path, differences):
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        for key in a:
            compare_json(a[key], b[key], path + "." + json.dumps(key), differences)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for index, (left, right) in enumerate(zip(a, b)):
            compare_json(left, right, path + "[" + str(index) + "]", differences)
    elif type(a) in (int, Decimal) and type(b) in (int, Decimal):
        if type(a) != type(b) or str(a) != str(b):
            differences.append(path)
    elif type(a) != type(b) or a != b:
        raise ValueError("JSON value changed on restore")


def check_numeric_paths(cur, name, key, ids):
    marks = ",".join(["%s"] * len(ids))
    cur.execute("SELECT id, JSON_TYPE(val), CAST(val AS CHAR), val + 0e0 "
                "FROM (SELECT id,JSON_EXTRACT(detail_json,%s) AS val FROM `" + name +
                "` WHERE id IN (" + marks + ")) AS compared ORDER BY id", (key, *ids))
    out = {}
    for row_id, kind, value, floating in cur.fetchall():
        if kind == "DOUBLE":
            canonical = struct.pack("!d", floating)
        elif kind in ("INTEGER", "UNSIGNED INTEGER", "DECIMAL"):
            canonical = Decimal(value)
        else:
            raise RuntimeError("unexpected JSON numeric type")
        out[row_id] = (kind, canonical)
    return out


def verify_typed_target(name):
    if name not in JSON_TARGETS:
        raise ValueError("unexpected JSON table")
    paths = defaultdict(list)
    with closing(connect()) as source, closing(connect(local=True)) as restored:
        with source.cursor() as left, restored.cursor() as right:
            for cur in (left, right):
                cur.execute("SELECT * FROM `" + name + "` ORDER BY id")
            if [c[0] for c in left.description] != [c[0] for c in right.description]:
                raise RuntimeError("restored columns changed")
            cols = [c[0] for c in left.description]
            pos = cols.index("detail_json")
            id_pos = cols.index("id")
            rows_checked = 0
            while True:
                left_rows, right_rows = left.fetchmany(256), right.fetchmany(256)
                if len(left_rows) != len(right_rows):
                    raise RuntimeError("target row count changed")
                if not left_rows:
                    break
                for a, b in zip(left_rows, right_rows):
                    if a[:pos] + a[pos+1:] != b[:pos] + b[pos+1:]:
                        raise RuntimeError("non-JSON column changed")
                    if a[pos] != b[pos]:
                        differences = []
                        compare_json(json.loads(a[pos], parse_float=Decimal),
                                     json.loads(b[pos], parse_float=Decimal), "$", differences)
                        for key in differences:
                            paths[key].append(a[id_pos])
                    rows_checked += 1
            for key, ids in paths.items():
                for offset in range(0, len(ids), 1000):
                    batch = ids[offset:offset+1000]
                    src = check_numeric_paths(left, name, key, batch)
                    dst = check_numeric_paths(right, name, key, batch)
                    if src.keys() != set(batch) or src != dst:
                        raise RuntimeError("JSON type or exact numeric value changed")
    return dict(rows=rows_checked, numeric_paths=len(paths),
                typed_numeric_comparisons=sum(len(v) for v in paths.values()))


def verify_restoration(manifest):
    with closing(connect(local=True)) as db, db.cursor() as cur:
        facts = source_facts(cur)
    if any(facts[key] != manifest[key] for key in facts):
        raise RuntimeError("restored counts or unresolved states differ")
    hashes, typed = {}, {}
    def verify(part):
        return part, logical_dump(part, local=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for part, digest in pool.map(verify, manifest["parts"]):
            name = part["table"]
            if digest != part["sql_sha256"]:
                if name not in JSON_TARGETS:
                    raise RuntimeError("ordered restored data differs: " + part["file"])
                typed[name] = verify_typed_target(name)
            hashes[part["file"]] = digest
    write_json(ROOT / "restore-verified.json",
               dict(at=stamp(), manifest_sha256=file_hash(BUNDLE / "manifest.json"),
                    restored_sql_sha256=hashes, byte_identical_parts=len(hashes)-len(typed),
                    json_type_and_value_checks=typed, **facts))
    print(stamp(), "restore verified", len(hashes), "parts", flush=True)


def restore(*, local):
    manifest = read_bundle()
    db = connect(local=local, writer=not local)
    with db.cursor() as cur:
        if present(cur):
            raise RuntimeError("restore requires all retired table names to be unoccupied")
    db.close()
    apply_file(BUNDLE / "schema.sql", local)
    for part in (x for x in manifest["parts"] if x["table"] != DEVICE):
        apply_file(BUNDLE / part["file"], local)
    device_parts = [x for x in manifest["parts"] if x["table"] == DEVICE]
    def load_device(part):
        apply_file(BUNDLE / part["file"], local)
        print(stamp(), "restored", part["file"], flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3 if local else 1) as pool:
        list(pool.map(load_device, device_parts))
    if local:
        verify_restoration(manifest)
    else:
        with closing(connect()) as db, db.cursor() as cur:
            facts = source_facts(cur)
        if any(facts[key] != manifest[key] for key in facts):
            raise RuntimeError("check replica before considering rollback complete")
        print(stamp(), "rollback counts and ledger states verified on replica", flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup", "rehearse", "verify", "rollback"])
    choice = parser.parse_args().action
    if choice == "backup":
        backup()
    elif choice == "verify":
        verify_restoration(read_bundle())
    else:
        restore(local=(choice == "rehearse"))
