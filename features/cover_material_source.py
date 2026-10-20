"""Single-image custom-material ingestion with durable, non-retrying outbox.

This module never imports the publisher application or writes its database.
"""
import contextlib
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from urllib.parse import urlparse

ENDPOINT = "https://aa.yingliangads.com/api/material/source"
ROOT = "/mnt/data-disk/ai-cover-material-source"
SOURCE_DB = "/root/drama_material_service/data/drama_material_jobs.sqlite3"
PRODUCTS = {1479: "Dramawave", 979: "FreeReels", 3360: "moboreels", 2373: "MoboShort"}
LANGUAGE_COUNTRY = dict(en="US", es="ES", pt="PT", fr="FR", de="DE", it="IT",
                        pl="PL", ru="RU", tr="TR", ar="SA", ja="JP", ko="KR",
                        id="ID", th="TH", vi="VN", cs="CZ", el="GR", ro="RO",
                        **{"zh-tw": "TW", "zh-cn": "CN"})
COUNTRIES = set(("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ "
                 "CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR "
                 "GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP "
                 "KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT "
                 "MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW "
                 "SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG "
                 "UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW").split())
SPECS = (("square_1x1", "1:1"), ("landscape_1_91x1", "1.91:1"), ("portrait_4x5", "4:5"))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_language(value):
    value = str(value or "").strip().replace("_", "-").lower()
    aliases = {"zh-hant": "zh-tw", "zh-hans": "zh-cn", "zh": "zh-cn",
               "jp": "ja", "kr": "ko", "in": "id", "vn": "vi", "cz": "cs"}
    value = aliases.get(value, value)
    if value in ("zh-tw", "zh-cn"):
        return value
    return value.split("-")[0]


def country_language(country, language):
    lang = canonical_language(language)
    if lang not in LANGUAGE_COUNTRY:
        raise ValueError("language_unmapped:" + lang)
    country = str(country or "").strip().upper()
    return country if country in COUNTRIES else LANGUAGE_COUNTRY[lang], lang


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


@contextlib.contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def identity(source):
    frozen = {k: source[k] for k in ("source_kind", "source_key", "app_id", "sha256")}
    return hashlib.sha256(("ai-cover-source-v1:" + encoded(frozen)).encode()).hexdigest()


def make_payload(source, person_id):
    if int(source["app_id"]) not in PRODUCTS:
        raise ValueError("unsupported_cover_product")
    if not str(source.get("content_id") or "").strip():
        raise ValueError("content_id_missing")
    if type(person_id) is not int or person_id <= 0:
        raise ValueError("person_mapping_invalid")
    country, language = country_language(source.get("country"), source.get("language"))
    return dict(app_id=int(source["app_id"]), content_id=str(source["content_id"]),
                country=country, language=language, content_sign=identity(source),
                url=source["url"], name=source["name"], user_id=person_id, initiator=person_id,
                category=source["category"],
                remark=encoded(dict(origin="ai-cover", source=source["source_key"],
                                    content_id=str(source["content_id"]))))


