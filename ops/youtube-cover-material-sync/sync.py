#!/usr/bin/env python3
"""Independent, read-only YouTube cover consumer with a durable material outbox."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temp), str(path))


def identity(source):
    key = "youtube-cover:{task_id}:v{version}:{asset_id}".format(**source)
    return key, str(int(hashlib.sha1(key.encode()).hexdigest()[:15], 16))


def is_public(ledger):
    return bool(ledger and ledger.get("workflow") == "reviewed_thumbnail"
                and ledger.get("video_state") == "published"
                and not ledger.get("unknown_outcome")
                and all(ledger.get(k) == "succeeded" for k in
                        ("public_status", "thumbnail_status", "processing_status"))
                and re.fullmatch(r"[A-Za-z0-9_-]{11}", ledger.get("video_id") or ""))


def image_category(width, height):
    for name, ratio in (("1:1", 1), ("16:9", 16 / 9), ("1.91:1", 1.91), ("4:5", 4 / 5)):
        if abs(width / height / ratio - 1) <= 0.02:
            return name
    divisor = math.gcd(width, height)
    return "{}:{}".format(width // divisor, height // divisor)


def candidates(db_path, asset_root, scope="published"):
    """One consistent source snapshot. Never import the publisher application."""
    root = Path(asset_root).resolve()
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    output, excluded = [], []
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        ledger_rows = connection.execute("SELECT * FROM drama_youtube_publish WHERE workflow='reviewed_thumbnail'").fetchall()
        ledgers = {}
        for row in ledger_rows:
            key = row["preparation_id"]
            if key in ledgers:
                raise ValueError("duplicate_preparation_ledger")
            ledgers[key] = dict(row)
        assets = {r["id"]: dict(r) for r in connection.execute("SELECT * FROM youtube_auto_asset")}
        users = {}
        for row in connection.execute("SELECT tenant_key,user_id,name,email FROM drama_admin_user"):
            key = (row["tenant_key"], row["user_id"])
            if key in users:
                raise ValueError("duplicate_owner_identity")
            users[key] = dict(row)
        for row in connection.execute("SELECT id,tenant,owner,body FROM youtube_auto_preparation ORDER BY created_at,id"):
            body = json.loads(row["body"])
            version = (body.get("versions") or [{}])[-1]
            if not version.get("approved") or not version.get("asset_id"):
                continue
            ledger = ledgers.get(row["id"])
            if scope == "published" and not is_public(ledger):
                continue
            try:
                asset = assets[version["asset_id"]]
                owner = users[(row["tenant"], row["owner"])]
                material = body["material"]
                path = Path(asset["path"]).resolve()
                if root not in path.parents or not path.is_file():
                    raise ValueError("asset_missing_or_outside_root")
                if (asset["tenant"], asset["owner"]) != (row["tenant"], row["owner"]):
                    raise ValueError("asset_owner_mismatch")
                if str(material.get("app_id")) != "1479" or not material.get("content_id"):
                    raise ValueError("material_product_or_content_missing")
                if not owner.get("email"):
                    raise ValueError("owner_email_missing")
                if is_public(ledger) and (ledger["approved_cover_sha256"] != asset["sha256"]
                                         or Path(ledger["approved_cover_path"]).resolve() != path):
                    raise ValueError("published_cover_mismatch")
                from PIL import Image
                with Image.open(path) as image:
                    width, height = image.size
                output.append({"task_id": row["id"], "version": int(version["number"]),
                               "asset_id": asset["id"], "path": str(path), "sha256": asset["sha256"],
                               "asset_created_at": asset["created_at"], "tenant": row["tenant"],
                               "width": width, "height": height, "category": image_category(width, height),
                               "owner": row["owner"], "owner_name": owner["name"], "owner_email": owner["email"],
                               "content_id": str(material["content_id"]),
                               "title": material.get("macro_name") or material.get("name") or "YouTube cover",
                               "language": material.get("language", ""), "cover_source": version.get("source", ""),
                               "source_scope": scope, "video_id": (ledger or {}).get("video_id", "")})
            except (KeyError, ValueError, TypeError) as error:
                excluded.append({"task_id": row["id"], "reason": str(error)})
    finally:
        connection.close()
    return output, excluded


class Store:
    def __init__(self, path):
        self.connection = sqlite3.connect(str(path), timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS cover_sync (
                key TEXT PRIMARY KEY, material_id TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL, request TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, intake_id INTEGER, final_id INTEGER,
                final_json TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                next_check REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL, state TEXT NOT NULL,
                detail TEXT NOT NULL, created_at TEXT NOT NULL);
        """)

    def close(self):
        self.connection.close()

    def add(self, items):
        count = 0
        with self.connection:
            for source in items:
                key, mid = identity(source)
                old = self.connection.execute("SELECT source FROM cover_sync WHERE key=?", (key,)).fetchone()
                if old:
                    previous = json.loads(old["source"])
                    if any(previous[k] != source[k] for k in ("sha256", "content_id", "tenant", "owner", "path")):
                        raise ValueError("frozen_source_changed")
                    continue
                self.connection.execute("INSERT INTO cover_sync(key,material_id,source,state,updated_at) VALUES(?,?,?,'pending',?)",
                                        (key, mid, encoded(source), utcnow()))
                count += 1
        return count

    def rows(self, states, limit=50, due=True):
        placeholders = ",".join("?" for _ in states)
        sql = "SELECT * FROM cover_sync WHERE state IN (" + placeholders + ")"
        args = list(states)
        if due:
            sql += " AND next_check<=?"
            args.append(time.time())
        sql += " ORDER BY updated_at,key LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.connection.execute(sql, args)]

    def change(self, key, state, detail=None, **fields):
        allowed = {"request", "attempts", "intake_id", "final_id", "final_json", "error", "next_check"}
        if set(fields) - allowed:
            raise ValueError("invalid_store_field")
        fields.update(state=state, updated_at=utcnow())
        with self.connection:
            self.connection.execute("UPDATE cover_sync SET " + ",".join(k + "=?" for k in fields) + " WHERE key=?",
                                    list(fields.values()) + [key])
            self.connection.execute("INSERT INTO events(key,state,detail,created_at) VALUES(?,?,?,?)",
                                    (key, state, encoded(detail or {}), utcnow()))

    def begin_attempt(self, rows):
        """Commit uncertainty BEFORE the network call. A crash never authorizes a resend."""
        with self.connection:
            for row in rows:
                changed = self.connection.execute(
                    "UPDATE cover_sync SET state='unknown',attempts=1,next_check=?,updated_at=? "
                    "WHERE key=? AND state='ready' AND attempts=0", (time.time() + 60, utcnow(), row["key"])).rowcount
                if changed != 1:
                    raise ValueError("outbox_attempt_conflict")
                self.connection.execute("INSERT INTO events(key,state,detail,created_at) VALUES(?,'outbound_started','{}',?)",
                                        (row["key"], utcnow()))

    def summary(self):
        counts = {r["state"]: r["n"] for r in self.connection.execute("SELECT state,COUNT(*) n FROM cover_sync GROUP BY state")}
        issues = [dict(r) for r in self.connection.execute(
            "SELECT key,state,error,attempts,intake_id FROM cover_sync WHERE state IN ('blocked','unknown') ORDER BY key LIMIT 20")]
        return {"counts": counts, "total": sum(counts.values()), "issues": issues}


