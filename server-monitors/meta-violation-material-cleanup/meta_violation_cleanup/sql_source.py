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
_ACTIVE_SESSION = None
_BEIJING = timezone(timedelta(hours=8))
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_EOF = object()


class SqlSourceError(RuntimeError):
    """A deliberately non-sensitive adapter error."""

    _CODES = frozenset({
        "invalid_source_identifier", "source_environment_unavailable",
        "invalid_window_timestamp", "gated_mysql_unavailable",
        "read_only_proof_missing", "sql_session_closed", "sql_query_timeout",
        "sql_stream_incomplete", "invalid_sql_stream", "invalid_sql_frame",
        "sql_query_failed", "unexpected_sql_frame", "read_only_endpoint_required",
        "fifo_mysql_required", "source_credentials_unavailable",
        "source_schema_not_allowed", "unsafe_sql_limits", "sql_snapshot_unavailable",
        "another_source_snapshot_active", "product_directory_incomplete",
        "invalid_violation_window", "invalid_violation_statuses",
        "violation_page_overflow", "invalid_violation_row", "duplicate_violation_identity",
        "invalid_violation_order", "invalid_violation_evidence", "ad_lineage_incomplete",
        "unexpected_ad_lineage", "invalid_source_mapping", "credential_directory_incomplete",
        "invalid_credential_lineage", "credential_lineage_changed", "credential_queue_mismatch",
        "product_default_credential_missing", "credential_rule_unknown", "invalid_credential_row_id",
        "invalid_credential_route_result", "invalid_credential_availability",
        "credential_availability_incomplete", "publishing_credential_unavailable",
    })

    def __init__(self, code):
        # Callers may accidentally pass SQL, credentials or another exception.
        # Only this fixed vocabulary is safe for structured runtime events.
        self.code = code if type(code) is str and code in self._CODES else "sql_source_error"
        super().__init__(self.code)


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
            if deadline <= time.monotonic():
                raise SqlSourceError("sql_query_timeout")
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
        self._active_owner = None

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
        global _ACTIVE_SESSION
        # The reentrant lock is held for the entire outer snapshot. Other
        # threads wait and open a NEW transaction after it has been released.
        with _SQL_LOCK:
            owner = threading.get_ident()
            if self._active is not None:
                if self._active_owner != owner or self._active.closed:
                    raise SqlSourceError("sql_snapshot_unavailable")
                try:
                    yield self._active
                except BaseException:
                    # Do not let a caught page/query error silently start a new
                    # snapshot partway through a multi-method preflight.
                    self._active.close()
                    raise
                return
            # An RLock permits reentry from a different adapter on the same
            # thread. Reject that case rather than open a second SQL connection.
            if _ACTIVE_SESSION is not None:
                raise SqlSourceError("another_source_snapshot_active")
            session = _Session(self)
            self._active = _ACTIVE_SESSION = session
            self._active_owner = owner
            self._metrics["sessions"] += 1
            try:
                yield session.open()
            finally:
                try:
                    session.close()
                finally:
                    self._active = _ACTIVE_SESSION = None
                    self._active_owner = None

    @contextmanager
    def snapshot(self):
        """Reuse one gated read-only transaction for a short group of reads.

        Nested snapshots on this adapter share the original connection and
        deadline. No credentials or rows are cached between snapshots.
        """
        with self._session():
            yield self

    def close(self):
        if self._active is not None:
            self._active.close()

    def _select(self, expressions, table, tail=""):
        pairs = []
        for name, expression in expressions:
            pairs.extend((_quote(name), expression))
        return "SELECT JSON_OBJECT(%s) FROM `%s`.%s %s" % (",".join(pairs), self.schema, table, tail)

    def products(self):
        return self._products("")

    def scope_products(self):
        """Small current directory for the two allowed roots and their children."""
        return self._products("WHERE id IN (1479,3302) OR ads_app_id IN (1479,3302)")

    def _products(self, where):
        sql = self._select((("id", "CAST(id AS CHAR)"), ("name", "name"),
                            ("ads_app_id", "CAST(ads_app_id AS CHAR)"), ("app_type", "app_type"),
                            ("default_user", "CAST(default_user AS CHAR)")),
                           "ads_apps_setting", where + " ORDER BY id LIMIT 20001")
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

    @staticmethod
    def _credential_identity(lineage):
        if not isinstance(lineage, dict):
            raise SqlSourceError("invalid_credential_lineage")
        keys = ("id", "ad_id", "product", "user_id", "publish_queue_id")
        identity = {key: str(lineage.get(key) or "") for key in keys}
        if not identity["id"]:
            identity["id"] = str(lineage.get("row_id") or "")
        if lineage.get("row_id") and str(lineage["row_id"]) != identity["id"]:
            raise SqlSourceError("credential_lineage_changed")
        _ids(identity.values())
        return identity

    def _credential_query(self, tail):
        expressions = (("id", "CAST(a.id AS CHAR)"), ("ad_id", "a.ad_id"), ("product", "a.product"),
                       ("user_id", "CAST(a.user_id AS CHAR)"), ("publish_queue_id", "CAST(a.publish_queue_id AS CHAR)"),
                       ("queue_id", "CAST(pq.id AS CHAR)"), ("queue_product", "pq.product"),
                       ("queue_user_id", "CAST(pq.user_id AS CHAR)"), ("default_token", "CAST(pq.default_token AS CHAR)"),
                       ("setting_id", "CAST(p.id AS CHAR)"), ("default_user", "CAST(p.default_user AS CHAR)"))
        joins = ("ads_facebook_auto_created_data a FORCE INDEX(PRIMARY) "
                 "LEFT JOIN `{s}`.ads_template_make_queue pq ON pq.id=a.publish_queue_id "
                 "LEFT JOIN `{s}`.ads_apps_setting p ON p.id=a.product").format(s=self.schema)
        return self._select(expressions, joins, tail)

    @staticmethod
    def _credential_user(identity, row):
        if (any(str(row.get(key)) != value for key, value in identity.items()) or
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
        return user

    def selected_credentials(self, lineages):
        """Batch scan-time routes, returning row ID -> {user_id, error}.

        Each row has exactly one explicit outcome. Invalid/missing routes are
        per-row errors; an incomplete or contradictory SQL result fails the
        whole call. At most 200 primary IDs are joined per query, in one gated
        snapshot. Token availability is checked in batches and no Token is read
        into this method. No result survives this call as an adapter cache.
        """
        identities, result = {}, {}
        for lineage in lineages:
            if not isinstance(lineage, dict):
                raise SqlSourceError("invalid_credential_lineage")
            row_id = str(lineage.get("id") or lineage.get("row_id") or "")
            if not _ID.fullmatch(row_id):
                raise SqlSourceError("invalid_credential_row_id")
            try:
                identity = self._credential_identity(lineage)
            except SqlSourceError as exc:
                identities.pop(row_id, None)
                result[row_id] = {"user_id": "", "error": str(exc)}
                continue
            if row_id in result:
                continue
            if row_id in identities and identities[row_id] != identity:
                identities.pop(row_id)
                result[row_id] = {"user_id": "", "error": "ambiguous_credential_lineage"}
            else:
                identities[row_id] = identity
        if not identities:
            return result
        selected = {}
        with self._session() as session:
            for part in _chunks(list(identities), 200):
                rows = session.query(self._credential_query(
                    "WHERE a.id IN %s ORDER BY a.id LIMIT %d" % (_inside(part), len(part) + 1)))
                found = {}
                for row in rows:
                    row_id = str(row.get("id") or "")
                    if row_id not in part or row_id in found:
                        raise SqlSourceError("invalid_credential_route_result")
                    found[row_id] = row
                for row_id in part:
                    if row_id not in found:
                        result[row_id] = {"user_id": "", "error": "credential_lineage_changed"}
                        continue
                    try:
                        selected[row_id] = self._credential_user(identities[row_id], found[row_id])
                    except SqlSourceError as exc:
                        result[row_id] = {"user_id": "", "error": str(exc)}
            available = set()
            for users in _chunks(sorted(set(selected.values()), key=int), 200):
                directory = " UNION ALL ".join("SELECT %s AS user_id" % _quote(user) for user in users)
                sql = ("SELECT JSON_OBJECT(%s,selected.user_id,%s,EXISTS(SELECT 1 FROM "
                       "`%s`.ads_facebook_info i WHERE i.user_id=selected.user_id "
                       "AND TRIM(i.accessToken)<>'' LIMIT 1)) FROM (%s) selected ORDER BY selected.user_id") % (
                           _quote("user_id"), _quote("available"), self.schema, directory)
                seen = set()
                for row in session.query(sql):
                    user = str(row.get("user_id") or "")
                    value = row.get("available")
                    # MySQL JSON_OBJECT(..., EXISTS(...)) emits JSON booleans
                    # on the live reader. Some compatible servers return 0/1.
                    # Accept only these exact JSON types, never truthy strings.
                    if (set(row) != {"user_id", "available"} or user not in users or user in seen
                            or type(value) not in (bool, int) or value not in (0, 1)):
                        raise SqlSourceError("invalid_credential_availability")
                    seen.add(user)
                    if value == 1:
                        available.add(user)
                if seen != set(users):
                    raise SqlSourceError("credential_availability_incomplete")
            for row_id, user in selected.items():
                result[row_id] = ({"user_id": user, "error": ""} if user in available else
                                  {"user_id": "", "error": "publishing_credential_unavailable"})
        return result

    def selected_credential(self, lineage):
        """Fresh write-time route and non-secret internal user ID, never a cache."""
        identity = self._credential_identity(lineage)
        where = " AND ".join("a.%s=%s" % (key, _quote(value)) for key, value in identity.items())
        with self._session() as session:
            rows = session.query(self._credential_query("WHERE " + where + " LIMIT 2"))
            if len(rows) != 1:
                raise SqlSourceError("credential_lineage_changed")
            user = self._credential_user(identity, rows[0])
            tokens = session.query(self._select((("token", "accessToken"),), "ads_facebook_info",
                                   "WHERE user_id=%s AND TRIM(accessToken)<>'' ORDER BY id DESC LIMIT 1" % _quote(user)))
            if len(tokens) != 1 or not str(tokens[0].get("token") or "").strip():
                raise SqlSourceError("publishing_credential_unavailable")
            return user
