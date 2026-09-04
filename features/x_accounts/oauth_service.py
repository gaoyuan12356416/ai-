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
import math
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from features.x_accounts.language import (  # noqa: E402
    DEFAULT_DRAMA_LANGUAGE,
    canonical_drama_language,
    same_drama_language,
)


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
    "subscription_type",
)
PREMIUM_SUBSCRIPTION_TYPES = frozenset({"basic", "premium", "premium_plus"})
USERS_ME_URL = "https://api.x.com/2/users/me?" + urllib.parse.urlencode(
    {"user.fields": ",".join(USER_FIELDS)}
)
MAX_BODY_BYTES = 16 * 1024
MAX_DAILY_PLAN_BODY_BYTES = 2 * 1024 * 1024
MAX_DAILY_CHECK_BODY_BYTES = 128 * 1024
MAX_DRAMA_POOL_BODY_BYTES = 5 * 1024 * 1024
MAX_ERROR_TEXT = 240
MAX_DAILY_ACCOUNTS = 50
AUTO_TEMPLATE_MAX_DURATION_SECONDS = 600.0
TRANSIENT_VERIFY_ERROR_CODES = frozenset(
    {
        "x_post_rate_limited",
        "x_upstream_error",
        "x_accounts_unavailable",
    }
)
EXPLICITLY_DISABLED_STATUSES = frozenset(
    {"disabled", "disconnected", "revoke_pending"}
)
TOKEN_INVALID_LOGIN_MESSAGE = "Token失效，请重新登陆"


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


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    # Invalid feature-flag values fail closed instead of silently enabling a
    # new publishing route.
    return False


load_env_file(os.environ.get("X_POST_ENV_FILE", DEFAULT_ENV_FILE))