def literal(value):
    return "CONVERT(0x" + str(value).encode("utf-8").hex() + " USING utf8mb4) COLLATE utf8mb4_bin" if str(value) else "_utf8mb4'' COLLATE utf8mb4_bin"


class Catalog:
    """All automated reads use the host FIFO mysql wrapper, never a direct driver."""
    def __init__(self, env):
        self.env = env
        if env.get("ADMIN_MAPPING_MYSQL_HOST") != "101.32.56.53" or int(env.get("ADMIN_MAPPING_MYSQL_PORT", "63350")) != 63350:
            raise ValueError("catalog_must_use_read_replica")

    def query(self, sql):
        env = os.environ.copy()
        env.pop("SQL_GATE_BYPASS", None)
        env["SQL_GATE_JOB_NAME"] = "youtube-cover-material-sync"
        env["SQL_GATE_WAIT_TIMEOUT_SECONDS"] = "10"
        env["SQL_GATE_EXEC_TIMEOUT_SECONDS"] = "12"
        env["SQL_GATE_RETRY_DELAYS_SECONDS"] = ""
        env["MYSQL_PWD"] = self.env["ADMIN_MAPPING_MYSQL_PASSWORD"]
        args = ["/usr/bin/mysql", "-h", self.env["ADMIN_MAPPING_MYSQL_HOST"], "-P", "63350",
                "-u", self.env["ADMIN_MAPPING_MYSQL_USER"], "--default-character-set=utf8mb4",
                "--connect-timeout=5", "--batch", "--raw", "--skip-column-names",
                self.env.get("ADMIN_MAPPING_MYSQL_DATABASE", "kunlunads_dev"), "-e",
                "SET SESSION MAX_EXECUTION_TIME=8000; SELECT @@read_only; " + sql]
        result = subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", timeout=25)
        if result.returncode:
            raise RuntimeError("catalog_read_failed")
        lines = result.stdout.splitlines()
        if not lines or lines[0] != "1":
            raise RuntimeError("catalog_readonly_not_confirmed")
        return [json.loads(line) for line in lines[1:] if line.strip()]

    def people(self, emails):
        if not emails:
            return {}
        values = ",".join(literal(x.lower().strip()) for x in sorted(set(emails)))
        rows = self.query("SELECT JSON_OBJECT('email',LOWER(TRIM(aug.email)),'id',aug.sub_user_id,'username',au.username) "
                          "FROM admin_user_group aug JOIN admin_users au ON au.id=aug.sub_user_id "
                          "WHERE LOWER(TRIM(aug.email)) IN (" + values + ") LIMIT 100")
        result = {}
        for row in rows:
            result.setdefault(row["email"], []).append(row)
        return result

    def rows(self, where, limit=100):
        columns = ("id", "product", "category", "language", "country", "name", "type", "user_id", "designer",
                   "initiator", "data_source", "data_source_id", "task_id", "is_delete", "remark", "url", "created_at")
        projection = ",".join("'" + name + "',`" + name + "`" for name in columns)
        return self.query("SELECT JSON_OBJECT(" + projection + ") FROM ads_custom_source WHERE " + where + " LIMIT " + str(limit))


