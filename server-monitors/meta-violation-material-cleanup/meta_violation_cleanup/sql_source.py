"""Read-only business SQL through the CPU FIFO gate, never a direct driver.

The violation window is read in one repeatable-read transaction.  Every SELECT
has a random completion frame; missing rows/frames, SQL errors and timeouts fail
closed.  Exceptions and metrics deliberately contain no SQL or source values.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid


_SQL_LOCK = threading.RLock()
_BEIJING = timezone(timedelta(hours=8))
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_EOF = object()


class SqlSourceError(RuntimeError):
    """A deliberately non-sensitive adapter error."""


def _quote(value):
    value = str(value)
    return "_utf8mb4 0x" + value.encode("utf-8").hex() if value else "''"


def _ids(values):
    values = list(dict.fromkeys(str(value) for value in values))
    if any(not _ID.fullmatch(value) for value in values):
        raise SqlSourceError("invalid_source_identifier")
    return values


def _inside(values):
    return "(" + ",".join(_quote(value) for value in values) + ")"


def _chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _read_env(path):
    result = {}
    if path:
        try:
            lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            raise SqlSourceError("source_environment_unavailable") from None
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            key, value = key.strip(), value.strip()
            if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                value = value[1:-1]
            if _IDENTIFIER.fullmatch(key):
                result[key] = value
    result.update(os.environ)
    return result


def _date(value):
    try:
        if not isinstance(value, datetime):
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if value.tzinfo is not None:
            value = value.astimezone(_BEIJING).replace(tzinfo=None)
        return value
    except (TypeError, ValueError, OverflowError):
        raise SqlSourceError("invalid_window_timestamp") from None


def _timestamp(value):
    return _date(value).strftime("%Y-%m-%d %H:%M:%S.%f")


def _violation_expressions():
    return tuple((key, "CAST(%s AS CHAR)" % key) for key in
                 ("id", "ad_id", "source_id", "app_id")) + (
        ("created_at", "DATE_FORMAT(created_at,'%Y-%m-%d %H:%i:%s.%f')"),
        ("updated_at", "DATE_FORMAT(updated_at,'%Y-%m-%d %H:%i:%s.%f')"),
        ("issues_info", "issues_info"), ("is_dynamic", "is_dynamic"),
        ("delete_status", "delete_status"))


class _Session:
    """One gate-owned mysql process with an incremental JSONL protocol."""

    def __init__(self, source):
        self.source = source
        self.proc = None
        self.lines = queue.Queue()
        self.started = time.monotonic()
        self.closed = False

    def open(self):
        try:
            self.proc = subprocess.Popen(
                self.source._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self.source._process_env(),
                text=True, encoding="utf-8", errors="strict", bufsize=1,
            )
        except (OSError, ValueError):
            raise SqlSourceError("gated_mysql_unavailable") from None
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        try:
            proof = self.query("SELECT JSON_OBJECT('read_only',@@read_only)", first=True)
            if proof != [{"read_only": 1}]:
                raise SqlSourceError("read_only_proof_missing")
            self.query("SET SESSION time_zone='+08:00'; "
                       "SET SESSION max_execution_time=%d; "
                       "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ; "
                       "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY" %
                       (self.source.query_timeout * 1000))
            return self
        except Exception:
            self.close()
            raise

    def _read_stdout(self):
        try:
            while True:
                line = self.proc.stdout.readline(16 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 16 * 1024 * 1024 or not line.endswith("\n"):
                    self.lines.put(SqlSourceError("invalid_sql_frame"))
                    break
                self.lines.put(line)
        except Exception:
            self.lines.put(SqlSourceError("invalid_sql_stream"))
        finally:
            self.lines.put(_EOF)

    def query(self, sql, first=False):
        if self.closed:
            raise SqlSourceError("sql_session_closed")
        marker = "cleanup-complete-" + uuid.uuid4().hex
        started = time.monotonic()
        deadline = min(
            started + self.source.query_timeout + (40 if first else 5),
            self.started + self.source.session_timeout,
        )
        rows = []
        self.source._metrics["query_count"] += 1
        try:
            self.proc.stdin.write(sql.rstrip("; \n") + ";\nSELECT JSON_ARRAY(" + _quote(marker) + ");\n")
            self.proc.stdin.flush()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SqlSourceError("sql_query_timeout")
                try:
                    line = self.lines.get(timeout=remaining)
                except queue.Empty:
                    raise SqlSourceError("sql_query_timeout") from None
                if line is _EOF:
                    raise SqlSourceError("sql_stream_incomplete")
                if isinstance(line, Exception):
                    raise SqlSourceError("invalid_sql_stream")
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    raise SqlSourceError("invalid_sql_frame") from None
                if row == [marker]:
                    if self.proc.poll() not in (None, 0):
                        raise SqlSourceError("sql_query_failed")
                    return rows
                if not isinstance(row, dict):
                    raise SqlSourceError("unexpected_sql_frame")
                rows.append(row)
        except SqlSourceError:
            self.source._metrics["query_failures"] += 1
            self.close()
            raise
        except Exception:
            self.source._metrics["query_failures"] += 1
            self.close()
            raise SqlSourceError("sql_query_failed") from None
        finally:
            self.source._metrics["query_seconds"] += time.monotonic() - started

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.proc is None:
            return
        # Closing stdin lets mysql roll back the read-only transaction and the
        # FIFO wrapper release its slot.  Termination is bounded on bad streams.
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        finally:
            if self.proc.stdout:
                self.proc.stdout.close()


class SqlSource:
    """Adapter shared by preview and execution; source database is never written."""

    def __init__(self, config):
        get = config.get if hasattr(config, "get") else lambda key, default=None: getattr(config, key, default)
        env = _read_env(get("env_file", "/root/drama_material_service/.env"))
        host = env.get("DRAMA_DB_HOST") or env.get("ADMIN_MAPPING_MYSQL_HOST") or "101.32.56.53"
        port = env.get("DRAMA_DB_PORT") or env.get("ADMIN_MAPPING_MYSQL_PORT") or "63350"
        if host != "101.32.56.53" or str(port) != "63350":
            raise SqlSourceError("read_only_endpoint_required")
        if get("mysql_binary", "/usr/bin/mysql") != "/usr/bin/mysql":
            raise SqlSourceError("fifo_mysql_required")
        user = env.get("DRAMA_DB_USER") or env.get("ADMIN_MAPPING_MYSQL_USER")
        self._password = env.get("DRAMA_DB_PASSWORD") or env.get("ADMIN_MAPPING_MYSQL_PASSWORD")
        if not user or not self._password:
            raise SqlSourceError("source_credentials_unavailable")
        self.schema = get("schema") or env.get("AD_CONTROL_DB_NAME") or env.get("DRAMA_DB_NAME") or "kunlunads_dev"
        if self.schema != "kunlunads_dev":
            raise SqlSourceError("source_schema_not_allowed")
        self.query_timeout = int(get("query_timeout_seconds", 30))
        self.session_timeout = int(get("session_timeout_seconds", 1800))
        self.page_size = int(get("page_size", 2000))
        self.lineage_batch_size = int(get("lineage_batch_size", 200))
        if not (1 <= self.query_timeout <= 120 and 60 <= self.session_timeout <= 3600 and
                1 <= self.page_size <= 2000 and 1 <= self.lineage_batch_size <= 200):
            raise SqlSourceError("unsafe_sql_limits")
        # The installed FIFO wrapper prepends its own mysql options. MySQL only
        # accepts --no-defaults as the first option, so it cannot be used here.
        # Endpoint/user/protocol are explicit and every session proves read-only.
        self._command = ["/usr/bin/mysql", "--protocol=TCP", "--host=101.32.56.53",
                         "--port=63350", "--user=" + user, "--connect-timeout=5",
                         "--default-character-set=utf8mb4", "--batch", "--raw", "--skip-column-names",
                         "--unbuffered", "--quick", "--skip-reconnect"]
        self._metrics = {"query_count": 0, "query_failures": 0, "query_seconds": 0.0,
                         "sessions": 0, "violation_pages": 0}
        self._active = None

    def _process_env(self):
        env = os.environ.copy()
        env.update(MYSQL_PWD=self._password, MYSQL_HISTFILE=os.devnull, SQL_GATE_BYPASS="0",
                   SQL_GATE_RETRY_DELAYS_SECONDS="", SQL_GATE_WAIT_TIMEOUT_SECONDS="30",
                   SQL_GATE_EXEC_TIMEOUT_SECONDS=str(self.session_timeout + 15),
                   MYSQL_QUERY_TIMEOUT_SECONDS=str(self.session_timeout),
                   MYSQL_QUERY_CONNECT_TIMEOUT_SECONDS="5")
        return env

    @property
    def safe_metrics(self):
        return dict(self._metrics, query_seconds=round(self._metrics["query_seconds"], 3))

    @contextmanager
    def _session(self):
        # One process/session in this program, including separate SqlSource objects.
        with _SQL_LOCK:
            session = _Session(self)
            self._active = session
            self._metrics["sessions"] += 1
            try:
                yield session.open()
            finally:
                session.close()
                self._active = None

    def close(self):
        if self._active is not None:
            self._active.close()

    def _select(self, expressions, table, tail=""):
        pairs = []
        for name, expression in expressions:
            pairs.extend((_quote(name), expression))
        return "SELECT JSON_OBJECT(%s) FROM `%s`.%s %s" % (",".join(pairs), self.schema, table, tail)

    def products(self):
        sql = self._select((("id", "CAST(id AS CHAR)"), ("name", "name"),
                            ("ads_app_id", "CAST(ads_app_id AS CHAR)"), ("app_type", "app_type"),
                            ("default_user", "CAST(default_user AS CHAR)")),
                           "ads_apps_setting", "ORDER BY id LIMIT 20001")
        with self._session() as session:
            rows = session.query(sql)
        if len(rows) > 20000:
            raise SqlSourceError("product_directory_incomplete")
        return rows

    def violations(self, start, end):
        start, end = _date(start), _date(end)
        if start >= end or end - start > timedelta(days=30):
            raise SqlSourceError("invalid_violation_window")
        expressions = _violation_expressions()
        result = []
        seen = set()
        with self._session() as session:
            states = session.query(self._select((("delete_status", "delete_status"),),
                                   "ads_facebook_violations FORCE INDEX(dc)",
                                   "GROUP BY delete_status ORDER BY delete_status"))
            if len(states) > 100 or any(set(row) != {"delete_status"} for row in states):
                raise SqlSourceError("invalid_violation_statuses")
            for state in states:
                value = state["delete_status"]
                predicate = "delete_status IS NULL" if value is None else "delete_status=" + _quote(value)
                cursor = None
                while True:
                    where = "%s AND created_at>=%s AND created_at<%s" % (
                        predicate, _quote(_timestamp(start)), _quote(_timestamp(end)))
                    if cursor is not None:
                        where += " AND (created_at>%s OR (created_at=%s AND id>%s))" % (
                            _quote(cursor[0]), _quote(cursor[0]), cursor[1])
                    sql = self._select(expressions, "ads_facebook_violations FORCE INDEX(dc)",
                                       "WHERE %s ORDER BY created_at,id LIMIT %d" % (where, self.page_size))
                    page = session.query(sql)
                    self._metrics["violation_pages"] += 1
                    if len(page) > self.page_size:
                        raise SqlSourceError("violation_page_overflow")
                    for row in page:
                        if set(row) != {name for name, _ in expressions}:
                            raise SqlSourceError("invalid_violation_row")
                        identifier = str(row["id"])
                        if not _ID.fullmatch(identifier) or identifier in seen:
                            raise SqlSourceError("duplicate_violation_identity")
                        created = _date(row["created_at"])
                        current = (_timestamp(created), int(identifier))
                        if (not start <= created < end or row["delete_status"] != value or
                                cursor is not None and current <= (cursor[0], int(cursor[1]))):
                            raise SqlSourceError("invalid_violation_order")
                        cursor = (current[0], identifier)
                        seen.add(identifier)
                        result.append(row)
                    if len(page) < self.page_size:
                        break
        return result

    def violations_by_ids(self, ids):
        ids = _ids(ids)
        result = []
        if not ids:
            return result
        with self._session() as session:
            for part in _chunks(ids, 200):
                rows = session.query(self._select(_violation_expressions(),
                                     "ads_facebook_violations FORCE INDEX(PRIMARY)",
                                     "WHERE id IN %s ORDER BY id" % _inside(part)))
                found = set()
                for row in rows:
                    identifier = str(row.get("id"))
                    if (identifier not in part or identifier in found or
                            set(row) != {name for name, _ in _violation_expressions()}):
                        raise SqlSourceError("invalid_violation_evidence")
                    found.add(identifier)
                result.extend(rows)
        return result

    def lineages(self, ad_ids):
        ad_ids = _ids(ad_ids)
        result = {ad_id: [] for ad_id in ad_ids}
        if not ad_ids:
            return result
        expressions = tuple((key, "CAST(%s AS CHAR)" % key) for key in
                            ("id", "ad_id", "product", "source_id", "original_source_id", "creative_id",
                             "ad_account_id", "user_id", "publish_queue_id", "video_id", "image_ids")) + (
            ("created_at", "DATE_FORMAT(created_at,'%Y-%m-%d %H:%i:%s.%f')"),)
        with self._session() as session:
            for part in _chunks(ad_ids, self.lineage_batch_size):
                sql = self._select(expressions, "ads_facebook_auto_created_data",
                                   "WHERE ad_id IN %s ORDER BY ad_id,id LIMIT 20001" % _inside(part))
                rows = session.query(sql)
                if len(rows) > 20000:
                    raise SqlSourceError("ad_lineage_incomplete")
                for row in rows:
                    ad_id = str(row.get("ad_id"))
                    if ad_id not in part:
                        raise SqlSourceError("unexpected_ad_lineage")
                    row["row_id"] = str(row["id"])
                    result[ad_id].append(row)
        return result

    def sources(self, source_ids):
        source_ids = _ids(source_ids)
        result = {}
        if not source_ids:
            return result
        with self._session() as session:
            for part in _chunks(source_ids, 200):
                sql = self._select((("id", "CAST(id AS CHAR)"), ("source_type", "source_type"),
                                    ("source_id", "CAST(source_id AS CHAR)")),
                                   "ads_source FORCE INDEX(PRIMARY)", "WHERE id IN " + _inside(part))
                for row in session.query(sql):
                    identifier = str(row.get("id"))
                    if identifier not in part or identifier in result:
                        raise SqlSourceError("invalid_source_mapping")
                    result[identifier] = row
        return result

    def credentials(self):
        # The live schema has no validity/status field.  Newest nonempty source
        # credential per user is only a candidate; caller verifies via Meta GET.
        sql = self._select((("id", "CAST(id AS CHAR)"), ("user_id", "CAST(user_id AS CHAR)"),
                            ("token", "accessToken")), "ads_facebook_info",
                           "WHERE TRIM(accessToken)<>'' ORDER BY user_id,id DESC LIMIT 10001")
        with self._session() as session:
            rows = session.query(sql)
        if len(rows) > 10000:
            raise SqlSourceError("credential_directory_incomplete")
        result = {}
        for row in rows:
            user = str(row.get("user_id"))
            if _ID.fullmatch(user) and str(row.get("token") or "").strip():
                result.setdefault(user, str(row["token"]).strip())
        return result

    def selected_credential(self, lineage):
        """Return a non-secret internal user ID, never the selected raw Token."""
        keys = ("id", "ad_id", "product", "user_id", "publish_queue_id")
        identity = {key: str(lineage.get(key) or "") for key in keys}
        if not identity["id"]:
            identity["id"] = str(lineage.get("row_id") or "")
        _ids(identity.values())
        expressions = (("id", "CAST(a.id AS CHAR)"), ("ad_id", "a.ad_id"), ("product", "a.product"),
                       ("user_id", "CAST(a.user_id AS CHAR)"), ("publish_queue_id", "CAST(a.publish_queue_id AS CHAR)"),
                       ("queue_id", "CAST(pq.id AS CHAR)"), ("queue_product", "pq.product"),
                       ("queue_user_id", "CAST(pq.user_id AS CHAR)"), ("default_token", "CAST(pq.default_token AS CHAR)"),
                       ("setting_id", "CAST(p.id AS CHAR)"), ("default_user", "CAST(p.default_user AS CHAR)"))
        joins = ("ads_facebook_auto_created_data a "
                 "LEFT JOIN `{s}`.ads_template_make_queue pq ON pq.id=a.publish_queue_id "
                 "LEFT JOIN `{s}`.ads_apps_setting p ON p.id=a.product").format(s=self.schema)
        where = " AND ".join("a.%s=%s" % (key, _quote(identity[key])) for key in keys)
        with self._session() as session:
            rows = session.query(self._select(expressions, joins, "WHERE " + where + " LIMIT 2"))
            if len(rows) != 1:
                raise SqlSourceError("credential_lineage_changed")
            row = rows[0]
            if (any(str(row.get(key)) != identity[key] for key in keys) or
                    str(row.get("queue_id")) != identity["publish_queue_id"] or
                    str(row.get("queue_product")) != identity["product"] or
                    str(row.get("queue_user_id")) != identity["user_id"]):
                raise SqlSourceError("credential_queue_mismatch")
            flag = str(row.get("default_token"))
            if flag == "1":
                user = str(row.get("default_user") or "")
                if str(row.get("setting_id")) != identity["product"] or not _ID.fullmatch(user):
                    raise SqlSourceError("product_default_credential_missing")
            elif flag == "-1":
                user = identity["user_id"]
            else:
                raise SqlSourceError("credential_rule_unknown")
            tokens = session.query(self._select((("token", "accessToken"),), "ads_facebook_info",
                                   "WHERE user_id=%s AND TRIM(accessToken)<>'' ORDER BY id DESC LIMIT 1" % _quote(user)))
            if len(tokens) != 1 or not str(tokens[0].get("token") or "").strip():
                raise SqlSourceError("publishing_credential_unavailable")
            return user
