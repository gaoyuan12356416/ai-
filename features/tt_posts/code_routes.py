"""Durable four-character TT search codes and safe public URL resolution."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import socket
import sqlite3
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .links import (
    TTPostLinkError,
    build_generic_w2a_url,
    build_w2a_url_from_fields,
    validate_w2a_url,
)


CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
CODE_LENGTH = 4
POSITIVE_CACHE_TTL_SECONDS = 24 * 60 * 60
NEGATIVE_CACHE_TTL_SECONDS = 30
_CODE_RE = re.compile(r"^[A-Z0-9]{4}$")
_CONTENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PUBLIC_CONTENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,32}$")
_NEGATIVE_SENTINEL = {"missing": True}
_ROUTE_ROW_FIELDS = {
    "code",
    "queue_id",
    "content_id",
    "c",
    "af_adset",
    "af_adset_id",
    "af_ad",
    "af_ad_id",
    "af_channel",
    "af_c_id",
    "long_url",
    "state",
    "created_at",
    "published_at",
    "updated_at",
}


class TTCodeRouteError(ValueError):
    """Stable error emitted by the public code resolver."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code or "tt_code_route_error")[:96]
        self.status = int(status)
        super().__init__(str(message or "TT code route error")[:500])


def ensure_code_route_storage(
    conn: sqlite3.Connection,
    *,
    code_length: int = CODE_LENGTH,
) -> None:
    """Create the additive route ledger and automatic state-sync trigger."""

    if isinstance(code_length, bool):
        raise ValueError("code length is invalid")
    normalized_length = int(code_length)
    if normalized_length < 1 or normalized_length > 8:
        raise ValueError("code length is invalid")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tt_post_code_route (
            code TEXT PRIMARY KEY
                CHECK(
                    length(code)={code_length}
                    AND code=upper(code)
                    AND code NOT GLOB '*[^A-Z0-9]*'
                ),
            queue_id INTEGER NOT NULL UNIQUE,
            content_id TEXT NOT NULL,
            c TEXT NOT NULL,
            af_adset TEXT NOT NULL,
            af_adset_id TEXT NOT NULL,
            af_ad TEXT NOT NULL,
            af_ad_id TEXT NOT NULL,
            af_channel TEXT NOT NULL CHECK(af_channel='TT'),
            af_c_id TEXT NOT NULL,
            long_url TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """.format(code_length=normalized_length)
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tt_post_code_route_content_latest
            ON tt_post_code_route(
                content_id,state,published_at DESC,created_at DESC,
                queue_id DESC
            )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tt_post_code_route_oldest
            ON tt_post_code_route(created_at,code)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tt_post_code_recycle_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            old_queue_id INTEGER NOT NULL,
            old_content_id TEXT NOT NULL,
            new_queue_id INTEGER NOT NULL,
            recycled_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_tt_post_queue_code_route_state
        AFTER UPDATE OF status ON tt_post_queue
        FOR EACH ROW WHEN OLD.status<>NEW.status
        BEGIN
            UPDATE tt_post_code_route
            SET state=NEW.status,
                published_at=CASE
                    WHEN NEW.status='published' AND published_at=''
                    THEN NEW.updated_at
                    ELSE published_at
                END,
                updated_at=NEW.updated_at
            WHERE queue_id=NEW.id;
        END
        """
    )


def _validate_allocator_shape(alphabet: str, code_length: int) -> tuple[str, int]:
    normalized_alphabet = str(alphabet or "")
    if (
        not normalized_alphabet
        or len(set(normalized_alphabet)) != len(normalized_alphabet)
        or any(char not in CODE_ALPHABET for char in normalized_alphabet)
    ):
        raise ValueError("code alphabet must contain unique A-Z/0-9 characters")
    if isinstance(code_length, bool):
        raise ValueError("code length is invalid")
    normalized_length = int(code_length)
    if normalized_length < 1 or normalized_length > 8:
        raise ValueError("code length is invalid")
    return normalized_alphabet, normalized_length


def _route_values(route: Mapping[str, Any]) -> Dict[str, str]:
    required = {
        "content_id",
        "c",
        "af_adset",
        "af_adset_id",
        "af_ad",
        "af_ad_id",
        "af_channel",
        "af_c_id",
        "long_url",
        "state",
        "created_at",
        "published_at",
        "updated_at",
    }
    if not isinstance(route, Mapping) or set(route) != required:
        raise ValueError("route fields are incomplete")
    values = {name: str(route[name] or "").strip() for name in required}
    if any(not values[name] for name in required - {"published_at"}):
        raise ValueError("route fields must not be empty")
    if not _CONTENT_ID_RE.fullmatch(values["content_id"]):
        raise ValueError("route content_id is invalid")
    if values["af_channel"] != "TT":
        raise ValueError("frozen route channel must be TT")
    validate_w2a_url(values["long_url"])
    return values


