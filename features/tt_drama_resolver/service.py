"""Exact, cached resolver for the public TikTok DramaWave bridge."""

from collections import OrderedDict
from datetime import datetime
import logging
import queue
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit


CONTENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,32}$")
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
DEFAULT_COVER_HOSTS = frozenset(
    (
        "ads-cdn.yingliang.tech",
        "cdn.usrgrow.com",
        "static.mydramawave.com",
        "static-v1.mydramawave.com",
        "static-v2.mydramawave.com",
    )
)


class InvalidContentIdError(ValueError):
    """Raised when a resolver key is not an exact supported Content ID."""


class ResolverUnavailableError(RuntimeError):
    """Raised when the read-only source cannot safely answer a lookup."""


class ResolveOutcome:
    def __init__(self, found, item, cache_state):
        self.found = bool(found)
        self.item = dict(item or {}) if found else None
        self.cache_state = str(cache_state or "MISS")


class _CacheEntry:
    def __init__(self, found, item, expires_at, stale_until):
        self.found = bool(found)
        self.item = dict(item or {}) if found else None
        self.expires_at = float(expires_at)
        self.stale_until = float(stale_until)


class _Flight:
    def __init__(self):
        self.event = threading.Event()
        self.outcome = None
        self.error = None


def is_valid_content_id(value):
    text = str(value or "")
    return bool(CONTENT_ID_PATTERN.fullmatch(text))


def normalize_content_id(value):
    text = str(value or "")
    if text != text.strip() or not is_valid_content_id(text):
        raise InvalidContentIdError("invalid DramaWave content_id")
    return text


def compact_text(value, limit):
    text = " ".join(str(value or "").split())
    return text[: max(0, int(limit))]


