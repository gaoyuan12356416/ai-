"""Fixed-table MySQL access: FIFO-gated replica reads and scoped writes."""
import datetime
import errno
import json
import logging
import os
import re
import subprocess
import threading
import time


CATALOG_TABLE = "ads_ai.cdreader_koc_drama"
PROMOTION_TABLE = "ads_ai.cdreader_koc_promotion"
CATALOG_COLUMNS = (
    "id", "institution_user_id", "project_type", "story_id", "story_name", "c_name",
    "channel_text", "introduce", "img_src", "lang_id", "is_full_text", "baidu_link",
    "baidu_link_code", "source_publish_time", "is_show", "local_type", "local_type_text",
    "audio_type", "audio_type_text", "holder_type", "holder_text", "payepisfrom",
    "raw_json", "first_seen_at", "last_seen_at", "last_sync_run_id",
)
LIST_COLUMNS = tuple(c for c in CATALOG_COLUMNS if c not in {"raw_json", "introduce", "baidu_link"})
PROMOTION_COLUMNS = (
    "institution_user_id", "star_user_id", "project_type", "story_id", "tenant_key",
    "user_id", "star_name", "status", "request_id", "koc_code", "deep_link", "home_link",
    "request_json", "response_json", "created_at", "updated_at",
)


class DatabaseError(RuntimeError):
    pass


_TRANSIENT_MYSQL_ERRORS = {1040, 1203, 2002, 2003, 2006, 2013, 2055, 3024}
_TRANSIENT_OS_ERRORS = {errno.EAGAIN, errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
                        errno.EHOSTUNREACH, errno.ENETUNREACH}


def _read_failure(attempt, kind, mysql_errno=None, returncode=None, os_errno=None):
    # Never include stderr, SQL, subprocess arguments, response rows or exception text.
    logging.warning("Moboreels read failed: attempt=%d kind=%s mysql_errno=%s wrapper_rc=%s os_errno=%s",
                    attempt, kind, mysql_errno, returncode, os_errno)


def _literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if not text:
        return "CONVERT(X'' USING utf8mb4) COLLATE utf8mb4_unicode_ci"
    return "CONVERT(0x%s USING utf8mb4) COLLATE utf8mb4_unicode_ci" % text.encode("utf-8").hex()


def _json_expr(columns):
    return "JSON_OBJECT(" + ",".join("'%s',`%s`" % (c, c) for c in columns) + ")"


def _utc_datetime(value):
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


