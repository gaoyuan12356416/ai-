"""Discover recently active DramaWave W2A content IDs from read-only spend data."""

from datetime import date, datetime, timedelta, timezone
import re

from features.tt_drama_resolver import normalize_content_id


SHANGHAI_TZ = timezone(timedelta(hours=8))
VERIFIED_READONLY_HOST = "101.32.56.53"
VERIFIED_READONLY_PORT = 63350
VERIFIED_DATABASE = "kunlunads_dev"
VERIFIED_INSIGHT_TABLE = "ads_custom_source_insight"
VERIFIED_INSIGHT_INDEX = "as"
VERIFIED_PRODUCT = "Dramawave"
VERIFIED_SOURCE_APP_ID = "[w2a]drama-double"
VERIFIED_DATA_SOURCE = 6
RECENT_DAY_COUNT = 3
MAX_CANDIDATES = 5000
CANDIDATE_QUERY_LIMIT = MAX_CANDIDATES + 1
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
CONTENT_ID_SQL_PATTERN = r"^[A-Za-z0-9_-]{10,32}$"


class PrewarmSourceError(RuntimeError):
    """The verified read-only source could not safely return candidates."""

    error_code = "prewarm_source_error"


class CandidateOverflowError(PrewarmSourceError):
    """The active candidate scope exceeded its audited safety bound."""

    error_code = "candidate_limit_exceeded"

    def __init__(self, count_lower_bound=CANDIDATE_QUERY_LIMIT):
        self.count_lower_bound = max(
            CANDIDATE_QUERY_LIMIT,
            int(count_lower_bound),
        )
        super().__init__(
            "recent active drama candidates exceeded %d"
            % MAX_CANDIDATES
        )


class PrewarmCandidateConfig:
    """Validated, intentionally narrow W2A spend-candidate scope."""

    def __init__(
        self,
        *,
        database=VERIFIED_DATABASE,
        insight_table="ads_custom_source_insight",
        insight_index="as",
        product=VERIFIED_PRODUCT,
        source_app_id=VERIFIED_SOURCE_APP_ID,
        data_source=VERIFIED_DATA_SOURCE,
    ):
        for label, value in (
            ("database", database),
            ("insight_table", insight_table),
            ("insight_index", insight_index),
        ):
            if not SAFE_IDENTIFIER_PATTERN.fullmatch(str(value or "")):
                raise ValueError("%s is invalid" % label)
        self.database = str(database)
        self.insight_table = str(insight_table)
        self.insight_index = str(insight_index)
        self.product = str(product or "")
        self.source_app_id = str(source_app_id or "")
        self.data_source = int(data_source)
        if self.database != VERIFIED_DATABASE:
            raise ValueError("prewarm database scope must be kunlunads_dev")
        if self.insight_table != VERIFIED_INSIGHT_TABLE:
            raise ValueError(
                "prewarm insight table scope must remain fixed"
            )
        if self.insight_index != VERIFIED_INSIGHT_INDEX:
            raise ValueError(
                "prewarm insight index scope must remain fixed"
            )
        if self.product != VERIFIED_PRODUCT:
            raise ValueError("prewarm product scope must be Dramawave")
        if self.source_app_id != VERIFIED_SOURCE_APP_ID:
            raise ValueError(
                "prewarm app scope must be [w2a]drama-double"
            )
        if self.data_source != VERIFIED_DATA_SOURCE:
            raise ValueError("prewarm data_source scope must be 6")


def shanghai_now(now=None):
    if now is None:
        return datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def recent_shanghai_date_window(now=None):
    """Return the inclusive three-natural-day Shanghai date window."""
    end_date = shanghai_now(now).date()
    start_date = end_date - timedelta(days=RECENT_DAY_COUNT - 1)
    return start_date.isoformat(), end_date.isoformat()


def _normalize_date_window(start_date, end_date):
    try:
        start = date.fromisoformat(str(start_date or ""))
        end = date.fromisoformat(str(end_date or ""))
    except (TypeError, ValueError):
        raise PrewarmSourceError(
            "prewarm date window must use YYYY-MM-DD"
        ) from None
    if (end - start).days != RECENT_DAY_COUNT - 1:
        raise PrewarmSourceError(
            "prewarm date window must cover exactly three natural days"
        )
    return start.isoformat(), end.isoformat()


def _qualified(database, table):
    return "`%s`.`%s`" % (database, table)


def _close_quietly(value):
    if value is None:
        return
    try:
        value.close()
    except Exception:
        pass


