"""Build a public-safe, last-known-good cache of yesterday's top W2A dramas."""

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile

from features.tt_drama_resolver.service import (
    compact_text,
    normalize_content_id,
    sanitize_cover_url,
)


SHANGHAI_TZ = timezone(timedelta(hours=8))
DATA_DISK_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
CONTENT_ID_SQL_PATTERN = r"^[A-Za-z0-9_-]{10,32}$"
VERIFIED_READONLY_HOST = "101.32.56.53"
VERIFIED_READONLY_PORT = 63350
VERIFIED_DATABASE = "kunlunads_dev"
MAX_CANDIDATE_LIMIT = 20
MAX_METADATA_ROWS_PER_CONTENT = 500
SNAPSHOT_VERSION = 1
MAX_PUBLIC_SNAPSHOT_BYTES = 32 * 1024


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
        drama_table="ads_drama_resource",
        product="Dramawave",
        source_app_id="[w2a]drama-double",
        data_source=6,
        drama_app_id=1479,
        candidate_limit=20,
        item_limit=5,
        allowed_cover_hosts=None,
    ):
        for label, value in (
            ("database", database),
            ("insight_table", insight_table),
            ("insight_index", insight_index),
            ("drama_table", drama_table),
        ):
            if not SAFE_IDENTIFIER_PATTERN.fullmatch(str(value or "")):
                raise ValueError("%s is invalid" % label)
        self.database = str(database)
        self.insight_table = str(insight_table)
        self.insight_index = str(insight_index)
        self.drama_table = str(drama_table)
        self.product = compact_text(product, 100)
        self.source_app_id = compact_text(source_app_id, 100)
        self.data_source = int(data_source)
        self.drama_app_id = str(drama_app_id)
        self.candidate_limit = max(
            5,
            min(int(candidate_limit), MAX_CANDIDATE_LIMIT),
        )
        self.item_limit = max(1, min(int(item_limit), 10))
        if self.candidate_limit < self.item_limit:
            raise ValueError("candidate_limit must be at least item_limit")
        if not self.product or not self.source_app_id or not self.drama_app_id:
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


def _timestamp_key(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI_TZ)
        return value.timestamp()
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=SHANGHAI_TZ
        ).timestamp()
    except ValueError:
        return 0.0


def _positive_integer(value):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


class FeaturedDramaRepository:
    """Bounded two-step query against a verified read-only MySQL replica."""

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

    def _metadata_rows(self, connection, content_ids):
        cfg = self.config
        row_budget = (
            min(len(content_ids), cfg.candidate_limit)
            * MAX_METADATA_ROWS_PER_CONTENT
        )
        placeholders = ",".join(["%s"] * len(content_ids))
        sql = """
            SELECT /*+ MAX_EXECUTION_TIME(15000) */
                r.content_id,
                r.app,
                r.country,
                r.language,
                LEFT(r.name, 241) AS title,
                LEFT(r.cover, 2048) AS cover_url,
                r.sub_number,
                r.updated_at
              FROM {drama} r FORCE INDEX (content_id)
             WHERE r.content_id IN ({placeholders})
               AND r.app_id = %s
               AND r.type = 2
               AND r.sub_number > 0
               AND r.sub_url <> ''
             ORDER BY
                r.content_id ASC,
                r.app ASC,
                r.country ASC,
                r.language ASC,
                r.sub_number ASC,
                r.updated_at DESC
             LIMIT %s
        """.format(
            drama=_qualified(cfg.database, cfg.drama_table),
            placeholders=placeholders,
        )
        rows = self._rows(
            connection,
            sql,
            list(content_ids) + [cfg.drama_app_id, row_budget + 1],
        )
        if len(rows) > row_budget:
            raise FeaturedRefreshError(
                "featured metadata result exceeded the row budget"
            )
        return rows

    def fetch(self, source_date):
        source_date = normalize_source_date(source_date)
        connection = self._connect()
        try:
            spend_rows = self._spend_rows(connection, source_date)
            metadata_rows = self._metadata_rows(
                connection,
                [row["content_id"] for row in spend_rows],
            )
        finally:
            _close_quietly(connection)
        return spend_rows, metadata_rows


def _pick_metadata(metadata_rows, content_ids, allowed_cover_hosts=None):
    wanted = set(content_ids)
    variants = {}
    for source_row in metadata_rows:
        row = dict(source_row or {})
        content_id = str(row.get("content_id") or "")
        if content_id not in wanted:
            continue
        variant_key = (
            content_id,
            compact_text(row.get("app"), 100),
            compact_text(row.get("country"), 16),
            compact_text(row.get("language"), 16),
        )
        variants.setdefault(variant_key, []).append(row)

    best = {}
    for variant_key, rows in variants.items():
        episode_numbers = {
            _positive_integer(row.get("sub_number"))
            for row in rows
            if _positive_integer(row.get("sub_number"))
        }
        latest = max((_timestamp_key(row.get("updated_at")) for row in rows), default=0)
        score = (
            len(episode_numbers),
            latest,
            variant_key[1:],
        )
        content_id = variant_key[0]
        current = best.get(content_id)
        if current is None or score > current["score"]:
            best[content_id] = {
                "score": score,
                "rows": rows,
                "episode_count": len(episode_numbers),
                "country": variant_key[2],
                "language": variant_key[3],
            }

    result = {}
    for content_id, selected in best.items():
        rows = list(selected["rows"])
        rows.sort(
            key=lambda row: (
                _positive_integer(row.get("sub_number")) or 10 ** 9,
                -_timestamp_key(row.get("updated_at")),
            )
        )
        title = ""
        cover_url = ""
        for row in rows:
            if not title:
                title = compact_text(row.get("title"), 240)
            if not cover_url:
                cover_url = sanitize_cover_url(
                    row.get("cover_url"), allowed_cover_hosts
                )
        if (
            not title
            or not cover_url
            or selected["episode_count"] <= 0
        ):
            continue
        result[content_id] = {
            "content_id": content_id,
            "title": title,
            "cover_url": cover_url,
            "country": selected["country"],
            "language": selected["language"],
            "episode_count": selected["episode_count"],
        }
    return result


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
    metadata_rows,
    item_limit=5,
    allowed_cover_hosts=None,
):
    source_date = normalize_source_date(source_date)
    generated = shanghai_now(generated_at)
    limit = max(1, min(int(item_limit), 10))
    ranked_ids = [str(row.get("content_id") or "") for row in spend_rows]
    metadata = _pick_metadata(
        metadata_rows,
        ranked_ids,
        allowed_cover_hosts=allowed_cover_hosts,
    )
    selected = []
    for ranked in spend_rows:
        content_id = str(ranked.get("content_id") or "")
        item = metadata.get(content_id)
        if item is None:
            continue
        selected.append(
            {
                "content_id": item["content_id"],
                "title": item["title"],
                "cover_url": item["cover_url"],
                "language": item["language"],
                "episode_count": item["episode_count"],
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


def atomic_write_snapshot(path, snapshot):
    """Atomically replace a public JSON file; return True only when changed."""
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
    if len(payload) > MAX_PUBLIC_SNAPSHOT_BYTES:
        raise FeaturedCacheError("featured cache exceeds 32 KiB")
    if _contains_private_spend_key(snapshot):
        raise FeaturedCacheError("featured cache contains private spend data")

    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        existing = None
    except (OSError, ValueError, TypeError):
        existing = None
    if _snapshot_content(existing) == _snapshot_content(snapshot):
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
