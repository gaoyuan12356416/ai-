"""SQLite persistence and cross-process leases for W2A public resources."""

import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time

from .models import (
    ResourceOutcome,
    ResourceStorageError,
    normalize_content_id,
    normalize_landing_id,
)


DATA_DISK_ROOT = Path("/mnt/data-disk")
DATA_DISK_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
MIN_DATA_DISK_FREE_BYTES = 1024 * 1024 * 1024
SCHEMA_VERSION = 1


def _default_mount_info_provider(path):
    try:
        result = subprocess.run(
            ["findmnt", "-n", "-o", "TARGET,UUID", "--target", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        raise ResourceStorageError("cannot verify data-disk mount metadata") from None
    fields = str(result.stdout or "").strip().split()
    if len(fields) < 2:
        raise ResourceStorageError("data-disk mount metadata is incomplete")
    return {"mountpoint": fields[0], "uuid": fields[-1]}


def _reject_existing_symlinks(path):
    current = Path(path)
    candidates = [current]
    while current.parent != current:
        current = current.parent
        candidates.append(current)
    for candidate in candidates:
        if candidate.is_symlink():
            raise ResourceStorageError("resource cache path contains a symlink")


def validate_resource_cache_path(
    db_path,
    *,
    allow_test_path=False,
    expected_mount_uuid=DATA_DISK_UUID,
    mount_info_provider=None,
    min_free_bytes=MIN_DATA_DISK_FREE_BYTES,
):
    """Fail closed unless production state is on the verified data disk."""
    unnormalized = Path(db_path)
    if not unnormalized.is_absolute():
        raise ResourceStorageError("resource cache path must be absolute")
    candidate = Path(os.path.abspath(str(unnormalized)))
    _reject_existing_symlinks(candidate)

    if allow_test_path:
        parent = candidate.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ResourceStorageError(
                "test resource cache directory cannot be created"
            ) from None
        _reject_existing_symlinks(candidate)
        if (
            not parent.is_dir()
            or parent.is_symlink()
            or not os.access(str(parent), os.R_OK | os.W_OK | os.X_OK)
        ):
            raise ResourceStorageError("test resource cache directory is not writable")
        return candidate

    if sys.platform != "linux":
        raise ResourceStorageError(
            "non-production resource cache paths require allow_test_path"
        )
    root = DATA_DISK_ROOT
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise ResourceStorageError("verified data disk is unavailable")
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ResourceStorageError(
            "production resource cache must be under /mnt/data-disk"
        ) from None
    if os.stat(str(root)).st_dev == os.stat("/").st_dev:
        raise ResourceStorageError("data disk is not mounted separately from root")
    if shutil.disk_usage(str(root)).free < max(0, int(min_free_bytes)):
        raise ResourceStorageError("data disk has less than 1 GiB free")

    provider = mount_info_provider or _default_mount_info_provider
    info = provider(str(root))
    if not isinstance(info, dict):
        raise ResourceStorageError("data-disk mount metadata is invalid")
    mountpoint = Path(
        os.path.abspath(str(info.get("mountpoint") or ""))
    )
    if mountpoint != root:
        raise ResourceStorageError("data-disk mountpoint is not exact")
    if str(info.get("uuid") or "").strip().lower() != str(
        expected_mount_uuid or ""
    ).strip().lower():
        raise ResourceStorageError("data-disk UUID does not match")

    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ResourceStorageError(
            "resource cache directory cannot be created"
        ) from None
    _reject_existing_symlinks(candidate)
    if (
        not candidate.parent.is_dir()
        or candidate.parent.is_symlink()
        or not os.access(
            str(candidate.parent),
            os.R_OK | os.W_OK | os.X_OK,
        )
    ):
        raise ResourceStorageError(
            "resource cache directory is not writable by this service"
        )
    return candidate


class SQLiteResourceCache:
    """Small persistent cache with one row per landing and content ID."""

    def __init__(
        self,
        db_path,
        busy_timeout_seconds=5,
        clock=None,
        allow_test_path=False,
        expected_mount_uuid=DATA_DISK_UUID,
        mount_info_provider=None,
        min_free_bytes=MIN_DATA_DISK_FREE_BYTES,
    ):
        self.db_path = Path(db_path)
        self.busy_timeout_seconds = max(0.1, float(busy_timeout_seconds))
        self.clock = clock or time.time
        self.allow_test_path = bool(allow_test_path)
        self.expected_mount_uuid = str(expected_mount_uuid or "")
        self.mount_info_provider = mount_info_provider
        self.min_free_bytes = max(0, int(min_free_bytes))
        self._ready = False
        self._closed = False
        self._verified_device = None
        self._init_lock = threading.Lock()

    def _verify_storage_identity(self):
        if self._verified_device is None:
            raise ResourceStorageError(
                "resource cache storage identity is not initialized"
            )
        _reject_existing_symlinks(self.db_path)
        try:
            parent = self.db_path.parent
            parent_stat = parent.stat()
        except OSError:
            raise ResourceStorageError(
                "resource cache storage identity cannot be verified"
            ) from None
        if (
            not parent.is_dir()
            or parent.is_symlink()
            or int(parent_stat.st_dev) != int(self._verified_device)
        ):
            raise ResourceStorageError(
                "resource cache storage device changed"
            )

    def _connect(self):
        connection = None
        try:
            self._verify_storage_identity()
            connection = sqlite3.connect(
                str(self.db_path),
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
            )
            self._verify_storage_identity()
            connection.row_factory = sqlite3.Row
            connection.execute(
                "PRAGMA busy_timeout = %d"
                % int(self.busy_timeout_seconds * 1000)
            )
            connection.execute("PRAGMA foreign_keys = ON")
            self.normalize_permissions()
            return connection
        except ResourceStorageError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error:
            if connection is not None:
                connection.close()
            raise ResourceStorageError(
                "resource cache connection failed"
            ) from None

    def _initialize_schema(self):
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tt_drama_resource_cache (
                    landing_id INTEGER NOT NULL,
                    content_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ready', 'not_found')),
                    resolved_content_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL DEFAULT '',
                    fresh_until REAL NOT NULL,
                    stale_until REAL NOT NULL,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (landing_id, content_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tt_drama_resource_expiry
                    ON tt_drama_resource_cache (fresh_until, stale_until);
                CREATE TABLE IF NOT EXISTS tt_drama_resource_lease (
                    landing_id INTEGER NOT NULL,
                    content_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    lease_until REAL NOT NULL,
                    PRIMARY KEY (landing_id, content_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tt_drama_resource_lease_expiry
                    ON tt_drama_resource_lease (lease_until);
                CREATE TABLE IF NOT EXISTS tt_drama_resource_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        finally:
            connection.close()
        self.normalize_permissions()

    def normalize_permissions(self):
        """Keep the public-field cache writable by its dedicated service group."""
        for suffix in ("", "-wal", "-shm"):
            target = Path("%s%s" % (self.db_path, suffix))
            if not target.exists():
                continue
            if target.is_symlink():
                raise ResourceStorageError(
                    "resource cache database files cannot be symlinks"
                )
            try:
                current_mode = stat.S_IMODE(target.stat().st_mode)
            except OSError:
                if not self.allow_test_path:
                    raise ResourceStorageError(
                        "resource cache permissions could not be inspected"
                    ) from None
                continue
            if current_mode == 0o660:
                continue
            try:
                os.chmod(str(target), 0o660)
            except OSError:
                if not self.allow_test_path:
                    raise ResourceStorageError(
                        "resource cache permissions could not be normalized"
                    ) from None

    def _ensure_ready(self):
        if self._closed:
            raise ResourceStorageError("resource cache is closed")
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            validated_path = validate_resource_cache_path(
                self.db_path,
                allow_test_path=self.allow_test_path,
                expected_mount_uuid=self.expected_mount_uuid,
                mount_info_provider=self.mount_info_provider,
                min_free_bytes=self.min_free_bytes,
            )
            self.db_path = validated_path
            try:
                self._verified_device = int(
                    self.db_path.parent.stat().st_dev
                )
            except OSError:
                raise ResourceStorageError(
                    "resource cache storage identity cannot be initialized"
                ) from None
            try:
                self._initialize_schema()
            except ResourceStorageError:
                self._verified_device = None
                raise
            except (OSError, sqlite3.Error):
                self._verified_device = None
                raise ResourceStorageError(
                    "resource cache schema initialization failed"
                ) from None
            self._ready = True

    def warmup(self):
        self._ensure_ready()
        current = float(self.clock())
        now_text = str(current)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM tt_drama_resource_cache
                 WHERE (status = 'not_found' AND fresh_until <= ?)
                    OR (status = 'ready' AND stale_until <= ?)
                """,
                (current, current),
            )
            connection.execute(
                """
                DELETE FROM tt_drama_resource_lease
                 WHERE lease_until <= ?
                """,
                (current,),
            )
            connection.execute(
                """
                INSERT INTO tt_drama_resource_meta (key, value)
                VALUES ('last_warmup_epoch', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (now_text,),
            )
            connection.commit()
            self.normalize_permissions()
        except sqlite3.Error:
            connection.rollback()
            raise ResourceStorageError("resource cache is not writable") from None
        finally:
            connection.close()
        self.normalize_permissions()
        return True

    def close(self):
        self._closed = True

    @staticmethod
    def _item_from_row(row):
        return {
            "landing_id": int(row["landing_id"]),
            "content_id": str(row["content_id"]),
            "resolved_content_id": str(row["resolved_content_id"]),
            "title": str(row["title"]),
            "description": str(row["description"]),
            "cover_url": str(row["cover_url"]),
            "content_hash": str(row["content_hash"]),
            "fetched_at": str(row["fetched_at"]),
            "country": "",
            "language": "",
            "episode_count": 0,
            "source_updated_at": str(row["fetched_at"]),
        }

    def peek(self, landing_id, content_id, allow_stale=True, now=None):
        self._ensure_ready()
        landing = normalize_landing_id(landing_id)
        normalized = normalize_content_id(content_id)
        current = float(self.clock() if now is None else now)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT *
                  FROM tt_drama_resource_cache
                 WHERE landing_id = ? AND content_id = ?
                 LIMIT 1
                """,
                (landing, normalized),
            ).fetchone()
        finally:
            connection.close()
        self.normalize_permissions()
        if row is None:
            return None
        if row["status"] == "not_found":
            if float(row["fresh_until"]) > current:
                return ResourceOutcome(False, None, "NEGATIVE_HIT")
            return None
        if float(row["fresh_until"]) > current:
            return ResourceOutcome(True, self._item_from_row(row), "DISK_HIT")
        if allow_stale and float(row["stale_until"]) > current:
            return ResourceOutcome(True, self._item_from_row(row), "STALE")
        return None

    def put_ready(
        self,
        landing_id,
        content_id,
        item,
        *,
        positive_ttl_seconds,
        stale_ttl_seconds,
        now=None,
    ):
        self._ensure_ready()
        landing = normalize_landing_id(landing_id)
        normalized = normalize_content_id(content_id)
        current = float(self.clock() if now is None else now)
        fresh_until = current + max(1, int(positive_ttl_seconds))
        stale_until = current + max(
            int(positive_ttl_seconds),
            int(stale_ttl_seconds),
        )
        values = dict(item or {})
        if str(values.get("content_id") or "") != normalized:
            raise ResourceStorageError("resource cache item key does not match")
        if str(values.get("resolved_content_id") or "") != normalized:
            raise ResourceStorageError("resource cache resolved key does not match")
        fetched_at = str(values.get("fetched_at") or "")
        updated_at = fetched_at
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO tt_drama_resource_cache (
                    landing_id, content_id, status, resolved_content_id,
                    title, description, cover_url, content_hash,
                    fetched_at, fresh_until, stale_until, last_error_code,
                    last_error_at, updated_at
                ) VALUES (?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)
                ON CONFLICT(landing_id, content_id) DO UPDATE SET
                    status='ready',
                    resolved_content_id=excluded.resolved_content_id,
                    title=excluded.title,
                    description=excluded.description,
                    cover_url=excluded.cover_url,
                    content_hash=excluded.content_hash,
                    fetched_at=excluded.fetched_at,
                    fresh_until=excluded.fresh_until,
                    stale_until=excluded.stale_until,
                    last_error_code='',
                    last_error_at='',
                    updated_at=excluded.updated_at
                """,
                (
                    landing,
                    normalized,
                    normalized,
                    str(values.get("title") or ""),
                    str(values.get("description") or ""),
                    str(values.get("cover_url") or ""),
                    str(values.get("content_hash") or ""),
                    fetched_at,
                    fresh_until,
                    stale_until,
                    updated_at,
                ),
            )
            connection.commit()
            self.normalize_permissions()
        except sqlite3.Error:
            connection.rollback()
            raise ResourceStorageError("resource cache write failed") from None
        finally:
            connection.close()
        self.normalize_permissions()

    def put_negative(
        self,
        landing_id,
        content_id,
        *,
        negative_ttl_seconds,
        error_code="not_found",
        now=None,
        updated_at="",
    ):
        self._ensure_ready()
        landing = normalize_landing_id(landing_id)
        normalized = normalize_content_id(content_id)
        current = float(self.clock() if now is None else now)
        fresh_until = current + max(1, int(negative_ttl_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO tt_drama_resource_cache (
                    landing_id, content_id, status, fresh_until, stale_until,
                    last_error_code, last_error_at, updated_at
                ) VALUES (?, ?, 'not_found', ?, ?, ?, ?, ?)
                ON CONFLICT(landing_id, content_id) DO UPDATE SET
                    status='not_found',
                    resolved_content_id='',
                    title='',
                    description='',
                    cover_url='',
                    content_hash='',
                    fetched_at='',
                    fresh_until=excluded.fresh_until,
                    stale_until=excluded.stale_until,
                    last_error_code=excluded.last_error_code,
                    last_error_at=excluded.last_error_at,
                    updated_at=excluded.updated_at
                """,
                (
                    landing,
                    normalized,
                    fresh_until,
                    fresh_until,
                    str(error_code or "not_found")[:64],
                    str(updated_at or ""),
                    str(updated_at or ""),
                ),
            )
            connection.execute(
                """
                DELETE FROM tt_drama_resource_cache
                 WHERE status = 'not_found' AND fresh_until <= ?
                """,
                (current,),
            )
            connection.commit()
            self.normalize_permissions()
        except sqlite3.Error:
            connection.rollback()
            raise ResourceStorageError("negative resource cache write failed") from None
        finally:
            connection.close()
        self.normalize_permissions()

    def mark_error(
        self,
        landing_id,
        content_id,
        error_code,
        *,
        error_at="",
    ):
        self._ensure_ready()
        landing = normalize_landing_id(landing_id)
        normalized = normalize_content_id(content_id)
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE tt_drama_resource_cache
                   SET last_error_code = ?, last_error_at = ?
                 WHERE landing_id = ? AND content_id = ?
                """,
                (
                    str(error_code or "source_error")[:64],
                    str(error_at or ""),
                    landing,
                    normalized,
                ),
            )
            self.normalize_permissions()
        except sqlite3.Error:
            raise ResourceStorageError(
                "resource cache error-state update failed"
            ) from None
        finally:
            connection.close()

    def acquire_lease(
        self,
        landing_id,
        content_id,
        owner,
        *,
        lease_seconds,
        now=None,
    ):
        self._ensure_ready()
        landing = normalize_landing_id(landing_id)
        normalized = normalize_content_id(content_id)
        owner_text = str(owner or "").strip()
        if not owner_text:
            raise ResourceStorageError("resource cache lease owner is empty")
        current = float(self.clock() if now is None else now)
        lease_until = current + max(1.0, float(lease_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO tt_drama_resource_lease (
                    landing_id, content_id, owner, lease_until
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(landing_id, content_id) DO UPDATE SET
                    owner=excluded.owner,
                    lease_until=excluded.lease_until
                WHERE tt_drama_resource_lease.lease_until <= ?
                   OR tt_drama_resource_lease.owner = ?
                """,
                (
                    landing,
                    normalized,
                    owner_text,
                    lease_until,
                    current,
                    owner_text,
                ),
            )
            acquired = int(cursor.rowcount or 0) == 1
            connection.commit()
            self.normalize_permissions()
            return acquired
        except sqlite3.Error:
            connection.rollback()
            raise ResourceStorageError("resource cache lease failed") from None
        finally:
            connection.close()

    def release_lease(self, landing_id, content_id, owner):
        self._ensure_ready()
        landing = normalize_landing_id(landing_id)
        normalized = normalize_content_id(content_id)
        connection = self._connect()
        try:
            connection.execute(
                """
                DELETE FROM tt_drama_resource_lease
                 WHERE landing_id = ? AND content_id = ? AND owner = ?
                """,
                (landing, normalized, str(owner or "")),
            )
            self.normalize_permissions()
        except sqlite3.Error:
            raise ResourceStorageError(
                "resource cache lease release failed"
            ) from None
        finally:
            connection.close()