class ActiveDramaCandidateRepository:
    """Run one bounded aggregate query against the verified read-only replica."""

    def __init__(
        self,
        *,
        host,
        port,
        user,
        password,
        config=None,
        connect_timeout=5,
        read_timeout=30,
        connection_factory=None,
    ):
        self.host = str(host or "").strip()
        try:
            self.port = int(port)
        except (TypeError, ValueError):
            self.port = 0
        self.user = str(user or "").strip()
        self.password = "" if password is None else str(password)
        self.config = config or PrewarmCandidateConfig()
        if not isinstance(self.config, PrewarmCandidateConfig):
            raise ValueError("config must be PrewarmCandidateConfig")
        self.connect_timeout = max(1, min(int(connect_timeout), 10))
        self.read_timeout = max(5, min(int(read_timeout), 60))
        self.connection_factory = connection_factory

    @property
    def configured(self):
        return bool(
            self.connection_factory
            or (
                self.host
                and self.port > 0
                and self.user
                and self.password
            )
        )

    def _connect(self):
        if not self.configured:
            raise PrewarmSourceError(
                "read-only prewarm database is not configured"
            )
        if self.connection_factory is None:
            if self.host != VERIFIED_READONLY_HOST:
                raise PrewarmSourceError(
                    "prewarm must use the verified read-only host"
                )
            if self.port != VERIFIED_READONLY_PORT:
                raise PrewarmSourceError(
                    "prewarm must use the verified read-only port 63350"
                )
            if self.config.database != VERIFIED_DATABASE:
                raise PrewarmSourceError(
                    "prewarm must use the verified database"
                )

        connection = None
        try:
            if self.connection_factory is not None:
                connection = self.connection_factory()
            else:
                try:
                    import pymysql
                except ImportError:
                    raise PrewarmSourceError("PyMySQL is unavailable") from None
                connection = pymysql.connect(
                    host=self.host,
                    port=self.port,
                    user=self.user,
                    password=self.password,
                    database=self.config.database,
                    charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=True,
                    connect_timeout=self.connect_timeout,
                    read_timeout=self.read_timeout,
                    write_timeout=self.read_timeout,
                )

            cursor = connection.cursor()
            try:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute("SELECT @@read_only AS read_only")
                row = cursor.fetchone()
            finally:
                _close_quietly(cursor)
            if not row or int(row.get("read_only") or 0) != 1:
                raise PrewarmSourceError(
                    "prewarm source endpoint is not read-only"
                )
            return connection
        except PrewarmSourceError:
            _close_quietly(connection)
            raise
        except Exception as exc:
            _close_quietly(connection)
            raise PrewarmSourceError(
                "read-only prewarm connection failed: %s"
                % type(exc).__name__
            ) from None

    def fetch(self, start_date, end_date):
        start_date, end_date = _normalize_date_window(
            start_date,
            end_date,
        )
        connection = self._connect()
        try:
            cfg = self.config
            sql = """
                SELECT /*+ MAX_EXECUTION_TIME(30000) */
                    MIN(i.data_source_id) AS content_id,
                    SUM(COALESCE(i.spend, 0)) AS spend_n
                  FROM {insight} i FORCE INDEX (`{index_name}`)
                 WHERE i.app_id = %s
                   AND i.dt BETWEEN %s AND %s
                   AND i.product = %s
                   AND i.data_source = %s
                   AND i.data_source_id <> ''
                   AND BINARY i.data_source_id REGEXP %s
                 GROUP BY BINARY i.data_source_id
                HAVING SUM(COALESCE(i.spend, 0)) > 0
                 ORDER BY spend_n DESC, MIN(BINARY i.data_source_id) ASC
                 LIMIT %s
            """.format(
                insight=_qualified(
                    cfg.database,
                    cfg.insight_table,
                ),
                index_name=cfg.insight_index,
            )
            cursor = connection.cursor()
            try:
                cursor.execute(
                    sql,
                    (
                        cfg.source_app_id,
                        start_date,
                        end_date,
                        cfg.product,
                        cfg.data_source,
                        CONTENT_ID_SQL_PATTERN,
                        CANDIDATE_QUERY_LIMIT,
                    ),
                )
                rows = [dict(row) for row in cursor.fetchall()]
            except Exception as exc:
                raise PrewarmSourceError(
                    "prewarm candidate query failed: %s"
                    % type(exc).__name__
                ) from None
            finally:
                _close_quietly(cursor)
        finally:
            _close_quietly(connection)

        if len(rows) > MAX_CANDIDATES:
            raise CandidateOverflowError(len(rows))
        content_ids = []
        seen = set()
        for row in rows:
            candidate = str(row.get("content_id") or "")
            try:
                content_id = normalize_content_id(candidate)
            except ValueError:
                raise PrewarmSourceError(
                    "prewarm source returned an invalid content_id"
                ) from None
            if content_id in seen:
                raise PrewarmSourceError(
                    "prewarm source returned a duplicate content_id"
                )
            seen.add(content_id)
            content_ids.append(content_id)
        return content_ids