def inspect_image(url, expected_sha="", expected_category="", session=None):
    from PIL import Image
    import requests
    parts = urlparse(url)
    allowed = ("ai.yingliangads.com", "advertising-1306474899.cos.ap-hongkong.myqcloud.com")
    if parts.scheme != "https" or parts.hostname not in allowed or not parts.path or parts.username:
        raise ValueError("untrusted_image_url")
    client = session or requests
    with client.get(url, stream=True, timeout=(5, 40), allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("image_http_" + str(response.status_code))
        buf = io.BytesIO()
        for block in response.iter_content(65536):
            buf.write(block)
            if buf.tell() > 30 * 1024 * 1024:
                raise ValueError("image_too_large")
    raw = buf.getvalue()
    sha = hashlib.sha256(raw).hexdigest()
    if expected_sha and sha != expected_sha:
        raise ValueError("image_sha_changed")
    with Image.open(io.BytesIO(raw)) as picture:
        width, height = picture.size
        picture.verify()
    if expected_category:
        left, right = map(float, expected_category.split(":"))
        if abs(width / height / (left / right) - 1) > 0.025:
            raise ValueError("image_ratio_mismatch")
    return dict(sha256=sha, width=width, height=height, byte_length=len(raw))


def literal(value):
    return "CONVERT(0x" + str(value).encode().hex() + " USING utf8mb4) COLLATE utf8mb4_bin" if str(value) else "_utf8mb4'' COLLATE utf8mb4_bin"


class Catalog:
    def __init__(self, env):
        self.env = env
        if env.get("ADMIN_MAPPING_MYSQL_HOST") != "101.32.56.53" or int(env.get("ADMIN_MAPPING_MYSQL_PORT", "63350")) != 63350:
            raise ValueError("catalog_must_use_read_replica")

    def query(self, sql):
        env = dict(os.environ)
        env.pop("SQL_GATE_BYPASS", None)
        env.update(MYSQL_PWD=self.env["ADMIN_MAPPING_MYSQL_PASSWORD"],
                   SQL_GATE_JOB_NAME="ai-cover-material-source", SQL_GATE_WAIT_TIMEOUT_SECONDS="20",
                   SQL_GATE_EXEC_TIMEOUT_SECONDS="15", SQL_GATE_RETRY_DELAYS_SECONDS="")
        args = ["/usr/bin/mysql", "-h", "101.32.56.53", "-P", "63350",
                "-u", self.env["ADMIN_MAPPING_MYSQL_USER"], "--default-character-set=utf8mb4",
                "--connect-timeout=5", "--batch", "--raw", "--skip-column-names",
                self.env.get("ADMIN_MAPPING_MYSQL_DATABASE", "kunlunads_dev")]
        sql = ("SET SESSION MAX_EXECUTION_TIME=10000; SELECT IF(@@read_only=1,1,JSON_EXTRACT('invalid','$')); "
               "SET SESSION TRANSACTION READ ONLY; START TRANSACTION; " + sql + "; COMMIT;")
        result = subprocess.run(args, input=sql, env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=40)
        if result.returncode:
            error = re.search(r"ERROR (\d+)", result.stderr)
            raise RuntimeError("catalog_read_failed_" + (error.group(1) if error else "unknown"))
        lines = result.stdout.splitlines()
        if not lines or lines[0] != "1":
            raise RuntimeError("catalog_not_readonly")
        return [json.loads(line) for line in lines[1:] if line.strip()]

    def person(self, source):
        if source.get("system_actor"):
            where = "sub_user_id=248"
        else:
            email = str(source.get("owner_email") or "").strip().lower()
            if not email:
                raise ValueError("person_email_missing")
            where = "LOWER(TRIM(email))=" + literal(email)
        rows = self.query("SELECT DISTINCT JSON_OBJECT('id',sub_user_id) FROM admin_user_group WHERE status=0 AND " + where + " LIMIT 3")
        if len(rows) != 1 or not rows[0].get("id"):
            raise ValueError("person_mapping_not_unique")
        return int(rows[0]["id"])

    def rows(self, where, limit=5):
        columns = ("id", "product", "type", "data_source", "data_source_id", "content_sign", "name",
                   "url", "category", "country", "language", "user_id", "initiator", "designer", "created_at", "is_delete")
        return self.query("SELECT JSON_OBJECT(" + ",".join("'" + c + "',`" + c + "`" for c in columns)
                          + ") FROM ads_custom_source WHERE " + where + " LIMIT " + str(int(limit)))

    def find(self, request):
        return self.rows("content_sign=" + literal(request["content_sign"]))


def verify_final(row, request):
    expected = dict(product=PRODUCTS[request["app_id"]], type=1, content_sign=request["content_sign"],
                    data_source_id=request["content_id"], name=request["name"], category=request["category"],
                    user_id=request["user_id"], initiator=request["initiator"], is_delete=0)
    differences = [key for key, value in expected.items() if str(row.get(key)) != str(value)]
    # Confirmed API contract: drama catalog country overrides the requested
    # country, including an empty catalog value. Keep both values as evidence.
    if canonical_language(row.get("language")) != request["language"]:
        differences.append("language")
    return differences


class Ledger:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "upload.sqlite3"
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS uploads(
            key TEXT PRIMARY KEY, source_json TEXT NOT NULL, request_json TEXT NOT NULL,
            state TEXT NOT NULL, api_id INTEGER, final_id INTEGER, final_json TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, key TEXT NOT NULL, state TEXT NOT NULL,
            detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, key):
        row = self.db.execute("SELECT * FROM uploads WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def prepare(self, source, request):
        key = identity(source)
        with self.db:
            previous = self.get(key)
            if previous:
                old_request = json.loads(previous["request_json"])
                if old_request != request:
                    raise ValueError("frozen_request_changed")
                return previous
            self.db.execute("INSERT INTO uploads(key,source_json,request_json,state,created_at,updated_at) VALUES(?,?,?,'prepared',?,?)",
                            (key, encoded(source), encoded(request), utcnow(), utcnow()))
            self.event(key, "prepared", {})
        return self.get(key)

    def event(self, key, state, detail):
        self.db.execute("INSERT INTO events(key,state,detail_json,created_at) VALUES(?,?,?,?)",
                        (key, state, encoded(detail), utcnow()))

    def change(self, key, state, detail=None, **fields):
        if not set(fields).issubset({"api_id", "final_id", "final_json", "error", "source_json"}):
            raise ValueError("unsupported_ledger_field")
        fields.update(state=state, updated_at=utcnow())
        with self.db:
            self.db.execute("UPDATE uploads SET " + ",".join(k + "=?" for k in fields) + " WHERE key=?",
                            [*fields.values(), key])
            self.event(key, state, detail or {})
        return self.get(key)

    def begin_attempt(self, key):
        with self.db:
            changed = self.db.execute("UPDATE uploads SET state='unknown',attempts=attempts+1,updated_at=? "
                                     "WHERE key=? AND state='prepared'", (utcnow(), key)).rowcount
            if changed != 1:
                raise ValueError("outbound_already_attempted")
            row = self.get(key)
            self.event(key, "outbound_started", {
                "api_version": "material-source-v1",
                "request_sha256": hashlib.sha256(row["request_json"].encode()).hexdigest(),
                "attempt": row["attempts"]})

    def summary(self):
        return {r[0]: r[1] for r in self.db.execute("SELECT state,count(*) FROM uploads GROUP BY state")}


class Client:
    def __init__(self, env, session=None):
        import requests
        self.env = env
        self.session = session or requests.Session()
        self.endpoint = env.get("AD_MATERIAL_SOURCE_API_URL", ENDPOINT)
        if self.endpoint != ENDPOINT:
            raise ValueError("unexpected_material_source_endpoint")
        self.token = env.get("AD_MATERIAL_SOURCE_API_TOKEN", "")
        if not self.token:
            raise ValueError("material_source_token_missing")

    def post(self, request):
        response = self.session.post(self.endpoint, json=request,
                                     headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
                                     timeout=(5, 60), allow_redirects=False)
        receipt = {"http_status": response.status_code}
        if response.status_code in (401, 403):
            receipt["reason"] = "authentication_rejected"
            return "rejected", receipt
        try:
            data = response.json()
        except ValueError:
            return "unknown", receipt
        if not isinstance(data, dict):
            return "unknown", receipt
        code = data.get("code")
        try:
            numeric_code = float(code)
            if not math.isfinite(numeric_code):
                numeric_code = None
        except (TypeError, ValueError):
            numeric_code = None
        receipt["code"] = numeric_code if numeric_code is not None else ("success" if code == "success" else None)
        message = str(data.get("msg") or data.get("message") or data.get("error") or "")
        receipt["response_sha256"] = hashlib.sha256(encoded(data).encode()).hexdigest()
        safe_errors = ("产品app_id参数无效", "产品不存在", "不支持短剧类型产品创建素材",
                       "国家参数无效", "语言参数无效", "素材内容签名参数无效", "素材已经存在",
                       "素材url参数无效", "素材名称参数无效", "设计人员参数无效",
                       "设计人员不存在", "发起人员参数无效", "发起人员不存在",
                       "素材category参数无效", "素材标签必须为字符串", "content_id")
        receipt["validation_fields"] = [item for item in safe_errors if item in message]
        # Fixed categories only: never persist arbitrary remote text or headers.
        # Product restriction was confirmed by the operator on 2026-10-20.
        if numeric_code == 403:
            receipt["reason"] = "business_product_restricted"
            return "rejected", receipt
        if numeric_code == -1:
            if any(word in message for word in ("已经存在", "已存在", "duplicate")):
                receipt["reason"] = "duplicate_requires_readback"
                return "unknown", receipt
            receipt["reason"] = "business_validation_rejected"
            return "rejected", receipt
        if numeric_code == -2 or not 200 <= response.status_code < 300:
            return "unknown", receipt
        if data.get("success") is False or (numeric_code is not None and numeric_code < 0):
            return "unknown", receipt
        identifier = data.get("data", {}).get("id") if isinstance(data.get("data"), dict) else None
        if isinstance(identifier, bool) or not re.fullmatch(r"[1-9][0-9]*", str(identifier or "")):
            return "unknown", receipt
        receipt["api_id"] = int(identifier)
        return "accepted", receipt


def valid_api_receipt(row):
    identifier = row.get("api_id")
    return isinstance(identifier, int) and not isinstance(identifier, bool) and identifier > 0


def reconcile(ledger, catalog, row):
    request = json.loads(row["request_json"])
    matches = catalog.find(request)
    if not matches:
        if row["state"] == "confirmed" and not valid_api_receipt(row):
            return ledger.change(row["key"], "unknown", error="valid_api_receipt_missing")
        return row
    if len(matches) != 1:
        return ledger.change(row["key"], "blocked", error="formal_identity_not_unique")
    final = matches[0]
    errors = verify_final(final, request)
    if errors:
        return ledger.change(row["key"], "blocked", error="formal_mismatch:" + ",".join(errors),
                             final_id=int(final["id"]), final_json=encoded(final))
    # Formal existence and the API's data.id are separate evidence; neither replaces the other.
    receipt_valid = valid_api_receipt(row)
    return ledger.change(row["key"], "confirmed" if receipt_valid else "unknown", {
                         "resolved_by": "content_sign_readback",
                         "api_receipt_valid": receipt_valid,
                         "formal_verified": True,
                         "country_policy": "drama_catalog_override",
                         "requested_country": request["country"],
                         "formal_country": final.get("country")},
                         final_id=int(final["id"]), final_json=encoded(final),
                         error="" if receipt_valid else "formal_found_receipt_missing")


def submit(source, ledger, catalog, client, verify_image=inspect_image):
    key = identity(source)
    with file_lock(ledger.root / "locks" / (key + ".lock")):
        row = ledger.get(key)
        if row is None:
            person = catalog.person(source)
            row = ledger.prepare(source, make_payload(source, person))
        else:
            frozen_source = json.loads(row["source_json"])
            immutable = ("app_id", "content_id", "source_kind", "source_key", "sha256",
                         "url", "name", "category", "owner_email", "system_actor")
            if any(str(frozen_source.get(k, "")) != str(source.get(k, "")) for k in immutable):
                raise ValueError("frozen_source_changed")
            if country_language(source.get("country"), source.get("language")) != country_language(
                    frozen_source.get("country"), frozen_source.get("language")):
                raise ValueError("frozen_source_changed")
        if row["state"] == "confirmed" and valid_api_receipt(row):
            return row
        row = reconcile(ledger, catalog, row)  # Failed readback must prevent any new write.
        if row["state"] != "prepared":
            return row
        request = json.loads(row["request_json"])
        verify_image(request["url"], source["sha256"], request["category"])
        with file_lock(ledger.root / "outbound.lock"):
            # Rollback and preflight use a persistent hold, never a fallback to the old API.
            if (ledger.root / "HOLD").exists():
                raise RuntimeError("material_source_outbound_on_hold")
            ledger.begin_attempt(key)
            try:
                state, receipt = client.post(request)
            except Exception as exc:
                state, receipt = "unknown", {"exception_type": type(exc).__name__}
            row = ledger.change(key, state, receipt, api_id=receipt.get("api_id"),
                                error="" if state == "accepted" else receipt.get("reason", "outcome_requires_reconciliation"))
        if state in ("accepted", "unknown"):
            row = reconcile(ledger, catalog, row)
        return row


def owner_email(db_path, user_id):
    with contextlib.closing(readonly(db_path)) as conn:
        rows = conn.execute("SELECT DISTINCT LOWER(TRIM(email)) FROM drama_admin_user WHERE user_id=? LIMIT 3", (user_id,)).fetchall()
    if len(rows) != 1 or not rows[0][0]:
        raise ValueError("creator_email_not_unique")
    return rows[0][0]


def screenshot_sources(job, db_path=SOURCE_DB, inspect=inspect_image):
    system = str(job.get("creator_user_id") or "") in ("screenshot-api-direct", "api:screenshot-api-direct") or str(job.get("creator_name") or "") == "screenshot-api-direct"
    email = "" if system else owner_email(db_path, job.get("creator_user_id"))
    output = []
    for size, category in SPECS:
        url = str(job.get(size + "_url") or "").strip()
        if not url:
            raise ValueError("three_cover_images_required")
        asset = (job.get("assets") or {}).get(size, {})
        expected_sha = ""
        for field in ("public_output_path", "workspace_output_path"):
            path = Path(asset.get(field) or "")
            if path.is_file():
                expected_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                break
        image = inspect(url, expected_sha, category)
        output.append(dict(source_kind="screenshot", source_key="screenshot:" + job["job_id"] + ":" + size,
                           app_id=int(job["app_id"]), content_id=str(job["content_id"]), category=category,
                           name="{}-{}-{}-{}".format(job["content_id"], job.get("drama_name") or "screenshot", category, job["job_id"][:8]),
                           url=url, language=job.get("language"), country=job.get("country"),
                           owner_name=job.get("creator_name") or "", owner_email=email, system_actor=system,
                           generated_at=job.get("finished_at") or job.get("updated_at") or job.get("created_at"),
                           is_backfill=False, backfill_kind="", legacy_final_ids=[], **image))
    return output


def upload_screenshot(job, env=None):
    env = dict(os.environ if env is None else env)
    ledger = Ledger(env.get("AI_COVER_SOURCE_ROOT", ROOT))
    try:
        catalog, client = Catalog(env), Client(env)
        rows = [submit(source, ledger, catalog, client)
                for source in screenshot_sources(job, env.get("DRAMA_JOB_DB_PATH", SOURCE_DB))]
        pending = [r for r in rows if r["state"] != "confirmed"]
        if pending:
            raise RuntimeError("material_source_pending:" + ",".join(r["state"] + ":" + r["key"][:12] for r in pending))
        return {"sent_count": len(rows), "api_version": "material-source-v1",
                "confirmed_ids": [r["final_id"] for r in rows]}
    finally:
        ledger.close()
