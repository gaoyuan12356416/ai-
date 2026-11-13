"""Exact-decimal metric generations in ads_ai; no publisher state or runtime DDL."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Mapping

from .core import StoreError, utc_iso
from .metrics import MetricTotals, MetricWindow, checksum_rows, metric_date, nonnegative_decimal

SCHEMA = "ads_ai"
TABLES = tuple("fb_auto_metric_" + name for name in ("generation", "daily", "active_pointer"))
BATCH_SIZE = 5000


def exact_amount(value):
    amount = nonnegative_decimal(value)
    with localcontext() as context:
        context.prec = 65
        if amount >= Decimal("1e26") or amount != amount.quantize(Decimal("0.000000000001")):
            raise StoreError("fb_auto_metric_decimal_precision", "指标金额超出精确存储范围", 500)
    return amount


def amount_text(value):
    text = format(exact_amount(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def exact_add(left, right):
    with localcontext() as context:
        context.prec = 65
        return left + right


def canonical_row(raw):
    content, material = str(raw["content_id"]), str(raw["material_id"])
    if (not content or content != content.strip() or len(content) > 256
            or not re.fullmatch(r"[1-9][0-9]{0,63}", material)):
        raise StoreError("fb_auto_metric_row_invalid", "指标缓存行身份无效", 400)
    return {"content_id": content, "material_id": material,
            "spend": amount_text(raw["spend"]), "af_revenue0": amount_text(raw["af_revenue0"])}


def digest_row(digest, row):
    digest.update(json.dumps([row["content_id"], row["material_id"], row["spend"], row["af_revenue0"]],
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")


def config_from_file(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))["mysql"]
    if (value.get("host") != "101.32.56.53" or int(value.get("read_port", 0)) != 63350
            or int(value.get("write_port", 0)) != 63353 or value.get("database") != SCHEMA):
        raise ValueError("invalid metric database endpoints")
    return value


def literal(value):
    return "_utf8mb4 0x" + str(value).encode("utf-8").hex()


class GatedMetricReader:
    """Root-owned fixed metric queries through the existing host FIFO wrapper."""
    def __init__(self, config, *, manual_migration=False):
        self.config = config
        # The explicit migration CLI may use the operator pool. Production
        # callers never set this flag; no environment variable can bypass FIFO.
        self.client = "/usr/bin/mysql.real" if manual_migration else "/usr/bin/mysql"

    def query(self, statements, *, timeout=60, execution_ms=20000):
        for attempt in range(3):
            try:
                return self._query_once(statements, timeout=timeout, execution_ms=execution_ms)
            except StoreError as error:
                transient = error.code in {
                    "fb_auto_metric_query_timeout", "fb_auto_metric_query_mysql_1040",
                    "fb_auto_metric_query_mysql_2002", "fb_auto_metric_query_mysql_2003",
                    "fb_auto_metric_query_mysql_2006", "fb_auto_metric_query_mysql_2013",
                    "fb_auto_metric_query_mysql_2055"}
                if not transient or attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def _query_once(self, statements, *, timeout, execution_ms):
        env = os.environ.copy()
        env.pop("SQL_GATE_BYPASS", None)
        env["MYSQL_PWD"] = self.config["password"]
        env["SQL_GATE_JOB"] = "fb-metric-ads-ai-read"
        sql = ("SET SESSION MAX_EXECUTION_TIME=" + str(int(execution_ms)) + "; SELECT @@read_only; "
               "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ; "
               "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY; "
               + ";".join(statements) + "; ROLLBACK;\n")
        process = None
        try:
            process = subprocess.Popen([self.client, "-h", "101.32.56.53", "-P", "63350",
                                     "-u", self.config["user"], "--batch", "--raw", "--skip-column-names",
                                     "--default-character-set=utf8mb4", "--connect-timeout=10", "--compress"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, start_new_session=True)
            output, errors = process.communicate(sql.encode(), timeout=timeout)
            lines = output.splitlines()
            if process.returncode:
                match = re.search(rb"ERROR\s+(\d{3,5})\b", errors)
                reason = "mysql_" + match.group(1).decode() if match else "exit_" + str(process.returncode)
                raise StoreError("fb_auto_metric_query_" + reason, "指标只读数据库暂不可用", 503)
            if not lines or lines[0] != b"1":
                raise StoreError("fb_auto_metric_query_endpoint", "指标只读端点验证失败", 503)
            return [json.loads(bytes.fromhex(line.decode()).decode("utf-8")) for line in lines[1:] if line]
        except subprocess.TimeoutExpired:
            raise StoreError("fb_auto_metric_query_timeout", "指标只读查询超时", 503) from None
        except (OSError, ValueError, subprocess.SubprocessError):
            raise StoreError("fb_auto_metric_query_failed", "指标只读数据库暂不可用", 503) from None
        finally:
            if process is not None and process.poll() is None:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.communicate()

    def generation(self, generation_id=None, generation_key=None):
        condition = "id=" + str(int(generation_id)) if generation_id is not None else "generation_key=" + literal(generation_key)
        columns = ("id", "generation_key", "platform", "metric_date", "product", "status", "row_count", "checksum",
                   "storage_checksum", "refreshed_at_utc", "created_at_utc", "ready_at_utc", "error_code", "error_message")
        expression = ",".join("'%s',%s" % (key, key) for key in columns)
        rows = self.query(["SELECT HEX(JSON_OBJECT(" + expression + ")) FROM ads_ai.fb_auto_metric_generation WHERE " + condition])
        return rows[0] if rows else None

    def rows(self, generation_id):
        return self.query(["SELECT HEX(JSON_OBJECT('content_id',content_id,'material_id',material_id,"
                           "'spend',CAST(spend AS CHAR),'af_revenue0',CAST(af_revenue0 AS CHAR))) "
                           "FROM ads_ai.fb_auto_metric_daily WHERE generation_id=" + str(int(generation_id)) +
                           " ORDER BY content_id,CHAR_LENGTH(material_id),material_id"])

    def count(self, generation_id):
        return self.query(["SELECT HEX(JSON_OBJECT('n',COUNT(*))) FROM ads_ai.fb_auto_metric_daily WHERE generation_id="
                           + str(int(generation_id))])[0]["n"]

    def window(self, product, platform, dates):
        scope = ("p.platform=" + str(int(platform)) + " AND p.product=" + literal(product)
                 + " AND p.metric_date IN (" + ",".join(literal(day) for day in dates) + ")")
        return self.query([
            "SELECT HEX(JSON_OBJECT('kind','generation','day',p.metric_date,'id',p.generation_id,'status',g.status)) "
            "FROM ads_ai.fb_auto_metric_active_pointer p JOIN ads_ai.fb_auto_metric_generation g ON g.id=p.generation_id WHERE " + scope,
            "SELECT HEX(JSON_OBJECT('kind','row','content_id',d.content_id,'material_id',d.material_id,"
            "'spend',CAST(SUM(d.spend) AS CHAR),'af_revenue0',CAST(SUM(d.af_revenue0) AS CHAR))) "
            "FROM ads_ai.fb_auto_metric_active_pointer p JOIN ads_ai.fb_auto_metric_generation g ON g.id=p.generation_id "
            "JOIN ads_ai.fb_auto_metric_daily d ON d.generation_id=g.id WHERE " + scope +
            " AND g.status='ready' GROUP BY d.content_id,d.material_id"])


def window_from_rows(product, platform, dates, rows):
    normalized = tuple(dict.fromkeys(metric_date(value) for value in dates))
    if not normalized or len(normalized) > 30 or product != "Dramawave" or int(platform) != 0:
        raise StoreError("fb_auto_metric_product_unsupported", "指标窗口产品映射未开放", 409)
    ready = {str(row["day"]): int(row["id"]) for row in rows
             if row.get("kind") == "generation" and row["status"] == "ready"}
    missing = [day for day in normalized if day not in ready]
    if missing:
        error = StoreError("fb_auto_metric_window_not_ready", "指标窗口缺少完整READY自然日", 409)
        error.conflicts = [{"missing_dates": missing}]
        raise error
    by_material, by_drama = {}, {}
    for row in rows:
        if row.get("kind") != "row":
            continue
        content, material = str(row["content_id"]), str(row["material_id"])
        spend, revenue = nonnegative_decimal(row["spend"]), nonnegative_decimal(row["af_revenue0"])
        by_material[(content, material)] = MetricTotals(spend, revenue)
        old = by_drama.get(content, MetricTotals())
        by_drama[content] = MetricTotals(exact_add(old.spend, spend), exact_add(old.revenue, revenue))
    return MetricWindow(tuple(ready[day] for day in normalized), normalized, by_drama, by_material)


class MySQLMetricStore:
    def __init__(self, config, *, reader=None, writer_factory=None):
        self.config, self.reader = config, reader or GatedMetricReader(config)
        self.writer_factory = writer_factory or self._connect_writer

    def _connect_writer(self):
        import mysql.connector
        return mysql.connector.connect(host="101.32.56.53", port=63353, user=self.config["user"],
                                       password=self.config["password"], database=SCHEMA, charset="utf8mb4",
                                       connection_timeout=10, read_timeout=60, write_timeout=60,
                                       compress=True, use_pure=True, autocommit=False)

    def load_metric_window(self, *, product, platform, dates):
        normalized = tuple(dict.fromkeys(metric_date(day) for day in dates))
        if not normalized or len(normalized) > 30 or product != "Dramawave" or int(platform) != 0:
            raise StoreError("fb_auto_metric_product_unsupported", "指标窗口产品映射未开放", 409)
        return window_from_rows(product, platform, normalized, self.reader.window(product, platform, normalized))

    def verify(self, generation_id, count, checksum, *, wait_seconds=30):
        deadline = time.monotonic() + wait_seconds
        while self.reader.count(generation_id) != count:
            if time.monotonic() >= deadline:
                raise StoreError("fb_auto_metric_replica_pending", "指标副本尚未完整同步", 503)
            time.sleep(1)
        digest, actual_count, spend, revenue = hashlib.sha256(), 0, Decimal(0), Decimal(0)
        for raw in self.reader.rows(generation_id):
            row = canonical_row(raw); digest_row(digest, row)
            actual_count += 1
            spend = exact_add(spend, Decimal(row["spend"])); revenue = exact_add(revenue, Decimal(row["af_revenue0"]))
        if actual_count != count or digest.hexdigest() != checksum:
            raise StoreError("fb_auto_metric_checksum_mismatch", "指标数据库逐行校验失败", 500)
        return {"rows": actual_count, "storage_checksum": checksum, "spend": str(spend), "af_revenue0": str(revenue)}

    @staticmethod
    def activate(cursor, header, activated_at):
        # Fixed one-row write; an older refresh can never replace a newer day.
        cursor.execute("INSERT INTO ads_ai.fb_auto_metric_active_pointer "
                       "(platform,metric_date,product,generation_id,activated_at_utc,refreshed_at_utc) VALUES(%s,%s,%s,%s,%s,%s) "
                       "ON DUPLICATE KEY UPDATE generation_id=IF(refreshed_at_utc<=VALUES(refreshed_at_utc),VALUES(generation_id),generation_id),"
                       "activated_at_utc=IF(refreshed_at_utc<=VALUES(refreshed_at_utc),VALUES(activated_at_utc),activated_at_utc),"
                       "refreshed_at_utc=GREATEST(refreshed_at_utc,VALUES(refreshed_at_utc))",
                       (header["platform"], header["metric_date"], header["product"], header["id"], activated_at, header["refreshed_at_utc"]))

    def import_generation(self, header, rows, *, activate=False):
        """Idempotent migration and streaming refresh. Incomplete days are never active."""
        existing = self.reader.generation(generation_key=header["generation_key"])
        if existing and existing["status"] == "ready":
            if header.get("id") is not None and int(existing["id"]) != int(header["id"]):
                raise StoreError("fb_auto_metric_generation_conflict", "指标代次身份冲突", 409)
            if header.get("checksum") and existing["checksum"] != header["checksum"]:
                raise StoreError("fb_auto_metric_generation_conflict", "指标代次校验值冲突", 409)
            result = self.verify(existing["id"], existing["row_count"], existing["storage_checksum"])
            if activate:
                with closing(self.writer_factory()) as db, db.cursor() as cursor:
                    self.activate(cursor, existing, utc_iso(datetime.now(timezone.utc))); db.commit()
            return dict(result, id=existing["id"], row_count=result["rows"], status="ready", checksum=existing["checksum"], idempotent=True)
        now = utc_iso(datetime.now(timezone.utc))
        keys = ("id", "generation_key", "platform", "metric_date", "product", "refreshed_at_utc", "created_at_utc")
        values = {key: header[key] for key in keys if key in header}
        values.update(status="building", row_count=0, checksum="", storage_checksum="", ready_at_utc="", error_code="", error_message="")
        with closing(self.writer_factory()) as db, db.cursor() as cursor:
            if existing:
                generation_id = int(existing["id"])
                # Resuming this exact non-active generation only; never clear published data.
                cursor.execute("DELETE d FROM ads_ai.fb_auto_metric_daily d "
                               "JOIN ads_ai.fb_auto_metric_generation g ON g.id=d.generation_id "
                               "LEFT JOIN ads_ai.fb_auto_metric_active_pointer p ON p.generation_id=g.id "
                               "WHERE d.generation_id=%s AND g.status='building' AND p.generation_id IS NULL", (generation_id,))
            else:
                keys = list(values)
                cursor.execute("INSERT INTO ads_ai.fb_auto_metric_generation (" + ",".join(keys) + ") VALUES (" + ",".join(["%s"] * len(keys)) + ")", tuple(values[k] for k in keys))
                generation_id = int(header.get("id") or cursor.lastrowid)
            db.commit()
            source_digest, digest, count, previous, batch = hashlib.sha256(), hashlib.sha256(), 0, None, []
            spend, revenue = Decimal(0), Decimal(0)
            for raw in rows:
                row = canonical_row(raw)
                key = (row["content_id"].encode("utf-8"), len(row["material_id"]), row["material_id"])
                if previous is not None and key <= previous:
                    raise StoreError("fb_auto_metric_row_invalid", "指标行顺序或唯一性无效", 500)
                previous = key
                digest_row(source_digest, {**row, "spend": str(raw["spend"]), "af_revenue0": str(raw["af_revenue0"])})
                digest_row(digest, row); count += 1
                spend = exact_add(spend, Decimal(row["spend"])); revenue = exact_add(revenue, Decimal(row["af_revenue0"]))
                batch.append((generation_id, row["content_id"], row["material_id"], row["spend"], row["af_revenue0"]))
                if len(batch) == BATCH_SIZE:
                    cursor.executemany("INSERT INTO ads_ai.fb_auto_metric_daily VALUES(%s,%s,%s,%s,%s)", batch); db.commit(); batch = []
            if batch:
                cursor.executemany("INSERT INTO ads_ai.fb_auto_metric_daily VALUES(%s,%s,%s,%s,%s)", batch); db.commit()
            checksum, storage_checksum = source_digest.hexdigest(), digest.hexdigest()
            if header.get("checksum") and (checksum != header["checksum"] or count != header["row_count"]):
                raise StoreError("fb_auto_metric_source_checksum_mismatch", "原指标库完整性校验失败", 500)
            proof = self.verify(generation_id, count, storage_checksum)
            cursor.execute("UPDATE ads_ai.fb_auto_metric_generation SET status='ready',row_count=%s,checksum=%s,storage_checksum=%s,ready_at_utc=%s WHERE id=%s",
                           (count, checksum, storage_checksum, header.get("ready_at_utc") or now, generation_id))
            if activate:
                self.activate(cursor, dict(header, id=generation_id), now)
            db.commit()
        return dict(proof, id=generation_id, row_count=count, status="ready", checksum=checksum, idempotent=False)

    def record_metric_generation_streaming(self, *, platform, metric_date: str, product, rows, refreshed_at_utc):
        day = globals()["metric_date"](metric_date)
        if product != "Dramawave" or int(platform) != 0:
            raise StoreError("fb_auto_metric_product_unsupported", "指标产品映射未开放", 409)
        refreshed_at_utc = utc_iso(datetime.fromisoformat(refreshed_at_utc))
        identity = json.dumps([int(platform), day, product, refreshed_at_utc], separators=(",", ":"))
        header = {"generation_key": "fb-auto-metric-v1-" + hashlib.sha256(identity.encode()).hexdigest(),
                  "platform": int(platform), "metric_date": day, "product": product,
                  "refreshed_at_utc": refreshed_at_utc, "created_at_utc": utc_iso(datetime.now(timezone.utc))}
        return self.import_generation(header, rows, activate=True)


class MetricBrokerClient:
    """The unprivileged publisher sends only a fixed window DTO, never SQL."""
    def __init__(self, env):
        self.token = str(env.get("FB_AUTO_POST_INTERNAL_TOKEN", ""))
        self.port = int(env.get("FB_AUTO_METRIC_BROKER_PORT", "8849"))

    def load_metric_window(self, *, product, platform, dates):
        import requests
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.post("http://127.0.0.1:%d/window" % self.port,
                                        json={"product": product, "platform": platform, "dates": list(dates)},
                                        headers={"Authorization": "Bearer " + self.token}, timeout=(3, 65), allow_redirects=False)
            data = response.json()
            if response.status_code != 200:
                code = data.get("code", "fb_auto_metric_query_failed")
                error = StoreError(code, data.get("message", "指标只读服务暂不可用"), response.status_code)
                if "conflicts" in data: error.conflicts = data["conflicts"]
                raise error
            return window_from_rows(product, platform, dates, data["rows"])
        except StoreError:
            raise
        except (requests.RequestException, ValueError, KeyError, TypeError):
            raise StoreError("fb_auto_metric_query_failed", "指标只读服务暂不可用", 503) from None


def metric_store_from_env(env, *, writer=False):
    backend = str(env.get("FB_AUTO_METRIC_STORAGE", "sqlite"))
    if backend == "mysql":
        if writer:
            config = config_from_file(env.get("FB_AUTO_METRIC_CONFIG", "/etc/codex-private-storage-archive.json"))
            return MySQLMetricStore(config)
        return MetricBrokerClient(env)
    if backend != "sqlite":
        raise ValueError("invalid FB_AUTO_METRIC_STORAGE")
    from .core import FBAutoPostStore
    return FBAutoPostStore(env.get("FB_AUTO_METRIC_DB_PATH", "/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3"))


class GatedMetricSource:
    """The metric refresher's existing exact-day source SQL, through the same FIFO."""
    def __init__(self, config, schema="kunlunads_dev"):
        if schema != "kunlunads_dev":
            raise ValueError("invalid metric source schema")
        self.schema, self.reader = schema, GatedMetricReader(config)

    def iter_select(self, sql, params):
        from features.fb_video_publish.runtime_source import GatedReadOnlyMySQL
        bound = GatedReadOnlyMySQL._bind(sql, params)
        query = ("SELECT HEX(JSON_OBJECT('content_id',content_id,'material_id',material_id,"
                 "'spend',CAST(spend AS CHAR),'af_revenue0',CAST(af_revenue0 AS CHAR))) FROM (" + bound
                 + ") metric_source ORDER BY CONVERT(content_id USING binary),CHAR_LENGTH(material_id),material_id")
        yield from self.reader.query([query], timeout=180, execution_ms=120000)