class Database:
    def __init__(self, settings):
        self.settings = settings
        if (settings.db_host, settings.read_port, settings.write_port, settings.database) != (
            "101.32.56.53", 63350, 63353, "ads_ai"
        ):
            raise ValueError("Moboreels database endpoint boundary does not match")
        self._write_lock = threading.RLock()
        self._writer = None
        self._writer_rows = 0

    def _read(self, statements):
        """Read only through the host FIFO wrapper; no direct-driver bypass."""
        s = self.settings
        env = dict(os.environ)
        env["MYSQL_PWD"] = s.db_password
        env["SQL_GATE_JOB_NAME"] = "moboreels-library"
        env["SQL_GATE_WAIT_TIMEOUT_SECONDS"] = "30"
        env["SQL_GATE_EXEC_TIMEOUT_SECONDS"] = "60"
        env.pop("SQL_GATE_BYPASS", None)
        env.pop("SQL_GATE_TEST_MODE", None)
        command = [s.mysql_binary, "--host=" + s.db_host, "--port=63350", "--user=" + s.db_user,
                   "--database=ads_ai", "--default-character-set=utf8mb4", "--batch", "--raw",
                   "--skip-column-names", "--connect-timeout=5"]
        sql = "SELECT @@read_only; SET SESSION MAX_EXECUTION_TIME=15000;\n" + statements
        for attempt in range(1, 4):
            try:
                result = subprocess.run(command, input=sql, encoding="utf-8", errors="strict",
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=env, timeout=100, check=False)
            except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
                os_errno = exc.errno if isinstance(exc, OSError) else None
                transient = isinstance(exc, subprocess.TimeoutExpired) or os_errno in _TRANSIENT_OS_ERRORS
                _read_failure(attempt, type(exc).__name__, os_errno=os_errno)
                if transient and attempt < 3:
                    time.sleep(attempt)
                    continue
                raise DatabaseError("数据库读取暂不可用，请稍后重试") from exc
            if result.returncode:
                match = re.search(r"\bERROR\s+(\d{4,5})\b", result.stderr or "")
                mysql_errno = int(match.group(1)) if match else None
                _read_failure(attempt, "command", mysql_errno, result.returncode)
                # Each retry re-enters the same FIFO wrapper. No driver or endpoint fallback.
                transient = mysql_errno in _TRANSIENT_MYSQL_ERRORS or result.returncode in (75, 124)
                if transient and attempt < 3:
                    time.sleep(attempt)
                    continue
                raise DatabaseError("数据库读取失败，请稍后重试")
            # JSON strings may contain literal NEL/LS/PS characters. Only LF separates CLI rows.
            lines = result.stdout.split("\n")
            if not lines or lines[0].strip() != "1":
                _read_failure(attempt, "readonly_validation")
                raise DatabaseError("数据库只读端验证失败")
            try:
                return [json.loads(line) for line in lines[1:] if line.strip()]
            except (ValueError, TypeError) as exc:
                _read_failure(attempt, "json_format")
                raise DatabaseError("数据库读取结果格式异常") from exc

    def _scope(self):
        return "institution_user_id=%s AND project_type=2" % _literal(self.settings.institution_user_id)

    def save_media_registration(self, record):
        columns = ("record_key", "institution_user_id", "tenant_key", "user_id", "star_user_id", "star_name",
                   "project_type", "platform_type", "account_id", "nickname", "platform_link", "status", "message",
                   "provider_id", "request_id", "request_json", "response_json", "created_at", "updated_at")
        row = dict(record, record_key=record["id"], request_json=json.dumps(record["request"], ensure_ascii=False),
                   response_json=json.dumps(record.get("response"), ensure_ascii=False),
                   created_at=_utc_datetime(record["created_at"]), updated_at=_utc_datetime(record["updated_at"]))
        updates = [c for c in columns if c not in {"record_key", "institution_user_id", "tenant_key", "user_id", "star_user_id", "created_at"}]
        accept = "(VALUES(updated_at)>=updated_at AND (status<>'succeeded' OR VALUES(status)='succeeded'))"
        assignments = ",".join("`%s`=IF(%s,VALUES(`%s`),`%s`)" % (c,accept,c,c) for c in updates)
        sql = "INSERT INTO ads_ai.cdreader_koc_media_account (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s" % (
            ",".join("`%s`" % c for c in columns), ",".join(["%s"] * len(columns)), assignments)
        self._write(sql, tuple(row.get(c) for c in columns))

    def list_dramas(self, params=None):
        params = params or {}
        def integer(key, default, minimum, maximum):
            value = params.get(key, default)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else default
            if value in (None, ""):
                value = default
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError("分页或筛选参数无效")
            if not minimum <= value <= maximum:
                raise ValueError("分页或筛选参数超出范围")
            return value
        page = integer("page_index", 1, 1, 1000000)
        size = integer("page_size", 50, 1, 100)
        where = [self._scope()]
        raw_status = params.get("is_show", params.get("status", "1"))
        if isinstance(raw_status, (list, tuple)):
            raw_status = raw_status[0] if raw_status else "1"
        if str(raw_status) not in ("all", "-1", ""):
            if str(raw_status) not in ("0", "1"):
                raise ValueError("上下架筛选参数无效")
            where.append("is_show=" + str(raw_status))
        for name in ("lang_id", "local_type", "audio_type"):
            value = params.get(name)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else None
            if value not in (None, "", "all"):
                parsed = integer(name, 0, -1, 32767)
                where.append("`%s`=%d" % (name, parsed))
        query = params.get("q", params.get("search", ""))
        if isinstance(query, (list, tuple)):
            query = query[0] if query else ""
        query = str(query or "").strip()
        if len(query) > 200:
            raise ValueError("搜索内容过长")
        if query:
            where.append("(LOCATE(%s,story_name)>0 OR LOCATE(%s,story_id)>0)" % (_literal(query), _literal(query)))
        condition = " AND ".join(where)
        sql = "SELECT JSON_OBJECT('_kind','count','total',COUNT(*)) FROM %s WHERE %s;\n" % (CATALOG_TABLE, condition)
        sql += "SELECT %s FROM %s WHERE %s ORDER BY source_publish_time DESC,story_id DESC LIMIT %d OFFSET %d;\n" % (
            _json_expr(LIST_COLUMNS), CATALOG_TABLE, condition, size, (page - 1) * size)
        sql += "SELECT JSON_OBJECT('_kind','language','lang_id',lang_id) FROM %s WHERE %s GROUP BY lang_id ORDER BY lang_id;" % (CATALOG_TABLE, self._scope())
        rows = self._read(sql)
        return {"list": [r for r in rows if "_kind" not in r],
                "total": next((r["total"] for r in rows if r.get("_kind") == "count"), 0),
                "page_index": page, "page_size": size,
                "filters": {"languages": [r["lang_id"] for r in rows if r.get("_kind") == "language"]}}

    def get_drama(self, story_id):
        if not str(story_id).strip() or len(str(story_id)) > 64:
            raise ValueError("短剧ID无效")
        rows = self._read("SELECT %s FROM %s WHERE %s AND story_id=%s LIMIT 1;" % (
            _json_expr(CATALOG_COLUMNS), CATALOG_TABLE, self._scope(), _literal(str(story_id))))
        return rows[0] if rows else None

    def iter_catalog_rows(self, batch_size=500):
        batch_size = max(1, min(int(batch_size), 1000))
        last_id = 0
        while True:
            rows = self._read("SELECT %s FROM %s WHERE %s AND id>%d ORDER BY id LIMIT %d;" % (
                _json_expr(CATALOG_COLUMNS), CATALOG_TABLE, self._scope(), last_id, batch_size))
            if not rows:
                return
            for row in rows:
                yield row
            last_id = rows[-1]["id"]
            if len(rows) < batch_size:
                return

    def read_catalog_ids(self, story_ids):
        ids = list(dict.fromkeys(str(v) for v in story_ids))
        if not ids:
            return {}
        if len(ids) > 500:
            raise ValueError("批次最多500条")
        rows = self._read("SELECT %s FROM %s WHERE %s AND story_id IN (%s);" % (
            _json_expr(CATALOG_COLUMNS), CATALOG_TABLE, self._scope(), ",".join(_literal(v) for v in ids)))
        return {str(row["story_id"]): row for row in rows}

    def _connection(self):
        if self._writer is None:
            import pymysql
            s = self.settings
            self._writer = pymysql.connect(host=s.db_host, port=63353, user=s.db_user,
                                           password=s.db_password, database="ads_ai", charset="utf8mb4",
                                           connect_timeout=5, read_timeout=30, write_timeout=20,
                                           autocommit=False)
            try:
                with self._writer.cursor() as cursor:
                    cursor.execute("SELECT DATABASE(),@@read_only")
                    if cursor.fetchone() != ("ads_ai", 0):
                        raise DatabaseError("数据库写入端验证失败")
            except Exception:
                self.close_writer()
                raise
            self._writer_rows = 0
        return self._writer

    def close_writer(self):
        with self._write_lock:
            if self._writer is not None:
                try:
                    self._writer.close()
                finally:
                    self._writer = None
                    self._writer_rows = 0

    def _write(self, sql, rows, many=False, row_count=1):
        with self._write_lock:
            if self._writer_rows >= 500:
                self.close_writer()
            connection = self._connection()
            try:
                with connection.cursor() as cursor:
                    if many:
                        cursor.executemany(sql, rows)
                    else:
                        cursor.execute(sql, rows)
                connection.commit()
                self._writer_rows += row_count
            except Exception:
                # The caller reconciles the replica before deciding to send another write.
                self.close_writer()
                raise

    def apply_catalog_batch(self, records, run_id, seen_at):
        if not records:
            return
        if len(records) > 20:
            raise ValueError("变更写入批次最多20条")
        columns = CATALOG_COLUMNS[1:]
        moment = _utc_datetime(seen_at)
        values = []
        for row in records:
            record = dict(row)
            record.update(institution_user_id=self.settings.institution_user_id, project_type=2,
                          first_seen_at=moment, last_seen_at=moment, last_sync_run_id=run_id)
            record["raw_json"] = json.dumps(record["raw_json"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            values.append(tuple(record.get(col) for col in columns))
        updates = [col for col in columns if col not in {"institution_user_id", "project_type", "story_id", "first_seen_at"}]
        sql = "INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s" % (
            CATALOG_TABLE, ",".join("`%s`" % c for c in columns), ",".join(["%s"] * len(columns)),
            ",".join("`%s`=VALUES(`%s`)" % (c, c) for c in updates))
        self._write(sql, values, many=True, row_count=len(values))

    def touch_catalog(self, story_ids, run_id, seen_at):
        ids = list(dict.fromkeys(str(v) for v in story_ids))
        if not ids:
            return
        if len(ids) > 500:
            raise ValueError("同步标记批次最多500条")
        sql = "UPDATE %s SET last_seen_at=%%s,last_sync_run_id=%%s WHERE institution_user_id=%%s AND project_type=2 AND story_id IN (%s)" % (CATALOG_TABLE, ",".join(["%s"] * len(ids)))
        self._write(sql, [_utc_datetime(seen_at), run_id, self.settings.institution_user_id] + ids, row_count=len(ids))

    def get_promotion(self, star_user_id, story_id):
        rows = self._read("SELECT %s FROM %s WHERE %s AND star_user_id=%s AND story_id=%s LIMIT 1;" % (
            _json_expr(PROMOTION_COLUMNS), PROMOTION_TABLE, self._scope(), _literal(star_user_id), _literal(story_id)))
        return rows[0] if rows else None

    def mirror_promotion(self, record):
        row = dict(record)
        if row.get("institution_user_id") != self.settings.institution_user_id or int(row.get("project_type", 0)) != 2:
            raise ValueError("推广记录机构或业务类型不匹配")
        for key in ("request_json", "response_json"):
            row[key] = None if row.get(key) is None else json.dumps(row[key], ensure_ascii=False, separators=(",", ":"))
        for key in ("created_at", "updated_at"):
            row[key] = _utc_datetime(row[key])
        updates = [c for c in PROMOTION_COLUMNS if c not in {"institution_user_id", "star_user_id", "project_type", "story_id", "created_at"}]
        # A delayed outbox item cannot replace a newer receipt or downgrade a success.
        accept = "(VALUES(updated_at)>=updated_at AND (status<>'succeeded' OR VALUES(status)='succeeded'))"
        sql = "INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s" % (
            PROMOTION_TABLE, ",".join("`%s`" % c for c in PROMOTION_COLUMNS), ",".join(["%s"] * len(PROMOTION_COLUMNS)),
            ",".join("`%s`=IF(%s,VALUES(`%s`),`%s`)" % (c, accept, c, c) for c in updates))
        self._write(sql, tuple(row.get(c) for c in PROMOTION_COLUMNS))