def payload(source, person, domain):
    _, mid = identity(source)
    key = "drama-screenshot-materials/youtube-cover/{task_id}/v{version}-{asset_id}.jpg".format(**source)
    created = dt.datetime.fromisoformat(source["asset_created_at"].replace("Z", "+00:00"))
    created = created.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    category = source["category"]
    name = "{}-{}-{}-YouTube-{}-v{}".format(source["content_id"], source["title"][:150], category, source["task_id"][:8], source["version"])
    return {"app_package": "com.dramawave.app", "material_id": int(mid), "content_id": source["content_id"],
            "category": category, "name": name, "url": "https://" + domain.strip("/") + "/" + key,
            "email": person["username"], "created_at": created}, key


def verify_final(final, request, person_id):
    expected = {"product": "Drama-AI素材", "category": request["category"], "type": 1, "data_source": 6,
                "data_source_id": request["content_id"], "name": request["name"], "is_delete": 0,
                "user_id": person_id, "initiator": person_id, "designer": str(person_id)}
    return [key for key, value in expected.items() if str(final.get(key)) != str(value)]


def reconcile(store, catalog, rows):
    if not rows:
        return
    mids = ",".join(literal(r["material_id"]) for r in rows)
    intakes = catalog.rows("data_source=102 AND data_source_id IN (" + mids + ")", len(rows) * 2)
    by_mid = {}
    for intake in intakes:
        by_mid.setdefault(str(intake["data_source_id"]), []).append(intake)
    ids = sorted({int(i["remark"]) for i in intakes if re.fullmatch(r"[1-9][0-9]*", str(i.get("remark") or ""))})
    finals = {int(r["id"]): r for r in catalog.rows("id IN (" + ",".join(map(str, ids)) + ")", len(ids))} if ids else {}
    for row in rows:
        matches = by_mid.get(row["material_id"], [])
        if not matches:
            if row["attempts"]:
                store.change(row["key"], row["state"], next_check=time.time() + 120,
                             error="awaiting_intake_readback")
            continue
        frozen = json.loads(row["request"])
        request, person_id = frozen["payload"], frozen["person_id"]
        intake = matches[0]
        if len(matches) != 1 or any(str(intake.get(k)) != str(request[k]) for k in ("name", "url", "category")):
            store.change(row["key"], "blocked", error="intake_identity_mismatch")
            continue
        final = finals.get(int(intake["remark"])) if re.fullmatch(r"[1-9][0-9]*", str(intake.get("remark") or "")) else None
        if final:
            mismatch = verify_final(final, request, person_id)
            if mismatch:
                store.change(row["key"], "blocked", error="final_mismatch:" + ",".join(mismatch),
                             intake_id=int(intake["id"]), final_id=int(final["id"]), final_json=encoded(final))
            else:
                store.change(row["key"], "confirmed", {"adopted": row["attempts"] == 0},
                             intake_id=int(intake["id"]), final_id=int(final["id"]), final_json=encoded(final), error="", next_check=0)
        else:
            store.change(row["key"], "accepted", intake_id=int(intake["id"]), next_check=time.time() + 60, error="")