def _candidate_code(
    alphabet: str,
    code_length: int,
    choice_fn: Callable[[Sequence[str]], str],
) -> str:
    return "".join(str(choice_fn(alphabet)) for _item in range(code_length))


def _first_unused_code(
    conn: sqlite3.Connection,
    alphabet: str,
    code_length: int,
) -> str:
    """Find the first free code in O(capacity) time and bounded memory."""

    radix = len(alphabet)
    capacity = radix ** code_length
    positions = {char: index for index, char in enumerate(alphabet)}
    occupied = bytearray(capacity)
    for row in conn.execute("SELECT code FROM tt_post_code_route"):
        code = str(row[0])
        if len(code) != code_length or any(char not in positions for char in code):
            continue
        index = 0
        for char in code:
            index = index * radix + positions[char]
        occupied[index] = 1
    try:
        selected_index = occupied.index(0)
    except ValueError:
        return ""
    characters = [alphabet[0]] * code_length
    for offset in range(code_length - 1, -1, -1):
        selected_index, remainder = divmod(selected_index, radix)
        characters[offset] = alphabet[remainder]
    return "".join(characters)


def allocate_code_route(
    conn: sqlite3.Connection,
    queue_id: int,
    route: Mapping[str, Any],
    *,
    alphabet: str = CODE_ALPHABET,
    code_length: int = CODE_LENGTH,
    choice_fn: Callable[[Sequence[str]], str] = secrets.choice,
    random_attempts: int = 128,
) -> Dict[str, Any]:
    """Allocate inside the caller's ``BEGIN IMMEDIATE`` transaction.

    A queue is idempotent. At full capacity the oldest mapping is deleted and
    its exact code is reused, with ``code`` as the deterministic tie-break.
    """

    normalized_alphabet, normalized_length = _validate_allocator_shape(
        alphabet,
        code_length,
    )
    normalized_queue_id = int(queue_id)
    if normalized_queue_id <= 0:
        raise ValueError("queue_id is invalid")
    existing = conn.execute(
        "SELECT * FROM tt_post_code_route WHERE queue_id=?",
        (normalized_queue_id,),
    ).fetchone()
    if existing is not None:
        return dict(existing)
    values = _route_values(route)
    capacity = len(normalized_alphabet) ** normalized_length
    used = int(
        conn.execute("SELECT COUNT(*) FROM tt_post_code_route").fetchone()[0]
    )
    selected = ""
    recycled = None
    if used >= capacity:
        oldest = conn.execute(
            """
            SELECT code,queue_id,content_id FROM tt_post_code_route
            ORDER BY created_at ASC,code ASC LIMIT 1
            """
        ).fetchone()
        if oldest is None:
            raise RuntimeError("code capacity accounting is inconsistent")
        selected = str(oldest["code"])
        recycled = {
            "code": selected,
            "queue_id": int(oldest["queue_id"]),
            "content_id": str(oldest["content_id"]),
        }
        conn.execute("DELETE FROM tt_post_code_route WHERE code=?", (selected,))
    else:
        attempts = max(1, min(int(random_attempts), capacity))
        for _attempt in range(attempts):
            candidate = _candidate_code(
                normalized_alphabet,
                normalized_length,
                choice_fn,
            )
            if (
                len(candidate) == normalized_length
                and all(char in normalized_alphabet for char in candidate)
                and conn.execute(
                    "SELECT 1 FROM tt_post_code_route WHERE code=?",
                    (candidate,),
                ).fetchone()
                is None
            ):
                selected = candidate
                break
        if not selected:
            selected = _first_unused_code(
                conn,
                normalized_alphabet,
                normalized_length,
            )
    if not selected:
        raise RuntimeError("no code could be allocated")
    columns = (
        "code",
        "queue_id",
        "content_id",
        "c",
        "af_adset",
        "af_adset_id",
        "af_ad",
        "af_ad_id",
        "af_channel",
        "af_c_id",
        "long_url",
        "state",
        "created_at",
        "published_at",
        "updated_at",
    )
    conn.execute(
        "INSERT INTO tt_post_code_route(%s) VALUES(%s)"
        % (",".join(columns), ",".join("?" for _item in columns)),
        (
            selected,
            normalized_queue_id,
            values["content_id"],
            values["c"],
            values["af_adset"],
            values["af_adset_id"],
            values["af_ad"],
            values["af_ad_id"],
            values["af_channel"],
            values["af_c_id"],
            values["long_url"],
            values["state"],
            values["created_at"],
            values["published_at"],
            values["updated_at"],
        ),
    )
    if recycled is not None:
        conn.execute(
            """
            INSERT INTO tt_post_code_recycle_audit(
                code,old_queue_id,old_content_id,new_queue_id,recycled_at
            ) VALUES(?,?,?,?,?)
            """,
            (
                recycled["code"],
                recycled["queue_id"],
                recycled["content_id"],
                normalized_queue_id,
                values["created_at"],
            ),
        )
    row = conn.execute(
        "SELECT * FROM tt_post_code_route WHERE code=?",
        (selected,),
    ).fetchone()
    return dict(row)


