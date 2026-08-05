"""Build a public-safe, last-known-good cache of yesterday's top W2A dramas."""

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile

from features.tt_drama_resources import (
    ResourceError,
    normalize_content_id,
    sanitize_cover_url,
)
from features.tt_drama_resources.models import compact_text


SHANGHAI_TZ = timezone(timedelta(hours=8))
DATA_DISK_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
CONTENT_ID_SQL_PATTERN = r"^[A-Za-z0-9_-]{10,32}$"
VERIFIED_READONLY_HOST = "101.32.56.53"
VERIFIED_READONLY_PORT = 63350
VERIFIED_DATABASE = "kunlunads_dev"
VERIFIED_INSIGHT_TABLE = "ads_custom_source_insight"
VERIFIED_INSIGHT_INDEX = "as"
MAX_CANDIDATE_LIMIT = 20
SNAPSHOT_VERSION = 1
MAX_PUBLIC_SNAPSHOT_BYTES = 32 * 1024
LANGUAGE_SNAPSHOT_VERSION = 2
DEFAULT_FEATURED_LANGUAGE = "en"
MAX_LANGUAGE_CANDIDATES = 5000
MAX_LANGUAGE_BUCKETS = 32
MAX_LANGUAGE_SNAPSHOT_BYTES = 256 * 1024
# Keep this compatible with the verified MySQL 5.7 REGEXP engine as well as
# Python; MySQL 5.7 does not support non-capturing groups.
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")


class FeaturedRefreshError(RuntimeError):
    """The read-only source could not safely produce a complete ranking."""


class FeaturedCacheError(RuntimeError):
    """The local last-known-good cache could not be safely written."""


