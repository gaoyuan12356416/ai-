#!/usr/bin/env python3
"""X OAuth 2.0 PKCE sidecar with multi-account token isolation.

Only /health and /callback are public. The /internal/* endpoints are loopback-only
and require a separate bearer token. OAuth credentials and user tokens never
leave this process.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


DEFAULT_ENV_FILE = "/etc/x-post-automation.env"
EXPECTED_SCOPE_DEFAULT = "tweet.read tweet.write users.read offline.access media.write"
REQUIRED_SCOPES = tuple(EXPECTED_SCOPE_DEFAULT.split())
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
USER_FIELDS = (
    "profile_image_url",
    "public_metrics",
    "created_at",
    "verified",
    "protected",
    "location",
)
USERS_ME_URL = "https://api.x.com/2/users/me?" + urllib.parse.urlencode(
    {"user.fields": ",".join(USER_FIELDS)}
)
MAX_BODY_BYTES = 16 * 1024
MAX_DAILY_PLAN_BODY_BYTES = 256 * 1024
MAX_ERROR_TEXT = 240


def load_env_file(path):
    env_path = Path(path)
    if not env_path.exists():
        return
    try:
        raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    except PermissionError:
        # systemd reads EnvironmentFile as PID 1 before dropping to the
        # unprivileged service user. The application must not reopen the
        # root-only secret file after those values are already injected.
        return
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)) or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def env_positive_int_tuple(name):
    values = []
    for raw in os.environ.get(name, "").replace(" ", "").split(","):
        if not raw:
            continue
        if not re.fullmatch(r"[1-9][0-9]*", raw):
            return ()
        value = int(raw)
        if value in values:
            return ()
        values.append(value)
    return tuple(values)


load_env_file(os.environ.get("X_POST_ENV_FILE", DEFAULT_ENV_FILE))

CLIENT_ID = os.environ.get("X_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "").strip()
INTERNAL_TOKEN = (
    os.environ.get("X_INTERNAL_TOKEN", "").strip()
    or os.environ.get("X_POST_AUTOMATION_INTERNAL_TOKEN", "").strip()
)
DAILY_INTERNAL_TOKEN = os.environ.get("X_POST_DAILY_INTERNAL_TOKEN", "").strip()
DAILY_ACCOUNT_IDS = env_positive_int_tuple("X_POST_DAILY_ACCOUNT_IDS")
PUBLIC_BASE_URL = os.environ.get(
    "X_PUBLIC_BASE_URL", "https://ai.yingliangads.com/x-oauth"
).rstrip("/")
_public_parts = urllib.parse.urlsplit(PUBLIC_BASE_URL)
ADMIN_RETURN_URL = os.environ.get(
    "X_ADMIN_RETURN_URL",
    urllib.parse.urlunsplit((_public_parts.scheme, _public_parts.netloc, "/x-accounts.html", "", "")),
).strip()
LISTEN_HOST = os.environ.get("X_LISTEN_HOST", "127.0.0.1").strip()
LISTEN_PORT = env_int("X_LISTEN_PORT", 8810, 1, 65535)
DATA_DIR = Path(os.environ.get("X_DATA_DIR", "/var/lib/x-post-automation"))
DB_PATH = Path(os.environ.get("X_ACCOUNTS_DB", str(DATA_DIR / "accounts.sqlite3")))
TOKENS_DIR = Path(os.environ.get("X_TOKENS_DIR", str(DATA_DIR / "tokens")))
SCOPES = tuple(dict.fromkeys(os.environ.get("X_OAUTH_SCOPES", EXPECTED_SCOPE_DEFAULT).split()))
STATE_TTL_SECONDS = env_int("X_STATE_TTL_SECONDS", 600, 60, 1800)
HTTP_TIMEOUT_SECONDS = env_int("X_HTTP_TIMEOUT_SECONDS", 30, 5, 120)
POST_DB_PATH = Path(os.environ.get("X_POST_DB_PATH", "").strip() or str(DB_PATH))
POST_PUBLIC_ROOT = Path(
    os.environ.get("X_POST_PUBLIC_ROOT", "").strip()
    or "/mnt/data-disk/x-post-automation/s2l"
)
POST_SHORT_BASE_URL = os.environ.get(
    "X_POST_SHORT_BASE_URL", "https://ai.yingliangads.com/s2l"
).strip().rstrip("/")
POST_STORAGE_MOUNT_ROOT = Path(
    os.environ.get("X_POST_STORAGE_MOUNT_ROOT", "/mnt/data-disk").strip()
)
POST_STORAGE_ROOT = Path(
    os.environ.get(
        "X_POST_STORAGE_ROOT", "/mnt/data-disk/x-post-automation"
    ).strip()
)
POST_MEDIA_ALLOWED_HOSTS = tuple(
    dict.fromkeys(
        value.strip().lower()
        for value in os.environ.get("X_POST_MEDIA_ALLOWED_HOSTS", "").replace(",", " ").split()
        if value.strip()
    )
)
POST_HTTP_TIMEOUT_SECONDS = env_int("X_POST_HTTP_TIMEOUT_SECONDS", 30, 5, 120)
POST_MAX_MEDIA_BYTES = env_int(
    "X_POST_MAX_MEDIA_BYTES", 512 * 1024 * 1024, 1024, 512 * 1024 * 1024
)

CANARY_ACTOR = {
    "tenant_key": "internal",
    "user_id": "x-post-canary",
    "name": "X Post Canary",
    "email": "",
    "role": "admin",
}

_DB_LOCK = threading.RLock()
_ACCOUNT_LOCKS = {}
_ACCOUNT_LOCKS_LOCK = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())


class ServiceError(RuntimeError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = str(code or "invalid_request")
        self.status = int(status or 400)


def now_epoch():
    return int(time.time())


def iso_utc(epoch=None):
    value = time.time() if epoch is None else float(epoch)
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_epoch(value):
    if not value:
        return 0
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def clean_text(value, limit=MAX_ERROR_TEXT):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"(?i)(access_token|refresh_token|client_secret|code_verifier|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    return text[:limit]


def require_oauth_config():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise ServiceError("x_oauth_not_configured", "X OAuth客户端尚未完整配置", 503)
    missing = [scope for scope in REQUIRED_SCOPES if scope not in set(SCOPES)]
    if missing:
        raise ServiceError("x_oauth_not_configured", "X OAuth必需权限配置不完整", 503)


def callback_url():
    return PUBLIC_BASE_URL + "/callback"


def ensure_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    for path in (DATA_DIR, DB_PATH.parent, TOKENS_DIR):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    with _DB_LOCK:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        try:
            conn.executescript(
                """
                PRAGMA journal_mode=DELETE;
                PRAGMA busy_timeout=30000;
                CREATE TABLE IF NOT EXISTS x_authorized_account (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    x_user_id TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    profile_image_url TEXT NOT NULL DEFAULT '',
                    token_store_key TEXT NOT NULL DEFAULT '',
                    token_type TEXT NOT NULL DEFAULT 'bearer',
                    scopes_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    first_authorized_at TEXT NOT NULL,
                    last_authorized_at TEXT NOT NULL,
                    access_expires_at TEXT NOT NULL DEFAULT '',
                    last_token_refresh_at TEXT NOT NULL DEFAULT '',
                    last_verified_at TEXT NOT NULL DEFAULT '',
                    last_error_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    authorized_by_user_id TEXT NOT NULL DEFAULT '',
                    authorized_by_name TEXT NOT NULL DEFAULT '',
                    authorized_by_email TEXT NOT NULL DEFAULT '',
                    owner_tenant_key TEXT NOT NULL DEFAULT '',
                    owner_user_id TEXT NOT NULL DEFAULT '',
                    owner_name TEXT NOT NULL DEFAULT '',
                    owner_email TEXT NOT NULL DEFAULT '',
                    followers_count INTEGER,
                    following_count INTEGER,
                    tweet_count INTEGER,
                    listed_count INTEGER,
                    like_count INTEGER,
                    media_count INTEGER,
                    verified INTEGER,
                    protected INTEGER,
                    location TEXT,
                    x_created_at TEXT,
                    profile_synced_at TEXT NOT NULL DEFAULT '',
                    disconnected_at TEXT NOT NULL DEFAULT '',
                    disconnected_by_tenant_key TEXT NOT NULL DEFAULT '',
                    disconnected_by_user_id TEXT NOT NULL DEFAULT '',
                    disconnected_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS x_oauth_state (
                    state_hash TEXT PRIMARY KEY,
                    code_verifier TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL DEFAULT '',
                    actor_tenant_key TEXT NOT NULL DEFAULT '',
                    actor_name TEXT NOT NULL DEFAULT '',
                    actor_email TEXT NOT NULL DEFAULT '',
                    actor_role TEXT NOT NULL DEFAULT '',
                    redirect_uri TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS x_oauth_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    x_user_id TEXT NOT NULL DEFAULT '',
                    actor_user_id TEXT NOT NULL DEFAULT '',
                    actor_tenant_key TEXT NOT NULL DEFAULT '',
                    actor_name TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_x_account_updated ON x_authorized_account(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_x_oauth_state_expires ON x_oauth_state(expires_at);
                CREATE INDEX IF NOT EXISTS idx_x_oauth_event_created ON x_oauth_event(created_at DESC);
                """
            )
            account_columns = {
                "owner_tenant_key": "TEXT NOT NULL DEFAULT ''",
                "owner_user_id": "TEXT NOT NULL DEFAULT ''",
                "owner_name": "TEXT NOT NULL DEFAULT ''",
                "owner_email": "TEXT NOT NULL DEFAULT ''",
                "followers_count": "INTEGER",
                "following_count": "INTEGER",
                "tweet_count": "INTEGER",
                "listed_count": "INTEGER",
                "like_count": "INTEGER",
                "media_count": "INTEGER",
                "verified": "INTEGER",
                "protected": "INTEGER",
                "location": "TEXT",
                "x_created_at": "TEXT",
                "profile_synced_at": "TEXT NOT NULL DEFAULT ''",
                "disconnected_at": "TEXT NOT NULL DEFAULT ''",
                "disconnected_by_tenant_key": "TEXT NOT NULL DEFAULT ''",
                "disconnected_by_user_id": "TEXT NOT NULL DEFAULT ''",
                "disconnected_by_name": "TEXT NOT NULL DEFAULT ''",
            }
            existing = {row[1] for row in conn.execute("PRAGMA table_info(x_authorized_account)")}
            for column, definition in account_columns.items():
                if column not in existing:
                    conn.execute("ALTER TABLE x_authorized_account ADD COLUMN %s %s" % (column, definition))

            state_columns = {row[1] for row in conn.execute("PRAGMA table_info(x_oauth_state)")}
            if "actor_tenant_key" not in state_columns:
                conn.execute("ALTER TABLE x_oauth_state ADD COLUMN actor_tenant_key TEXT NOT NULL DEFAULT ''")
            event_columns = {row[1] for row in conn.execute("PRAGMA table_info(x_oauth_event)")}
            if "actor_tenant_key" not in event_columns:
                conn.execute("ALTER TABLE x_oauth_event ADD COLUMN actor_tenant_key TEXT NOT NULL DEFAULT ''")

            # Historical rows did not capture tenant_key. Preserve their known user
            # identity but leave tenant blank so owner-scoped operations fail closed
            # until an operator performs an explicit tenant backfill.
            conn.execute(
                """
                UPDATE x_authorized_account
                SET owner_user_id=authorized_by_user_id,
                    owner_name=authorized_by_name,
                    owner_email=authorized_by_email
                WHERE owner_user_id='' AND authorized_by_user_id<>''
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_account_owner_updated "
                "ON x_authorized_account(owner_tenant_key,owner_user_id,updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_x_account_status_updated "
                "ON x_authorized_account(status,updated_at DESC)"
            )
            conn.commit()
        finally:
            conn.close()
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


def db_connect():
    ensure_storage()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def state_digest(state):
    return hashlib.sha256(str(state).encode("utf-8")).hexdigest()


def pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def normalize_actor(actor):
    actor = actor if isinstance(actor, dict) else {}
    return {
        "user_id": clean_text(actor.get("user_id", ""), 255),
        "tenant_key": clean_text(actor.get("tenant_key", ""), 255),
        "name": clean_text(actor.get("name", ""), 255),
        "email": clean_text(actor.get("email", ""), 255),
        "role": clean_text(actor.get("role", "user"), 32),
    }


def require_actor_subject(actor):
    actor = normalize_actor(actor)
    if not actor["tenant_key"] or not actor["user_id"]:
        raise ServiceError("invalid_request", "后台用户身份不完整", 400)
    return actor


def owner_lock_key(actor):
    actor = require_actor_subject(actor)
    tenant_key = actor["tenant_key"]
    user_id = actor["user_id"]
    return "owner:%d:%s:%d:%s" % (len(tenant_key), tenant_key, len(user_id), user_id)


def record_event(event_type, outcome, actor=None, x_user_id="", error_code=""):
    actor = normalize_actor(actor)
    with _DB_LOCK:
        conn = db_connect()
        try:
            conn.execute(
                "INSERT INTO x_oauth_event(event_type,outcome,x_user_id,actor_user_id,actor_tenant_key,actor_name,error_code,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    clean_text(event_type, 64), clean_text(outcome, 32), clean_text(x_user_id, 64),
                    actor["user_id"], actor["tenant_key"], actor["name"], clean_text(error_code, 64), iso_utc(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def safe_record_event(event_type, outcome, actor=None, x_user_id="", error_code=""):
    try:
        record_event(event_type, outcome, actor, x_user_id=x_user_id, error_code=error_code)
    except Exception:
        # Audit failure must never reverse an already committed OAuth/token result.
        return False
    return True


def create_authorization(actor):
    require_oauth_config()
    actor = require_actor_subject(actor)
    with account_lock(owner_lock_key(actor)):
        return create_authorization_state(actor)


def create_authorization_state(actor):
    raw_state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    created = now_epoch()
    expires = created + STATE_TTL_SECONDS
    with _DB_LOCK:
        conn = db_connect()
        try:
            conn.execute("DELETE FROM x_oauth_state WHERE expires_at <= ?", (iso_utc(created),))
            conn.execute(
                "INSERT INTO x_oauth_state(state_hash,code_verifier,actor_user_id,actor_tenant_key,actor_name,actor_email,actor_role,redirect_uri,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    state_digest(raw_state), verifier, actor["user_id"], actor["tenant_key"], actor["name"], actor["email"],
                    actor["role"], callback_url(), iso_utc(created), iso_utc(expires),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    safe_record_event("authorization", "started", actor)
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": callback_url(),
            "scope": " ".join(SCOPES),
            "state": raw_state,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return {
        "authorization_url": AUTHORIZE_URL + "?" + query,
        "callback_url": callback_url(),
        "scopes": list(SCOPES),
        "expires_at": iso_utc(expires),
    }


def consume_state(raw_state):
    if not raw_state:
        raise ServiceError("invalid_request", "OAuth state缺失", 400)
    digest = state_digest(raw_state)
    with _DB_LOCK:
        conn = db_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM x_oauth_state WHERE state_hash=?", (digest,)).fetchone()
            if row:
                conn.execute("DELETE FROM x_oauth_state WHERE state_hash=?", (digest,))
            conn.commit()
        finally:
            conn.close()
    if not row or parse_iso_epoch(row["expires_at"]) <= now_epoch():
        raise ServiceError("invalid_request", "OAuth state无效、已过期或已使用", 400)
    return dict(row)


def basic_auth_header():
    raw = (CLIENT_ID + ":" + CLIENT_SECRET).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def is_x_rate_limit_payload(payload):
    if not isinstance(payload, dict):
        return False
    candidates = [payload]
    errors = payload.get("errors")
    if isinstance(errors, list):
        candidates.extend(item for item in errors if isinstance(item, dict))
    allowed_types = {
        "https://api.x.com/2/problems/usage-capped",
        "https://api.x.com/2/problems/rate-limit-exceeded",
    }
    for item in candidates:
        if str(item.get("type", "") or "").rstrip("/") in allowed_types:
            return True
        if str(item.get("code", "") or "") == "88":
            return True
    return False


def http_json(url, method="GET", headers=None, body=None, allow_revoked=False, allow_non_json=False):
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ServiceError("x_upstream_error", "X API响应过大", 502)
            if not raw:
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                if allow_non_json:
                    return {}
                raise
            if is_x_rate_limit_payload(payload):
                raise ServiceError("x_post_rate_limited", "X API触发限流或用量上限", 429)
            return payload
    except ServiceError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(64 * 1024)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        finally:
            exc.close()
        upstream_code = clean_text(payload.get("error") or payload.get("title") or "http_%s" % exc.code, 64)
        if exc.code == 429 or is_x_rate_limit_payload(payload):
            raise ServiceError("x_post_rate_limited", "X API触发限流或用量上限", 429) from None
        if allow_revoked and upstream_code.lower() in {"invalid_token", "invalid_grant", "token_revoked"}:
            return {"revoked": True}
        if exc.code in {400, 401, 403} and upstream_code.lower() in {"invalid_grant", "unauthorized", "client forbidden"}:
            raise ServiceError("x_token_revoked", "X授权已失效，请重新授权", 409) from None
        raise ServiceError("x_upstream_error", "X API请求失败（HTTP %s，%s）" % (exc.code, upstream_code), 502) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        raise ServiceError("x_upstream_error", "X API网络请求失败", 502) from None


def token_request(fields):
    require_oauth_config()
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return http_json(
        TOKEN_URL,
        method="POST",
        headers={
            "Authorization": basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        body=body,
    )


def user_request(access_token):
    if not access_token:
        raise ServiceError("x_token_missing", "X Access Token缺失", 409)
    return http_json(
        USERS_ME_URL,
        headers={"Authorization": "Bearer " + str(access_token), "Accept": "application/json"},
    )


def parse_scopes(value, fallback=()):
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        values = [str(item) for item in value]
    else:
        values = list(fallback)
    return list(dict.fromkeys(item.strip() for item in values if item and item.strip()))


def token_path(x_user_id):
    value = str(x_user_id or "")
    if not re.fullmatch(r"[0-9]{1,32}", value):
        raise ServiceError("invalid_request", "X用户ID无效", 400)
    return TOKENS_DIR / (value + ".json")


def atomic_write_bytes(path, payload):
    ensure_storage()
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def atomic_write_json(path, value):
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)


def read_token_file(x_user_id):
    path = token_path(x_user_id)
    if not path.exists():
        raise ServiceError("x_token_missing", "X账号Token不存在，请重新授权", 409)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise ServiceError("x_token_missing", "X账号Token不可用，请重新授权", 409) from None
    if not isinstance(data, dict):
        raise ServiceError("x_token_missing", "X账号Token不可用，请重新授权", 409)
    return data


def account_lock(account_id):
    key = str(account_id)
    with _ACCOUNT_LOCKS_LOCK:
        return _ACCOUNT_LOCKS.setdefault(key, threading.Lock())


def actor_from_state(state_row):
    return {
        "user_id": state_row.get("actor_user_id", ""),
        "tenant_key": state_row.get("actor_tenant_key", ""),
        "name": state_row.get("actor_name", ""),
        "email": state_row.get("actor_email", ""),
        "role": state_row.get("actor_role", ""),
    }


def status_for(scopes, access_expires_at, token=None, stored="active"):
    if stored in {"revoked", "error", "token_missing", "revoke_pending", "disabled", "disconnected"}:
        return stored, [scope for scope in REQUIRED_SCOPES if scope not in set(scopes)]
    missing = [scope for scope in REQUIRED_SCOPES if scope not in set(scopes)]
    if missing:
        return "scope_missing", missing
    if access_expires_at and parse_iso_epoch(access_expires_at) <= now_epoch():
        return ("refresh_required" if token and token.get("refresh_token") else "revoked"), []
    return "active", []


def optional_nonnegative_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or parsed > 9223372036854775807:
        return None
    return parsed


def optional_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    return None


def optional_clean_text(value, limit):
    if value is None:
        return None
    return clean_text(value, limit)


def profile_snapshot(account, previous=None, timestamp=None):
    account = account if isinstance(account, dict) else {}
    previous = previous or {}
    metrics = account.get("public_metrics")
    if not isinstance(metrics, dict):
        metrics = None

    result = {}
    for field in ("followers_count", "following_count", "tweet_count", "listed_count", "like_count", "media_count"):
        if metrics is not None and field in metrics:
            result[field] = optional_nonnegative_int(metrics.get(field))
        else:
            result[field] = previous.get(field)
    result["verified"] = optional_bool(account.get("verified")) if "verified" in account else previous.get("verified")
    result["protected"] = optional_bool(account.get("protected")) if "protected" in account else previous.get("protected")
    result["location"] = optional_clean_text(account.get("location"), 255) if "location" in account else previous.get("location")
    result["x_created_at"] = optional_clean_text(account.get("created_at"), 64) if "created_at" in account else previous.get("x_created_at")
    result["profile_synced_at"] = timestamp or iso_utc()
    return result


def same_owner(row, actor):
    actor = require_actor_subject(actor)
    return bool(
        row
        and str(row["owner_tenant_key"] or "") == actor["tenant_key"]
        and str(row["owner_user_id"] or "") == actor["user_id"]
    )


def complete_authorization(code, raw_state):
    actor = {}
    event_x_user_id = ""
    state_consumed = False
    owner_guard = None
    try:
        state_row = consume_state(raw_state)
        state_consumed = True
        actor = require_actor_subject(actor_from_state(state_row))
        owner_guard = account_lock(owner_lock_key(actor))
        owner_guard.acquire()
        token = token_request(
            {
                "code": str(code),
                "grant_type": "authorization_code",
                "redirect_uri": state_row["redirect_uri"],
                "code_verifier": state_row["code_verifier"],
            }
        )
        obtained = now_epoch()
        token["obtained_at"] = iso_utc(obtained)
        scopes = parse_scopes(token.get("scope"), fallback=SCOPES)
        token["scope"] = " ".join(scopes)
        expires = iso_utc(obtained + int(token.get("expires_in", 0) or 0)) if token.get("expires_in") else ""
        account_payload = user_request(token.get("access_token", ""))
        account = account_payload.get("data", {}) if isinstance(account_payload, dict) else {}
        x_user_id = str(account.get("id", "") or "")
        event_x_user_id = x_user_id
        token_file = token_path(x_user_id)
        with account_lock("x:" + x_user_id):
            with _DB_LOCK:
                conn = db_connect()
                try:
                    existing_row = conn.execute(
                        "SELECT * FROM x_authorized_account WHERE x_user_id=?", (x_user_id,)
                    ).fetchone()
                finally:
                    conn.close()
            if existing_row and not same_owner(existing_row, actor):
                raise ServiceError("x_account_owned_by_other", "该X账号已由其他后台用户管理", 409)
            previous_token = token_file.read_bytes() if token_file.exists() else None
            atomic_write_json(token_file, token)
            timestamp = iso_utc(obtained)
            status, _missing = status_for(scopes, expires, token, "active")
            profile = profile_snapshot(account, dict(existing_row) if existing_row else {}, timestamp)
            columns = (
                "x_user_id", "username", "display_name", "profile_image_url", "token_store_key", "token_type",
                "scopes_json", "status", "first_authorized_at", "last_authorized_at", "access_expires_at",
                "last_token_refresh_at", "last_verified_at", "last_error_at", "last_error",
                "authorized_by_user_id", "authorized_by_name", "authorized_by_email",
                "owner_tenant_key", "owner_user_id", "owner_name", "owner_email",
                "followers_count", "following_count", "tweet_count", "listed_count", "like_count", "media_count",
                "verified", "protected", "location", "x_created_at", "profile_synced_at",
                "disconnected_at", "disconnected_by_tenant_key", "disconnected_by_user_id", "disconnected_by_name",
                "created_at", "updated_at",
            )
            values = (
                x_user_id, clean_text(account.get("username", ""), 255), clean_text(account.get("name", ""), 255),
                clean_text(account.get("profile_image_url", ""), 1024), token_file.name,
                clean_text(token.get("token_type", "bearer"), 32).lower(), json.dumps(scopes), status,
                timestamp, timestamp, expires, "", timestamp, "", "", actor["user_id"], actor["name"],
                actor["email"], actor["tenant_key"], actor["user_id"], actor["name"], actor["email"],
                profile["followers_count"], profile["following_count"], profile["tweet_count"],
                profile["listed_count"], profile["like_count"], profile["media_count"], profile["verified"],
                profile["protected"], profile["location"], profile["x_created_at"], profile["profile_synced_at"],
                "", "", "", "", timestamp, timestamp,
            )
            update_columns = (
                "username", "display_name", "profile_image_url", "token_store_key", "token_type", "scopes_json",
                "status", "last_authorized_at", "access_expires_at", "last_token_refresh_at", "last_verified_at",
                "last_error_at", "last_error", "authorized_by_user_id", "authorized_by_name", "authorized_by_email",
                "followers_count", "following_count", "tweet_count", "listed_count", "like_count", "media_count",
                "verified", "protected", "location", "x_created_at", "profile_synced_at", "disconnected_at",
                "disconnected_by_tenant_key", "disconnected_by_user_id", "disconnected_by_name", "updated_at",
            )
            try:
                with _DB_LOCK:
                    conn = db_connect()
                    try:
                        conn.execute(
                            "INSERT INTO x_authorized_account(%s) VALUES(%s) ON CONFLICT(x_user_id) DO UPDATE SET %s"
                            % (
                                ",".join(columns),
                                ",".join("?" for _column in columns),
                                ",".join("%s=excluded.%s" % (column, column) for column in update_columns),
                            ),
                            values,
                        )
                        conn.commit()
                    finally:
                        conn.close()
            except Exception:
                if previous_token is None:
                    token_file.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(token_file, previous_token)
                raise
        safe_record_event("authorization", "completed", actor, x_user_id=x_user_id)
        return find_account_by_x_user_id(x_user_id)
    except ServiceError as exc:
        if state_consumed:
            safe_record_event("authorization", "failed", actor, x_user_id=event_x_user_id, error_code=exc.code)
        raise
    except Exception:
        if state_consumed:
            safe_record_event("authorization", "failed", actor, x_user_id=event_x_user_id, error_code="x_accounts_unavailable")
        raise ServiceError("x_accounts_unavailable", "X授权处理失败", 503) from None
    finally:
        if owner_guard is not None:
            owner_guard.release()


def row_to_item(row):
    item = dict(row)
    try:
        scopes = parse_scopes(json.loads(item.pop("scopes_json", "[]")))
    except (TypeError, ValueError, json.JSONDecodeError):
        scopes = []
    stored_status = item.get("status", "active")
    token = None
    terminal_statuses = {"revoke_pending", "disabled", "disconnected"}
    if stored_status not in terminal_statuses:
        try:
            token = read_token_file(item["x_user_id"])
        except ServiceError:
            pass
    status, missing = status_for(scopes, item.get("access_expires_at", ""), token, stored_status)
    if token is None and stored_status not in terminal_statuses:
        status = "token_missing"
    item["status"] = status
    item["publish_eligible"] = status == "active"
    item["scopes"] = scopes
    item["missing_scopes"] = missing
    for field in ("verified", "protected"):
        if item.get(field) is not None:
            item[field] = bool(item[field])
    username = str(item.get("username", "") or "")
    item["profile_url"] = "https://x.com/" + username if re.fullmatch(r"[A-Za-z0-9_]{1,50}", username) else ""
    item["last_profile_sync_at"] = item.get("profile_synced_at", "")
    item["owner"] = {
        "tenant_key": item.get("owner_tenant_key", ""),
        "user_id": item.get("owner_user_id", ""),
        "name": item.get("owner_name", ""),
        "email": item.get("owner_email", ""),
    }
    item.pop("token_store_key", None)
    item.pop("token_type", None)
    return item


def normalize_account_scope(actor, scope):
    actor = require_actor_subject(actor)
    scope = str(scope or "mine").strip().lower()
    if scope not in {"mine", "all"}:
        raise ServiceError("invalid_request", "X账号查询范围无效", 400)
    if scope == "all" and actor.get("role") != "admin":
        raise ServiceError("x_admin_required", "仅管理员可查看全部X账号", 403)
    return actor, scope


def list_accounts(actor, scope="mine"):
    actor, scope = normalize_account_scope(actor, scope)
    with _DB_LOCK:
        conn = db_connect()
        try:
            if scope == "all":
                rows = conn.execute("SELECT * FROM x_authorized_account ORDER BY updated_at DESC,id DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM x_authorized_account WHERE owner_tenant_key=? AND owner_user_id=? "
                    "ORDER BY updated_at DESC,id DESC",
                    (actor["tenant_key"], actor["user_id"]),
                ).fetchall()
        finally:
            conn.close()
    items = [row_to_item(row) for row in rows]
    return {"items": items, "total": len(items), "updated_at": iso_utc()}


def find_account(account_id):
    with _DB_LOCK:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM x_authorized_account WHERE id=?", (int(account_id),)).fetchone()
        finally:
            conn.close()
    if not row:
        raise ServiceError("x_account_not_found", "X账号记录不存在", 404)
    return row_to_item(row)


def find_scoped_account_row(account_id, actor, scope="mine"):
    actor, scope = normalize_account_scope(actor, scope)
    with _DB_LOCK:
        conn = db_connect()
        try:
            if scope == "all":
                row = conn.execute("SELECT * FROM x_authorized_account WHERE id=?", (int(account_id),)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM x_authorized_account WHERE id=? AND owner_tenant_key=? AND owner_user_id=?",
                    (int(account_id), actor["tenant_key"], actor["user_id"]),
                ).fetchone()
        finally:
            conn.close()
    if not row:
        raise ServiceError("x_account_not_found", "X账号记录不存在", 404)
    return row


def find_account_by_x_user_id(x_user_id):
    with _DB_LOCK:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM x_authorized_account WHERE x_user_id=?", (str(x_user_id),)).fetchone()
        finally:
            conn.close()
    if not row:
        raise ServiceError("x_account_not_found", "X账号记录不存在", 404)
    return row_to_item(row)


def update_account_error(account_id, status, error):
    timestamp = iso_utc()
    with _DB_LOCK:
        conn = db_connect()
        try:
            conn.execute(
                "UPDATE x_authorized_account SET status=?,last_error_at=?,last_error=?,updated_at=? WHERE id=?",
                (status, timestamp, clean_text(error), timestamp, int(account_id)),
            )
            conn.commit()
        finally:
            conn.close()


def verify_account(account_id, actor, scope="mine"):
    account_id = int(account_id)
    actor, scope = normalize_account_scope(actor, scope)
    initial_row = find_scoped_account_row(account_id, actor, scope)
    x_user_id = str(initial_row["x_user_id"])
    with account_lock("x:" + x_user_id):
        row = find_scoped_account_row(account_id, actor, scope)
        stored_status = str(row["status"] or "")
        if stored_status == "disabled":
            raise ServiceError("x_account_disabled", "X账号已在后台停用；重新授权后才能校验", 409)
        if stored_status == "disconnected":
            raise ServiceError("x_token_missing", "X账号已解除授权，请重新授权", 409)
        if stored_status == "revoke_pending":
            raise ServiceError("x_disconnect_pending", "X账号存在旧退出待处理状态，请先完成停用", 409)
        item = row_to_item(row)
        try:
            token = read_token_file(item["x_user_id"])
            timestamp = iso_utc()
            refresh_at = item.get("last_token_refresh_at", "")
            expires_at = item.get("access_expires_at", "")
            if not token.get("access_token") or (expires_at and parse_iso_epoch(expires_at) <= now_epoch() + 120):
                refresh_token = token.get("refresh_token")
                if not refresh_token:
                    raise ServiceError("x_token_revoked", "X Refresh Token缺失，请重新授权", 409)
                refreshed = token_request({"grant_type": "refresh_token", "refresh_token": str(refresh_token)})
                if not refreshed.get("refresh_token"):
                    refreshed["refresh_token"] = refresh_token
                if not refreshed.get("scope"):
                    refreshed["scope"] = token.get("scope", " ".join(item.get("scopes", [])))
                refreshed["obtained_at"] = timestamp
                token = refreshed
                atomic_write_json(token_path(item["x_user_id"]), token)
                refresh_at = timestamp
                expires_at = iso_utc(now_epoch() + int(token.get("expires_in", 0) or 0)) if token.get("expires_in") else ""
            account_payload = user_request(token.get("access_token", ""))
            account = account_payload.get("data", {}) if isinstance(account_payload, dict) else {}
            verified_x_user_id = str(account.get("id", "") or "")
            if not verified_x_user_id or not secrets.compare_digest(verified_x_user_id, item["x_user_id"]):
                raise ServiceError("x_identity_mismatch", "X Token账号身份不匹配，请重新授权", 409)
            scopes = parse_scopes(token.get("scope"), fallback=item.get("scopes", []))
            status, _missing = status_for(scopes, expires_at, token, "active")
            profile = profile_snapshot(account, item, timestamp)
            with _DB_LOCK:
                conn = db_connect()
                try:
                    conn.execute(
                        """
                        UPDATE x_authorized_account SET username=?,display_name=?,profile_image_url=?,scopes_json=?,status=?,
                            access_expires_at=?,last_token_refresh_at=?,last_verified_at=?,last_error_at='',last_error='',
                            followers_count=?,following_count=?,tweet_count=?,listed_count=?,like_count=?,media_count=?,
                            verified=?,protected=?,location=?,x_created_at=?,profile_synced_at=?,updated_at=?
                        WHERE id=?
                        """,
                        (
                            clean_text(account.get("username", item.get("username", "")), 255),
                            clean_text(account.get("name", item.get("display_name", "")), 255),
                            clean_text(account.get("profile_image_url", item.get("profile_image_url", "")), 1024),
                            json.dumps(scopes), status, expires_at, refresh_at, timestamp,
                            profile["followers_count"], profile["following_count"], profile["tweet_count"],
                            profile["listed_count"], profile["like_count"], profile["media_count"], profile["verified"],
                            profile["protected"], profile["location"], profile["x_created_at"],
                            profile["profile_synced_at"], timestamp, account_id,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            safe_record_event("verify", "completed", actor, x_user_id=item["x_user_id"])
            return find_account(account_id)
        except ServiceError as exc:
            state = "revoked" if exc.code == "x_token_revoked" else ("token_missing" if exc.code == "x_token_missing" else "error")
            update_account_error(account_id, state, str(exc))
            safe_record_event("verify", "failed", actor, x_user_id=item["x_user_id"], error_code=exc.code)
            raise
        except Exception:
            update_account_error(account_id, "error", "X账号校验失败")
            safe_record_event("verify", "failed", actor, x_user_id=item["x_user_id"], error_code="x_accounts_unavailable")
            raise ServiceError("x_accounts_unavailable", "X账号校验失败", 503) from None


@contextlib.contextmanager
def publish_credentials(account_id, actor, scope="mine"):
    """Yield active credentials while holding the account's publish/disable lock.

    Future X publishing code must perform the upstream publish inside this
    context. Callers must never serialize or expose the yielded token.
    """
    account_id = int(account_id)
    actor, scope = normalize_account_scope(actor, scope)
    initial_row = find_scoped_account_row(account_id, actor, scope)
    x_user_id = str(initial_row["x_user_id"])
    with account_lock("x:" + x_user_id):
        row = find_scoped_account_row(account_id, actor, scope)
        item = row_to_item(row)
        if item["status"] == "disabled":
            raise ServiceError("x_account_disabled", "X账号已在后台停用，禁止用于发布", 409)
        if item["status"] != "active":
            raise ServiceError("x_account_not_publishable", "X账号当前状态不可用于发布", 409)
        token = read_token_file(x_user_id)
        access_token = str(token.get("access_token", "") or "")
        if not access_token:
            raise ServiceError("x_token_missing", "X账号Access Token不存在，请重新授权", 409)
        yield item, access_token


def _x_posts_api():
    """Load the sibling feature when the sidecar is executed as a script."""
    repo_root = str(Path(__file__).resolve().parents[2])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from features.x_posts import XPostError, XPostStore, publish_canary
    except (ImportError, ModuleNotFoundError):
        raise ServiceError("x_posts_unavailable", "X发布服务暂不可用", 503) from None
    return XPostError, XPostStore, publish_canary


def _raise_x_post_error(exc, secrets_to_redact=()):
    code = str(getattr(exc, "code", "x_posts_unavailable") or "x_posts_unavailable")
    status = int(getattr(exc, "status", 503) or 503)
    if bool(getattr(exc, "unknown_outcome", False)):
        code, status = "x_publish_unknown", 503
    message = clean_text(str(exc))
    for secret in secrets_to_redact:
        secret = str(secret or "")
        if secret:
            message = message.replace(secret, "[redacted]")
    raise ServiceError(clean_text(code, 64), message, status) from None


def _safe_canary_result(result):
    if not isinstance(result, dict):
        raise ServiceError("x_posts_unavailable", "X发布服务返回无效", 503)
    allowed = ("status", "log_id", "short_url", "post_id", "preview_url")
    return {key: result[key] for key in allowed if key in result}


def _safe_daily_plan_result(result):
    if not isinstance(result, dict) or not isinstance(result.get("queues"), list):
        raise ServiceError("x_posts_unavailable", "X每日发布计划返回无效", 503)
    allowed_run = (
        "id", "run_date", "source_date", "status", "expected_count", "queued_count",
        "published_count", "failed_count", "unknown_count", "started_at",
        "finished_at", "created_at", "updated_at", "created",
    )
    allowed_queue = (
        "id", "run_id", "run_date", "source_date", "account_id", "account_username",
        "pool_item_id", "pool_created_at",
        "material_id", "material_name", "content_id", "material_language", "drama_name",
        "tag", "candidate_rank", "spend", "status", "created_at", "updated_at",
    )
    safe = {key: result[key] for key in allowed_run if key in result}
    safe["queues"] = [
        {key: queue[key] for key in allowed_queue if key in queue}
        for queue in result["queues"]
        if isinstance(queue, dict)
    ]
    if len(safe["queues"]) != 3:
        raise ServiceError("x_posts_unavailable", "X每日发布计划队列数量异常", 503)
    return safe


def _safe_daily_plan_query_result(result):
    """Expose only the frozen run/queue identities needed by the daily runner."""
    if not isinstance(result, dict) or not isinstance(result.get("found"), bool):
        raise ServiceError("x_posts_unavailable", "X daily plan query returned invalid data", 503)
    queues = result.get("queues")
    run = result.get("run")
    if not result["found"]:
        if run is not None or queues != []:
            raise ServiceError("x_posts_unavailable", "X daily plan query returned invalid data", 503)
        return {"found": False, "run": None, "queues": []}
    if not isinstance(run, dict) or not isinstance(queues, list):
        raise ServiceError("x_posts_unavailable", "X daily plan query returned invalid data", 503)
    allowed_run = (
        "id",
        "run_date",
        "source_date",
        "status",
        "expected_count",
        "queued_count",
        "published_count",
        "failed_count",
        "unknown_count",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )
    allowed_queue = (
        "id",
        "run_id",
        "run_date",
        "source_date",
        "account_id",
        "candidate_rank",
        "status",
        "created_at",
        "updated_at",
    )
    return {
        "found": True,
        "run": {key: run[key] for key in allowed_run if key in run},
        "queues": [
            {key: queue[key] for key in allowed_queue if key in queue}
            for queue in queues
            if isinstance(queue, dict)
        ],
    }


def _safe_run_result(result):
    if not isinstance(result, dict):
        raise ServiceError("x_posts_unavailable", "X每日发布批次返回无效", 503)
    allowed = (
        "id", "run_date", "source_date", "status", "expected_count", "queued_count",
        "published_count", "failed_count", "unknown_count", "error_code",
        "error_message", "started_at", "finished_at", "created_at", "updated_at",
        "recorded",
    )
    return {key: result[key] for key in allowed if key in result}


def ensure_x_posts_storage():
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        XPostStore(POST_DB_PATH)
    except XPostError as exc:
        _raise_x_post_error(exc)


def publish_canary_request(payload):
    """Verify one account, enqueue once, then publish while its lock is held."""
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    raw_account_id = payload.get("account_id")
    if isinstance(raw_account_id, bool):
        raise ServiceError("invalid_request", "account_id无效", 400)
    try:
        account_id = int(raw_account_id)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "account_id无效", 400) from None
    if account_id <= 0:
        raise ServiceError("invalid_request", "account_id无效", 400)

    actor = dict(CANARY_ACTOR)
    verify_account(account_id, actor, "all")
    candidate = {
        field: payload.get(field)
        for field in (
            "source_date",
            "material_id",
            "content_id",
            "material_url",
            "material_name",
            "material_language",
            "drama_name",
            "tag",
            "description",
        )
    }
    XPostError, XPostStore, publish_canary = _x_posts_api()
    with publish_credentials(account_id, actor, "all") as (account, access_token):
        candidate.update(
            {
                "account_id": int(account["id"]),
                "account_username": str(account.get("username", "") or ""),
                "page_name": str(
                    account.get("display_name", "") or account.get("username", "") or ""
                ),
                "page_id": str(account.get("x_user_id", "") or ""),
            }
        )
        try:
            queued = XPostStore(POST_DB_PATH).enqueue(candidate)
            if isinstance(queued, dict):
                queue_id = queued.get("id")
            else:
                queue_id = getattr(queued, "id", queued)
            try:
                queue_id = int(queue_id)
            except (TypeError, ValueError, OverflowError):
                raise ServiceError("x_posts_unavailable", "X发布队列返回无效", 503) from None
            result = publish_canary(
                db_path=POST_DB_PATH,
                queue_id=queue_id,
                account=account,
                access_token=access_token,
                public_root=POST_PUBLIC_ROOT,
                short_base_url=POST_SHORT_BASE_URL,
                allowed_media_hosts=POST_MEDIA_ALLOWED_HOSTS,
                timeout=POST_HTTP_TIMEOUT_SECONDS,
                max_media_bytes=POST_MAX_MEDIA_BYTES,
                storage_guard=preflight_post_storage_request,
                durable_storage={
                    "mount_root": POST_STORAGE_MOUNT_ROOT,
                    "storage_root": POST_STORAGE_ROOT,
                },
            )
        except XPostError as exc:
            _raise_x_post_error(exc, (access_token,))
    return _safe_canary_result(result)


def _daily_account_scope(allowed_account_ids):
    if allowed_account_ids is None:
        return None
    try:
        values = tuple(int(value) for value in allowed_account_ids)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("x_daily_scope_invalid", "X每日发布账号范围配置无效", 503) from None
    if (
        len(values) != 3
        or len(set(values)) != 3
        or any(value <= 0 for value in values)
    ):
        raise ServiceError("x_daily_scope_invalid", "X每日发布账号范围配置无效", 503)
    return frozenset(values)


def create_daily_plan_request(
    payload, allowed_account_ids=None, require_pool=False
):
    """Freeze three trusted account identities and candidates in one transaction."""
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ServiceError("invalid_request", "candidates必须为数组", 400)
    allowed_accounts = _daily_account_scope(allowed_account_ids)
    requested_accounts = []
    for raw in candidates:
        if not isinstance(raw, dict):
            raise ServiceError("invalid_request", "candidate必须为对象", 400)
        raw_account_id = raw.get("account_id")
        if isinstance(raw_account_id, bool):
            raise ServiceError("invalid_request", "account_id无效", 400)
        try:
            requested_accounts.append(int(raw_account_id))
        except (TypeError, ValueError, OverflowError):
            raise ServiceError("invalid_request", "account_id无效", 400) from None
    if allowed_accounts is not None and (
        len(requested_accounts) != 3
        or len(set(requested_accounts)) != 3
        or frozenset(requested_accounts) != allowed_accounts
    ):
        raise ServiceError(
            "x_daily_account_scope_denied",
            "X每日发布计划只能使用配置的三个账号",
            403,
        )
    trusted = []
    for raw, account_id in zip(candidates, requested_accounts):
        account = find_account(account_id)
        if account.get("status") != "active" or not account.get("publish_eligible"):
            raise ServiceError("x_account_not_publishable", "X账号当前状态不可用于发布", 409)
        candidate = dict(raw)
        candidate.update(
            {
                "account_id": int(account["id"]),
                "account_username": str(account.get("username", "") or ""),
                "page_name": str(
                    account.get("display_name", "") or account.get("username", "") or ""
                ),
                "page_id": str(account.get("x_user_id", "") or ""),
            }
        )
        trusted.append(candidate)
    # This check is intentionally adjacent to the SQLite plan transaction.
    # A lost mount must never allow a formal daily reservation to be created.
    preflight_post_storage_request()
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        result = XPostStore(POST_DB_PATH).create_daily_plan(
            payload.get("run_date"),
            payload.get("source_date"),
            trusted,
            require_pool=bool(require_pool),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return _safe_daily_plan_result(result)


def query_daily_plan_request(payload, allowed_account_ids=None):
    """Read a frozen daily plan without exposing post copy, URLs, or credentials."""
    if not isinstance(payload, dict) or set(payload) != {"run_date"}:
        raise ServiceError("invalid_request", "run_date is required", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        result = XPostStore(POST_DB_PATH).query_daily_plan(payload.get("run_date"))
    except XPostError as exc:
        _raise_x_post_error(exc)
    safe = _safe_daily_plan_query_result(result)
    allowed_accounts = _daily_account_scope(allowed_account_ids)
    if allowed_accounts is not None and safe["found"]:
        queue_accounts = {
            int(queue.get("account_id") or 0)
            for queue in safe["queues"]
        }
        if not queue_accounts.issubset(allowed_accounts):
            raise ServiceError(
                "x_daily_account_scope_denied",
                "X daily plan contains an account outside the configured scope",
                403,
            )
    return safe


def preflight_post_storage_request():
    try:
        from features.x_posts import preflight_post_storage
    except (ImportError, ModuleNotFoundError):
        raise ServiceError("x_posts_unavailable", "X发布服务暂不可用", 503) from None
    try:
        return preflight_post_storage(
            POST_PUBLIC_ROOT,
            mount_root=POST_STORAGE_MOUNT_ROOT,
            storage_root=POST_STORAGE_ROOT,
            minimum_free_bytes=(POST_MAX_MEDIA_BYTES * 3) + (64 * 1024 * 1024),
        )
    except Exception as exc:
        XPostError, _XPostStore, _publish_canary = _x_posts_api()
        if isinstance(exc, XPostError):
            _raise_x_post_error(exc)
        raise


def publish_queue_request(queue_id, allowed_account_ids=None):
    """Publish one frozen queue row; no request field can override its account or copy."""
    XPostError, XPostStore, publish_canary = _x_posts_api()
    try:
        store = XPostStore(POST_DB_PATH)
        queue = store.get_queue(queue_id)
        allowed_accounts = _daily_account_scope(allowed_account_ids)
        if allowed_accounts is not None and (
            int(queue.get("account_id") or 0) not in allowed_accounts
            or int(queue.get("run_id") or 0) <= 0
        ):
            raise ServiceError(
                "x_daily_account_scope_denied",
                "X每日发布只能处理配置账号的正式日更队列",
                403,
            )
        log = store.reserve_log(queue["id"])
    except ServiceError:
        raise
    except XPostError as exc:
        _raise_x_post_error(exc)
    if log["status"] == "published":
        return _safe_canary_result(
            {
                "status": "published",
                "log_id": int(log["id"]),
                "short_url": log["short_url"],
                "post_id": log["x_post_id"],
                "preview_url": log["x_post_url"],
            }
        )
    if log["status"] != "reserved":
        unknown = bool(log["unknown_outcome"]) or log["status"] == "post_creating"
        code = "x_post_unknown_outcome" if unknown else "x_post_retry_requires_review"
        _raise_x_post_error(
            XPostError(
                code,
                "发布日志已执行，禁止自动重复发帖",
                409,
                unknown,
            )
        )

    account_id = int(queue["account_id"])
    actor = dict(CANARY_ACTOR)
    try:
        verify_account(account_id, actor, "all")
    except ServiceError as exc:
        try:
            store.mark_failed_if_reserved(log["id"], exc.code, str(exc))
        except XPostError as storage_exc:
            _raise_x_post_error(storage_exc)
        raise

    try:
        with publish_credentials(account_id, actor, "all") as (account, access_token):
            try:
                result = publish_canary(
                    db_path=POST_DB_PATH,
                    queue_id=int(queue["id"]),
                    account=account,
                    access_token=access_token,
                    public_root=POST_PUBLIC_ROOT,
                    short_base_url=POST_SHORT_BASE_URL,
                    allowed_media_hosts=POST_MEDIA_ALLOWED_HOSTS,
                    timeout=POST_HTTP_TIMEOUT_SECONDS,
                    max_media_bytes=POST_MAX_MEDIA_BYTES,
                    storage_guard=preflight_post_storage_request,
                    durable_storage={
                        "mount_root": POST_STORAGE_MOUNT_ROOT,
                        "storage_root": POST_STORAGE_ROOT,
                    },
                )
            except XPostError as exc:
                _raise_x_post_error(exc, (access_token,))
    except ServiceError as exc:
        try:
            store.mark_failed_if_reserved(log["id"], exc.code, str(exc))
        except XPostError as storage_exc:
            _raise_x_post_error(storage_exc)
        raise
    return _safe_canary_result(result)


def _admin_post_query(payload, method_name):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    _actor, scope = normalize_account_scope(payload.get("actor", {}), payload.get("scope", "all"))
    if scope != "all":
        raise ServiceError("x_admin_required", "仅管理员可查看X发布日志", 403)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        store = XPostStore(POST_DB_PATH)
        return getattr(store, method_name)(payload)
    except XPostError as exc:
        _raise_x_post_error(exc)


def query_post_logs_request(payload):
    return _admin_post_query(payload, "query_logs")


def query_post_runs_request(payload):
    return _admin_post_query(payload, "query_runs")


POST_MATERIAL_POOL_NAVIGATION_ITEM = "xPostMaterialPool"


def _material_pool_actor(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    actor = require_actor_subject(payload.get("actor", {}))
    scope = str(payload.get("scope", "all") or "all").strip().lower()
    if scope != "all":
        raise ServiceError("x_admin_required", "仅授权用户可维护X素材池", 403)
    navigation_item = clean_text(payload.get("navigation_item", ""), 64)
    if (
        actor.get("role") != "admin"
        and navigation_item != POST_MATERIAL_POOL_NAVIGATION_ITEM
    ):
        raise ServiceError("x_admin_required", "仅授权用户可维护X素材池", 403)
    return actor


def query_post_material_pool_request(payload):
    _material_pool_actor(payload)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        query = dict(payload)
        query.pop("navigation_item", None)
        return XPostStore(POST_DB_PATH).query_pool(query)
    except XPostError as exc:
        _raise_x_post_error(exc)


def add_post_material_pool_request(payload):
    actor = _material_pool_actor(payload)
    material_ids = payload.get("material_ids")
    if material_ids is None and payload.get("material_id") not in (None, ""):
        material_ids = [payload.get("material_id")]
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return XPostStore(POST_DB_PATH).add_pool_materials(
            material_ids,
            actor,
            validation_checks=payload.get("validation_checks"),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)


def delete_post_material_pool_request(payload, pool_item_id):
    _material_pool_actor(payload)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return XPostStore(POST_DB_PATH).delete_pool_material(pool_item_id)
    except XPostError as exc:
        _raise_x_post_error(exc)


def available_post_material_pool_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return {
            "items": XPostStore(POST_DB_PATH).available_pool_items(
                payload.get("limit", 50)
            )
        }
    except XPostError as exc:
        _raise_x_post_error(exc)


def record_post_material_pool_checks_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return XPostStore(POST_DB_PATH).record_pool_checks(payload.get("checks"))
    except XPostError as exc:
        _raise_x_post_error(exc)


def record_post_run_failure_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).record_run_failure(
            payload.get("run_date"),
            payload.get("source_date"),
            payload.get("error_code"),
            payload.get("error_message") or payload.get("message"),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return _safe_run_result(item)


def query_post_material_keys_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    values = payload.get("material_keys")
    if values is None:
        values = payload.get("material_ids")
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        material_keys = XPostStore(POST_DB_PATH).query_material_keys(values)
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"material_keys": material_keys}


def delete_token_artifacts(x_user_id):
    """Delete the live token and any legacy disconnect tombstones for one X user."""
    token_file = token_path(x_user_id)
    candidates = [token_file]
    candidates.extend(TOKENS_DIR.glob(".%s.*.disconnecting" % token_file.name))
    for candidate in candidates:
        try:
            candidate.unlink(missing_ok=True)
        except TypeError:  # Python 3.7 compatibility for Path.unlink(missing_ok=...).
            if candidate.exists():
                candidate.unlink()


def cleanup_disconnected_token_artifacts():
    """One-time startup cleanup for credentials left by an interrupted old logout."""
    with _DB_LOCK:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT x_user_id FROM x_authorized_account WHERE status='disconnected'"
            ).fetchall()
        finally:
            conn.close()
    for row in rows:
        x_user_id = str(row["x_user_id"])
        with account_lock("x:" + x_user_id):
            delete_token_artifacts(x_user_id)


def logout_account(account_id, actor):
    account_id = int(account_id)
    actor = require_actor_subject(actor)
    with account_lock(owner_lock_key(actor)):
        return logout_account_for_owner(account_id, actor)


def logout_account_for_owner(account_id, actor):
    initial_row = find_scoped_account_row(account_id, actor, "mine")
    x_user_id = str(initial_row["x_user_id"])
    with account_lock("x:" + x_user_id):
        row = find_scoped_account_row(account_id, actor, "mine")
        current_status = str(row["status"] or "")
        if current_status == "disabled":
            return row_to_item(row)
        if current_status == "disconnected":
            try:
                delete_token_artifacts(x_user_id)
            except OSError:
                safe_record_event("logout", "failed", actor, x_user_id=x_user_id, error_code="x_disconnect_failed")
                raise ServiceError("x_disconnect_failed", "X历史授权凭证清理失败，请稍后重试", 502) from None
            return row_to_item(row)

        timestamp = iso_utc()
        try:
            with _DB_LOCK:
                conn = db_connect()
                try:
                    cursor = conn.execute(
                        """
                        UPDATE x_authorized_account SET status='disabled',
                            last_error_at='',last_error='',disconnected_at=?,disconnected_by_tenant_key=?,
                            disconnected_by_user_id=?,disconnected_by_name=?,updated_at=?
                        WHERE id=? AND owner_tenant_key=? AND owner_user_id=?
                        """,
                        (
                            timestamp, actor["tenant_key"], actor["user_id"], actor["name"], timestamp,
                            account_id, actor["tenant_key"], actor["user_id"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ServiceError("x_account_not_found", "X账号记录不存在", 404)
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:
            safe_record_event("logout", "failed", actor, x_user_id=x_user_id, error_code="x_accounts_unavailable")
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError("x_accounts_unavailable", "X账号停用状态保存失败，请重试", 503) from None
        safe_record_event("logout", "completed", actor, x_user_id=x_user_id)
        return find_account(account_id)


def config_payload():
    required_complete = all(scope in set(SCOPES) for scope in REQUIRED_SCOPES)
    return {
        "configured": bool(CLIENT_ID and CLIENT_SECRET and required_complete),
        "callback_url": callback_url(),
        "scopes": list(SCOPES),
        "required_scopes": list(REQUIRED_SCOPES),
        "state_ttl_seconds": STATE_TTL_SECONDS,
    }


def redirect_target(success, reason=""):
    parts = urllib.parse.urlsplit(ADMIN_RETURN_URL)
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    params.append(("oauth", "success" if success else "error"))
    if reason and not success:
        params.append(("reason", clean_text(reason, 64)))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(params), ""))


class Handler(BaseHTTPRequestHandler):
    server_version = "XAccountsSidecar/2.0"

    def log_message(self, _format, *_args):
        path = urllib.parse.urlsplit(self.path).path
        sys.stderr.write("%s - %s %s\n" % (self.address_string(), self.command, path))

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status, text):
        body = str(text).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, target):
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def internal_role(self):
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                return ""
        except ValueError:
            return ""
        supplied = str(self.headers.get("Authorization", "") or "")
        prefix = "Bearer "
        if not supplied.startswith(prefix):
            return ""
        token = supplied[len(prefix):]
        if INTERNAL_TOKEN and secrets.compare_digest(token, INTERNAL_TOKEN):
            return "backend"
        if DAILY_INTERNAL_TOKEN and secrets.compare_digest(token, DAILY_INTERNAL_TOKEN):
            return "daily"
        return ""

    def is_internal_authorized(self):
        """Backward-compatible boolean used by older diagnostics."""
        return bool(self.internal_role())

    def require_internal(self, allow_daily=False):
        role = self.internal_role()
        if role == "backend" or (allow_daily and role == "daily"):
            return role
        self.send_json(403, {"error": "x_internal_auth_failed", "message": "内部鉴权失败"})
        return ""

    def read_json(self, max_body_bytes=MAX_BODY_BYTES):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            raise ServiceError("invalid_request", "Content-Length无效", 400) from None
        if length < 0 or length > int(max_body_bytes):
            raise ServiceError("invalid_request", "请求体过大", 413)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise ServiceError("invalid_request", "JSON请求体无效", 400) from None
        if not isinstance(payload, dict):
            raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
        return payload

    def send_service_error(self, exc, include_write_outcome=False):
        code = exc.code if isinstance(exc, ServiceError) else "x_accounts_unavailable"
        status = exc.status if isinstance(exc, ServiceError) else 503
        message = clean_text(str(exc) if isinstance(exc, ServiceError) else "X账号服务暂不可用")
        payload = {"error": code, "message": message}
        if include_write_outcome:
            unknown = code == "x_publish_unknown"
            payload["unknown_outcome"] = unknown
            payload["outcome_known"] = not unknown
        self.send_json(status, payload)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health":
            self.send_text(200, "ok\n")
            return
        if parsed.path == "/callback":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            raw_state = params.get("state", [""])[0]
            if params.get("error"):
                actor = {}
                state_consumed = False
                try:
                    if raw_state:
                        actor = actor_from_state(consume_state(raw_state))
                        state_consumed = True
                except ServiceError:
                    pass
                if state_consumed:
                    safe_record_event("authorization", "failed", actor, error_code="oauth_denied")
                self.send_redirect(redirect_target(False, "oauth_denied"))
                return
            code = params.get("code", [""])[0]
            if not code or not raw_state:
                actor = {}
                state_consumed = False
                if raw_state:
                    try:
                        actor = actor_from_state(consume_state(raw_state))
                        state_consumed = True
                    except ServiceError:
                        pass
                if state_consumed:
                    safe_record_event("authorization", "failed", actor, error_code="invalid_request")
                self.send_redirect(redirect_target(False, "invalid_request"))
                return
            try:
                complete_authorization(code, raw_state)
                self.send_redirect(redirect_target(True))
            except ServiceError as exc:
                self.send_redirect(redirect_target(False, exc.code))
            return
        if parsed.path == "/internal/config":
            if self.require_internal():
                try:
                    self.send_json(200, config_payload())
                except Exception:
                    self.send_service_error(ServiceError("x_accounts_unavailable", "X账号服务暂不可用", 503))
            return
        self.send_text(404, "not found\n")

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith("/internal/"):
            self.send_text(404, "not found\n")
            return
        daily_verify_match = re.fullmatch(
            r"/internal/posts/accounts/([0-9]+)/verify", parsed.path
        )
        daily_publish_match = re.fullmatch(
            r"/internal/posts/queue/([0-9]+)/publish", parsed.path
        )
        pool_delete_match = re.fullmatch(
            r"/internal/posts/material-pool/([0-9]+)/delete", parsed.path
        )
        daily_exact_paths = {
            "/internal/posts/daily-plan",
            "/internal/posts/daily-plan/query",
            "/internal/posts/storage/preflight",
            "/internal/posts/runs/record-failure",
            "/internal/posts/material-keys/query",
            "/internal/posts/material-pool/available",
            "/internal/posts/material-pool/check",
        }
        allow_daily = bool(
            parsed.path in daily_exact_paths
            or daily_verify_match
            or daily_publish_match
        )
        internal_role = self.require_internal(allow_daily=allow_daily)
        if not internal_role:
            return
        try:
            payload = self.read_json(
                MAX_DAILY_PLAN_BODY_BYTES
                if parsed.path == "/internal/posts/daily-plan"
                else MAX_BODY_BYTES
            )
            if parsed.path == "/internal/posts/canary":
                self.send_json(200, {"item": publish_canary_request(payload)})
                return
            if parsed.path == "/internal/posts/daily-plan":
                if internal_role == "daily":
                    plan = create_daily_plan_request(
                        payload,
                        DAILY_ACCOUNT_IDS,
                        require_pool=True,
                    )
                else:
                    plan = create_daily_plan_request(payload)
                self.send_json(
                    200,
                    {"item": plan},
                )
                return
            if parsed.path == "/internal/posts/daily-plan/query":
                self.send_json(
                    200,
                    {
                        "item": query_daily_plan_request(
                            payload,
                            DAILY_ACCOUNT_IDS
                            if internal_role == "daily"
                            else None,
                        )
                    },
                )
                return
            if parsed.path == "/internal/posts/storage/preflight":
                self.send_json(200, {"item": preflight_post_storage_request()})
                return
            if parsed.path == "/internal/posts/logs/query":
                self.send_json(200, query_post_logs_request(payload))
                return
            if parsed.path == "/internal/posts/runs/query":
                self.send_json(200, query_post_runs_request(payload))
                return
            if parsed.path == "/internal/posts/runs/record-failure":
                self.send_json(200, {"item": record_post_run_failure_request(payload)})
                return
            if parsed.path == "/internal/posts/material-keys/query":
                self.send_json(200, {"item": query_post_material_keys_request(payload)})
                return
            if parsed.path == "/internal/posts/material-pool/available":
                self.send_json(200, available_post_material_pool_request(payload))
                return
            if parsed.path == "/internal/posts/material-pool/check":
                self.send_json(
                    200,
                    {"item": record_post_material_pool_checks_request(payload)},
                )
                return
            if parsed.path == "/internal/posts/material-pool/query":
                self.send_json(200, query_post_material_pool_request(payload))
                return
            if parsed.path == "/internal/posts/material-pool/add":
                self.send_json(200, add_post_material_pool_request(payload))
                return
            if pool_delete_match:
                self.send_json(
                    200,
                    {
                        "item": delete_post_material_pool_request(
                            payload,
                            pool_delete_match.group(1),
                        )
                    },
                )
                return
            if daily_verify_match:
                account_id = int(daily_verify_match.group(1))
                allowed_accounts = _daily_account_scope(DAILY_ACCOUNT_IDS)
                if internal_role == "daily" and account_id not in allowed_accounts:
                    raise ServiceError(
                        "x_daily_account_scope_denied",
                        "X每日发布只能校验配置的三个账号",
                        403,
                    )
                self.send_json(
                    200,
                    {
                        "item": verify_account(
                            account_id,
                            {
                                "tenant_key": "internal",
                                "user_id": "x-post-daily",
                                "name": "X Post Daily",
                                "email": "",
                                "role": "admin",
                            },
                            "all",
                        )
                    },
                )
                return
            if daily_publish_match:
                if internal_role == "daily":
                    published = publish_queue_request(
                        daily_publish_match.group(1),
                        DAILY_ACCOUNT_IDS,
                    )
                else:
                    published = publish_queue_request(daily_publish_match.group(1))
                self.send_json(
                    200,
                    {"item": published},
                )
                return
            if parsed.path == "/internal/authorize":
                self.send_json(200, create_authorization(payload.get("actor", {})))
                return
            if parsed.path == "/internal/accounts/query":
                self.send_json(
                    200,
                    list_accounts(payload.get("actor", {}), payload.get("scope", "mine")),
                )
                return
            match = re.fullmatch(r"/internal/accounts/([0-9]+)/verify", parsed.path)
            if match:
                self.send_json(
                    200,
                    {
                        "item": verify_account(
                            match.group(1), payload.get("actor", {}), payload.get("scope", "mine")
                        )
                    },
                )
                return
            match = re.fullmatch(r"/internal/accounts/([0-9]+)/logout", parsed.path)
            if match:
                self.send_json(200, {"item": logout_account(match.group(1), payload.get("actor", {}))})
                return
            self.send_text(404, "not found\n")
        except ServiceError as exc:
            self.send_service_error(
                exc,
                include_write_outcome=bool(
                    daily_publish_match
                    or parsed.path == "/internal/posts/daily-plan"
                ),
            )
        except Exception:
            self.send_service_error(ServiceError("x_accounts_unavailable", "X账号服务暂不可用", 503))


def serve():
    if LISTEN_HOST not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("X sidecar must listen on loopback")
    if not INTERNAL_TOKEN:
        raise RuntimeError("X_INTERNAL_TOKEN is required")
    if not DAILY_INTERNAL_TOKEN:
        raise RuntimeError("X_POST_DAILY_INTERNAL_TOKEN is required")
    if secrets.compare_digest(INTERNAL_TOKEN, DAILY_INTERNAL_TOKEN):
        raise RuntimeError("daily and backend internal tokens must be different")
    if len(DAILY_ACCOUNT_IDS) != 3:
        raise RuntimeError("X_POST_DAILY_ACCOUNT_IDS must contain three unique positive IDs")
    require_oauth_config()
    os.umask(0o077)
    ensure_storage()
    ensure_x_posts_storage()
    cleanup_disconnected_token_artifacts()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    print("X account sidecar listening on %s:%s" % (LISTEN_HOST, LISTEN_PORT), flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="X OAuth account sidecar")
    parser.add_argument("command", choices=("serve",), nargs="?", default="serve")
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