CLIENT_ID = os.environ.get("X_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "").strip()
INTERNAL_TOKEN = (
    os.environ.get("X_INTERNAL_TOKEN", "").strip()
    or os.environ.get("X_POST_AUTOMATION_INTERNAL_TOKEN", "").strip()
)
DAILY_INTERNAL_TOKEN = os.environ.get("X_POST_DAILY_INTERNAL_TOKEN", "").strip()
AUTO_INTERNAL_TOKEN = os.environ.get(
    "X_POST_AUTO_INTERNAL_TOKEN",
    "",
).strip()
DAILY_ACCOUNT_IDS = env_positive_int_tuple("X_POST_DAILY_ACCOUNT_IDS")
CATCHUP_REASON_SCOPE_EXPANSION = "scope_expansion_v1"
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
    "X_POST_SHORT_BASE_URL", "https://gy.g2flow.com/s2l"
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
POST_DRAMA_DURATION_ROUTING_ENABLED = env_bool(
    "X_POST_DRAMA_DURATION_ROUTING_ENABLED", False
)

CANARY_ACTOR = {
    "tenant_key": "internal",
    "user_id": "x-post-canary",
    "name": "X Post Canary",
    "email": "",
    "role": "admin",
}

AUTO_TEMPLATE_ACTOR = {
    "tenant_key": "internal",
    "user_id": "x-post-auto-template",
    "name": "X Post Auto Template",
    "email": "",
    "role": "admin",
}
AUTO_TEMPLATE_ACTOR_LABEL = "x_auto_post_service"

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
                    publish_approved INTEGER NOT NULL DEFAULT 0,
                    drama_language TEXT NOT NULL DEFAULT 'en',
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
                    subscription_type TEXT NOT NULL DEFAULT 'unknown',
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
                "subscription_type": "TEXT NOT NULL DEFAULT 'unknown'",
                "location": "TEXT",
                "x_created_at": "TEXT",
                "profile_synced_at": "TEXT NOT NULL DEFAULT ''",
                "disconnected_at": "TEXT NOT NULL DEFAULT ''",
                "disconnected_by_tenant_key": "TEXT NOT NULL DEFAULT ''",
                "disconnected_by_user_id": "TEXT NOT NULL DEFAULT ''",
                "disconnected_by_name": "TEXT NOT NULL DEFAULT ''",
                "publish_approved": "INTEGER NOT NULL DEFAULT 0",
                "drama_language": "TEXT NOT NULL DEFAULT 'en'",
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


def atomic_write_owner(path):
    parent_stat = path.parent.stat()
    return parent_stat.st_uid, parent_stat.st_gid


def atomic_write_bytes(path, payload):
    ensure_storage()
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            # A privileged maintenance process may import this module. Keep the
            # replacement owned like the token directory instead of silently
            # replacing a service-owned credential with a root-owned 0600 file.
            fchown = getattr(os, "fchown", None)
            if callable(fchown):
                owner_uid, owner_gid = atomic_write_owner(path)
                temporary_stat = os.fstat(handle.fileno())
                if (
                    temporary_stat.st_uid != owner_uid
                    or temporary_stat.st_gid != owner_gid
                ):
                    fchown(handle.fileno(), owner_uid, owner_gid)
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
        # A long upload refreshes credentials on the same thread between X
        # requests while retaining exclusion against disable/refresh threads.
        return _ACCOUNT_LOCKS.setdefault(key, threading.RLock())


def actor_from_state(state_row):
    return {
        "user_id": state_row.get("actor_user_id", ""),
        "tenant_key": state_row.get("actor_tenant_key", ""),
        "name": state_row.get("actor_name", ""),
        "email": state_row.get("actor_email", ""),
        "role": state_row.get("actor_role", ""),
    }


def status_for(scopes, access_expires_at, token=None, stored="active"):
    scope_set = set(scopes)
    missing = [scope for scope in REQUIRED_SCOPES if scope not in scope_set]
    if stored in EXPLICITLY_DISABLED_STATUSES:
        return stored, missing
    if stored in {"revoked", "error", "token_missing"}:
        return stored, missing
    if missing:
        return "scope_missing", missing
    if not isinstance(token, dict):
        return "token_missing", []
    refreshable = bool(
        "offline.access" in scope_set
        and str(token.get("refresh_token", "") or "").strip()
    )
    access_token = str(token.get("access_token", "") or "").strip()
    expired = bool(
        access_expires_at
        and parse_iso_epoch(access_expires_at) <= now_epoch()
    )
    if expired and not refreshable:
        return "expired", []
    if not access_token and not refreshable:
        return "token_missing", []
    return "active", []


def access_token_is_expired(access_expires_at, *, at_epoch=None):
    if not access_expires_at:
        return False
    current = now_epoch() if at_epoch is None else int(at_epoch)
    return parse_iso_epoch(access_expires_at) <= current


def token_refresh_available(scopes, token):
    return bool(
        isinstance(token, dict)
        and "offline.access" in set(scopes or ())
        and str(token.get("refresh_token", "") or "").strip()
    )


def access_token_refresh_required(
    access_expires_at,
    token,
    *,
    leeway_seconds=120,
    at_epoch=None,
):
    current = now_epoch() if at_epoch is None else int(at_epoch)
    if not isinstance(token, dict) or not str(
        token.get("access_token", "") or ""
    ).strip():
        return True
    if not access_expires_at:
        return False
    return parse_iso_epoch(access_expires_at) <= current + int(
        leeway_seconds
    )


def access_token_status(access_expires_at, token, *, refreshable=False):
    access_token = bool(
        isinstance(token, dict)
        and str(token.get("access_token", "") or "").strip()
    )
    if not access_token:
        return "missing_refreshable" if refreshable else "missing"
    if access_token_is_expired(access_expires_at):
        return "expired_refreshable" if refreshable else "expired"
    return "valid"


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


def normalize_subscription_type(value):
    """Normalize X's token-scoped Premium entitlement to a fail-closed value."""
    normalized = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    aliases = {
        "none": "none",
        "basic": "basic",
        "premium": "premium",
        "premiumplus": "premium_plus",
        "premium_plus": "premium_plus",
        "premium+": "premium_plus",
    }
    return aliases.get(normalized, "unknown")


def is_premium_subscriber(value):
    return normalize_subscription_type(value) in PREMIUM_SUBSCRIPTION_TYPES


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
    # /users/me is authenticated. Missing or unrecognized entitlement data must
    # not retain an older Premium value because that could authorize a long post.
    result["subscription_type"] = normalize_subscription_type(
        account.get("subscription_type")
        if "subscription_type" in account
        else None
    )
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
            previous_token = None
            if token_file.exists():
                try:
                    previous_token = token_file.read_bytes()
                except OSError:
                    # Completing OAuth for the same owner is an explicit
                    # credential replacement. An unreadable stale token must
                    # not discard the newly exchanged credential; if the DB
                    # commit later fails, the new file is removed below.
                    previous_token = None
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
                "verified", "protected", "subscription_type", "location", "x_created_at", "profile_synced_at",
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
                profile["protected"], profile["subscription_type"], profile["location"], profile["x_created_at"],
                profile["profile_synced_at"],
                "", "", "", "", timestamp, timestamp,
            )
            update_columns = (
                "username", "display_name", "profile_image_url", "token_store_key", "token_type", "scopes_json",
                "status", "last_authorized_at", "access_expires_at", "last_token_refresh_at", "last_verified_at",
                "last_error_at", "last_error", "authorized_by_user_id", "authorized_by_name", "authorized_by_email",
                "followers_count", "following_count", "tweet_count", "listed_count", "like_count", "media_count",
                "verified", "protected", "subscription_type", "location", "x_created_at", "profile_synced_at", "disconnected_at",
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
    raw_drama_language = item.get(
        "drama_language", DEFAULT_DRAMA_LANGUAGE
    )
    try:
        item["drama_language"] = canonical_drama_language(
            raw_drama_language
        )
    except ValueError:
        # Keep corrupt/manual values visible to administrators and invalid to
        # routing checks.  Silently coercing them to ``en`` could misroute a
        # Post, whereas the additive migration itself always writes valid en.
        item["drama_language"] = str(raw_drama_language or "")
    try:
        scopes = parse_scopes(json.loads(item.pop("scopes_json", "[]")))
    except (TypeError, ValueError, json.JSONDecodeError):
        scopes = []
    stored_status = item.get("status", "active")
    token = None
    terminal_statuses = EXPLICITLY_DISABLED_STATUSES
    if stored_status not in terminal_statuses:
        try:
            token = read_token_file(item["x_user_id"])
        except ServiceError:
            pass
    status, missing = status_for(scopes, item.get("access_expires_at", ""), token, stored_status)
    publish_approved = bool(int(item.get("publish_approved") or 0))
    refresh_token_available = token_refresh_available(scopes, token)
    authorization_refreshable = bool(
        status == "active" and refresh_token_available
    )
    item["status"] = status
    item["publish_approved"] = publish_approved
    item["access_token_expired"] = access_token_is_expired(
        item.get("access_expires_at", "")
    )
    item["refresh_token_available"] = refresh_token_available
    item["authorization_refreshable"] = authorization_refreshable
    item["access_token_status"] = access_token_status(
        item.get("access_expires_at", ""),
        token,
        refreshable=authorization_refreshable,
    )
    item["credential_publish_eligible"] = status == "active"
    item["publish_eligible"] = (
        status == "active" and publish_approved
    )
    item["subscription_type"] = normalize_subscription_type(
        item.get("subscription_type")
    )
    item["premium_subscriber"] = is_premium_subscriber(
        item["subscription_type"]
    )
    item["long_video_eligible"] = item["premium_subscriber"]
    item["long_video_publish_eligible"] = (
        item["publish_eligible"] and item["long_video_eligible"]
    )
    item["daily_auto_publish_configured"] = (
        int(item.get("id") or 0) in DAILY_ACCOUNT_IDS
    )
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
    try:
        _XPostError, XPostStore, _publish_canary = _x_posts_api()
        schedule_store = XPostStore(POST_DB_PATH)
        material_config = schedule_store.get_schedule_config("material")
        drama_config = schedule_store.get_schedule_config("drama")
        material_ids = set(
            material_config["account_ids"]
            if material_config["enabled"]
            else []
        )
        drama_ids = set(
            drama_config["account_ids"]
            if drama_config["enabled"]
            else []
        )
    except Exception as exc:
        XPostError, _XPostStore, _publish_canary = _x_posts_api()
        if isinstance(exc, XPostError):
            _raise_x_post_error(exc)
        raise
    for item in items:
        account_id = int(item.get("id") or 0)
        sources = []
        if account_id in material_ids:
            sources.append("material")
        if account_id in drama_ids:
            sources.append("drama")
        item["daily_auto_publish_configured"] = bool(sources)
        item["auto_publish_sources"] = sources
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


def set_account_publish_approval(account_id, approved, actor):
    if not isinstance(approved, bool):
        raise ServiceError("invalid_request", "approved必须是布尔值", 400)
    account_id = int(account_id)
    actor, _scope = normalize_account_scope(actor, "all")
    initial_row = find_scoped_account_row(account_id, actor, "all")
    x_user_id = str(initial_row["x_user_id"])
    with account_lock("x:" + x_user_id):
        find_scoped_account_row(account_id, actor, "all")
        timestamp = iso_utc()
        with _DB_LOCK:
            conn = db_connect()
            try:
                cursor = conn.execute(
                    "UPDATE x_authorized_account "
                    "SET publish_approved=?,updated_at=? WHERE id=?",
                    (1 if approved else 0, timestamp, account_id),
                )
                if cursor.rowcount != 1:
                    raise ServiceError(
                        "x_account_not_found",
                        "X账号记录不存在",
                        404,
                    )
                conn.commit()
            finally:
                conn.close()
    safe_record_event(
        "publish_approval_enabled" if approved else "publish_approval_disabled",
        "completed",
        actor,
        x_user_id=x_user_id,
    )
    return find_account(account_id)


def set_account_drama_language(account_id, drama_language, actor):
    try:
        normalized_language = canonical_drama_language(drama_language)
    except ValueError as exc:
        raise ServiceError(
            "x_account_drama_language_invalid",
            clean_text(exc),
            400,
        ) from None
    account_id = int(account_id)
    actor, _scope = normalize_account_scope(actor, "all")
    initial_row = find_scoped_account_row(account_id, actor, "all")
    x_user_id = str(initial_row["x_user_id"])
    with account_lock("x:" + x_user_id):
        current = row_to_item(
            find_scoped_account_row(account_id, actor, "all")
        )
        if same_drama_language(
            current.get("drama_language"), normalized_language
        ):
            return current
        XPostError, XPostStore, _publish_canary = _x_posts_api()
        try:
            # Ensure the X Post schema exists before entering the cross-table
            # transaction below.  Production keeps both tables in one DB; an
            # attached DB preserves the same atomic check-and-update contract
            # for supported split-path configurations.
            XPostStore(POST_DB_PATH)
        except XPostError as exc:
            _raise_x_post_error(exc)
        timestamp = iso_utc()
        with _DB_LOCK:
            conn = db_connect()
            attached = False
            try:
                account_db = os.path.normcase(os.path.abspath(str(DB_PATH)))
                post_db = os.path.normcase(os.path.abspath(str(POST_DB_PATH)))
                drama_table = "x_post_drama_pool"
                if account_db != post_db:
                    conn.execute(
                        "ATTACH DATABASE ? AS xpost_language_guard",
                        (str(POST_DB_PATH),),
                    )
                    attached = True
                    drama_table = (
                        "xpost_language_guard.x_post_drama_pool"
                    )
                conn.execute("BEGIN IMMEDIATE")
                bound = conn.execute(
                    "SELECT content_id,language FROM %s "
                    "WHERE assigned_account_id=? "
                    "AND status IN ('pending','active') "
                    "AND next_sub_number<=free_episode_count "
                    "ORDER BY created_at,id LIMIT 1" % drama_table,
                    (account_id,),
                ).fetchone()
                if bound and not same_drama_language(
                    bound["language"], normalized_language
                ):
                    conn.rollback()
                    raise ServiceError(
                        "x_account_drama_language_conflict",
                        "Account has an unfinished bound drama in another language",
                        409,
                    )
                cursor = conn.execute(
                    "UPDATE x_authorized_account "
                    "SET drama_language=?,updated_at=? WHERE id=?",
                    (normalized_language, timestamp, account_id),
                )
                if cursor.rowcount != 1:
                    raise ServiceError(
                        "x_account_not_found",
                        "X account record does not exist",
                        404,
                    )
                conn.commit()
            finally:
                if conn.in_transaction:
                    conn.rollback()
                if attached:
                    conn.execute("DETACH DATABASE xpost_language_guard")
                conn.close()
    safe_record_event(
        "drama_language_changed",
        "completed",
        actor,
        x_user_id=x_user_id,
    )
    return find_account(account_id)


def update_account_error(account_id, status, error):
    timestamp = iso_utc()
    with _DB_LOCK:
        conn = db_connect()
        try:
            if status is None:
                conn.execute(
                    "UPDATE x_authorized_account SET last_error_at=?,last_error=?,updated_at=? WHERE id=?",
                    (timestamp, clean_text(error), timestamp, int(account_id)),
                )
            else:
                conn.execute(
                    "UPDATE x_authorized_account SET status=?,last_error_at=?,last_error=?,updated_at=? WHERE id=?",
                    (status, timestamp, clean_text(error), timestamp, int(account_id)),
                )
            conn.commit()
        finally:
            conn.close()


def verify_account(
    account_id,
    actor,
    scope="mine",
    *,
    only_refresh_required=False,
    preserve_transient_status=False,
    require_publish_approved=False,
    allow_token_refresh=True,
):
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
        if require_publish_approved and item.get("publish_approved") is not True:
            raise ServiceError(
                "x_account_publish_not_approved",
                "该X账号尚未勾选允许发布",
                409,
            )
        if require_publish_approved and item.get("status") != "active":
            raise ServiceError(
                "x_account_not_publishable",
                "X账号授权当前不可用于发布，请先重新授权或校验",
                409,
            )
        try:
            expires_at = item.get("access_expires_at", "")
            refresh_required = bool(
                item.get("access_token_status")
                in {"missing", "missing_refreshable"}
                or (
                    expires_at
                    and parse_iso_epoch(expires_at) <= now_epoch() + 120
                )
            )
            if only_refresh_required and not refresh_required:
                return item
            token = read_token_file(item["x_user_id"])
            timestamp = iso_utc()
            refresh_at = item.get("last_token_refresh_at", "")
            needs_token_refresh = access_token_refresh_required(
                expires_at,
                token,
            )
            if needs_token_refresh and not allow_token_refresh:
                raise ServiceError(
                    "x_token_invalid", TOKEN_INVALID_LOGIN_MESSAGE, 409
                )
            if needs_token_refresh:
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
                            verified=?,protected=?,subscription_type=?,location=?,x_created_at=?,profile_synced_at=?,updated_at=?
                        WHERE id=?
                        """,
                        (
                            clean_text(account.get("username", item.get("username", "")), 255),
                            clean_text(account.get("name", item.get("display_name", "")), 255),
                            clean_text(account.get("profile_image_url", item.get("profile_image_url", "")), 1024),
                            json.dumps(scopes), status, expires_at, refresh_at, timestamp,
                            profile["followers_count"], profile["following_count"], profile["tweet_count"],
                            profile["listed_count"], profile["like_count"], profile["media_count"], profile["verified"],
                            profile["protected"], profile["subscription_type"], profile["location"], profile["x_created_at"],
                            profile["profile_synced_at"], timestamp, account_id,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            safe_record_event("verify", "completed", actor, x_user_id=item["x_user_id"])
            return find_account(account_id)
        except ServiceError as exc:
            if preserve_transient_status and exc.code in TRANSIENT_VERIFY_ERROR_CODES:
                state = None
            else:
                state = "revoked" if exc.code == "x_token_revoked" else ("token_missing" if exc.code == "x_token_missing" else "error")
            update_account_error(account_id, state, str(exc))
            safe_record_event("verify", "failed", actor, x_user_id=item["x_user_id"], error_code=exc.code)
            raise
        except Exception:
            update_account_error(
                account_id,
                None if preserve_transient_status else "error",
                "X账号校验失败",
            )
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
        if item["status"] in EXPLICITLY_DISABLED_STATUSES:
            raise ServiceError("x_account_disabled", "X账号已在后台停用，禁止用于发布", 409)
        if item["status"] != "active":
            raise ServiceError(
                "x_account_not_publishable",
                "X账号授权当前不可用于发布，请先重新授权或校验",
                409,
            )
        if item.get("publish_approved") is not True:
            raise ServiceError(
                "x_account_publish_not_approved",
                "该X账号尚未勾选允许发布",
                409,
            )
        expires_at = str(item.get("access_expires_at", "") or "")
        if expires_at and parse_iso_epoch(expires_at) <= now_epoch():
            raise ServiceError(
                "x_token_invalid", TOKEN_INVALID_LOGIN_MESSAGE, 409
            )
        try:
            token = read_token_file(x_user_id)
        except ServiceError as exc:
            if exc.code in {"x_token_missing", "x_token_revoked"}:
                raise ServiceError(
                    "x_token_invalid", TOKEN_INVALID_LOGIN_MESSAGE, 409
                ) from None
            raise
        access_token = str(token.get("access_token", "") or "")
        if not access_token:
            raise ServiceError(
                "x_token_invalid", TOKEN_INVALID_LOGIN_MESSAGE, 409
            )
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


def publishing_token_provider(account_id, actor, frozen_language="", require_premium=False):
    """Refresh before each upload request without releasing the outer lock."""
    def current_token(*, verify_now=False):
        XPostError, _store, _publish = _x_posts_api()
        try:
            verify_account(
                account_id, actor, "all", only_refresh_required=not verify_now,
                preserve_transient_status=True, require_publish_approved=True,
            )
            with publish_credentials(account_id, actor, "all") as (account, token):
                if frozen_language and not same_drama_language(account.get("drama_language"), frozen_language):
                    raise ServiceError("x_post_account_language_mismatch", "X account language changed during upload", 409)
                if require_premium and (
                    not account.get("long_video_publish_eligible") or account.get("protected") is not False
                ):
                    raise ServiceError("x_post_premium_relay_unavailable", "Long-video account is no longer eligible", 409)
                return token
        except ServiceError as exc:
            # No publish request has been sent by this credential check.
            raise XPostError(exc.code, str(exc), exc.status) from None
    return current_token


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
    allowed = (
        "status",
        "queue_id",
        "delivery_mode",
        "preflight_duration",
        "error_code",
        "log_id",
        "short_url",
        "post_id",
        "preview_url",
    )
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
        "id", "run_id", "catchup_run_id", "batch_kind", "run_date", "source_date",
        "account_id", "account_username",
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
    expected_count = safe.get("expected_count")
    if (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 1
        or expected_count > MAX_DAILY_ACCOUNTS
        or len(safe["queues"]) != expected_count
    ):
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
        "catchup_run_id",
        "batch_kind",
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


def _catchup_identity(payload, expected_keys):
    if not isinstance(payload, dict) or set(payload) != set(expected_keys):
        raise ServiceError("invalid_request", "X catch-up request fields are invalid", 400)
    raw_parent_run_id = payload.get("parent_run_id")
    if isinstance(raw_parent_run_id, bool):
        raise ServiceError("invalid_request", "parent_run_id is invalid", 400)
    try:
        parent_run_id = int(raw_parent_run_id)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "parent_run_id is invalid", 400) from None
    if parent_run_id <= 0:
        raise ServiceError("invalid_request", "parent_run_id is invalid", 400)
    reason = str(payload.get("reason", "") or "").strip()
    if reason != CATCHUP_REASON_SCOPE_EXPANSION:
        raise ServiceError("x_catchup_reason_denied", "X catch-up reason is not allowed", 403)
    return parent_run_id, reason


def _safe_catchup_queue(queue):
    allowed = (
        "id",
        "run_id",
        "catchup_run_id",
        "batch_kind",
        "run_date",
        "source_date",
        "account_id",
        "account_username",
        "pool_item_id",
        "pool_created_at",
        "material_id",
        "material_name",
        "content_id",
        "material_language",
        "drama_name",
        "tag",
        "candidate_rank",
        "spend",
        "status",
        "created_at",
        "updated_at",
    )
    safe = {key: queue[key] for key in allowed if key in queue}
    if "batch_kind" not in safe:
        try:
            run_id = int(safe.get("run_id") or 0)
            catchup_run_id = int(safe.get("catchup_run_id") or 0)
        except (TypeError, ValueError, OverflowError):
            raise ServiceError("x_posts_unavailable", "X queue parent is invalid", 503) from None
        if catchup_run_id > 0:
            safe["batch_kind"] = "catchup"
        elif run_id > 0:
            safe["batch_kind"] = "daily"
        else:
            safe["batch_kind"] = "canary"
    return safe


def _safe_catchup_plan_query_result(
    result, parent_run_id, reason, missing_account_ids
):
    if not isinstance(result, dict) or not isinstance(result.get("found"), bool):
        raise ServiceError("x_posts_unavailable", "X catch-up plan query returned invalid data", 503)
    run = result.get("run")
    queues = result.get("queues")
    base = {
        "found": bool(result["found"]),
        "parent_run_id": int(parent_run_id),
        "reason": reason,
        "missing_account_ids": [int(value) for value in missing_account_ids],
    }
    if not result["found"]:
        if run is not None or queues != []:
            raise ServiceError("x_posts_unavailable", "X catch-up plan query returned invalid data", 503)
        base.update({"run": None, "queues": []})
        return base
    if not isinstance(run, dict) or not isinstance(queues, list):
        raise ServiceError("x_posts_unavailable", "X catch-up plan query returned invalid data", 503)
    if any(not isinstance(queue, dict) for queue in queues):
        raise ServiceError("x_posts_unavailable", "X catch-up plan query returned invalid data", 503)
    allowed_run = (
        "id",
        "parent_run_id",
        "batch_kind",
        "reason",
        "account_ids",
        "run_date",
        "source_date",
        "status",
        "expected_count",
        "queued_count",
        "published_count",
        "failed_count",
        "unknown_count",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )
    safe_run = {key: run[key] for key in allowed_run if key in run}
    safe_queues = [
        _safe_catchup_queue(queue)
        for queue in queues
        if isinstance(queue, dict)
    ]
    expected_count = safe_run.get("expected_count")
    catchup_run_id = safe_run.get("id")
    stored_account_ids = safe_run.get("account_ids")
    if (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count != len(missing_account_ids)
        or int(safe_run.get("parent_run_id") or 0) != int(parent_run_id)
        or safe_run.get("reason") != reason
        or safe_run.get("batch_kind") != "catchup"
        or not isinstance(catchup_run_id, int)
        or isinstance(catchup_run_id, bool)
        or catchup_run_id <= 0
        or not isinstance(stored_account_ids, list)
        or tuple(stored_account_ids) != tuple(missing_account_ids)
    ):
        raise ServiceError("x_posts_unavailable", "X catch-up plan scope is inconsistent", 503)
    failed_preflight = safe_run.get("status") == "failed_preflight"
    if (
        (failed_preflight and safe_queues)
        or (not failed_preflight and len(safe_queues) != expected_count)
    ):
        raise ServiceError("x_posts_unavailable", "X catch-up plan queue count is inconsistent", 503)
    if failed_preflight:
        base.update({"run": safe_run, "queues": []})
        return base
    queue_accounts = []
    for queue in safe_queues:
        try:
            account_id = int(queue.get("account_id") or 0)
            queue_run_id = int(queue.get("run_id") or 0)
            queue_catchup_run_id = int(queue.get("catchup_run_id") or 0)
        except (TypeError, ValueError, OverflowError):
            raise ServiceError("x_posts_unavailable", "X catch-up queue is invalid", 503) from None
        if (
            queue_run_id != 0
            or queue_catchup_run_id != catchup_run_id
            or queue.get("batch_kind") != "catchup"
        ):
            raise ServiceError("x_posts_unavailable", "X catch-up queue parent is invalid", 503)
        queue_accounts.append(account_id)
    if tuple(queue_accounts) != tuple(missing_account_ids):
        raise ServiceError("x_posts_unavailable", "X catch-up account scope is inconsistent", 503)
    base.update({"run": safe_run, "queues": safe_queues})
    return base


def _safe_catchup_plan_result(
    result, parent_run_id, reason, missing_account_ids
):
    if not isinstance(result, dict) or not isinstance(result.get("queues"), list):
        raise ServiceError("x_posts_unavailable", "X catch-up plan returned invalid data", 503)
    if any(not isinstance(queue, dict) for queue in result["queues"]):
        raise ServiceError("x_posts_unavailable", "X catch-up plan returned invalid data", 503)
    allowed_run = (
        "id",
        "parent_run_id",
        "batch_kind",
        "reason",
        "account_ids",
        "run_date",
        "source_date",
        "status",
        "expected_count",
        "queued_count",
        "published_count",
        "failed_count",
        "unknown_count",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        "created",
    )
    safe = {key: result[key] for key in allowed_run if key in result}
    safe["queues"] = [
        _safe_catchup_queue(queue)
        for queue in result["queues"]
        if isinstance(queue, dict)
    ]
    safe["missing_account_ids"] = [int(value) for value in missing_account_ids]
    expected_count = safe.get("expected_count")
    catchup_run_id = safe.get("id")
    if (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count != len(missing_account_ids)
        or len(safe["queues"]) != expected_count
        or int(safe.get("parent_run_id") or 0) != int(parent_run_id)
        or safe.get("reason") != reason
        or safe.get("batch_kind") != "catchup"
        or not isinstance(catchup_run_id, int)
        or isinstance(catchup_run_id, bool)
        or catchup_run_id <= 0
        or not isinstance(safe.get("account_ids"), list)
        or tuple(safe.get("account_ids")) != tuple(missing_account_ids)
    ):
        raise ServiceError("x_posts_unavailable", "X catch-up plan scope is inconsistent", 503)
    queue_accounts = []
    for queue in safe["queues"]:
        try:
            account_id = int(queue.get("account_id") or 0)
            queue_run_id = int(queue.get("run_id") or 0)
            queue_catchup_run_id = int(queue.get("catchup_run_id") or 0)
        except (TypeError, ValueError, OverflowError):
            raise ServiceError("x_posts_unavailable", "X catch-up queue is invalid", 503) from None
        if (
            queue_run_id != 0
            or queue_catchup_run_id != catchup_run_id
            or queue.get("batch_kind") != "catchup"
        ):
            raise ServiceError("x_posts_unavailable", "X catch-up queue parent is invalid", 503)
        queue_accounts.append(account_id)
    if tuple(queue_accounts) != tuple(missing_account_ids):
        raise ServiceError("x_posts_unavailable", "X catch-up account scope is inconsistent", 503)
    return safe


def _safe_catchup_run_result(
    result, parent_run_id, reason, missing_account_ids
):
    if not isinstance(result, dict):
        raise ServiceError("x_posts_unavailable", "X catch-up run returned invalid data", 503)
    allowed = (
        "id",
        "parent_run_id",
        "batch_kind",
        "reason",
        "account_ids",
        "run_date",
        "source_date",
        "status",
        "expected_count",
        "queued_count",
        "published_count",
        "failed_count",
        "unknown_count",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        "recorded",
    )
    safe = {key: result[key] for key in allowed if key in result}
    safe["missing_account_ids"] = [int(value) for value in missing_account_ids]
    if (
        int(safe.get("parent_run_id") or 0) != int(parent_run_id)
        or safe.get("reason") != reason
        or safe.get("batch_kind") != "catchup"
        or int(safe.get("expected_count") or 0) != len(missing_account_ids)
        or not isinstance(safe.get("account_ids"), list)
        or tuple(safe.get("account_ids")) != tuple(missing_account_ids)
    ):
        raise ServiceError("x_posts_unavailable", "X catch-up run scope is inconsistent", 503)
    return safe


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
    snapshot = find_account(account_id)
    if snapshot.get("status") in EXPLICITLY_DISABLED_STATUSES:
        raise ServiceError(
            "x_account_disabled", "X账号已在后台停用，禁止用于发布", 409
        )
    if snapshot.get("publish_approved") is not True:
        raise ServiceError(
            "x_account_publish_not_approved", "该X账号尚未勾选允许发布", 409
        )
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
        not values
        or len(values) > MAX_DAILY_ACCOUNTS
        or len(set(values)) != len(values)
        or any(value <= 0 for value in values)
    ):
        raise ServiceError("x_daily_scope_invalid", "X每日发布账号范围配置无效", 503)
    return values


def _require_candidate_duration_capability(candidate, account):
    raw_duration = candidate.get("preflight_duration", 0)
    try:
        duration = float(raw_duration or 0)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError(
            "invalid_request", "preflight_duration is invalid", 400
        ) from None
    if not math.isfinite(duration) or duration < 0:
        raise ServiceError(
            "invalid_request", "preflight_duration is invalid", 400
        )
    if duration > 140.0 and not account.get(
        "long_video_publish_eligible"
    ):
        raise ServiceError(
            "x_long_video_requires_premium",
            "Videos longer than 140 seconds require a token-confirmed X Premium subscription",
            409,
        )


def _active_schedule_account_scope():
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return tuple(
            XPostStore(POST_DB_PATH).scheduled_account_ids(
                enabled_only=True,
                include_nonterminal_runs=True,
            )
        )
    except XPostError as exc:
        _raise_x_post_error(exc)


def _active_manual_account_scope():
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return tuple(XPostStore(POST_DB_PATH).active_manual_account_ids())
    except XPostError as exc:
        _raise_x_post_error(exc)


def create_daily_plan_request(
    payload, allowed_account_ids=None, require_pool=False
):
    """Freeze every configured account identity and candidate in one transaction."""
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
        len(requested_accounts) != len(allowed_accounts)
        or tuple(requested_accounts) != allowed_accounts
    ):
        raise ServiceError(
            "x_daily_account_scope_denied",
            "X每日发布计划必须完整使用当前配置的账号范围",
            403,
        )
    trusted = []
    for raw, account_id in zip(candidates, requested_accounts):
        account = find_account(account_id)
        if not account.get("publish_eligible"):
            raise ServiceError("x_account_not_publishable", "X账号当前状态不可用于发布", 409)
        _require_candidate_duration_capability(raw, account)
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
    preflight_post_storage_request(len(trusted))
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


def _catchup_scope_snapshot(
    store, run_date, parent_run_id, reason, allowed_account_ids
):
    allowed_accounts = _daily_account_scope(allowed_account_ids)
    if allowed_accounts is None:
        raise ServiceError(
            "x_daily_account_scope_denied",
            "X catch-up requires the configured daily account scope",
            403,
        )
    try:
        parent_result = store.query_daily_plan(run_date)
    except Exception as exc:
        XPostError, _XPostStore, _publish_canary = _x_posts_api()
        if isinstance(exc, XPostError):
            _raise_x_post_error(exc)
        raise
    parent = _safe_daily_plan_query_result(parent_result)
    if not parent["found"]:
        raise ServiceError("x_catchup_parent_not_found", "X catch-up parent run does not exist", 409)
    parent_run = parent["run"]
    if (
        int(parent_run.get("id") or 0) != int(parent_run_id)
        or str(parent_run.get("run_date") or "") != str(run_date or "")
    ):
        raise ServiceError("x_catchup_parent_mismatch", "X catch-up parent run does not match", 409)
    parent_expected_count = int(parent_run.get("expected_count") or 0)
    parent_published_count = int(parent_run.get("published_count") or 0)
    parent_unknown_count = int(parent_run.get("unknown_count") or 0)
    parent_queues = parent["queues"]
    parent_accounts = tuple(
        int(queue.get("account_id") or 0)
        for queue in parent_queues
    )
    if (
        parent_run.get("status") != "completed"
        or parent_expected_count < 1
        or parent_expected_count != len(parent_queues)
        or parent_published_count != parent_expected_count
        or parent_unknown_count != 0
        or len(set(parent_accounts)) != len(parent_accounts)
        or any(queue.get("status") != "published" for queue in parent_queues)
    ):
        raise ServiceError(
            "x_catchup_parent_not_completed",
            "X catch-up requires a fully published parent run with no ambiguous outcome",
            409,
        )
    configured_parent_order = tuple(
        account_id for account_id in allowed_accounts if account_id in set(parent_accounts)
    )
    if (
        parent_accounts != configured_parent_order
        or not set(parent_accounts).issubset(set(allowed_accounts))
    ):
        raise ServiceError(
            "x_daily_account_scope_denied",
            "X catch-up parent accounts are outside the configured daily scope",
            403,
        )
    missing_account_ids = tuple(
        account_id for account_id in allowed_accounts if account_id not in set(parent_accounts)
    )
    if not missing_account_ids:
        raise ServiceError(
            "x_catchup_no_missing_accounts",
            "All configured X accounts already have a parent daily queue",
            409,
        )
    try:
        catchup_result = store.query_catchup_plan(
            run_date,
            parent_run_id,
            reason=reason,
        )
    except Exception as exc:
        XPostError, _XPostStore, _publish_canary = _x_posts_api()
        if isinstance(exc, XPostError):
            _raise_x_post_error(exc)
        raise
    safe_catchup = _safe_catchup_plan_query_result(
        catchup_result,
        parent_run_id,
        reason,
        missing_account_ids,
    )
    return {
        "parent": parent,
        "catchup": safe_catchup,
        "missing_account_ids": missing_account_ids,
    }


def query_catchup_plan_request(payload, allowed_account_ids=None):
    parent_run_id, reason = _catchup_identity(
        payload,
        {"run_date", "parent_run_id", "reason"},
    )
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        store = XPostStore(POST_DB_PATH)
        snapshot = _catchup_scope_snapshot(
            store,
            payload.get("run_date"),
            parent_run_id,
            reason,
            allowed_account_ids,
        )
    except ServiceError:
        raise
    except XPostError as exc:
        _raise_x_post_error(exc)
    return snapshot["catchup"]


def create_catchup_plan_request(
    payload, allowed_account_ids=None, require_pool=False
):
    parent_run_id, reason = _catchup_identity(
        payload,
        {"run_date", "source_date", "parent_run_id", "reason", "candidates"},
    )
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ServiceError("invalid_request", "candidates must be an array", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        store = XPostStore(POST_DB_PATH)
        snapshot = _catchup_scope_snapshot(
            store,
            payload.get("run_date"),
            parent_run_id,
            reason,
            allowed_account_ids,
        )
    except ServiceError:
        raise
    except XPostError as exc:
        _raise_x_post_error(exc)
    parent_source_date = str(
        snapshot["parent"]["run"].get("source_date", "") or ""
    )
    if str(payload.get("source_date", "") or "") != parent_source_date:
        raise ServiceError(
            "x_catchup_parent_mismatch",
            "X catch-up source_date does not match the parent run",
            409,
        )
    missing_account_ids = snapshot["missing_account_ids"]
    requested_account_ids = []
    for raw in candidates:
        if not isinstance(raw, dict) or isinstance(raw.get("account_id"), bool):
            raise ServiceError("invalid_request", "candidate account_id is invalid", 400)
        try:
            requested_account_ids.append(int(raw.get("account_id")))
        except (TypeError, ValueError, OverflowError):
            raise ServiceError("invalid_request", "candidate account_id is invalid", 400) from None
    if tuple(requested_account_ids) != tuple(missing_account_ids):
        raise ServiceError(
            "x_daily_account_scope_denied",
            "X catch-up candidates must exactly match the configured missing accounts",
            403,
        )
    trusted = []
    for raw, account_id in zip(candidates, requested_account_ids):
        account = find_account(account_id)
        if not account.get("publish_eligible"):
            raise ServiceError(
                "x_account_not_publishable",
                "X catch-up target account is not publishable",
                409,
            )
        _require_candidate_duration_capability(raw, account)
        candidate = dict(raw)
        candidate.update(
            {
                "account_id": int(account["id"]),
                "account_username": str(account.get("username", "") or ""),
                "page_name": str(
                    account.get("display_name", "")
                    or account.get("username", "")
                    or ""
                ),
                "page_id": str(account.get("x_user_id", "") or ""),
            }
        )
        trusted.append(candidate)
    preflight_post_storage_request(len(missing_account_ids))
    try:
        result = store.create_catchup_plan(
            payload.get("run_date"),
            payload.get("source_date"),
            parent_run_id,
            reason,
            trusted,
            tuple(_daily_account_scope(allowed_account_ids)),
            require_pool=bool(require_pool),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return _safe_catchup_plan_result(
        result,
        parent_run_id,
        reason,
        missing_account_ids,
    )


def record_catchup_failure_request(payload, allowed_account_ids=None):
    parent_run_id, reason = _catchup_identity(
        payload,
        {
            "run_date",
            "source_date",
            "parent_run_id",
            "reason",
            "expected_missing_count",
            "error_code",
            "error_message",
        },
    )
    raw_expected_count = payload.get("expected_missing_count")
    if isinstance(raw_expected_count, bool):
        raise ServiceError("invalid_request", "expected_missing_count is invalid", 400)
    try:
        expected_missing_count = int(raw_expected_count)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "expected_missing_count is invalid", 400) from None
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        store = XPostStore(POST_DB_PATH)
        snapshot = _catchup_scope_snapshot(
            store,
            payload.get("run_date"),
            parent_run_id,
            reason,
            allowed_account_ids,
        )
    except ServiceError:
        raise
    except XPostError as exc:
        _raise_x_post_error(exc)
    missing_account_ids = snapshot["missing_account_ids"]
    if expected_missing_count != len(missing_account_ids):
        raise ServiceError(
            "x_daily_account_scope_denied",
            "X catch-up failure count does not match the configured missing scope",
            403,
        )
    parent_source_date = str(
        snapshot["parent"]["run"].get("source_date", "") or ""
    )
    if str(payload.get("source_date", "") or "") != parent_source_date:
        raise ServiceError(
            "x_catchup_parent_mismatch",
            "X catch-up source_date does not match the parent run",
            409,
        )
    try:
        result = store.record_catchup_failure(
            payload.get("run_date"),
            payload.get("source_date"),
            parent_run_id,
            reason,
            expected_missing_count,
            tuple(_daily_account_scope(allowed_account_ids)),
            payload.get("error_code"),
            payload.get("error_message"),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return _safe_catchup_run_result(
        result,
        parent_run_id,
        reason,
        missing_account_ids,
    )


def preflight_post_storage_request(required_media_count=1):
    if isinstance(required_media_count, bool):
        raise ServiceError("invalid_request", "required_media_count无效", 400)
    try:
        required_media_count = int(required_media_count)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "required_media_count无效", 400) from None
    if required_media_count < 1 or required_media_count > MAX_DAILY_ACCOUNTS:
        raise ServiceError("invalid_request", "required_media_count无效", 400)
    try:
        from features.x_posts import preflight_post_storage
    except (ImportError, ModuleNotFoundError):
        raise ServiceError("x_posts_unavailable", "X发布服务暂不可用", 503) from None
    try:
        return preflight_post_storage(
            POST_PUBLIC_ROOT,
            mount_root=POST_STORAGE_MOUNT_ROOT,
            storage_root=POST_STORAGE_ROOT,
            minimum_free_bytes=(POST_MAX_MEDIA_BYTES * required_media_count)
            + (64 * 1024 * 1024),
        )
    except Exception as exc:
        XPostError, _XPostStore, _publish_canary = _x_posts_api()
        if isinstance(exc, XPostError):
            _raise_x_post_error(exc)
        raise


def _resolve_duration_pending_queue(store, queue, actor, contexts):
    """Resolve a new short-drama route before any publish log or credential.

    The caller owns ``contexts`` through the source upload so a freshly
    prepared final file can be passed straight to ``publish_canary``.
    """
    if str(queue.get("delivery_mode") or "") != "duration_pending":
        return queue, None, None
    if (
        queue.get("source_type") != "drama"
        or int(queue.get("schedule_run_id") or 0) <= 0
        or str(queue.get("route_state") or "")
        not in {"duration_pending", "waiting_relay"}
    ):
        raise ServiceError(
            "x_posts_unavailable",
            "Short-drama duration route state is invalid",
            503,
        )
    if not POST_DRAMA_DURATION_ROUTING_ENABLED:
        parked = {
            # The schedule runner already understands waiting_relay as a
            # zero-write nonterminal result. Reuse that safe park signal even
            # when the companion row is still duration_pending.
            "status": "waiting_relay",
            "queue_id": int(queue["id"]),
            "delivery_mode": "duration_pending",
            "preflight_duration": float(
                queue.get("preflight_duration", 0) or 0
            ),
            "error_code": "x_post_drama_duration_routing_disabled",
        }
        return queue, None, parked
    try:
        frozen_language = canonical_drama_language(
            queue.get("account_drama_language")
        )
    except ValueError as exc:
        raise ServiceError(
            "x_post_account_language_mismatch", clean_text(exc), 409
        ) from None

    prepared_media = None
    media_evidence = None
    if str(queue.get("route_state") or "") == "duration_pending":
        from features.x_posts.publish_media_repair import (
            prepare_duration_pending_drama_media,
        )

        prepared_media = contexts.enter_context(
            prepare_duration_pending_drama_media(
                queue=queue,
                public_root=POST_PUBLIC_ROOT,
                allowed_media_hosts=POST_MEDIA_ALLOWED_HOSTS,
                timeout=POST_HTTP_TIMEOUT_SECONDS,
                max_media_bytes=POST_MAX_MEDIA_BYTES,
                storage_guard=preflight_post_storage_request,
                durable_storage={
                    "mount_root": POST_STORAGE_MOUNT_ROOT,
                    "storage_root": POST_STORAGE_ROOT,
                },
            )
        )
        media_evidence = dict(prepared_media.evidence)
        final_duration = float(media_evidence["preflight_duration"])
    else:
        final_duration = float(queue.get("preflight_duration", 0) or 0)
        if not math.isfinite(final_duration) or final_duration <= 0:
            raise ServiceError(
                "x_posts_unavailable",
                "Waiting short-drama queue is missing final media evidence",
                503,
            )

    target = verify_account(
        int(queue["account_id"]),
        actor,
        "all",
        preserve_transient_status=True,
        require_publish_approved=True,
    )
    if not same_drama_language(
        target.get("drama_language"), frozen_language
    ):
        raise ServiceError(
            "x_post_account_language_mismatch",
            "X target account drama language no longer matches the frozen queue",
            409,
        )
    target_long_video_eligible = bool(
        target.get("long_video_publish_eligible")
        and target.get("protected") is False
    )
    relay_accounts = []
    if final_duration > 140.0 and not target_long_video_eligible:
        relay_accounts = _premium_relay_accounts(
            str(queue.get("run_date") or ""),
            refresh=True,
            drama_language=frozen_language,
        )
    try:
        queue = store.resolve_drama_duration_route(
            int(queue["id"]),
            media_evidence,
            target_long_video_eligible,
            relay_accounts,
        )
    except Exception:
        raise
    if str(queue.get("route_state") or "") == "waiting_relay":
        return queue, None, {
            "status": "waiting_relay",
            "queue_id": int(queue["id"]),
            "delivery_mode": "duration_pending",
            "preflight_duration": float(
                queue.get("preflight_duration", 0) or 0
            ),
            "error_code": "x_post_premium_relay_unavailable",
        }
    if (
        str(queue.get("route_state") or "") != "resolved"
        or str(queue.get("delivery_mode") or "")
        not in {"direct", "premium_relay_repost"}
    ):
        raise ServiceError(
            "x_posts_unavailable",
            "Short-drama duration route did not resolve atomically",
            503,
        )
    if prepared_media is not None:
        prepared_media = prepared_media.bind_resolved(queue)
    return queue, prepared_media, None


def publish_queue_request(
    queue_id,
    allowed_account_ids=None,
    *,
    allow_schedule=None,
    allow_manual=False,
    expected_manual_trigger_source=None,
    require_manual_parent=False,
):
    """Publish one frozen queue row; no request field can override its account or copy."""
    XPostError, XPostStore, publish_canary = _x_posts_api()
    duration_route_contexts = contextlib.ExitStack()
    duration_prepared_media = None
    try:
        if allow_schedule is None:
            allow_schedule = allowed_account_ids is not None
        store = XPostStore(POST_DB_PATH)
        queue = store.get_queue(queue_id)
        allowed_accounts = (
            ()
            if (
                (allow_schedule or expected_manual_trigger_source is not None)
                and not allowed_account_ids
            )
            else _daily_account_scope(allowed_account_ids)
        )
        run_id = int(queue.get("run_id") or 0)
        catchup_run_id = int(queue.get("catchup_run_id") or 0)
        schedule_run_id = int(queue.get("schedule_run_id") or 0)
        manual_run_id = int(queue.get("manual_run_id") or 0)
        manual_run = None
        if expected_manual_trigger_source not in {
            None,
            "manual",
            "auto_template",
        }:
            raise ServiceError(
                "invalid_request",
                "manual trigger source is invalid",
                400,
            )
        if not isinstance(require_manual_parent, bool):
            raise ServiceError(
                "invalid_request",
                "manual parent requirement is invalid",
                400,
            )
        parent_count = sum(
            value > 0
            for value in (
                run_id,
                catchup_run_id,
                schedule_run_id,
                manual_run_id,
            )
        )
        if allowed_accounts is not None and (
            parent_count != 1
            or (
                schedule_run_id > 0
                and not allow_schedule
            )
            or (
                manual_run_id > 0
                and not allow_manual
            )
            or (
                require_manual_parent
                and manual_run_id == 0
            )
            or (
                schedule_run_id == 0
                and manual_run_id == 0
                and int(queue.get("account_id") or 0)
                not in allowed_accounts
            )
        ):
            raise ServiceError(
                "x_daily_account_scope_denied",
                "X自动发布只能处理已冻结的授权账号队列",
                403,
            )
        # A manual-parent queue is always source-gated, including calls made
        # with the backend bearer.  This keeps the new auto-template queues
        # reachable only through their dedicated bearer/route while preserving
        # the historical backend path for operator-created manual queues.
        if manual_run_id > 0:
            manual_run = store.get_manual_run(
                manual_run_id,
                expected_manual_trigger_source or "manual",
            )
            if (
                expected_manual_trigger_source == "auto_template"
                and str(manual_run.get("status") or "")
                not in {"running", "completed"}
            ):
                raise ServiceError(
                    "x_post_auto_template_recovery_fenced",
                    "auto template publish was stopped by canonical recovery",
                    409,
                )
            if allowed_accounts is not None and int(
                queue.get("account_id") or 0
            ) not in manual_run[
                "account_ids"
            ]:
                raise ServiceError(
                    "x_daily_account_scope_denied",
                    "X手动发布队列账号与冻结任务不一致",
                    403,
                )
        actor = dict(
            AUTO_TEMPLATE_ACTOR
            if expected_manual_trigger_source == "auto_template"
            else CANARY_ACTOR
        )
        frozen_drama_language = None
        if int(queue.get("account_drama_language_frozen") or 0) == 1:
            try:
                frozen_drama_language = canonical_drama_language(
                    queue.get("account_drama_language")
                )
            except ValueError as exc:
                raise ServiceError(
                    "x_post_account_language_mismatch",
                    clean_text(exc),
                    409,
                ) from None
        queue, duration_prepared_media, parked_result = (
            _resolve_duration_pending_queue(
                store,
                queue,
                actor,
                duration_route_contexts,
            )
        )
        if parked_result is not None:
            duration_route_contexts.close()
            return _safe_canary_result(parked_result)
        log = store.reserve_log(queue["id"])
    except ServiceError:
        duration_route_contexts.close()
        raise
    except XPostError as exc:
        duration_route_contexts.close()
        _raise_x_post_error(exc)
    relay_delivery = bool(
        queue.get("delivery_mode") == "premium_relay_repost"
    )
    if log["status"] == "published":
        duration_route_contexts.close()
        return _safe_canary_result(
            {
                "status": "published",
                "log_id": int(log["id"]),
                "short_url": log["short_url"],
                "post_id": log["x_post_id"],
                "preview_url": log["x_post_url"],
            }
        )
    if log["status"] not in (
        {"reserved", "source_published"}
        if relay_delivery
        else {"reserved"}
    ):
        duration_route_contexts.close()
        unknown = bool(log["unknown_outcome"]) or log["status"] in {
            "post_creating",
            "repost_creating",
        }
        code = "x_post_unknown_outcome" if unknown else "x_post_retry_requires_review"
        _raise_x_post_error(
            XPostError(
                code,
                "发布日志已执行，禁止自动重复发帖",
                409,
                unknown,
            )
        )

    if log["status"] == "reserved":
        source_account_id = int(
            queue["relay_account_id"]
            if relay_delivery
            else queue["account_id"]
        )
        try:
            try:
                duration = float(queue.get("preflight_duration", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                duration = 0.0
            deferred_media_validation = bool(
                queue.get("media_validation_mode") == "deferred"
            )
            if relay_delivery or duration > 140.0 or deferred_media_validation:
                verified_source = verify_account(
                    source_account_id,
                    actor,
                    "all",
                    preserve_transient_status=True,
                    require_publish_approved=True,
                )
                if frozen_drama_language and not same_drama_language(
                    verified_source.get("drama_language"),
                    frozen_drama_language,
                ):
                    raise ServiceError(
                        "x_post_account_language_mismatch",
                        "X account drama language no longer matches the frozen queue",
                        409,
                    )
                if (relay_delivery or duration > 140.0) and (
                    not verified_source.get("long_video_publish_eligible")
                    or verified_source.get("protected") is not False
                ):
                    raise ServiceError(
                        "x_post_premium_relay_unavailable",
                        "Long-video account is no longer eligible",
                        409,
                    )
            else:
                verify_account(
                    source_account_id,
                    actor,
                    "all",
                    only_refresh_required=True,
                    preserve_transient_status=True,
                    require_publish_approved=True,
                )
            with contextlib.ExitStack() as publish_contexts:
                prepared_media = duration_prepared_media
                if deferred_media_validation and queue.get("source_type") == "drama":
                    from features.x_posts.publish_media_repair import prepare_deferred_drama_media

                    # GPU repair may outlive the current Access Token. Keep
                    # downloading/repair outside publish_credentials, then
                    # refresh and recheck the exact frozen source afterwards.
                    prepared_media = publish_contexts.enter_context(
                        prepare_deferred_drama_media(
                            queue=queue,
                            log=log,
                            account=verified_source,
                            public_root=POST_PUBLIC_ROOT,
                            allowed_media_hosts=POST_MEDIA_ALLOWED_HOSTS,
                            timeout=POST_HTTP_TIMEOUT_SECONDS,
                            max_media_bytes=POST_MAX_MEDIA_BYTES,
                            storage_guard=preflight_post_storage_request,
                            durable_storage={
                                "mount_root": POST_STORAGE_MOUNT_ROOT,
                                "storage_root": POST_STORAGE_ROOT,
                            },
                        )
                    )
                    if relay_delivery:
                        verified_target = verify_account(
                            int(queue["account_id"]),
                            actor,
                            "all",
                            only_refresh_required=True,
                            preserve_transient_status=True,
                            require_publish_approved=True,
                        )
                        if frozen_drama_language and not same_drama_language(
                            verified_target.get("drama_language"), frozen_drama_language,
                        ):
                            raise ServiceError(
                                "x_post_account_language_mismatch",
                                "X target account drama language no longer matches the frozen queue",
                                409,
                            )
                    verified_source = verify_account(
                        source_account_id,
                        actor,
                        "all",
                        preserve_transient_status=True,
                        require_publish_approved=True,
                    )
                    if frozen_drama_language and not same_drama_language(
                        verified_source.get("drama_language"), frozen_drama_language,
                    ):
                        raise ServiceError(
                            "x_post_account_language_mismatch",
                            "X account drama language no longer matches the frozen queue",
                            409,
                        )
                    if (relay_delivery or duration > 140.0) and (
                        not verified_source.get("long_video_publish_eligible")
                        or verified_source.get("protected") is not False
                    ):
                        raise ServiceError(
                            "x_post_premium_relay_unavailable",
                            "Long-video account is no longer eligible",
                            409,
                        )
                account, access_token = publish_contexts.enter_context(
                    publish_credentials(source_account_id, actor, "all")
                )
                try:
                    if frozen_drama_language and not same_drama_language(
                        account.get("drama_language"),
                        frozen_drama_language,
                    ):
                        raise XPostError(
                            "x_post_account_language_mismatch",
                            "X account drama language no longer matches the frozen queue",
                            409,
                        )
                    if relay_delivery and (
                        not account.get("long_video_publish_eligible")
                        or account.get("protected") is not False
                    ):
                        raise XPostError(
                            "x_post_premium_relay_unavailable",
                            "Frozen relay account is no longer eligible",
                            409,
                        )
                    if expected_manual_trigger_source == "auto_template":
                        store.assert_auto_template_publishable(
                            int(queue["id"]),
                            int(log["id"]),
                        )
                    result = publish_canary(
                        db_path=POST_DB_PATH,
                        queue_id=int(queue["id"]),
                        account=account,
                        access_token=access_token,
                        access_token_provider=publishing_token_provider(
                            source_account_id, actor, frozen_drama_language,
                            require_premium=relay_delivery or duration > 140.0,
                        ),
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
                        **({"prepared_media": prepared_media} if prepared_media is not None else {}),
                    )
                except XPostError as exc:
                    _raise_x_post_error(exc, (access_token,))
        except XPostError as exc:
            # Preparation is still before every X attempt. Persist a known
            # failure so this episode cannot be silently replayed on re-entry.
            try:
                store.mark_failed_if_reserved(log["id"], exc.code, str(exc))
            except XPostError as storage_exc:
                _raise_x_post_error(storage_exc)
            _raise_x_post_error(exc)
        except ServiceError as exc:
            try:
                store.mark_failed_if_reserved(
                    log["id"], exc.code, str(exc)
                )
            except XPostError as storage_exc:
                _raise_x_post_error(storage_exc)
            raise
        finally:
            duration_route_contexts.close()
        if not relay_delivery:
            return _safe_canary_result(result)
        log = store.get_log(log["id"])

    duration_route_contexts.close()
    # The source Post has a confirmed durable ID. Resuming from here can only
    # execute the target Repost; it can never upload or create the source again.
    if not relay_delivery or log["status"] != "source_published":
        _raise_x_post_error(
            XPostError(
                "x_post_repost_state_conflict",
                "Premium relay source is not ready for Repost",
                409,
            )
        )
    target_account_id = int(queue["account_id"])
    try:
        from features.x_posts import XApiClient

        verify_account(
            target_account_id,
            actor,
            "all",
            only_refresh_required=True,
            preserve_transient_status=True,
            require_publish_approved=True,
        )
        with publish_credentials(
            target_account_id, actor, "all"
        ) as (target_account, target_access_token):
            if frozen_drama_language and not same_drama_language(
                target_account.get("drama_language"),
                frozen_drama_language,
            ):
                raise ServiceError(
                    "x_post_account_language_mismatch",
                    "X account drama language no longer matches the frozen queue",
                    409,
                )
            relay = store.mark_reposting(int(queue["id"]))
            try:
                reposted = XApiClient(
                    timeout=POST_HTTP_TIMEOUT_SECONDS
                ).repost(
                    target_access_token,
                    target_account["x_user_id"],
                    relay["source_post_id"],
                )
            except XPostError as exc:
                try:
                    store.mark_repost_failed(
                        int(queue["id"]),
                        exc.code,
                        str(exc),
                        exc.unknown_outcome,
                    )
                except XPostError as storage_exc:
                    _raise_x_post_error(
                        storage_exc, (target_access_token,)
                    )
                _raise_x_post_error(exc, (target_access_token,))
            try:
                store.mark_reposted(
                    int(queue["id"]), reposted.get("repost_id", "")
                )
            except XPostError as exc:
                try:
                    store.mark_repost_failed(
                        int(queue["id"]),
                        "x_repost_outcome_unknown",
                        "X confirmed Repost but the final ledger commit failed",
                        True,
                    )
                except XPostError:
                    pass
                _raise_x_post_error(exc, (target_access_token,))
    except ServiceError:
        raise
    published_log = store.get_log(log["id"])
    return _safe_canary_result(
        {
            "status": "published",
            "log_id": int(published_log["id"]),
            "short_url": published_log["short_url"],
            "post_id": published_log["x_post_id"],
            "preview_url": published_log["x_post_url"],
        }
    )


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
POST_DRAMA_POOL_NAVIGATION_ITEM = "xPostDramaPool"
POST_PAGE_NAVIGATION_ITEMS = frozenset(
    {
        POST_MATERIAL_POOL_NAVIGATION_ITEM,
        POST_DRAMA_POOL_NAVIGATION_ITEM,
    }
)


def _safe_post_account(account):
    account = account if isinstance(account, dict) else {}
    return {
        "id": int(account["id"]),
        "x_user_id": str(account.get("x_user_id", "") or ""),
        "username": str(account.get("username", "") or ""),
        "display_name": str(
            account.get("display_name", "")
            or account.get("username", "")
            or ""
        ),
        "profile_image_url": str(
            account.get("profile_image_url", "") or ""
        ),
        "status": str(account.get("status", "") or ""),
        "publish_approved": bool(account.get("publish_approved")),
        "publish_eligible": bool(account.get("publish_eligible")),
        "access_token_expired": bool(
            account.get("access_token_expired")
        ),
        "refresh_token_available": bool(
            account.get("refresh_token_available")
        ),
        "authorization_refreshable": bool(
            account.get("authorization_refreshable")
        ),
        "access_token_status": str(
            account.get("access_token_status", "") or ""
        ),
        "drama_language": canonical_drama_language(
            account.get("drama_language", DEFAULT_DRAMA_LANGUAGE)
        ),
        "subscription_type": str(
            account.get("subscription_type", "unknown") or "unknown"
        ),
        "premium_subscriber": bool(account.get("premium_subscriber")),
        "long_video_eligible": bool(account.get("long_video_eligible")),
        "long_video_publish_eligible": bool(
            account.get("long_video_publish_eligible")
        ),
        "protected": bool(account.get("protected", True)),
    }


def _premium_relay_accounts(run_date, *, refresh=False, drama_language=None):
    normalized_language = (
        canonical_drama_language(drama_language)
        if drama_language not in (None, "")
        else None
    )
    with _DB_LOCK:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT * FROM x_authorized_account ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
    accounts = []
    for row in rows:
        account = row_to_item(row)
        if not account.get("publish_eligible"):
            continue
        if normalized_language and not same_drama_language(
            account.get("drama_language"), normalized_language
        ):
            continue
        try:
            account = verify_account(
                int(account["id"]),
                CANARY_ACTOR,
                "all",
                preserve_transient_status=True,
                require_publish_approved=True,
            )
        except ServiceError:
            continue
        if normalized_language and not same_drama_language(
            account.get("drama_language"), normalized_language
        ):
            continue
        if (
            account.get("publish_eligible")
            and account.get("long_video_publish_eligible")
            and account.get("protected") is False
        ):
            accounts.append(account)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        loads = XPostStore(POST_DB_PATH).premium_relay_account_loads(
            run_date,
            [int(account["id"]) for account in accounts],
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    by_id = {int(account["id"]): account for account in accounts}
    result = []
    for load in loads:
        account = _safe_post_account(by_id[int(load["account_id"])])
        account["relay_assignment_count"] = int(
            load["relay_assignment_count"]
        )
        result.append(account)
    return result


def premium_relay_accounts_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON body must be an object", 400)
    run_date = str(payload.get("run_date", "") or "").strip()
    try:
        datetime.strptime(run_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ServiceError("invalid_request", "run_date is invalid", 400) from None
    try:
        drama_language = canonical_drama_language(payload.get("drama_language"))
    except ValueError as exc:
        raise ServiceError(
            "x_account_drama_language_invalid",
            clean_text(exc),
            400,
        ) from None
    return {
        "items": _premium_relay_accounts(
            run_date,
            refresh=True,
            drama_language=drama_language,
        )
    }


def auto_template_accounts_request(_payload):
    with _DB_LOCK:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT * FROM x_authorized_account "
                "ORDER BY updated_at DESC,id DESC"
            ).fetchall()
        finally:
            conn.close()
    items = [_safe_post_account(row_to_item(row)) for row in rows]
    return {"items": items, "total": len(items), "updated_at": iso_utc()}


def verify_auto_template_account_request(payload, account_id):
    payload = payload if isinstance(payload, dict) else {}
    allowed = {
        "only_refresh_required",
        "preserve_transient_status",
        "require_publish_approved",
    }
    if set(payload).difference(allowed) or any(
        not isinstance(payload.get(key), bool) for key in payload
    ):
        raise ServiceError("invalid_request", "账号校验请求无效", 400)
    account = verify_account(
        account_id,
        AUTO_TEMPLATE_ACTOR,
        "all",
        only_refresh_required=payload.get("only_refresh_required") is True,
        preserve_transient_status=payload.get("preserve_transient_status") is True,
        require_publish_approved=payload.get("require_publish_approved") is True,
    )
    return {"item": _safe_post_account(account)}


def _post_page_actor(payload, navigation_item):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    actor = require_actor_subject(payload.get("actor", {}))
    scope = str(payload.get("scope", "all") or "all").strip().lower()
    if scope != "all":
        raise ServiceError("x_admin_required", "仅授权用户可维护X素材池", 403)
    requested_navigation_item = clean_text(
        payload.get("navigation_item", ""),
        64,
    )
    if navigation_item not in POST_PAGE_NAVIGATION_ITEMS:
        raise ServiceError(
            "x_admin_required",
            "X Post页面权限配置无效",
            403,
        )
    if (
        actor.get("role") != "admin"
        and requested_navigation_item != navigation_item
    ):
        raise ServiceError(
            "x_admin_required",
            "仅页面授权用户可维护X Post配置",
            403,
        )
    return actor


def _material_pool_actor(payload):
    return _post_page_actor(payload, POST_MATERIAL_POOL_NAVIGATION_ITEM)


def _drama_pool_actor(payload):
    return _post_page_actor(payload, POST_DRAMA_POOL_NAVIGATION_ITEM)


def post_schedule_account_options_request(payload, navigation_item):
    _post_page_actor(payload, navigation_item)
    with _DB_LOCK:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT * FROM x_authorized_account "
                "ORDER BY updated_at DESC,id DESC"
            ).fetchall()
        finally:
            conn.close()
    items = []
    for row in rows:
        items.append(_safe_post_account(row_to_item(row)))
    return {"items": items, "total": len(items), "updated_at": iso_utc()}


def query_post_schedule_request(payload, source_type, navigation_item):
    _post_page_actor(payload, navigation_item)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return {"item": XPostStore(POST_DB_PATH).get_schedule_config(source_type)}
    except XPostError as exc:
        _raise_x_post_error(exc)


def save_post_schedule_request(payload, source_type, navigation_item):
    actor = _post_page_actor(payload, navigation_item)
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        settings = {
            key: payload.get(key)
            for key in (
                "enabled",
                "timezone",
                "account_ids",
                "publish_times",
                "schedule_mode",
                "random_daily_count",
                "body_template",
                "version",
            )
        }
    with _DB_LOCK:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT * FROM x_authorized_account ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
    eligible_ids = [
        int(account["id"])
        for account in (row_to_item(row) for row in rows)
        if account.get("publish_eligible")
    ]
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).save_schedule_config(
            source_type,
            settings,
            actor,
            eligible_account_ids=eligible_ids,
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": item}


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


def _manual_publish_accounts(account_ids):
    if not isinstance(account_ids, list):
        raise ServiceError("invalid_request", "account_ids必须是数组", 400)
    if not account_ids or len(account_ids) > MAX_DAILY_ACCOUNTS:
        raise ServiceError("invalid_request", "手动发布账号数量无效", 400)
    normalized = []
    seen = set()
    for raw in account_ids:
        if isinstance(raw, bool):
            raise ServiceError("invalid_request", "account_id无效", 400)
        try:
            account_id = int(raw)
        except (TypeError, ValueError, OverflowError):
            raise ServiceError("invalid_request", "account_id无效", 400) from None
        if account_id <= 0 or account_id in seen:
            raise ServiceError("invalid_request", "account_ids不能重复", 400)
        account = find_account(account_id)
        if not account.get("publish_eligible"):
            raise ServiceError(
                "x_account_not_publishable",
                "X账号当前不可用于手动发布",
                409,
            )
        seen.add(account_id)
        normalized.append(account)
    return normalized


def _public_manual_run(item):
    """Return only fields needed by the browser status view."""
    source = item if isinstance(item, dict) else {}
    allowed_run = (
        "id",
        "run_date",
        "source_date",
        "publish_mode",
        "scheduled_at",
        "scheduled_timezone",
        "account_ids",
        "material_ids",
        "status",
        "expected_count",
        "queued_count",
        "published_count",
        "failed_count",
        "unknown_count",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        "created",
        "recorded",
    )
    allowed_queue = (
        "id",
        "manual_run_id",
        "run_date",
        "source_date",
        "account_id",
        "account_username",
        "material_id",
        "candidate_rank",
        "queue_status",
        "status",
        "unknown_outcome",
        "preview_url",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
    )
    result = {key: source[key] for key in allowed_run if key in source}
    result["queues"] = [
        {key: queue[key] for key in allowed_queue if key in queue}
        for queue in source.get("queues", [])
        if isinstance(queue, dict)
    ]
    return result


def _auto_template_run(item):
    """Return the frozen execution identity required by the auto runner."""
    source = item if isinstance(item, dict) else {}
    allowed_run = (
        "id",
        "trigger_source",
        "external_task_key",
        "template_ref",
        "template_version",
        "body_template_sha256",
        "run_date",
        "source_date",
        "account_ids",
        "material_ids",
        "body_template",
        "status",
        "expected_count",
        "queued_count",
        "published_count",
        "failed_count",
        "unknown_count",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        "created",
        "recorded",
    )
    allowed_queue = (
        "id",
        "manual_run_id",
        "run_date",
        "source_date",
        "account_id",
        "account_username",
        "material_id",
        "candidate_rank",
        "queue_status",
        "status",
        "unknown_outcome",
        "log_id",
        "post_id",
        "preview_url",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
    )
    result = {key: source[key] for key in allowed_run if key in source}
    result["queues"] = [
        {key: queue[key] for key in allowed_queue if key in queue}
        for queue in source.get("queues", [])
        if isinstance(queue, dict)
    ]
    if result.get("trigger_source") != "auto_template":
        raise ServiceError(
            "x_post_auto_template_invalid_response",
            "X auto template run source is invalid",
            503,
        )
    return result


def create_post_auto_template_run_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON request body must be an object", 400)
    if str(payload.get("actor", "") or "").strip() != AUTO_TEMPLATE_ACTOR_LABEL:
        raise ServiceError(
            "invalid_request",
            "auto template actor is invalid",
            400,
        )
    accounts = _manual_publish_accounts([payload.get("account_id")])
    account = accounts[0]
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).create_auto_template_run(
            payload.get("material_id"),
            int(account["id"]),
            payload.get("external_task_key"),
            payload.get("template_ref"),
            payload.get("template_version"),
            payload.get("body_template"),
            AUTO_TEMPLATE_ACTOR,
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": _auto_template_run(item)}


def query_post_auto_template_run_request(_payload, run_id):
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).get_manual_run(
            run_id,
            "auto_template",
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": _auto_template_run(item)}


def recover_post_auto_template_run_request(_payload, run_id):
    """Recover one exact stranded auto run only when its publish lock is free."""
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    store = XPostStore(POST_DB_PATH)
    try:
        current = store.get_manual_run(run_id, "auto_template")
    except XPostError as exc:
        _raise_x_post_error(exc)
    account_ids = list(current.get("account_ids") or [])
    if len(account_ids) != 1:
        raise ServiceError(
            "x_post_auto_template_scope_mismatch",
            "frozen auto template run scope is invalid",
            409,
        )
    account = find_account(account_ids[0])
    publish_guard = account_lock("x:" + str(account.get("x_user_id", "") or ""))
    if not publish_guard.acquire(blocking=False):
        return {
            "item": {
                "busy": True,
                "recovered": False,
                "run": _auto_template_run(current),
            }
        }
    try:
        try:
            result = store.recover_auto_template_run(run_id)
        except XPostError as exc:
            _raise_x_post_error(exc)
    finally:
        publish_guard.release()
    return {
        "item": {
            "busy": False,
            "recovered": bool(result.get("recovered")),
            "run": _auto_template_run(result.get("run")),
        }
    }


def claim_post_auto_template_run_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON request body must be an object", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        claimed = XPostStore(POST_DB_PATH).claim_manual_run(
            "auto_template"
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    run = claimed.get("run") if isinstance(claimed, dict) else None
    if not claimed.get("found"):
        return {"item": {"found": False, "run": None}}
    return {
        "item": {
            "found": True,
            "run": _auto_template_run(run),
        }
    }


def create_post_auto_template_plan_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON request body must be an object", 400)
    try:
        run_id = int(payload.get("run_id"))
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "run_id is invalid", 400) from None
    candidates = payload.get("candidates")
    if run_id <= 0 or not isinstance(candidates, list) or len(candidates) != 1:
        raise ServiceError(
            "x_post_auto_template_scope_mismatch",
            "auto template plan requires exactly one candidate",
            400,
        )
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    store = XPostStore(POST_DB_PATH)
    try:
        frozen = store.get_manual_run(run_id, "auto_template")
    except XPostError as exc:
        _raise_x_post_error(exc)
    if len(frozen["account_ids"]) != 1 or len(frozen["material_ids"]) != 1:
        raise ServiceError(
            "x_post_auto_template_scope_mismatch",
            "frozen auto template run scope is invalid",
            409,
        )
    account = _manual_publish_accounts(list(frozen["account_ids"]))[0]
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise ServiceError("invalid_request", "candidate must be an object", 400)
    try:
        candidate_language = canonical_drama_language(
            candidate.get("material_language")
            or candidate.get("language")
        )
    except ValueError as exc:
        raise ServiceError(
            "x_account_drama_language_invalid",
            clean_text(exc),
            400,
        ) from None
    if not same_drama_language(
        account.get("drama_language"), candidate_language
    ):
        raise ServiceError(
            "x_auto_account_language_mismatch",
            "X account drama language does not match the frozen auto task language",
            409,
        )
    _require_candidate_duration_capability(candidate, account)
    try:
        duration = float(candidate.get("preflight_duration", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "preflight_duration is invalid", 400) from None
    if not math.isfinite(duration) or duration < 0:
        raise ServiceError("invalid_request", "preflight_duration is invalid", 400)
    if duration <= 0 or duration > AUTO_TEMPLATE_MAX_DURATION_SECONDS:
        raise ServiceError(
            "x_post_auto_template_duration_exceeded",
            "automatic X materials cannot exceed 600 seconds",
            409,
        )
    trusted = dict(candidate)
    trusted.update(
        {
            "account_id": int(account["id"]),
            "account_username": str(account.get("username", "") or ""),
            "page_name": str(
                account.get("display_name", "")
                or account.get("username", "")
                or ""
            ),
            "page_id": str(account.get("x_user_id", "") or ""),
            "account_drama_language": canonical_drama_language(
                account.get("drama_language")
            ),
        }
    )
    preflight_post_storage_request(1)
    try:
        item = store.create_manual_plan(
            run_id,
            [trusted],
            "auto_template",
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": _auto_template_run(item)}


def record_post_auto_template_failure_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON request body must be an object", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).record_manual_failure(
            payload.get("run_id"),
            payload.get("error_code"),
            payload.get("error_message") or payload.get("message"),
            "auto_template",
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": _auto_template_run(item)}


def query_post_auto_template_material_keys_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON request body must be an object", 400)
    values = payload.get("material_keys")
    if values is None:
        values = payload.get("material_ids")
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        material_keys = XPostStore(POST_DB_PATH).query_material_keys(
            values,
            include_pool=True,
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": {"material_keys": material_keys}}


def create_post_manual_run_request(payload):
    actor = _material_pool_actor(payload)
    accounts = _manual_publish_accounts(payload.get("account_ids"))
    material_ids = payload.get("material_ids")
    if not isinstance(material_ids, list) or len(material_ids) != len(accounts):
        raise ServiceError(
            "x_post_manual_scope_mismatch",
            "手动发布的素材数必须与目标账号数一致",
            400,
        )
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).create_manual_run(
            material_ids,
            [int(account["id"]) for account in accounts],
            payload.get("idempotency_key"),
            actor,
            publish_mode=payload.get("publish_mode", "immediate"),
            scheduled_at=payload.get("scheduled_at", ""),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": _public_manual_run(item)}


def query_post_manual_run_request(payload, run_id):
    _material_pool_actor(payload)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).get_manual_run(run_id, "manual")
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": _public_manual_run(item)}


def claim_post_manual_run_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).claim_manual_run("manual")
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": item}


def create_post_manual_plan_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    try:
        run_id = int(payload.get("run_id"))
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "run_id无效", 400) from None
    candidates = payload.get("candidates")
    if run_id <= 0 or not isinstance(candidates, list):
        raise ServiceError("invalid_request", "手动发布计划无效", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    store = XPostStore(POST_DB_PATH)
    try:
        frozen = store.get_manual_run(run_id, "manual")
    except XPostError as exc:
        _raise_x_post_error(exc)
    if len(candidates) != len(frozen["account_ids"]):
        raise ServiceError(
            "x_post_manual_candidate_shortage",
            "手动发布候选数量与冻结账号数量不一致",
            409,
        )
    accounts = _manual_publish_accounts(list(frozen["account_ids"]))
    trusted = []
    for candidate, account in zip(candidates, accounts):
        if not isinstance(candidate, dict):
            raise ServiceError("invalid_request", "candidate必须是对象", 400)
        _require_candidate_duration_capability(candidate, account)
        item = dict(candidate)
        item.update(
            {
                "account_id": int(account["id"]),
                "account_username": str(account.get("username", "") or ""),
                "page_name": str(
                    account.get("display_name", "")
                    or account.get("username", "")
                    or ""
                ),
                "page_id": str(account.get("x_user_id", "") or ""),
            }
        )
        trusted.append(item)
    preflight_post_storage_request(len(trusted))
    try:
        item = store.create_manual_plan(run_id, trusted, "manual")
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": item}


def record_post_manual_failure_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).record_manual_failure(
            payload.get("run_id"),
            payload.get("error_code"),
            payload.get("error_message") or payload.get("message"),
            "manual",
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": item}


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


def query_post_drama_pool_request(payload):
    _drama_pool_actor(payload)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        query = dict(payload)
        query.pop("navigation_item", None)
        return XPostStore(POST_DB_PATH).query_drama_pool(query)
    except XPostError as exc:
        _raise_x_post_error(exc)


def add_post_drama_pool_request(payload):
    actor = _drama_pool_actor(payload)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return XPostStore(POST_DB_PATH).add_drama_pool_items(
            payload.get("drama_ids"),
            payload.get("validation_checks"),
            actor,
        )
    except XPostError as exc:
        _raise_x_post_error(exc)


def set_post_drama_pool_priority_request(payload, pool_item_id):
    actor = _drama_pool_actor(payload)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).set_drama_pool_priority(
            pool_item_id,
            payload.get("high_priority"),
            actor,
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"item": item}


def delete_post_drama_pool_request(payload, pool_item_id):
    _drama_pool_actor(payload)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return XPostStore(POST_DB_PATH).delete_drama_pool_item(pool_item_id)
    except XPostError as exc:
        _raise_x_post_error(exc)


def batch_delete_post_drama_pool_request(payload):
    _drama_pool_actor(payload)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return XPostStore(POST_DB_PATH).delete_drama_pool_items(
            payload.get("pool_item_ids")
        )
    except XPostError as exc:
        _raise_x_post_error(exc)


def query_post_drama_pool_episodes_request(payload, pool_item_id):
    _drama_pool_actor(payload)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return XPostStore(POST_DB_PATH).query_drama_pool_episodes(
            pool_item_id,
            payload,
        )
    except XPostError as exc:
        _raise_x_post_error(exc)


def available_post_drama_pool_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        account_ids = payload.get("account_ids")
        accounts = (
            _manual_publish_accounts(account_ids)
            if account_ids is not None
            else []
        )
        return {
            "items": XPostStore(POST_DB_PATH).available_drama_pool_items(
                payload.get("limit", 50),
                account_ids=account_ids,
                account_languages={
                    int(account["id"]): account.get(
                        "drama_language", DEFAULT_DRAMA_LANGUAGE
                    )
                    for account in accounts
                },
                premium_account_ids=[
                    int(account["id"])
                    for account in accounts
                    if account.get("long_video_eligible")
                ],
                configured_account_ids=payload.get("configured_account_ids"),
            )
        }
    except XPostError as exc:
        _raise_x_post_error(exc)


def record_post_drama_pool_checks_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError(
            "invalid_request",
            "JSON request body must be an object",
            400,
        )
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        return XPostStore(POST_DB_PATH).record_drama_pool_checks(
            payload.get("checks"),
            validate_only=payload.get("validate_only", False),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)


def _safe_schedule_queue(queue):
    if not isinstance(queue, dict):
        raise ServiceError(
            "x_posts_unavailable",
            "X定时发布队列返回无效",
            503,
        )
    try:
        queue_id = int(queue.get("id") or 0)
        account_id = int(queue.get("account_id") or 0)
        candidate_rank = int(queue.get("candidate_rank") or 0)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError(
            "x_posts_unavailable",
            "X定时发布队列返回无效",
            503,
        ) from None
    if queue_id <= 0 or account_id <= 0 or candidate_rank <= 0:
        raise ServiceError(
            "x_posts_unavailable",
            "X定时发布队列返回无效",
            503,
        )
    status = str(queue.get("status", "") or "")
    if status not in {
        "queued",
        "waiting_relay",
        "reserved",
        "publishing",
        "published",
        "failed",
    }:
        raise ServiceError(
            "x_posts_unavailable",
            "X定时发布队列状态无效",
            503,
        )
    error_code = str(queue.get("error_code", "") or "").strip()
    if len(error_code) > 64 or (
        error_code and not re.fullmatch(r"[A-Za-z0-9_.:-]+", error_code)
    ):
        raise ServiceError(
            "x_posts_unavailable",
            "X定时发布队列错误码无效",
            503,
        )
    delivery_mode = str(
        queue.get("delivery_mode", "direct") or "direct"
    )
    route_state = str(queue.get("route_state", "") or "")
    resolved_delivery_mode = str(
        queue.get("resolved_delivery_mode", "") or ""
    )
    repost_status = str(queue.get("repost_status", "") or "")
    relay_account_id = int(queue.get("relay_account_id") or 0)
    relay_account_username = str(
        queue.get("relay_account_username", "") or ""
    ).strip().lstrip("@")
    try:
        preflight_duration = float(queue.get("preflight_duration", 0) or 0)
        preflight_width = int(queue.get("preflight_width", 0) or 0)
        preflight_height = int(queue.get("preflight_height", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError(
            "x_posts_unavailable",
            "X schedule media evidence is invalid",
            503,
        ) from None
    if (
        not math.isfinite(preflight_duration)
        or preflight_duration < 0
        or preflight_width < 0
        or preflight_height < 0
        or (
            relay_account_username
            and not re.fullmatch(r"[A-Za-z0-9_]{1,50}", relay_account_username)
        )
    ):
        raise ServiceError(
            "x_posts_unavailable",
            "X schedule media evidence is invalid",
            503,
        )
    if delivery_mode not in {
        "duration_pending",
        "direct",
        "premium_relay_repost",
    } or (
        delivery_mode == "duration_pending"
        and (
            route_state not in {"duration_pending", "waiting_relay"}
            or resolved_delivery_mode
            or relay_account_id != 0
            or relay_account_username
            or repost_status
            or status == "waiting_relay" and route_state != "waiting_relay"
        )
    ) or (
        delivery_mode == "direct"
        and (relay_account_id != 0 or repost_status)
    ) or (
        delivery_mode == "premium_relay_repost"
        and (
            relay_account_id <= 0
            or repost_status
            not in {
                "reserved",
                "source_publishing",
                "source_published",
                "reposting",
                "reposted",
                "failed",
                "needs_review",
            }
        )
    ):
        raise ServiceError(
            "x_posts_unavailable",
            "X schedule relay state is invalid",
            503,
        )
    return {
        "id": queue_id,
        "account_id": account_id,
        "candidate_rank": candidate_rank,
        "status": status,
        "error_code": error_code,
        "unknown_outcome": bool(queue.get("unknown_outcome", False)),
        "delivery_mode": delivery_mode,
        "route_state": route_state,
        "resolved_delivery_mode": resolved_delivery_mode,
        "relay_account_id": relay_account_id,
        "relay_account_username": relay_account_username,
        "repost_status": repost_status,
        "preflight_duration": preflight_duration,
        "preflight_width": preflight_width,
        "preflight_height": preflight_height,
    }


def _safe_schedule_plan_query_result(result):
    if not isinstance(result, dict) or not isinstance(result.get("found"), bool):
        raise ServiceError(
            "x_posts_unavailable",
            "X定时发布计划查询返回无效",
            503,
        )
    if not result["found"]:
        if result.get("run") is not None or result.get("queues") != []:
            raise ServiceError(
                "x_posts_unavailable",
                "X定时发布计划查询返回无效",
                503,
            )
        return {"found": False, "run": None, "queues": []}
    run = result.get("run")
    queues = result.get("queues")
    if not isinstance(run, dict) or not isinstance(queues, list):
        raise ServiceError(
            "x_posts_unavailable",
            "X定时发布计划查询返回无效",
            503,
        )
    allowed_run = (
        "id",
        "slot_key",
        "source_type",
        "run_date",
        "publish_time",
        "timezone",
        "config_version",
        "account_ids",
        "status",
        "expected_count",
        "queued_count",
        "published_count",
        "failed_count",
        "unknown_count",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "plan_attempted_at",
        "created_at",
        "updated_at",
    )
    return {
        "found": True,
        "run": {key: run[key] for key in allowed_run if key in run},
        "queues": [_safe_schedule_queue(queue) for queue in queues],
    }


def due_post_schedules_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    try:
        requested_now = datetime.fromisoformat(
            str(payload.get("now", "") or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        raise ServiceError("invalid_request", "now无效", 400) from None
    if requested_now.tzinfo is None:
        raise ServiceError("invalid_request", "now必须包含时区", 400)
    requested_now = requested_now.astimezone(timezone(timedelta(hours=8)))
    server_now = datetime.now(timezone(timedelta(hours=8)))
    if abs((server_now - requested_now).total_seconds()) > 120:
        raise ServiceError(
            "x_post_schedule_clock_skew",
            "调度器时间与服务端时间不一致",
            409,
        )
    if (
        str(payload.get("run_date", "") or "")
        != server_now.date().isoformat()
        or payload.get("grace_seconds") != 90
    ):
        raise ServiceError(
            "invalid_request",
            "调度时间范围无效",
            400,
        )
    try:
        limit = int(payload.get("limit", 4))
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "limit无效", 400) from None
    if limit < 1 or limit > 10:
        raise ServiceError("invalid_request", "limit无效", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        result = XPostStore(POST_DB_PATH).due_schedule_slots(
            now=server_now,
            grace_seconds=90,
            limit=limit,
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list) or len(items) > limit:
        raise ServiceError(
            "x_posts_unavailable",
            "X定时发布待执行范围超过冻结上限",
            503,
        )
    return {"items": list(items)}


def previous_day_due_post_schedules_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    reason = str(payload.get("operator_reason", "") or "")
    deployed_commit = str(payload.get("deployed_commit", "") or "").lower()
    if (
        reason != "operator_previous_day_stale_claim_recovery_v1"
        or not re.fullmatch(r"[a-f0-9]{40}", deployed_commit)
        or payload.get("grace_seconds") != 90
    ):
        raise ServiceError(
            "x_post_previous_day_runner_not_allowed",
            "跨日补偿请求未通过审计约束",
            409,
        )
    try:
        requested_now = datetime.fromisoformat(
            str(payload.get("now", "") or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        raise ServiceError("invalid_request", "now无效", 400) from None
    if requested_now.tzinfo is None:
        raise ServiceError("invalid_request", "now必须包含时区", 400)
    requested_now = requested_now.astimezone(timezone(timedelta(hours=8)))
    server_now = datetime.now(timezone(timedelta(hours=8)))
    run_date = str(payload.get("run_date", "") or "")
    if (
        run_date != (server_now.date() - timedelta(days=1)).isoformat()
        or requested_now.date().isoformat() != run_date
    ):
        raise ServiceError(
            "x_post_previous_day_runner_date_conflict",
            "跨日补偿日期范围无效",
            409,
        )
    try:
        limit = int(payload.get("limit", 4))
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "limit无效", 400) from None
    if limit < 1 or limit > 10:
        raise ServiceError("invalid_request", "limit无效", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        result = XPostStore(POST_DB_PATH).previous_day_recovered_schedule_slots(
            run_date,
            deployed_commit,
            now=server_now,
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {"items": list(result.get("items", []))[:limit]}


def query_post_schedule_plan_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        result = XPostStore(POST_DB_PATH).query_schedule_plan(
            payload.get("source_type"),
            payload.get("run_date"),
            payload.get("publish_time"),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    safe = _safe_schedule_plan_query_result(result)
    if safe["found"] and int(
        safe["run"].get("config_version") or 0
    ) != int(payload.get("version") or 0):
        raise ServiceError(
            "x_post_schedule_run_exists",
            "该时间点已存在其他版本的冻结计划",
            409,
        )
    return safe


def create_post_schedule_plan_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    store = XPostStore(POST_DB_PATH)
    candidates = payload.get("candidates")
    account_ids = payload.get("account_ids")
    if not isinstance(candidates, list) or not isinstance(account_ids, list):
        raise ServiceError(
            "invalid_request",
            "定时发布计划账号或候选无效",
            400,
        )
    try:
        requested_ids = [int(value) for value in account_ids]
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "account_ids无效", 400) from None
    if (
        not requested_ids
        or len(requested_ids) != len(candidates)
        or len(set(requested_ids)) != len(requested_ids)
        or any(value <= 0 for value in requested_ids)
    ):
        raise ServiceError("invalid_request", "account_ids无效", 400)
    trusted = []
    premium_account_ids = []
    premium_relay_accounts = {}
    source_type = str(payload.get("source_type", "") or "").strip().lower()
    run_date = str(payload.get("run_date", "") or "").strip()
    fifo_capacity_skips = payload.get("fifo_capacity_skips", [])
    if not isinstance(fifo_capacity_skips, list):
        raise ServiceError(
            "invalid_request", "fifo_capacity_skips无效", 400
        )
    material_language_capacities = {}
    if fifo_capacity_skips:
        if source_type != "material":
            raise ServiceError(
                "invalid_request", "短剧计划不接受素材容量证据", 400
            )
        try:
            frozen = store.query_schedule_plan(
                source_type,
                payload.get("run_date"),
                payload.get("publish_time"),
            )
        except XPostError as exc:
            _raise_x_post_error(exc)
        if not frozen.get("found"):
            raise ServiceError(
                "x_post_schedule_plan_incomplete",
                "素材容量证据必须绑定已冻结批次",
                409,
            )
        frozen_blockers = store.schedule_account_blockers(frozen["run"]["account_ids"])
        for frozen_account_id in frozen["run"]["account_ids"]:
            if frozen_account_id in frozen_blockers:
                continue
            account = find_account(int(frozen_account_id))
            try:
                language = canonical_drama_language(
                    account.get("drama_language")
                )
            except ValueError as exc:
                raise ServiceError(
                    "x_account_drama_language_invalid",
                    clean_text(exc),
                    400,
                ) from None
            material_language_capacities[language] = (
                material_language_capacities.get(language, 0) + 1
            )
    for candidate, account_id in zip(candidates, requested_ids):
        if not isinstance(candidate, dict):
            raise ServiceError("invalid_request", "candidate必须是对象", 400)
        account = find_account(account_id)
        if not account.get("publish_eligible"):
            raise ServiceError(
                "x_account_not_publishable",
                "X账号当前状态不可用于发布",
                409,
            )
        try:
            candidate_language = canonical_drama_language(
                candidate.get("material_language")
                or candidate.get("language")
            )
        except ValueError as exc:
            raise ServiceError(
                "x_account_drama_language_invalid",
                clean_text(exc),
                400,
            ) from None
        if not same_drama_language(
            account.get("drama_language"), candidate_language
        ):
            raise ServiceError(
                "x_post_account_language_mismatch",
                "X account drama language does not match the candidate language",
                409,
            )
        try:
            duration = float(candidate.get("preflight_duration", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            raise ServiceError(
                "invalid_request", "preflight_duration is invalid", 400
            ) from None
        requested_delivery_mode = str(
            candidate.get("delivery_mode", "direct") or "direct"
        ).strip().lower()
        duration_pending = requested_delivery_mode == "duration_pending"
        if duration_pending and not (
            POST_DRAMA_DURATION_ROUTING_ENABLED and source_type == "drama"
        ):
            raise ServiceError(
                "invalid_request",
                "duration_pending is restricted to enabled short-drama schedules",
                400,
            )
        if (
            POST_DRAMA_DURATION_ROUTING_ENABLED
            and source_type == "drama"
            and not duration_pending
        ):
            raise ServiceError(
                "invalid_request",
                "new short-drama schedules must defer routing until final duration is known",
                400,
            )
        relay_required = bool(
            not duration_pending
            and
            source_type in {"drama", "material"}
            and math.isfinite(duration)
            and duration > 140.0
            and not account.get("long_video_publish_eligible")
        )
        if relay_required:
            if candidate_language not in premium_relay_accounts:
                premium_relay_accounts[candidate_language] = _premium_relay_accounts(
                    run_date,
                    refresh=False,
                    drama_language=candidate_language,
                )
            selectable_relays = [
                relay
                for relay in premium_relay_accounts[candidate_language]
                if int(relay["id"]) != int(account["id"])
            ]
            if not selectable_relays:
                raise ServiceError(
                    "x_post_premium_relay_unavailable",
                    "No currently eligible public Premium relay account is available",
                    409,
                )
            selected_relay = selectable_relays[0]
            if source_type == "material":
                try:
                    requested_relay_id = int(
                        candidate.get("relay_account_id") or 0
                    )
                except (TypeError, ValueError, OverflowError):
                    requested_relay_id = 0
                selected_relay = next(
                    (
                        relay
                        for relay in selectable_relays
                        if int(relay["id"]) == requested_relay_id
                    ),
                    None,
                )
                if selected_relay is None:
                    raise ServiceError(
                        "x_post_premium_relay_unavailable",
                        "Frozen same-language material relay account is no longer eligible",
                        409,
                    )
        elif not duration_pending:
            _require_candidate_duration_capability(candidate, account)
        if account.get("long_video_eligible"):
            premium_account_ids.append(int(account["id"]))
        item = dict(candidate)
        item.update(
            {
                "account_id": int(account["id"]),
                "account_username": str(
                    account.get("username", "") or ""
                ),
                "page_name": str(
                    account.get("display_name", "")
                    or account.get("username", "")
                    or ""
                ),
                "page_id": str(account.get("x_user_id", "") or ""),
                "account_drama_language": canonical_drama_language(
                    account.get("drama_language")
                ),
            }
        )
        if duration_pending:
            item.update(
                {
                    "delivery_mode": "duration_pending",
                    "relay_account_id": 0,
                    "relay_account_username": "",
                }
            )
        elif relay_required:
            item.update(
                {
                    "delivery_mode": "premium_relay_repost",
                    "relay_account_id": int(selected_relay["id"]),
                    "relay_account_username": str(
                        selected_relay["username"]
                    ),
                }
            )
        else:
            item.update(
                {
                    "delivery_mode": "direct",
                    "relay_account_id": 0,
                    "relay_account_username": "",
                }
            )
        trusted.append(item)
    # Schedule queues download and probe one media file at a time. Reserve
    # capacity for that serialized working set, not the whole frozen batch.
    account_languages = None
    if source_type == "drama":
        frozen = store.query_schedule_plan(
            source_type, payload.get("run_date"), payload.get("publish_time")
        )
        configured_ids = (
            frozen["run"]["account_ids"] if frozen.get("found")
            else store.get_schedule_config(source_type)["account_ids"]
        )
        # Reservation must replay the same language-aware selection as the
        # available-pool endpoint, including eligible accounts omitted by
        # candidate-local preflight. Request fields cannot supply this map.
        account_languages = {
            int(account_id): canonical_drama_language(
                find_account(int(account_id)).get("drama_language")
            )
            for account_id in configured_ids
        }
    preflight_post_storage_request(1)
    try:
        result = store.create_schedule_plan(
            payload.get("source_type"),
            payload.get("run_date"),
            payload.get("publish_time"),
            payload.get("version"),
            trusted,
            premium_account_ids=premium_account_ids,
            premium_relay_accounts=[
                account
                for accounts in premium_relay_accounts.values()
                for account in accounts
            ],
            fifo_capacity_skips=fifo_capacity_skips,
            material_language_capacities=material_language_capacities,
            account_languages=account_languages,
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    queues = [
        _safe_schedule_queue(queue)
        for queue in result.get("queues", [])
    ]
    if [queue["account_id"] for queue in queues] != requested_ids:
        raise ServiceError(
            "x_posts_unavailable",
            "X定时发布计划账号范围不一致",
            503,
        )
    return {
        "run": {
            key: result[key]
            for key in (
                "id",
                "source_type",
                "run_date",
                "publish_time",
                "config_version",
                "account_ids",
                "status",
                "created",
            )
            if key in result
        },
        "queues": queues,
    }


def record_post_schedule_failure_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        result = XPostStore(POST_DB_PATH).record_schedule_failure(
            payload.get("source_type"),
            payload.get("run_date"),
            payload.get("publish_time"),
            payload.get("version"),
            payload.get("account_ids"),
            payload.get("error_code"),
            payload.get("error_message") or payload.get("message"),
            drama_pool_item_id=payload.get("drama_pool_item_id"),
            content_id=payload.get("content_id"),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {
        key: result[key]
        for key in (
            "id",
            "source_type",
            "run_date",
            "publish_time",
            "config_version",
            "account_ids",
            "status",
            "error_code",
            "error_message",
            "recorded",
        )
        if key in result
    }


def heartbeat_post_schedule_run_request(payload):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        result = XPostStore(POST_DB_PATH).heartbeat_schedule_run(
            payload.get("source_type"),
            payload.get("run_date"),
            payload.get("publish_time"),
            payload.get("version"),
            payload.get("account_ids"),
            plan_attempt=payload.get("plan_attempt", False),
        )
    except XPostError as exc:
        _raise_x_post_error(exc)
    return {
        key: result[key]
        for key in (
            "id",
            "source_type",
            "run_date",
            "publish_time",
            "config_version",
            "account_ids",
            "status",
            "heartbeat_recorded",
            "plan_attempt_recorded",
        )
        if key in result
    }


def record_post_run_failure_request(payload, allowed_account_ids=None):
    if not isinstance(payload, dict):
        raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
    raw_expected_count = payload.get("expected_count")
    if isinstance(raw_expected_count, bool):
        raise ServiceError("invalid_request", "expected_count无效", 400)
    try:
        expected_count = int(raw_expected_count)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError("invalid_request", "expected_count无效", 400) from None
    allowed_accounts = _daily_account_scope(allowed_account_ids)
    if (
        expected_count < 1
        or expected_count > MAX_DAILY_ACCOUNTS
        or (
            allowed_accounts is not None
            and expected_count != len(allowed_accounts)
        )
    ):
        raise ServiceError(
            "x_daily_account_scope_denied",
            "X每日发布账号范围配置无效",
            403,
        )
    XPostError, XPostStore, _publish_canary = _x_posts_api()
    try:
        item = XPostStore(POST_DB_PATH).record_run_failure(
            payload.get("run_date"),
            payload.get("source_date"),
            payload.get("error_code"),
            payload.get("error_message") or payload.get("message"),
            expected_count,
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
        if AUTO_INTERNAL_TOKEN and secrets.compare_digest(token, AUTO_INTERNAL_TOKEN):
            return "auto"
        return ""

    def is_internal_authorized(self):
        """Backward-compatible boolean used by older diagnostics."""
        return bool(self.internal_role())

    def require_internal(self, allow_daily=False, allow_auto=False):
        role = self.internal_role()
        if (
            role == "backend"
            or (allow_daily and role == "daily")
            or (allow_auto and role == "auto")
        ):
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
        auto_verify_match = re.fullmatch(
            r"/internal/posts/auto-template/accounts/([0-9]+)/verify",
            parsed.path,
        )
        auto_run_query_match = re.fullmatch(
            r"/internal/posts/auto-template/runs/([0-9]+)/query",
            parsed.path,
        )
        auto_run_recover_match = re.fullmatch(
            r"/internal/posts/auto-template/runs/([0-9]+)/recover",
            parsed.path,
        )
        auto_publish_match = re.fullmatch(
            r"/internal/posts/auto-template/queue/([0-9]+)/publish",
            parsed.path,
        )
        pool_delete_match = re.fullmatch(
            r"/internal/posts/material-pool/([0-9]+)/delete", parsed.path
        )
        drama_pool_delete_match = re.fullmatch(
            r"/internal/posts/drama-pool/([0-9]+)/delete",
            parsed.path,
        )
        drama_pool_episodes_match = re.fullmatch(
            r"/internal/posts/drama-pool/([0-9]+)/episodes",
            parsed.path,
        )
        drama_pool_priority_match = re.fullmatch(
            r"/internal/posts/drama-pool/([0-9]+)/priority",
            parsed.path,
        )
        manual_run_query_match = re.fullmatch(
            r"/internal/posts/manual-runs/([0-9]+)/query",
            parsed.path,
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
        catchup_exact_paths = {
            "/internal/posts/catchup-plan",
            "/internal/posts/catchup-plan/query",
            "/internal/posts/catchup-runs/record-failure",
        }
        schedule_exact_paths = {
            "/internal/posts/schedules/due",
            "/internal/posts/schedules/previous-day-due",
            "/internal/posts/schedule-plan",
            "/internal/posts/schedule-plan/query",
            "/internal/posts/schedule-runs/heartbeat",
            "/internal/posts/schedule-runs/record-failure",
            "/internal/posts/drama-pool/available",
            "/internal/posts/drama-pool/check",
            "/internal/posts/premium-relay/accounts",
        }
        manual_worker_exact_paths = {
            "/internal/posts/manual-runs/claim",
            "/internal/posts/manual-plan",
            "/internal/posts/manual-runs/record-failure",
        }
        auto_exact_paths = {
            "/internal/posts/auto-template/accounts",
            "/internal/posts/auto-template/material-keys/query",
            "/internal/posts/auto-template/runs/create",
            "/internal/posts/auto-template/runs/claim",
            "/internal/posts/auto-template/plan",
            "/internal/posts/auto-template/runs/record-failure",
            "/internal/posts/auto-template/storage/preflight",
        }
        is_auto_route = bool(
            parsed.path in auto_exact_paths
            or auto_verify_match
            or auto_run_query_match
            or auto_run_recover_match
            or auto_publish_match
        )
        allow_daily = bool(
            parsed.path in daily_exact_paths
            or parsed.path in catchup_exact_paths
            or parsed.path in schedule_exact_paths
            or parsed.path in manual_worker_exact_paths
            or daily_verify_match
            or daily_publish_match
        )
        internal_role = self.require_internal(
            allow_daily=allow_daily,
            allow_auto=is_auto_route,
        )
        if not internal_role:
            return
        if is_auto_route and internal_role != "auto":
            self.send_json(
                403,
                {
                    "error": "x_auto_internal_required",
                    "message": "X auto template routes require the auto internal bearer",
                },
            )
            return
        if (
            parsed.path
            in catchup_exact_paths.union(schedule_exact_paths).union(
                manual_worker_exact_paths
            )
            and internal_role != "daily"
        ):
            self.send_json(
                403,
                {
                    "error": "x_daily_internal_required",
                    "message": "X scheduled routes require the daily internal bearer",
                },
            )
            return
        try:
            request_body_limit = MAX_BODY_BYTES
            if parsed.path in {
                "/internal/posts/daily-plan",
                "/internal/posts/catchup-plan",
                "/internal/posts/schedule-plan",
                "/internal/posts/manual-plan",
                "/internal/posts/auto-template/plan",
            }:
                request_body_limit = MAX_DAILY_PLAN_BODY_BYTES
            elif parsed.path in {
                "/internal/posts/material-pool/check",
                "/internal/posts/auto-template/material-keys/query",
            }:
                request_body_limit = MAX_DAILY_CHECK_BODY_BYTES
            elif parsed.path == "/internal/posts/drama-pool/add":
                request_body_limit = MAX_DRAMA_POOL_BODY_BYTES
            payload = self.read_json(
                request_body_limit
            )
            if parsed.path == "/internal/posts/auto-template/accounts":
                self.send_json(200, auto_template_accounts_request(payload))
                return
            if auto_verify_match:
                self.send_json(
                    200,
                    verify_auto_template_account_request(
                        payload,
                        auto_verify_match.group(1),
                    ),
                )
                return
            if parsed.path == "/internal/posts/auto-template/material-keys/query":
                self.send_json(
                    200,
                    query_post_auto_template_material_keys_request(payload),
                )
                return
            if parsed.path == "/internal/posts/auto-template/runs/create":
                self.send_json(
                    200,
                    create_post_auto_template_run_request(payload),
                )
                return
            if auto_run_query_match:
                self.send_json(
                    200,
                    query_post_auto_template_run_request(
                        payload,
                        auto_run_query_match.group(1),
                    ),
                )
                return
            if auto_run_recover_match:
                self.send_json(
                    200,
                    recover_post_auto_template_run_request(
                        payload,
                        auto_run_recover_match.group(1),
                    ),
                )
                return
            if parsed.path == "/internal/posts/auto-template/runs/claim":
                self.send_json(
                    200,
                    claim_post_auto_template_run_request(payload),
                )
                return
            if parsed.path == "/internal/posts/auto-template/plan":
                self.send_json(
                    200,
                    create_post_auto_template_plan_request(payload),
                )
                return
            if parsed.path == "/internal/posts/auto-template/runs/record-failure":
                self.send_json(
                    200,
                    record_post_auto_template_failure_request(payload),
                )
                return
            if parsed.path == "/internal/posts/auto-template/storage/preflight":
                self.send_json(
                    200,
                    {"item": preflight_post_storage_request(1)},
                )
                return
            if auto_publish_match:
                published = publish_queue_request(
                    auto_publish_match.group(1),
                    (),
                    allow_schedule=False,
                    allow_manual=True,
                    expected_manual_trigger_source="auto_template",
                    require_manual_parent=True,
                )
                self.send_json(200, {"item": published})
                return
            if parsed.path == "/internal/posts/canary":
                self.send_json(200, {"item": publish_canary_request(payload)})
                return
            if parsed.path == "/internal/posts/manual-runs/claim":
                self.send_json(200, claim_post_manual_run_request(payload))
                return
            if parsed.path == "/internal/posts/manual-plan":
                self.send_json(200, create_post_manual_plan_request(payload))
                return
            if parsed.path == "/internal/posts/manual-runs/record-failure":
                self.send_json(
                    200,
                    record_post_manual_failure_request(payload),
                )
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
            if parsed.path == "/internal/posts/catchup-plan/query":
                self.send_json(
                    200,
                    {
                        "item": query_catchup_plan_request(
                            payload,
                            DAILY_ACCOUNT_IDS,
                        )
                    },
                )
                return
            if parsed.path == "/internal/posts/catchup-plan":
                self.send_json(
                    200,
                    {
                        "item": create_catchup_plan_request(
                            payload,
                            DAILY_ACCOUNT_IDS,
                            require_pool=True,
                        )
                    },
                )
                return
            if parsed.path == "/internal/posts/catchup-runs/record-failure":
                self.send_json(
                    200,
                    {
                        "item": record_catchup_failure_request(
                            payload,
                            DAILY_ACCOUNT_IDS,
                        )
                    },
                )
                return
            if parsed.path == "/internal/posts/schedules/due":
                self.send_json(200, due_post_schedules_request(payload))
                return
            if parsed.path == "/internal/posts/schedules/previous-day-due":
                self.send_json(
                    200,
                    previous_day_due_post_schedules_request(payload),
                )
                return
            if parsed.path == "/internal/posts/schedule-plan/query":
                self.send_json(
                    200,
                    {
                        "item": query_post_schedule_plan_request(payload)
                    },
                )
                return
            if parsed.path == "/internal/posts/schedule-plan":
                self.send_json(
                    200,
                    {
                        "item": create_post_schedule_plan_request(payload)
                    },
                )
                return
            if parsed.path == "/internal/posts/schedule-runs/record-failure":
                self.send_json(
                    200,
                    {
                        "item": record_post_schedule_failure_request(
                            payload
                        )
                    },
                )
                return
            if parsed.path == "/internal/posts/schedule-runs/heartbeat":
                self.send_json(
                    200,
                    {
                        "item": heartbeat_post_schedule_run_request(
                            payload
                        )
                    },
                )
                return
            if parsed.path == "/internal/posts/storage/preflight":
                self.send_json(
                    200,
                    {
                        "item": preflight_post_storage_request(
                            len(DAILY_ACCOUNT_IDS) if internal_role == "daily" else 1
                        )
                    },
                )
                return
            if parsed.path == "/internal/posts/logs/query":
                self.send_json(200, query_post_logs_request(payload))
                return
            if parsed.path == "/internal/posts/runs/query":
                self.send_json(200, query_post_runs_request(payload))
                return
            if parsed.path == "/internal/posts/runs/record-failure":
                self.send_json(
                    200,
                    {
                        "item": record_post_run_failure_request(
                            payload,
                            DAILY_ACCOUNT_IDS if internal_role == "daily" else None,
                        )
                    },
                )
                return
            if parsed.path == "/internal/posts/material-keys/query":
                self.send_json(200, {"item": query_post_material_keys_request(payload)})
                return
            if parsed.path == "/internal/posts/material-pool/available":
                self.send_json(200, available_post_material_pool_request(payload))
                return
            if parsed.path == "/internal/posts/drama-pool/available":
                self.send_json(
                    200,
                    available_post_drama_pool_request(payload),
                )
                return
            if parsed.path == "/internal/posts/premium-relay/accounts":
                self.send_json(
                    200,
                    premium_relay_accounts_request(payload),
                )
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
            if parsed.path == "/internal/posts/manual-runs/create":
                self.send_json(200, create_post_manual_run_request(payload))
                return
            if manual_run_query_match:
                self.send_json(
                    200,
                    query_post_manual_run_request(
                        payload,
                        manual_run_query_match.group(1),
                    ),
                )
                return
            if parsed.path == "/internal/posts/material-pool/account-options":
                self.send_json(
                    200,
                    post_schedule_account_options_request(
                        payload,
                        POST_MATERIAL_POOL_NAVIGATION_ITEM,
                    ),
                )
                return
            if parsed.path == "/internal/posts/material-pool/schedule/query":
                self.send_json(
                    200,
                    query_post_schedule_request(
                        payload,
                        "material",
                        POST_MATERIAL_POOL_NAVIGATION_ITEM,
                    ),
                )
                return
            if parsed.path == "/internal/posts/material-pool/schedule/save":
                self.send_json(
                    200,
                    save_post_schedule_request(
                        payload,
                        "material",
                        POST_MATERIAL_POOL_NAVIGATION_ITEM,
                    ),
                )
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
            if parsed.path == "/internal/posts/drama-pool/query":
                self.send_json(
                    200,
                    query_post_drama_pool_request(payload),
                )
                return
            if parsed.path == "/internal/posts/drama-pool/add":
                self.send_json(
                    200,
                    add_post_drama_pool_request(payload),
                )
                return
            if drama_pool_priority_match:
                self.send_json(
                    200,
                    set_post_drama_pool_priority_request(
                        payload,
                        drama_pool_priority_match.group(1),
                    ),
                )
                return
            if parsed.path == "/internal/posts/drama-pool/check":
                self.send_json(
                    200,
                    {"item": record_post_drama_pool_checks_request(payload)},
                )
                return
            if parsed.path == "/internal/posts/drama-pool/batch-delete":
                self.send_json(
                    200,
                    {
                        "item": batch_delete_post_drama_pool_request(
                            payload
                        )
                    },
                )
                return
            if parsed.path == "/internal/posts/drama-pool/account-options":
                self.send_json(
                    200,
                    post_schedule_account_options_request(
                        payload,
                        POST_DRAMA_POOL_NAVIGATION_ITEM,
                    ),
                )
                return
            if parsed.path == "/internal/posts/drama-pool/schedule/query":
                self.send_json(
                    200,
                    query_post_schedule_request(
                        payload,
                        "drama",
                        POST_DRAMA_POOL_NAVIGATION_ITEM,
                    ),
                )
                return
            if parsed.path == "/internal/posts/drama-pool/schedule/save":
                self.send_json(
                    200,
                    save_post_schedule_request(
                        payload,
                        "drama",
                        POST_DRAMA_POOL_NAVIGATION_ITEM,
                    ),
                )
                return
            if drama_pool_episodes_match:
                self.send_json(
                    200,
                    query_post_drama_pool_episodes_request(
                        payload,
                        drama_pool_episodes_match.group(1),
                    ),
                )
                return
            if drama_pool_delete_match:
                self.send_json(
                    200,
                    {
                        "item": delete_post_drama_pool_request(
                            payload,
                            drama_pool_delete_match.group(1),
                        )
                    },
                )
                return
            if daily_verify_match:
                account_id = int(daily_verify_match.group(1))
                allowed_accounts = tuple(
                    dict.fromkeys(
                        tuple(DAILY_ACCOUNT_IDS)
                        + tuple(_active_schedule_account_scope())
                        + tuple(_active_manual_account_scope())
                    )
                )
                if internal_role == "daily" and account_id not in allowed_accounts:
                    raise ServiceError(
                        "x_daily_account_scope_denied",
                        "X自动发布只能校验当前配置的账号",
                        403,
                    )
                if "schedule_preflight" in payload and not isinstance(payload["schedule_preflight"], bool):
                    raise ServiceError("invalid_request", "schedule_preflight必须是布尔值", 400)
                if payload.get("schedule_preflight") is True:
                    XPostError, XPostStore, _publish_canary = _x_posts_api()
                    blocker = XPostStore(POST_DB_PATH).schedule_account_blockers([account_id]).get(account_id)
                    if blocker:
                        raise ServiceError(blocker["code"], blocker["message"], 409)
                actor = {
                    "tenant_key": "internal",
                    "user_id": "x-post-daily",
                    "name": "X Post Daily",
                    "email": "",
                    "role": "admin",
                }
                item = (
                    find_account(account_id)
                    if payload.get("snapshot_only") is True
                    else verify_account(
                        account_id,
                        actor,
                        "all",
                        only_refresh_required=(
                            payload.get("only_refresh_required") is True
                        ),
                        preserve_transient_status=(
                            payload.get("preserve_transient_status") is True
                        ),
                        require_publish_approved=(
                            payload.get("require_publish_approved") is True
                        ),
                    )
                )
                if item.get("publish_eligible") is not True:
                    raise ServiceError(
                        "x_account_not_publishable",
                        "X account is not approved for publishing",
                        409,
                    )
                self.send_json(200, {"item": item})
                return
            if daily_publish_match:
                if internal_role == "daily":
                    published = publish_queue_request(
                        daily_publish_match.group(1),
                        DAILY_ACCOUNT_IDS,
                        allow_manual=True,
                        expected_manual_trigger_source="manual",
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
            match = re.fullmatch(
                r"/internal/accounts/([0-9]+)/publish-approval",
                parsed.path,
            )
            if match:
                self.send_json(
                    200,
                    {
                        "item": set_account_publish_approval(
                            match.group(1),
                            payload.get("approved"),
                            payload.get("actor", {}),
                        )
                    },
                )
                return
            match = re.fullmatch(
                r"/internal/accounts/([0-9]+)/drama-language",
                parsed.path,
            )
            if match:
                self.send_json(
                    200,
                    {
                        "item": set_account_drama_language(
                            match.group(1),
                            payload.get("drama_language"),
                            payload.get("actor", {}),
                        )
                    },
                )
                return
            match = re.fullmatch(r"/internal/accounts/([0-9]+)/verify", parsed.path)
            if match:
                self.send_json(
                    200,
                    {
                        "item": verify_account(
                            match.group(1),
                            payload.get("actor", {}),
                            payload.get("scope", "mine"),
                            only_refresh_required=payload.get("only_refresh_required") is True,
                            preserve_transient_status=payload.get("preserve_transient_status") is True,
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
                    or auto_publish_match
                    or parsed.path == "/internal/posts/daily-plan"
                    or parsed.path == "/internal/posts/catchup-plan"
                    or parsed.path == "/internal/posts/schedule-plan"
                    or parsed.path == "/internal/posts/manual-plan"
                    or parsed.path == "/internal/posts/auto-template/plan"
                    or auto_run_recover_match
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
    if not AUTO_INTERNAL_TOKEN:
        raise RuntimeError("X_POST_AUTO_INTERNAL_TOKEN is required")
    if len({INTERNAL_TOKEN, DAILY_INTERNAL_TOKEN, AUTO_INTERNAL_TOKEN}) != 3:
        raise RuntimeError(
            "backend, daily, and auto internal tokens must be different"
        )
    if (
        not DAILY_ACCOUNT_IDS
        or len(DAILY_ACCOUNT_IDS) > MAX_DAILY_ACCOUNTS
        or len(set(DAILY_ACCOUNT_IDS)) != len(DAILY_ACCOUNT_IDS)
        or any(account_id <= 0 for account_id in DAILY_ACCOUNT_IDS)
    ):
        raise RuntimeError(
            "X_POST_DAILY_ACCOUNT_IDS must contain 1 to %s unique positive IDs"
            % MAX_DAILY_ACCOUNTS
        )
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