class Publisher:
    def __init__(self, env):
        import requests
        from qcloud_cos import CosConfig, CosS3Client
        self.env = env
        self.session = requests.Session()
        if env.get("COS_BUCKET") != "advertising-1306474899" or env.get("COS_DOMAIN") != "advertising-1306474899.cos.ap-hongkong.myqcloud.com":
            raise ValueError("unexpected_cos_destination")
        self.cos = CosS3Client(CosConfig(Region=env["COS_REGION"], SecretId=env["COS_SECRET_ID"],
                                       SecretKey=env["COS_SECRET_KEY"], Timeout=30, KeepAlive=True), retry=0)

    def upload(self, source, request, key):
        from PIL import Image
        path = Path(source["path"]).resolve()
        root = Path(self.env.get("YOUTUBE_COVER_ASSET_ROOT", "/mnt/data-disk/youtube-auto-publish/assets")).resolve()
        if root not in path.parents:
            raise ValueError("asset_path_outside_root")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("asset_sha_changed")
        with Image.open(path) as image:
            width, height = image.size
            image.verify()
        if [width, height] != [source["width"], source["height"]] or image_category(width, height) != request["category"]:
            raise ValueError("asset_dimensions_changed")
        response = self.session.head(request["url"], timeout=(5, 20), allow_redirects=False)
        if response.status_code == 404:
            self.cos.put_object(Bucket=self.env["COS_BUCKET"], Body=raw, Key=key, ContentType="image/jpeg", EnableMD5=True)
            response = self.session.head(request["url"], timeout=(5, 20), allow_redirects=False)
        if response.status_code in (404, 408, 429) or response.status_code >= 500:
            raise RuntimeError("public_object_temporarily_unavailable")
        if (response.status_code != 200 or int(response.headers.get("Content-Length", "-1")) != len(raw)
                or response.headers.get("ETag", "").strip('"') != hashlib.md5(raw).hexdigest()):
            raise ValueError("public_object_readback_mismatch")

    def post(self, requests):
        raise RuntimeError("legacy_material_post_retired")