class RedisRESPClient:
    """Tiny dependency-free Redis client supporting the cache commands used."""

    def __init__(self, host: str, port: int, timeout: float):
        self.host = str(host or "127.0.0.1")
        self.port = int(port)
        self.timeout = float(timeout)

    @staticmethod
    def _command(*parts: Any) -> bytes:
        encoded = [str(part).encode("utf-8") for part in parts]
        payload = [b"*%d\r\n" % len(encoded)]
        for part in encoded:
            payload.extend((b"$%d\r\n" % len(part), part, b"\r\n"))
        return b"".join(payload)

    @staticmethod
    def _readline(handle: Any) -> bytes:
        line = handle.readline(65537)
        if not line.endswith(b"\r\n"):
            raise OSError("invalid Redis response")
        return line[:-2]

    def _execute(self, *parts: Any) -> Any:
        with socket.create_connection(
            (self.host, self.port),
            timeout=self.timeout,
        ) as connection:
            connection.settimeout(self.timeout)
            connection.sendall(self._command(*parts))
            handle = connection.makefile("rb")
            prefix = handle.read(1)
            if prefix == b"+":
                return self._readline(handle).decode("utf-8")
            if prefix == b":":
                return int(self._readline(handle))
            if prefix == b"$":
                length = int(self._readline(handle))
                if length == -1:
                    return None
                if length < 0 or length > 1024 * 1024:
                    raise OSError("invalid Redis bulk length")
                data = handle.read(length)
                if len(data) != length or handle.read(2) != b"\r\n":
                    raise OSError("truncated Redis response")
                return data.decode("utf-8")
            if prefix == b"-":
                raise OSError("Redis command failed")
            raise OSError("unsupported Redis response")

    def get(self, key: str) -> Optional[str]:
        return self._execute("GET", key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        result = self._execute("SETEX", key, int(ttl), value)
        if result != "OK":
            raise OSError("Redis SETEX failed")

    def delete(self, key: str) -> None:
        self._execute("DEL", key)


class TTCodeRouteResolver:
    """Read-through resolver with SQLite truth and best-effort Redis caching."""

    def __init__(
        self,
        db_path: Any,
        *,
        redis_client: Optional[Any] = None,
        lock: Optional[threading.RLock] = None,
        cache_namespace: Optional[str] = None,
    ):
        self.db_path = str(Path(db_path))
        self.redis = redis_client
        self.lock = lock or threading.RLock()
        self.cache_namespace = str(cache_namespace or secrets.token_hex(12))

    @staticmethod
    def _key_for_namespace(namespace: str, kind: str, identity: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return "tt-post-code:%s:%s:%s" % (
            namespace,
            kind,
            digest,
        )

    def _key(self, kind: str, identity: str) -> str:
        with self.lock:
            return self._key_for_namespace(
                self.cache_namespace,
                kind,
                identity,
            )

    def _namespace_key(self, kind: str, identity: str) -> tuple[str, str]:
        with self.lock:
            namespace = self.cache_namespace
            return namespace, self._key_for_namespace(
                namespace,
                kind,
                identity,
            )

    def _namespace_current(self, namespace: str) -> bool:
        with self.lock:
            return secrets.compare_digest(self.cache_namespace, namespace)

    def _rotate_namespace(self) -> None:
        self.cache_namespace = secrets.token_hex(12)

    def rotate_namespace(self) -> None:
        """Make every old-process cache key unreachable immediately."""

        with self.lock:
            self._rotate_namespace()

    def _cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        if self.redis is None:
            return None
        try:
            raw = self.redis.get(key)
            if raw is None:
                return None
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    def _cache_set(self, key: str, value: Mapping[str, Any], ttl: int) -> None:
        if self.redis is None:
            return
        try:
            self.redis.setex(
                key,
                int(ttl),
                json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            return

    @staticmethod
    def _cached_route(
        value: Optional[Mapping[str, Any]],
        *,
        code: Optional[str] = None,
        content_id: Optional[str] = None,
        published_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Accept only a complete cache row that matches its lookup identity."""

        if not isinstance(value, Mapping) or set(value) != _ROUTE_ROW_FIELDS:
            return None
        route = dict(value)
        normalized_code = str(route.get("code") or "")
        normalized_content_id = str(route.get("content_id") or "")
        if (
            not _CODE_RE.fullmatch(normalized_code)
            or not _CONTENT_ID_RE.fullmatch(normalized_content_id)
            or (code is not None and normalized_code != code)
            or (content_id is not None and normalized_content_id != content_id)
            or (published_only and str(route.get("state") or "") != "published")
            or str(route.get("af_channel") or "") != "TT"
        ):
            return None
        try:
            if int(route.get("queue_id")) <= 0:
                return None
            target = validate_w2a_url(route.get("long_url"))
            query = dict(
                urllib.parse.parse_qsl(
                    urllib.parse.urlsplit(target).query,
                    keep_blank_values=True,
                )
            )
        except (KeyError, TypeError, ValueError, TTPostLinkError):
            return None
        expected = {
            "c": str(route.get("c") or ""),
            "af_adset": str(route.get("af_adset") or ""),
            "af_adset_id": str(route.get("af_adset_id") or ""),
            "af_ad": str(route.get("af_ad") or ""),
            "af_ad_id": str(route.get("af_ad_id") or ""),
            "af_channel": "TT",
            "af_c_id": str(route.get("af_c_id") or ""),
            "af_dp": normalized_content_id,
        }
        if query != expected:
            return None
        return route

    def invalidate_latest(self, content_id: Any) -> None:
        old_key = self.prepare_latest_invalidation(content_id)
        self.complete_invalidation(old_key)

    def prepare_latest_invalidation(self, content_id: Any) -> Optional[str]:
        """Rotate cache truth under the caller's mutation lock, without I/O."""

        normalized = str(content_id or "").strip()
        if not _CONTENT_ID_RE.fullmatch(normalized) or self.redis is None:
            return None
        with self.lock:
            old_namespace = self.cache_namespace
            old_key = self._key_for_namespace(
                old_namespace,
                "latest",
                normalized,
            )
            # Make stale keys unreachable before any best-effort network I/O.
            self._rotate_namespace()
        return old_key

    def complete_invalidation(self, old_key: Optional[str]) -> None:
        """Best-effort removal of an unreachable key, always outside the lock."""

        if not old_key or self.redis is None:
            return
        try:
            self.redis.delete(old_key)
        except Exception:
            return

    def invalidate_code(self, code: Any) -> None:
        normalized = str(code or "").strip().upper()
        if not _CODE_RE.fullmatch(normalized) or self.redis is None:
            return
        with self.lock:
            old_namespace = self.cache_namespace
            old_key = self._key_for_namespace(
                old_namespace,
                "code",
                normalized,
            )
            self._rotate_namespace()
        self.complete_invalidation(old_key)

    def _lookup_code(self, code: str) -> Optional[Dict[str, Any]]:
        for _attempt in range(3):
            namespace, key = self._namespace_key("code", code)
            cached = self._cache_get(key)
            if cached == _NEGATIVE_SENTINEL:
                if self._namespace_current(namespace):
                    return None
                continue
            cached_route = self._cached_route(cached, code=code)
            if cached_route is not None:
                if self._namespace_current(namespace):
                    return cached_route
                continue
            row = self._read_code_row(code)
            if row is None:
                self._cache_set(
                    key,
                    _NEGATIVE_SENTINEL,
                    NEGATIVE_CACHE_TTL_SECONDS,
                )
                if self._namespace_current(namespace):
                    return None
                continue
            self._cache_set(key, row, POSITIVE_CACHE_TTL_SECONDS)
            if self._namespace_current(namespace):
                return row
        # Continuous route mutations are rare. The bounded final read holds
        # only the in-process mutation lock and performs no Redis I/O.
        with self.lock:
            return self._read_code_row(code)

    def _read_code_row(self, code: str) -> Optional[Dict[str, Any]]:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM tt_post_code_route WHERE code=?",
                (code,),
            ).fetchone()
        if row is None:
            return None
        route = self._cached_route(dict(row), code=code)
        if route is None:
            raise TTCodeRouteError(
                "tt_code_route_invalid",
                "stored code route is invalid",
                500,
            )
        return route

    def _lookup_latest(self, content_id: str) -> Optional[Dict[str, Any]]:
        for _attempt in range(3):
            namespace, key = self._namespace_key("latest", content_id)
            cached = self._cache_get(key)
            if cached == _NEGATIVE_SENTINEL:
                if self._namespace_current(namespace):
                    return None
                continue
            cached_route = self._cached_route(
                cached,
                content_id=content_id,
                published_only=True,
            )
            if cached_route is not None:
                if self._namespace_current(namespace):
                    return cached_route
                continue
            row = self._read_latest_row(content_id)
            if row is None:
                self._cache_set(
                    key,
                    _NEGATIVE_SENTINEL,
                    NEGATIVE_CACHE_TTL_SECONDS,
                )
                if self._namespace_current(namespace):
                    return None
                continue
            self._cache_set(key, row, POSITIVE_CACHE_TTL_SECONDS)
            if self._namespace_current(namespace):
                return row
        with self.lock:
            return self._read_latest_row(content_id)

    def _read_latest_row(self, content_id: str) -> Optional[Dict[str, Any]]:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM tt_post_code_route
                WHERE content_id=? AND state='published'
                ORDER BY published_at DESC,created_at DESC,queue_id DESC
                LIMIT 1
                """,
                (content_id,),
            ).fetchone()
        if row is None:
            return None
        route = self._cached_route(
            dict(row),
            content_id=content_id,
            published_only=True,
        )
        if route is None:
            raise TTCodeRouteError(
                "tt_code_route_invalid",
                "stored drama route is invalid",
                500,
            )
        return route

    @staticmethod
    def _frozen_target(row: Mapping[str, Any], channel: str) -> str:
        return build_w2a_url_from_fields(
            {
                "c": row["c"],
                "af_adset": row["af_adset"],
                "af_adset_id": row["af_adset_id"],
                "af_ad": row["af_ad"],
                "af_ad_id": row["af_ad_id"],
                "af_c_id": row["af_c_id"],
                "af_dp": row["content_id"],
            },
            channel=channel,
        )

    def resolve(self, query: Any, source: Any) -> Dict[str, Any]:
        normalized_query = str(query or "")
        normalized_source = str(source or "")
        if normalized_source not in {"Search", "Featured"}:
            raise TTCodeRouteError(
                "tt_code_source_invalid",
                "source must be Search or Featured",
                400,
            )
        is_code = bool(
            len(normalized_query) == CODE_LENGTH
            and normalized_query.isalnum()
        )
        if not is_code and not _PUBLIC_CONTENT_ID_RE.fullmatch(normalized_query):
            raise TTCodeRouteError(
                "tt_code_query_invalid",
                "query is invalid",
                400,
            )
        if is_code:
            code = normalized_query.upper()
            if not _CODE_RE.fullmatch(code):
                raise TTCodeRouteError(
                    "tt_code_query_invalid",
                    "code is invalid",
                    400,
                )
            row = self._lookup_code(code)
            if row is None:
                raise TTCodeRouteError(
                    "tt_code_not_found",
                    "code was not found",
                    404,
                )
            try:
                target = validate_w2a_url(row["long_url"])
            except (TTPostLinkError, KeyError, TypeError):
                raise TTCodeRouteError(
                    "tt_code_route_invalid",
                    "stored code route is invalid",
                    500,
                ) from None
            return {
                "found": True,
                "item": {
                    "content_id": str(row["content_id"]),
                    "target_url": target,
                    "query_type": "code",
                    "route_mode": "code_exact",
                    "code": code,
                    "source": normalized_source,
                    "af_channel": "TT",
                },
            }
        row = self._lookup_latest(normalized_query)
        if row is None:
            target = build_generic_w2a_url(
                normalized_query,
                normalized_source,
            )
            return {
                "found": True,
                "item": {
                    "content_id": normalized_query,
                    "target_url": target,
                    "query_type": "content_id",
                    "route_mode": "generic_fallback",
                    "source": normalized_source,
                    "af_channel": normalized_source,
                },
            }
        try:
            target = self._frozen_target(row, normalized_source)
        except (TTPostLinkError, KeyError, TypeError):
            raise TTCodeRouteError(
                "tt_code_route_invalid",
                "stored drama route is invalid",
                500,
            ) from None
        return {
            "found": True,
            "item": {
                "content_id": normalized_query,
                "target_url": target,
                "query_type": "content_id",
                "route_mode": "published_clone",
                "code": str(row["code"]),
                "source": normalized_source,
                "af_channel": normalized_source,
            },
        }