def normalize_timestamp(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return compact_text(value, 40)


def normalize_cover_hosts(value):
    if value is None:
        return DEFAULT_COVER_HOSTS
    if isinstance(value, str):
        values = value.split(",")
    else:
        values = value
    hosts = {
        str(item or "").strip().lower().rstrip(".")
        for item in values
        if str(item or "").strip()
    }
    return frozenset(hosts or DEFAULT_COVER_HOSTS)


def sanitize_cover_url(value, allowed_hosts=None):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    hosts = normalize_cover_hosts(allowed_hosts)
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or hostname not in hosts
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        return ""
    if hostname == "static.mydramawave.com":
        hostname = "static-v1.mydramawave.com"
    netloc = hostname
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


class TokenBucketRateLimiter:
    """Bounded per-key token bucket used only by the public resolver route."""

    def __init__(self, limit_per_minute=30, max_keys=10000, clock=None):
        self.limit = max(0, int(limit_per_minute))
        self.max_keys = max(100, int(max_keys))
        self.clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._buckets = OrderedDict()

    def allow(self, key):
        if self.limit <= 0:
            return True
        bucket_key = compact_text(key, 128) or "unknown"
        now = float(self.clock())
        refill_per_second = float(self.limit) / 60.0
        with self._lock:
            tokens, updated_at = self._buckets.pop(
                bucket_key, (float(self.limit), now)
            )
            tokens = min(
                float(self.limit),
                float(tokens) + max(0.0, now - float(updated_at)) * refill_per_second,
            )
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[bucket_key] = (tokens, now)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            return allowed


class TTDramaResolver:
    """Thread-safe TTL/LRU cache with per-Content-ID single-flight."""

    def __init__(
        self,
        loader,
        positive_ttl_seconds=3600,
        negative_ttl_seconds=300,
        stale_ttl_seconds=21600,
        max_entries=10000,
        wait_timeout_seconds=8,
        clock=None,
    ):
        if not callable(loader):
            raise ValueError("loader is required")
        self.loader = loader
        self.positive_ttl = max(1, int(positive_ttl_seconds))
        self.negative_ttl = max(1, int(negative_ttl_seconds))
        self.stale_ttl = max(self.positive_ttl, int(stale_ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.wait_timeout = max(1.0, float(wait_timeout_seconds))
        self.clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._flights = {}

    def clear(self):
        with self._lock:
            self._cache.clear()

    def _fresh_outcome_locked(self, content_id, now):
        entry = self._cache.get(content_id)
        if entry is None:
            return None
        if entry.expires_at > now:
            self._cache.move_to_end(content_id)
            state = "HIT" if entry.found else "NEGATIVE_HIT"
            return ResolveOutcome(entry.found, entry.item, state)
        if not entry.found or entry.stale_until <= now:
            self._cache.pop(content_id, None)
        return None

    def _stale_outcome_locked(self, content_id, now):
        entry = self._cache.get(content_id)
        if (
            entry is not None
            and entry.found
            and entry.expires_at <= now
            and entry.stale_until > now
        ):
            self._cache.move_to_end(content_id)
            return ResolveOutcome(True, entry.item, "STALE")
        return None

    def _store_locked(self, content_id, item, now):
        found = bool(item)
        ttl = self.positive_ttl if found else self.negative_ttl
        expires_at = now + ttl
        stale_until = (
            now + self.stale_ttl
            if found
            else expires_at
        )
        self._cache.pop(content_id, None)
        self._cache[content_id] = _CacheEntry(
            found, item, expires_at, stale_until
        )
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)

    @staticmethod
    def _follower_outcome(outcome):
        if outcome.cache_state == "STALE":
            state = "STALE"
        else:
            state = "HIT" if outcome.found else "NEGATIVE_HIT"
        return ResolveOutcome(outcome.found, outcome.item, state)

    def resolve(self, value):
        content_id = normalize_content_id(value)
        now = float(self.clock())
        with self._lock:
            cached = self._fresh_outcome_locked(content_id, now)
            if cached is not None:
                return cached
            stale = self._stale_outcome_locked(content_id, now)
            flight = self._flights.get(content_id)
            if flight is None:
                flight = _Flight()
                self._flights[content_id] = flight
                leader = True
            else:
                leader = False

        if not leader:
            if not flight.event.wait(self.wait_timeout):
                if stale is not None:
                    return stale
                raise ResolverUnavailableError("resolver lookup timed out")
            if flight.outcome is not None:
                return self._follower_outcome(flight.outcome)
            if stale is not None:
                return stale
            raise ResolverUnavailableError("resolver lookup failed")

        outcome = None
        error = None
        try:
            item = self.loader(content_id)
            if item is not None and not isinstance(item, dict):
                raise ResolverUnavailableError("resolver returned invalid data")
            now = float(self.clock())
            with self._lock:
                self._store_locked(content_id, item, now)
            outcome = ResolveOutcome(bool(item), item, "MISS")
            return outcome
        except Exception as exc:
            error = exc
            now = float(self.clock())
            with self._lock:
                stale = self._stale_outcome_locked(content_id, now)
            if stale is not None:
                outcome = stale
                return stale
            if isinstance(exc, ResolverUnavailableError):
                raise
            logging.warning(
                "TT drama resolver source lookup failed for %s: %s",
                content_id,
                type(exc).__name__,
            )
            raise ResolverUnavailableError("resolver source is unavailable") from None
        finally:
            with self._lock:
                current = self._flights.pop(content_id, None)
                if current is not None:
                    current.outcome = outcome
                    current.error = error
                    current.event.set()


class MySQLDramaRepository:
    """Small persistent pool for exact reads from the verified read-only replica."""

    def __init__(
        self,
        host,
        port,
        user,
        password,
        database,
        table,
        app_id=1479,
        connect_timeout_seconds=2,
        read_timeout_seconds=3,
        max_concurrency=4,
        allowed_cover_hosts=None,
    ):
        self.host = str(host or "").strip()
        try:
            self.port = int(port)
        except (TypeError, ValueError):
            self.port = 0
        self.user = str(user or "").strip()
        self.password = "" if password is None else str(password)
        self.database = str(database or "").strip()
        self.table = str(table or "").strip()
        self.app_id = str(app_id or "").strip()
        self.connect_timeout = max(1, int(connect_timeout_seconds))
        self.read_timeout = max(1, int(read_timeout_seconds))
        self.max_concurrency = max(1, min(int(max_concurrency), 16))
        self.allowed_cover_hosts = normalize_cover_hosts(allowed_cover_hosts)
        self._gate = threading.BoundedSemaphore(self.max_concurrency)
        self._pool = queue.LifoQueue(maxsize=self.max_concurrency)
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(self.database):
            raise ValueError("invalid drama database identifier")
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(self.table):
            raise ValueError("invalid drama table identifier")
        self._sql = self._build_sql()

    @property
    def configured(self):
        return bool(
            self.host
            and self.port > 0
            and self.user
            and self.password
            and self.app_id
        )

    def _build_sql(self):
        qualified = "`%s`.`%s`" % (self.database, self.table)
        return """
            SELECT /*+ MAX_EXECUTION_TIME(2000) */
                r.content_id,
                r.name AS title,
                r.`desc` AS description,
                r.cover AS cover_url,
                r.country,
                r.language,
                selected.episode_count,
                selected.latest_update AS source_updated_at
              FROM {qualified} r FORCE INDEX (content_id)
              JOIN (
                    SELECT
                        app,
                        country,
                        language,
                        COUNT(*) AS episode_count,
                        MAX(updated_at) AS latest_update
                      FROM {qualified} FORCE INDEX (content_id)
                     WHERE content_id = %s
                       AND BINARY content_id = BINARY %s
                       AND app_id = %s
                       AND type = 2
                       AND sub_number > 0
                       AND sub_url <> ''
                     GROUP BY app, country, language
                     ORDER BY episode_count DESC, latest_update DESC
                     LIMIT 1
              ) selected
                ON selected.app = r.app
               AND selected.country = r.country
               AND selected.language = r.language
             WHERE r.content_id = %s
               AND BINARY r.content_id = BINARY %s
               AND r.app_id = %s
               AND r.type = 2
               AND r.sub_number > 0
               AND r.sub_url <> ''
             ORDER BY r.sub_number ASC, r.updated_at DESC
             LIMIT 1
        """.format(qualified=qualified)

    def _create_connection(self):
        if not self.configured:
            raise ResolverUnavailableError("resolver database is not configured")
        try:
            import pymysql
        except ImportError:
            raise ResolverUnavailableError("PyMySQL is unavailable") from None
        connection = None
        try:
            connection = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
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
                cursor.close()
            if not row or int(row.get("read_only") or 0) != 1:
                raise ResolverUnavailableError(
                    "resolver database endpoint is not read-only"
                )
            return connection
        except ResolverUnavailableError:
            self._close_quietly(connection)
            raise
        except Exception as exc:
            self._close_quietly(connection)
            logging.warning(
                "TT drama resolver read-only connection failed: %s",
                type(exc).__name__,
            )
            raise ResolverUnavailableError(
                "resolver database connection failed"
            ) from None

    @staticmethod
    def _close_quietly(connection):
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            pass

    def _release_connection(self, connection):
        if connection is None:
            return
        try:
            self._pool.put_nowait(connection)
        except queue.Full:
            self._close_quietly(connection)

    def warmup(self):
        """Create one verified read-only connection before public traffic arrives."""
        if not self.configured or not self._pool.empty():
            return False
        if not self._gate.acquire(timeout=self.connect_timeout + self.read_timeout):
            return False
        connection = None
        try:
            if not self._pool.empty():
                return False
            connection = self._create_connection()
            self._release_connection(connection)
            connection = None
            return True
        finally:
            self._close_quietly(connection)
            self._gate.release()

    def _query_on_connection(self, connection, content_id):
        cursor = connection.cursor()
        try:
            cursor.execute(
                self._sql,
                (
                    content_id,
                    content_id,
                    self.app_id,
                    content_id,
                    content_id,
                    self.app_id,
                ),
            )
            return cursor.fetchone()
        finally:
            cursor.close()

    def lookup(self, content_id):
        normalized = normalize_content_id(content_id)
        gate_timeout = self.connect_timeout + self.read_timeout + 1
        if not self._gate.acquire(timeout=gate_timeout):
            raise ResolverUnavailableError("resolver database is busy")
        connection = None
        try:
            reused = True
            try:
                connection = self._pool.get_nowait()
            except queue.Empty:
                reused = False
                connection = self._create_connection()
            try:
                row = self._query_on_connection(connection, normalized)
            except Exception:
                self._close_quietly(connection)
                connection = None
                if not reused:
                    raise
                connection = self._create_connection()
                try:
                    row = self._query_on_connection(connection, normalized)
                except Exception:
                    self._close_quietly(connection)
                    connection = None
                    raise
            if not row:
                return None
            if str(row.get("content_id") or "") != normalized:
                logging.warning(
                    "TT drama resolver rejected non-exact source key for %s",
                    normalized,
                )
                return None
            return {
                "content_id": normalized,
                "title": compact_text(row.get("title"), 240) or normalized,
                "description": compact_text(row.get("description"), 600),
                "cover_url": sanitize_cover_url(
                    row.get("cover_url"), self.allowed_cover_hosts
                ),
                "country": compact_text(row.get("country"), 16),
                "language": compact_text(row.get("language"), 16),
                "episode_count": max(0, int(row.get("episode_count") or 0)),
                "source_updated_at": normalize_timestamp(
                    row.get("source_updated_at")
                ),
            }
        except ResolverUnavailableError:
            raise
        except Exception as exc:
            logging.warning(
                "TT drama resolver query failed for %s: %s",
                normalized,
                type(exc).__name__,
            )
            raise ResolverUnavailableError("resolver database query failed") from None
        finally:
            self._release_connection(connection)
            self._gate.release()