def tick(store, catalog, publisher, env, limit=50):
    # Readback failure stops the entire turn before any new material writes.
    waiting = store.rows(("accepted", "unknown"), limit=100)
    for index in range(0, len(waiting), 50):
        reconcile(store, catalog, waiting[index:index + 50])
    todo = store.rows(("pending", "ready"), limit=limit)
    people = catalog.people([json.loads(r["source"])["owner_email"] for r in todo if not r["request"]])
    prepared = []
    for row in todo:
        source = json.loads(row["source"])
        if not row["request"]:
            matches = people.get(source["owner_email"].strip().lower(), [])
            if len(matches) != 1 or not matches[0].get("username") or not matches[0].get("id"):
                store.change(row["key"], "blocked", error="person_mapping_not_unique")
                continue
            request, key = payload(source, matches[0], env["COS_DOMAIN"])
            frozen = {"payload": request, "person_id": int(matches[0]["id"]), "cos_key": key}
            row["request"] = encoded(frozen)
            store.change(row["key"], row["state"], request=row["request"])
        prepared.append(row)
    # Adopts the authorized one-item trial and any earlier successful uncertain outcome.
    reconcile(store, catalog, prepared)
    send = []
    for old in prepared:
        row = dict(store.connection.execute("SELECT * FROM cover_sync WHERE key=?", (old["key"],)).fetchone())
        if row["state"] not in ("pending", "ready") or row["attempts"]:
            continue
        frozen = json.loads(row["request"])
        try:
            publisher.upload(json.loads(row["source"]), frozen["payload"], frozen["cos_key"])
        except ValueError as error:
            store.change(row["key"], "blocked", error=str(error))
            continue
        except Exception as error:
            store.change(row["key"], row["state"], error="preupload:" + type(error).__name__, next_check=time.time() + 300)
            continue
        store.change(row["key"], "ready", error="", next_check=0)
        send.append(row)
    if not send:
        return
    store.begin_attempt(send)
    try:
        receipt = publisher.post([json.loads(r["request"])["payload"] for r in send])
    except Exception as error:
        receipt = {"accepted": False, "exception_type": type(error).__name__}
    for row in send:
        store.change(row["key"], "accepted" if receipt["accepted"] else "unknown", receipt,
                     error="" if receipt["accepted"] else "callback_outcome_requires_reconciliation", next_check=time.time() + 60)


def custom_tick(store, catalog, publisher, env, limit=50):
    """Retain old outstanding receipts; every new outbound uses the shared client."""
    from features import cover_material_source as custom
    waiting = store.rows(("accepted", "unknown"), limit=100)
    for index in range(0, len(waiting), 50):
        reconcile(store, catalog, waiting[index:index + 50])
    todo = store.rows(("pending", "ready", "custom_pending"), limit=limit)
    ledger = custom.Ledger(env.get("AI_COVER_SOURCE_ROOT", custom.ROOT))
    try:
        new_catalog, client = custom.Catalog(env), custom.Client(env)
        for row in todo:
            old_source = json.loads(row["source"])
            # Legacy in-flight requests must never cross protocols.
            if row["attempts"] and row["state"] != "custom_pending":
                continue
            if row["request"]:
                frozen = json.loads(row["request"])
                request, cos_key = frozen["payload"], frozen["cos_key"]
            else:
                request, cos_key = payload(old_source, {"username": ""}, env["COS_DOMAIN"])
                frozen = {"payload": request, "cos_key": cos_key, "api_version": "material-source-v1"}
                store.change(row["key"], row["state"], request=encoded(frozen))
            source = dict(source_kind="youtube", source_key=row["key"], app_id=1479,
                          content_id=old_source["content_id"], name=request["name"], url=request["url"],
                          category=old_source["category"], language=old_source["language"], country="",
                          sha256=old_source["sha256"], owner_email=old_source["owner_email"],
                          owner_name=old_source["owner_name"], system_actor=False,
                          generated_at=old_source["asset_created_at"], is_backfill=False, backfill_kind="",
                          legacy_final_ids=[], cover_source=old_source.get("cover_source", ""))
            try:
                previous = ledger.get(custom.identity(source))
                if not previous or (not previous["attempts"] and previous["state"] != "confirmed"):
                    publisher.upload(old_source, request, cos_key)
                result = custom.submit(source, ledger, new_catalog, client)
                state = result["state"]
                store.change(row["key"], "confirmed" if state == "confirmed" else
                             ("blocked" if state in ("blocked", "rejected") else "custom_pending"),
                             {"custom_key": result["key"], "api_version": "material-source-v1"},
                             attempts=result["attempts"], final_id=result["final_id"],
                             final_json=result["final_json"], error=result["error"],
                             next_check=0 if state == "confirmed" else time.time() + 120)
            except Exception as error:
                store.change(row["key"], "custom_pending",
                             error="custom_source:" + type(error).__name__, next_check=time.time() + 120)
    finally:
        ledger.close()