class FeaturedConfig:
    """Validated settings for one offline refresh."""

    def __init__(
        self,
        *,
        database="kunlunads_dev",
        insight_table="ads_custom_source_insight",
        insight_index="as",
        product="Dramawave",
        source_app_id="[w2a]drama-double",
        data_source=6,
        candidate_limit=20,
        item_limit=5,
        allowed_cover_hosts=None,
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
        self.product = compact_text(product, 100)
        self.source_app_id = compact_text(source_app_id, 100)
        self.data_source = int(data_source)
        if self.insight_table != VERIFIED_INSIGHT_TABLE:
            raise ValueError(
                "featured insight table scope cannot be expanded"
            )
        if self.insight_index != VERIFIED_INSIGHT_INDEX:
            raise ValueError(
                "featured insight index scope cannot be expanded"
            )
        if (
            self.product != "Dramawave"
            or self.source_app_id != "[w2a]drama-double"
            or self.data_source != 6
        ):
            raise ValueError("featured production scope cannot be expanded")
        self.candidate_limit = max(
            5,
            min(int(candidate_limit), MAX_CANDIDATE_LIMIT),
        )
        self.item_limit = max(1, min(int(item_limit), 10))
        if self.candidate_limit < self.item_limit:
            raise ValueError("candidate_limit must be at least item_limit")
        if not self.product or not self.source_app_id:
            raise ValueError("featured source scope is incomplete")
        self.allowed_cover_hosts = allowed_cover_hosts


def shanghai_now(now=None):
    """Return an aware fixed-offset Shanghai timestamp without zoneinfo."""
    if now is None:
        return datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def normalize_source_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        parsed = datetime.strptime(str(value or ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise FeaturedRefreshError("source_date must be YYYY-MM-DD") from None
    return parsed.isoformat()


def previous_source_date(now=None):
    return (shanghai_now(now).date() - timedelta(days=1)).isoformat()


def _qualified(database, table):
    return "`%s`.`%s`" % (database, table)


def _close_quietly(value):
    if value is None:
        return
    try:
        value.close()
    except Exception:
        pass


def _positive_integer(value):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


def normalize_featured_language(value):
    """Return one bounded language key used by ranking bundles."""
    normalized = str(value or "").strip().casefold().replace("_", "-")
    if not LANGUAGE_PATTERN.fullmatch(normalized):
        raise FeaturedRefreshError("featured drama language is invalid")
    return normalized


class FeaturedDramaRepository:
    """Bounded spend ranking query against a verified read-only replica."""

    def __init__(
        self,
        *,
        host,
        port,
        user,
        password,
        config,
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
        if not isinstance(config, FeaturedConfig):
            raise ValueError("config is required")
        self.config = config
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
            raise FeaturedRefreshError("read-only database is not configured")
        if self.connection_factory is None:
            if self.host != VERIFIED_READONLY_HOST:
                raise FeaturedRefreshError(
                    "featured refresh must use the verified read-only host"
                )
            if self.port != VERIFIED_READONLY_PORT:
                raise FeaturedRefreshError(
                    "featured refresh must use the verified read-only port 63350"
                )
            if self.config.database != VERIFIED_DATABASE:
                raise FeaturedRefreshError(
                    "featured refresh must use the verified database"
                )
        connection = None
        try:
            if self.connection_factory is not None:
                connection = self.connection_factory()
            else:
                try:
                    import pymysql
                except ImportError:
                    raise FeaturedRefreshError("PyMySQL is unavailable") from None
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
                raise FeaturedRefreshError(
                    "featured source endpoint is not read-only"
                )
            return connection
        except FeaturedRefreshError:
            _close_quietly(connection)
            raise
        except Exception as exc:
            _close_quietly(connection)
            raise FeaturedRefreshError(
                "read-only database connection failed: %s" % type(exc).__name__
            ) from None

    @staticmethod
    def _rows(connection, sql, params):
        if not re.match(r"(?is)^\s*SELECT\b", str(sql or "")):
            raise FeaturedRefreshError("non-read-only statement rejected")
        cursor = connection.cursor()
        try:
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except FeaturedRefreshError:
            raise
        except Exception as exc:
            raise FeaturedRefreshError(
                "featured source query failed: %s" % type(exc).__name__
            ) from None
        finally:
            _close_quietly(cursor)

    def _spend_rows(self, connection, source_date):
        cfg = self.config
        sql = """
            SELECT /*+ MAX_EXECUTION_TIME(10000) */
                MIN(i.data_source_id) AS content_id,
                SUM(COALESCE(i.spend, 0)) AS spend_n
              FROM {insight} i FORCE INDEX (`{index_name}`)
             WHERE i.app_id = %s
               AND i.dt = %s
               AND i.product = %s
               AND i.data_source = %s
               AND i.data_source_id <> ''
               AND BINARY i.data_source_id REGEXP %s
             GROUP BY BINARY i.data_source_id
            HAVING SUM(COALESCE(i.spend, 0)) > 0
             ORDER BY spend_n DESC, MIN(BINARY i.data_source_id) ASC
             LIMIT %s
        """.format(
            insight=_qualified(cfg.database, cfg.insight_table),
            index_name=cfg.insight_index,
        )
        rows = self._rows(
            connection,
            sql,
            (
                cfg.source_app_id,
                source_date,
                cfg.product,
                cfg.data_source,
                CONTENT_ID_SQL_PATTERN,
                cfg.candidate_limit,
            ),
        )
        result = []
        seen = set()
        for row in rows:
            candidate = str(row.get("content_id") or "")
            try:
                content_id = normalize_content_id(candidate)
            except ValueError:
                continue
            if content_id in seen:
                continue
            seen.add(content_id)
            result.append(
                {
                    "content_id": content_id,
                    "spend_n": row.get("spend_n"),
                }
            )
        if len(result) < cfg.item_limit:
            raise FeaturedRefreshError(
                "fewer than %d ranked content IDs were found" % cfg.item_limit
            )
        return result

    def _language_spend_rows(self, connection, source_date):
        """Fetch one bounded, stable ranking source for every drama language."""
        cfg = self.config
        sql = """
            SELECT /*+ MAX_EXECUTION_TIME(10000) */
                MIN(i.data_source_id) AS content_id,
                MIN(
                    LOWER(REPLACE(TRIM(i.drama_language), '_', '-'))
                ) AS drama_language,
                SUM(COALESCE(i.spend, 0)) AS spend_n,
                COUNT(
                    DISTINCT BINARY LOWER(
                        REPLACE(TRIM(i.drama_language), '_', '-')
                    )
                ) AS language_count
              FROM {insight} i FORCE INDEX (`{index_name}`)
             WHERE i.app_id = %s
               AND i.dt = %s
               AND i.product = %s
               AND i.data_source = %s
               AND i.data_source_id <> ''
               AND BINARY i.data_source_id REGEXP %s
             GROUP BY BINARY i.data_source_id
            HAVING SUM(COALESCE(i.spend, 0)) > 0
               AND language_count = 1
               AND MIN(
                    LOWER(REPLACE(TRIM(i.drama_language), '_', '-'))
               ) REGEXP %s
             ORDER BY drama_language ASC,
                      spend_n DESC,
                      MIN(BINARY i.data_source_id) ASC
             LIMIT %s
        """.format(
            insight=_qualified(cfg.database, cfg.insight_table),
            index_name=cfg.insight_index,
        )
        rows = self._rows(
            connection,
            sql,
            (
                cfg.source_app_id,
                source_date,
                cfg.product,
                cfg.data_source,
                CONTENT_ID_SQL_PATTERN,
                LANGUAGE_PATTERN.pattern,
                MAX_LANGUAGE_CANDIDATES + 1,
            ),
        )
        if len(rows) > MAX_LANGUAGE_CANDIDATES:
            raise FeaturedRefreshError(
                "language ranking exceeds %d candidates"
                % MAX_LANGUAGE_CANDIDATES
            )

        result = {}
        content_languages = {}
        for row in rows:
            try:
                content_id = normalize_content_id(row.get("content_id"))
                language = normalize_featured_language(
                    row.get("drama_language")
                )
                language_count = int(row.get("language_count") or 0)
            except (TypeError, ValueError, FeaturedRefreshError):
                raise FeaturedRefreshError(
                    "language ranking returned an invalid row"
                ) from None
            if language_count != 1:
                raise FeaturedRefreshError(
                    "language ranking returned an ambiguous drama"
                )
            previous = content_languages.setdefault(content_id, language)
            if previous != language:
                raise FeaturedRefreshError(
                    "one drama appeared in multiple language rankings"
                )
            bucket = result.setdefault(language, [])
            if len(bucket) >= cfg.candidate_limit:
                continue
            bucket.append(
                {
                    "content_id": content_id,
                    "drama_language": language,
                    "spend_n": row.get("spend_n"),
                }
            )
        if len(result) > MAX_LANGUAGE_BUCKETS:
            raise FeaturedRefreshError(
                "language ranking exceeds %d buckets" % MAX_LANGUAGE_BUCKETS
            )
        default_rows = result.get(DEFAULT_FEATURED_LANGUAGE) or []
        if len(default_rows) < cfg.item_limit:
            raise FeaturedRefreshError(
                "fewer than %d ranked English content IDs were found"
                % cfg.item_limit
            )
        return {
            language: result[language]
            for language in sorted(result)
        }

    def fetch_ranked(self, source_date):
        source_date = normalize_source_date(source_date)
        connection = self._connect()
        try:
            return self._spend_rows(connection, source_date)
        finally:
            _close_quietly(connection)

    def fetch_ranked_by_language(self, source_date):
        source_date = normalize_source_date(source_date)
        connection = self._connect()
        try:
            return self._language_spend_rows(connection, source_date)
        finally:
            _close_quietly(connection)

    def fetch(self, source_date):
        """Compatibility alias for callers that only need ranked IDs."""
        return self.fetch_ranked(source_date)


def resolve_ranked_resources(
    spend_rows,
    resource_service,
    *,
    item_limit=5,
    allowed_cover_hosts=None,
    require_complete=True,
):
    """Resolve ranked IDs in order, skipping only explicit not-found results."""
    if resource_service is None or not callable(
        getattr(resource_service, "resolve", None)
    ):
        raise FeaturedRefreshError("W2A resource service is unavailable")
    limit = max(1, min(int(item_limit), 10))
    selected = []
    seen = set()
    for ranked in spend_rows:
        candidate = str(dict(ranked or {}).get("content_id") or "")
        try:
            content_id = normalize_content_id(candidate)
        except ValueError:
            continue
        if content_id in seen:
            continue
        seen.add(content_id)
        try:
            outcome = resource_service.resolve(
                content_id,
                allow_stale=True,
            )
        except ResourceError as exc:
            raise FeaturedRefreshError(
                "W2A resource resolution failed: %s" % type(exc).__name__
            ) from None
        if outcome is None or not hasattr(outcome, "found"):
            raise FeaturedRefreshError(
                "W2A resource returned an invalid outcome"
            )
        if not bool(outcome.found):
            continue
        item = dict(getattr(outcome, "item", None) or {})
        if str(item.get("content_id") or "") != content_id:
            raise FeaturedRefreshError(
                "W2A resource returned a mismatched content ID"
            )
        title = compact_text(item.get("title"), 240)
        cover_url = sanitize_cover_url(
            item.get("cover_url"),
            allowed_cover_hosts,
        )
        if not title or not cover_url:
            raise FeaturedRefreshError(
                "W2A resource omitted required featured fields"
            )
        selected.append(
            {
                "content_id": content_id,
                "title": title,
                "cover_url": cover_url,
                "language": compact_text(item.get("language"), 16),
                "episode_count": max(
                    0,
                    _positive_integer(item.get("episode_count")),
                ),
            }
        )
        if len(selected) >= limit:
            break
    if require_complete and len(selected) != limit:
        raise FeaturedRefreshError(
            "only %d of %d required featured dramas were valid"
            % (len(selected), limit)
        )
    return selected


def resolve_ranked_resources_by_language(
    ranked_by_language,
    resource_service,
    *,
    item_limit=5,
    allowed_cover_hosts=None,
    default_language=DEFAULT_FEATURED_LANGUAGE,
):
    """Resolve complete language buckets, requiring the English fallback."""
    if not isinstance(ranked_by_language, dict):
        raise FeaturedRefreshError("language rankings are invalid")
    default_key = normalize_featured_language(default_language)
    if len(ranked_by_language) > MAX_LANGUAGE_BUCKETS:
        raise FeaturedRefreshError(
            "language ranking exceeds %d buckets" % MAX_LANGUAGE_BUCKETS
        )
    limit = max(1, min(int(item_limit), 10))
    resolved = {}
    content_languages = {}
    for raw_language in sorted(ranked_by_language):
        language = normalize_featured_language(raw_language)
        if language != raw_language:
            raise FeaturedRefreshError("language ranking key is not canonical")
        rows = ranked_by_language.get(raw_language)
        if not isinstance(rows, list):
            raise FeaturedRefreshError("language ranking bucket is invalid")
        for ranked in rows:
            row = dict(ranked or {})
            row_language = normalize_featured_language(
                row.get("drama_language", language)
            )
            if row_language != language:
                raise FeaturedRefreshError(
                    "language ranking row does not match its bucket"
                )
            try:
                content_id = normalize_content_id(row.get("content_id"))
            except ValueError:
                raise FeaturedRefreshError(
                    "language ranking contains an invalid content ID"
                ) from None
            previous = content_languages.setdefault(content_id, language)
            if previous != language:
                raise FeaturedRefreshError(
                    "one drama appeared in multiple language rankings"
                )
        selected = resolve_ranked_resources(
            rows,
            resource_service,
            item_limit=limit,
            allowed_cover_hosts=allowed_cover_hosts,
            require_complete=False,
        )
        if len(selected) != limit:
            if language == default_key:
                raise FeaturedRefreshError(
                    "only %d of %d required English dramas were valid"
                    % (len(selected), limit)
                )
            continue
        normalized_items = []
        for source_item in selected:
            item = dict(source_item)
            item["language"] = language
            normalized_items.append(item)
        resolved[language] = normalized_items
    if default_key not in resolved:
        raise FeaturedRefreshError("English featured ranking is unavailable")
    return resolved


def _deterministic_public_order(items, source_date):
    def key(item):
        material = ("%s:%s" % (source_date, item["content_id"])).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    return sorted(items, key=key)


def build_snapshot(
    *,
    source_date,
    generated_at,
    spend_rows,
    resource_items,
    item_limit=5,
    allowed_cover_hosts=None,
):
    source_date = normalize_source_date(source_date)
    generated = shanghai_now(generated_at)
    limit = max(1, min(int(item_limit), 10))
    ranked_ids = [
        str(dict(row or {}).get("content_id") or "")
        for row in spend_rows
    ]
    ranked_set = {content_id for content_id in ranked_ids if content_id}
    resources_by_id = {}
    for source_item in resource_items:
        item = dict(source_item or {})
        content_id = str(item.get("content_id") or "")
        if content_id in ranked_set and content_id not in resources_by_id:
            resources_by_id[content_id] = item
    selected = []
    seen = set()
    for content_id in ranked_ids:
        if content_id in seen:
            continue
        item = resources_by_id.get(content_id)
        if item is None:
            continue
        title = compact_text(item.get("title"), 240)
        cover_url = sanitize_cover_url(
            item.get("cover_url"),
            allowed_cover_hosts,
        )
        if not title or not cover_url:
            raise FeaturedRefreshError(
                "W2A resource omitted required featured fields"
            )
        seen.add(content_id)
        selected.append(
            {
                "content_id": content_id,
                "title": title,
                "cover_url": cover_url,
                "language": compact_text(item.get("language"), 16),
                "episode_count": max(
                    0,
                    _positive_integer(item.get("episode_count")),
                ),
            }
        )
        if len(selected) >= limit:
            break
    if len(selected) != limit:
        raise FeaturedRefreshError(
            "only %d of %d required featured dramas were valid"
            % (len(selected), limit)
        )
    selected = _deterministic_public_order(selected, source_date)
    snapshot = {
        "schema_version": SNAPSHOT_VERSION,
        "source_date": source_date,
        "generated_at": generated.isoformat(timespec="seconds"),
        "items": selected,
    }
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_PUBLIC_SNAPSHOT_BYTES:
        raise FeaturedRefreshError("public snapshot exceeds 32 KiB")
    if _contains_private_spend_key(snapshot):
        raise FeaturedRefreshError("public snapshot contains private spend data")
    return snapshot


def build_language_snapshot(
    *,
    source_date,
    generated_at,
    rankings,
    item_limit=5,
    allowed_cover_hosts=None,
    default_language=DEFAULT_FEATURED_LANGUAGE,
):
    """Build a public-safe v2 bundle containing complete language rankings."""
    source_date = normalize_source_date(source_date)
    generated = shanghai_now(generated_at)
    default_key = normalize_featured_language(default_language)
    limit = max(1, min(int(item_limit), 10))
    if not isinstance(rankings, dict) or len(rankings) > MAX_LANGUAGE_BUCKETS:
        raise FeaturedRefreshError("language rankings are invalid")

    public_rankings = {}
    content_languages = {}
    for raw_language in sorted(rankings):
        language = normalize_featured_language(raw_language)
        if language != raw_language:
            raise FeaturedRefreshError("language ranking key is not canonical")
        source_items = rankings.get(raw_language)
        if not isinstance(source_items, list) or len(source_items) != limit:
            raise FeaturedRefreshError(
                "language ranking must contain exactly %d dramas" % limit
            )
        selected = []
        seen = set()
        for source_item in source_items:
            item = dict(source_item or {})
            try:
                content_id = normalize_content_id(item.get("content_id"))
            except ValueError:
                raise FeaturedRefreshError(
                    "language ranking contains an invalid content ID"
                ) from None
            if content_id in seen:
                raise FeaturedRefreshError(
                    "language ranking contains a duplicate content ID"
                )
            previous = content_languages.setdefault(content_id, language)
            if previous != language:
                raise FeaturedRefreshError(
                    "one drama appeared in multiple language rankings"
                )
            title = compact_text(item.get("title"), 240)
            cover_url = sanitize_cover_url(
                item.get("cover_url"),
                allowed_cover_hosts,
            )
            if not title or not cover_url:
                raise FeaturedRefreshError(
                    "W2A resource omitted required featured fields"
                )
            seen.add(content_id)
            selected.append(
                {
                    "content_id": content_id,
                    "title": title,
                    "cover_url": cover_url,
                    # The W2A resource cache does not carry language metadata.
                    # The verified spend bucket is the source of this field.
                    "language": language,
                    "episode_count": max(
                        0,
                        _positive_integer(item.get("episode_count")),
                    ),
                }
            )
        public_rankings[language] = _deterministic_public_order(
            selected,
            "%s:%s" % (source_date, language),
        )
    if default_key not in public_rankings:
        raise FeaturedRefreshError("English featured ranking is unavailable")

    snapshot = {
        "schema_version": LANGUAGE_SNAPSHOT_VERSION,
        "source_date": source_date,
        "generated_at": generated.isoformat(timespec="seconds"),
        "default_language": default_key,
        "rankings": public_rankings,
    }
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_LANGUAGE_SNAPSHOT_BYTES:
        raise FeaturedRefreshError("language snapshot exceeds 256 KiB")
    if _contains_private_spend_key(snapshot):
        raise FeaturedRefreshError(
            "language snapshot contains private spend data"
        )
    return snapshot


def _snapshot_content(value):
    if not isinstance(value, dict):
        return None
    if set(value) != {
        "schema_version",
        "source_date",
        "generated_at",
        "items",
    }:
        return None
    items = value.get("items")
    if not isinstance(items, list) or len(items) != 5:
        return None
    expected_item_keys = {
        "content_id",
        "title",
        "cover_url",
        "language",
        "episode_count",
    }
    if any(
        not isinstance(item, dict) or set(item) != expected_item_keys
        for item in items
    ):
        return None
    if value.get("schema_version") != SNAPSHOT_VERSION:
        return None
    try:
        if normalize_source_date(value.get("source_date")) != value.get(
            "source_date"
        ):
            return None
        generated_at = datetime.fromisoformat(str(value.get("generated_at") or ""))
    except (FeaturedRefreshError, TypeError, ValueError):
        return None
    if generated_at.tzinfo is None:
        return None
    return {
        "schema_version": value.get("schema_version"),
        "source_date": value.get("source_date"),
        "items": items,
    }


def _language_snapshot_content(value):
    if not isinstance(value, dict):
        return None
    if set(value) != {
        "schema_version",
        "source_date",
        "generated_at",
        "default_language",
        "rankings",
    }:
        return None
    if value.get("schema_version") != LANGUAGE_SNAPSHOT_VERSION:
        return None
    try:
        if normalize_source_date(value.get("source_date")) != value.get(
            "source_date"
        ):
            return None
        generated_at = datetime.fromisoformat(str(value.get("generated_at") or ""))
        default_language = normalize_featured_language(
            value.get("default_language")
        )
    except (FeaturedRefreshError, TypeError, ValueError):
        return None
    if generated_at.tzinfo is None or default_language != DEFAULT_FEATURED_LANGUAGE:
        return None

    rankings = value.get("rankings")
    if (
        not isinstance(rankings, dict)
        or not rankings
        or len(rankings) > MAX_LANGUAGE_BUCKETS
        or default_language not in rankings
    ):
        return None
    expected_item_keys = {
        "content_id",
        "title",
        "cover_url",
        "language",
        "episode_count",
    }
    seen_content_ids = set()
    for raw_language, items in rankings.items():
        try:
            language = normalize_featured_language(raw_language)
        except FeaturedRefreshError:
            return None
        if language != raw_language:
            return None
        if not isinstance(items, list) or len(items) != 5:
            return None
        bucket_ids = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != expected_item_keys:
                return None
            try:
                content_id = normalize_content_id(item.get("content_id"))
            except ValueError:
                return None
            if (
                item.get("content_id") != content_id
                or item.get("language") != language
                or not isinstance(item.get("title"), str)
                or not item.get("title")
                or not isinstance(item.get("cover_url"), str)
                or not item.get("cover_url").startswith("https://")
                or not isinstance(item.get("episode_count"), int)
                or item.get("episode_count") < 0
                or content_id in bucket_ids
                or content_id in seen_content_ids
            ):
                return None
            bucket_ids.add(content_id)
            seen_content_ids.add(content_id)
    return {
        "schema_version": value.get("schema_version"),
        "source_date": value.get("source_date"),
        "default_language": default_language,
        "rankings": rankings,
    }


def _contains_private_spend_key(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key or "").strip().lower() in {"spend", "spend_n"}:
                return True
            if _contains_private_spend_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_private_spend_key(item) for item in value)
    return False


def _atomic_write_validated_snapshot(
    path,
    snapshot,
    *,
    maximum_bytes,
    snapshot_content,
    size_error,
):
    target = Path(path)
    parent = target.parent
    if target.is_symlink() or parent.is_symlink():
        raise FeaturedCacheError("featured cache path must not be a symlink")
    payload = (
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > int(maximum_bytes):
        raise FeaturedCacheError(size_error)
    if _contains_private_spend_key(snapshot):
        raise FeaturedCacheError("featured cache contains private spend data")
    normalized_snapshot = snapshot_content(snapshot)
    if normalized_snapshot is None:
        raise FeaturedCacheError("featured cache snapshot is invalid")

    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        existing = None
    except (OSError, ValueError, TypeError):
        existing = None
    if snapshot_content(existing) == normalized_snapshot:
        return False

    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % target.name,
            suffix=".tmp",
            dir=str(parent),
        )
        temporary = Path(temporary_name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o644)
        else:
            os.chmod(temporary, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))
        temporary = None
        if hasattr(os, "O_DIRECTORY"):
            try:
                directory_fd = os.open(
                    str(parent),
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                # The replacement is already atomic and contains a fully
                # fsynced payload. A directory fsync failure only weakens
                # crash-durability; reporting the refresh as failed would
                # falsely claim that the old inode is still live.
                logging.warning(
                    "featured cache directory fsync unavailable: %s",
                    type(exc).__name__,
                )
        return True
    except FeaturedCacheError:
        raise
    except Exception as exc:
        raise FeaturedCacheError(
            "featured cache atomic write failed: %s" % type(exc).__name__
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def atomic_write_snapshot(path, snapshot):
    """Atomically replace the public v1 JSON; return True only when changed."""
    return _atomic_write_validated_snapshot(
        path,
        snapshot,
        maximum_bytes=MAX_PUBLIC_SNAPSHOT_BYTES,
        snapshot_content=_snapshot_content,
        size_error="featured cache exceeds 32 KiB",
    )


def atomic_write_language_snapshot(path, snapshot):
    """Atomically replace the public v2 language bundle."""
    return _atomic_write_validated_snapshot(
        path,
        snapshot,
        maximum_bytes=MAX_LANGUAGE_SNAPSHOT_BYTES,
        snapshot_content=_language_snapshot_content,
        size_error="featured language cache exceeds 256 KiB",
    )


def ensure_safe_data_disk_target(
    path,
    *,
    mount_path="/mnt/data-disk",
    expected_uuid=DATA_DISK_UUID,
    minimum_free_bytes=1024 * 1024 * 1024,
    mount_info=None,
):
    """Validate the production data-disk boundary before creating cache files."""
    target = Path(path)
    mount = Path(mount_path)
    target_real = os.path.realpath(str(target))
    mount_real = os.path.realpath(str(mount))
    if not (
        target_real.startswith(mount_real + os.sep)
        and target_real != mount_real
    ):
        raise FeaturedCacheError("featured cache path is outside the data disk")
    if mount.is_symlink() or not mount.is_dir() or not os.path.ismount(str(mount)):
        raise FeaturedCacheError("data disk is not a mounted directory")
    try:
        if os.stat(str(mount)).st_dev == os.stat(os.sep).st_dev:
            raise FeaturedCacheError("data disk shares the root filesystem")
    except OSError as exc:
        raise FeaturedCacheError(
            "data disk stat failed: %s" % type(exc).__name__
        ) from None

    details = mount_info(str(mount)) if callable(mount_info) else {}
    actual_uuid = str((details or {}).get("uuid") or "").strip()
    if expected_uuid and actual_uuid and actual_uuid != expected_uuid:
        raise FeaturedCacheError("data disk UUID does not match")
    if expected_uuid and not actual_uuid and callable(mount_info):
        raise FeaturedCacheError("data disk UUID is unavailable")

    stats = os.statvfs(str(mount))
    free_bytes = int(stats.f_bavail) * int(stats.f_frsize)
    if free_bytes < int(minimum_free_bytes):
        raise FeaturedCacheError("data disk has insufficient free space")
    if target.exists() and target.is_symlink():
        raise FeaturedCacheError("featured cache target is a symlink")
    parent = target.parent
    if parent.exists() and parent.is_symlink():
        raise FeaturedCacheError("featured cache directory is a symlink")
    return target