@contextlib.contextmanager
def locked(root):
    import fcntl
    with (root / "worker.lock").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "freeze", "run", "status", "export"))
    parser.add_argument("--scope", choices=("approved", "published"), default="published")
    parser.add_argument("--max-new", type=int, default=50)
    args = parser.parse_args(argv)
    env = dict(os.environ)
    root = Path(env.get("YOUTUBE_COVER_SYNC_ROOT", "/mnt/data-disk/youtube-cover-material-sync"))
    db = env.get("DRAMA_JOB_DB_PATH", "/root/drama_material_service/data/drama_material_jobs.sqlite3")
    asset_root = env.get("YOUTUBE_COVER_ASSET_ROOT", "/mnt/data-disk/youtube-auto-publish/assets")
    if args.command == "plan":
        items, excluded = candidates(db, asset_root, args.scope)
        print(encoded({"scope": args.scope, "count": len(items), "excluded": excluded,
                       "manifest_sha256": hashlib.sha256(encoded(items).encode()).hexdigest()}))
        return
    if Path("/mnt/data-disk").resolve() not in root.resolve().parents:
        raise ValueError("state_root_outside_data_disk")
    mounted = subprocess.check_output(["findmnt", "-n", "-o", "UUID", "/mnt/data-disk"], text=True).strip()
    if mounted != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise ValueError("data_mount_uuid_mismatch")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(root):
        store = Store(root / "sync.sqlite3")
        try:
            if args.command == "freeze":
                target = root / "backfill-manifest.json"
                if target.exists():
                    snapshot = json.loads(target.read_text(encoding="utf-8"))
                    if snapshot["scope"] != args.scope or snapshot["sha256"] != hashlib.sha256(encoded(snapshot["items"]).encode()).hexdigest():
                        raise ValueError("frozen_manifest_mismatch")
                else:
                    items, excluded = candidates(db, asset_root, args.scope)
                    if excluded:
                        raise ValueError("backfill_has_exclusions:" + encoded(excluded))
                    snapshot = {"scope": args.scope, "created_at": utcnow(), "count": len(items), "items": items,
                                "sha256": hashlib.sha256(encoded(items).encode()).hexdigest()}
                    atomic_json(target, snapshot)
                store.add(snapshot["items"])
                print(encoded({k: v for k, v in snapshot.items() if k != "items"}))
            elif args.command == "run":
                items, excluded = candidates(db, asset_root, "published")
                store.add(items)
                atomic_json(root / "discovery-issues.json", {"checked_at": utcnow(), "excluded": excluded})
                custom_tick(store, Catalog(env), Publisher(env), env, min(50, max(1, args.max_new)))
                summary = dict(store.summary(), checked_at=utcnow(), discovery_exclusions=excluded)
                atomic_json(root / "status.json", summary)
                print(encoded(summary))
            elif args.command == "export":
                rows = [dict(r) for r in store.connection.execute("SELECT * FROM cover_sync ORDER BY key")]
                atomic_json(root / "export.json", {"checked_at": utcnow(), "rows": rows})
                print(encoded(dict(store.summary(), export=str(root / "export.json"))))
            else:
                print(encoded(store.summary()))
        finally:
            store.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Exception messages can contain remote URLs/headers. Keep journal errors categorical.
        safe = str(error) if re.fullmatch(r"[a-z0-9_,: -]{1,150}", str(error)) else "details_suppressed"
        print(encoded({"error_type": type(error).__name__, "error": safe, "state": "turn_failed"}))
        raise SystemExit(1)
