#!/usr/bin/env python3
"""X OAuth 2.0 PKCE sidecar with multi-account token isolation.

Only /health and /callback are public. The /internal/* endpoints are loopback-only
and require a separate bearer token. OAuth credentials and user tokens never
leave this process.
"""

from __future__ import annotations

import argparse
import base64
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
USERS_ME_URL = "https://api.x.com/2/users/me?user.fields=profile_image_url"
MAX_BODY_BYTES = 16 * 1024
MAX_ERROR_TEXT = 240


def load_env_file(path):
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
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


load_env_file(os.environ.get("X_POST_ENV_FILE", DEFAULT_ENV_FILE))

CLIENT_ID = os.environ.get("X_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "").strip()
INTERNAL_TOKEN = (
    os.environ.get("X_INTERNAL_TOKEN", "").strip()
    or os.environ.get("X_POST_AUTOMATION_INTERNAL_TOKEN", "").strip()
)
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS x_oauth_state (
                    state_hash TEXT PRIMARY KEY,
                    code_verifier TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL DEFAULT '',
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
                    actor_name TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_x_account_updated ON x_authorized_account(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_x_oauth_state_expires ON x_oauth_state(expires_at);
                CREATE INDEX IF NOT EXISTS idx_x_oauth_event_created ON x_oauth_event(created_at DESC);
                """
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
        "name": clean_text(actor.get("name", ""), 255),
        "email": clean_text(actor.get("email", ""), 255),
        "role": clean_text(actor.get("role", "user"), 32),
    }


def record_event(event_type, outcome, actor=None, x_user_id="", error_code=""):
    actor = normalize_actor(actor)
    with _DB_LOCK:
        conn = db_connect()
        try:
            conn.execute(
                "INSERT INTO x_oauth_event(event_type,outcome,x_user_id,actor_user_id,actor_name,error_code,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    clean_text(event_type, 64), clean_text(outcome, 32), clean_text(x_user_id, 64),
                    actor["user_id"], actor["name"], clean_text(error_code, 64), iso_utc(),
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
    actor = normalize_actor(actor)
    raw_state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    created = now_epoch()
    expires = created + STATE_TTL_SECONDS
    with _DB_LOCK:
        conn = db_connect()
        try:
            conn.execute("DELETE FROM x_oauth_state WHERE expires_at <= ?", (iso_utc(created),))
            conn.execute(
                "INSERT INTO x_oauth_state(state_hash,code_verifier,actor_user_id,actor_name,actor_email,actor_role,redirect_uri,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    state_digest(raw_state), verifier, actor["user_id"], actor["name"], actor["email"],
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


def http_json(url, method="GET", headers=None, body=None):
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ServiceError("x_upstream_error", "X API响应过大", 502)
            return json.loads(raw.decode("utf-8")) if raw else {}
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
        "name": state_row.get("actor_name", ""),
        "email": state_row.get("actor_email", ""),
        "role": state_row.get("actor_role", ""),
    }


def status_for(scopes, access_expires_at, token=None, stored="active"):
    if stored in {"revoked", "error", "token_missing"}:
        return stored, [scope for scope in REQUIRED_SCOPES if scope not in set(scopes)]
    missing = [scope for scope in REQUIRED_SCOPES if scope not in set(scopes)]
    if missing:
        return "scope_missing", missing
    if access_expires_at and parse_iso_epoch(access_expires_at) <= now_epoch():
        return ("refresh_required" if token and token.get("refresh_token") else "revoked"), []
    return "active", []


def complete_authorization(code, raw_state):
    actor = {}
    state_consumed = False
    try:
        state_row = consume_state(raw_state)
        state_consumed = True
        actor = actor_from_state(state_row)
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
        token_file = token_path(x_user_id)
        with account_lock("x:" + x_user_id):
            previous_token = token_file.read_bytes() if token_file.exists() else None
            atomic_write_json(token_file, token)
            timestamp = iso_utc(obtained)
            status, _missing = status_for(scopes, expires, token, "active")
            try:
                with _DB_LOCK:
                    conn = db_connect()
                    try:
                        conn.execute(
                            """
                            INSERT INTO x_authorized_account(
                                x_user_id,username,display_name,profile_image_url,token_store_key,token_type,scopes_json,status,
                                first_authorized_at,last_authorized_at,access_expires_at,last_token_refresh_at,last_verified_at,
                                last_error_at,last_error,authorized_by_user_id,authorized_by_name,authorized_by_email,created_at,updated_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(x_user_id) DO UPDATE SET
                                username=excluded.username,display_name=excluded.display_name,profile_image_url=excluded.profile_image_url,
                                token_store_key=excluded.token_store_key,token_type=excluded.token_type,scopes_json=excluded.scopes_json,
                                status=excluded.status,last_authorized_at=excluded.last_authorized_at,
                                access_expires_at=excluded.access_expires_at,last_token_refresh_at=excluded.last_token_refresh_at,
                                last_verified_at=excluded.last_verified_at,
                                last_error_at='',last_error='',authorized_by_user_id=excluded.authorized_by_user_id,
                                authorized_by_name=excluded.authorized_by_name,authorized_by_email=excluded.authorized_by_email,
                                updated_at=excluded.updated_at
                            """,
                            (
                                x_user_id, clean_text(account.get("username", ""), 255), clean_text(account.get("name", ""), 255),
                                clean_text(account.get("profile_image_url", ""), 1024), token_file.name,
                                clean_text(token.get("token_type", "bearer"), 32).lower(), json.dumps(scopes), status,
                                timestamp, timestamp, expires, "", timestamp, "", "", actor["user_id"], actor["name"],
                                actor["email"], timestamp, timestamp,
                            ),
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
            safe_record_event("authorization", "failed", actor, error_code=exc.code)
        raise
    except Exception:
        if state_consumed:
            safe_record_event("authorization", "failed", actor, error_code="x_accounts_unavailable")
        raise ServiceError("x_accounts_unavailable", "X授权处理失败", 503) from None


def row_to_item(row):
    item = dict(row)
    try:
        scopes = parse_scopes(json.loads(item.pop("scopes_json", "[]")))
    except (TypeError, ValueError, json.JSONDecodeError):
        scopes = []
    token = None
    try:
        token = read_token_file(item["x_user_id"])
    except ServiceError:
        pass
    status, missing = status_for(scopes, item.get("access_expires_at", ""), token, item.get("status", "active"))
    if token is None:
        status = "token_missing"
    item["status"] = status
    item["scopes"] = scopes
    item["missing_scopes"] = missing
    item.pop("token_store_key", None)
    item.pop("token_type", None)
    return item


def list_accounts():
    with _DB_LOCK:
        conn = db_connect()
        try:
            rows = conn.execute("SELECT * FROM x_authorized_account ORDER BY updated_at DESC,id DESC").fetchall()
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


def verify_account(account_id):
    account_id = int(account_id)
    initial_item = find_account(account_id)
    x_user_id = initial_item["x_user_id"]
    with account_lock("x:" + x_user_id):
        item = find_account(account_id)
        actor = {"user_id": "system", "name": "AI后台主动校验"}
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
            with _DB_LOCK:
                conn = db_connect()
                try:
                    conn.execute(
                        """
                        UPDATE x_authorized_account SET username=?,display_name=?,profile_image_url=?,scopes_json=?,status=?,
                            access_expires_at=?,last_token_refresh_at=?,last_verified_at=?,last_error_at='',last_error='',updated_at=?
                        WHERE id=?
                        """,
                        (
                            clean_text(account.get("username", item.get("username", "")), 255),
                            clean_text(account.get("name", item.get("display_name", "")), 255),
                            clean_text(account.get("profile_image_url", item.get("profile_image_url", "")), 1024),
                            json.dumps(scopes), status, expires_at, refresh_at, timestamp, timestamp, account_id,
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

    def is_internal_authorized(self):
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                return False
        except ValueError:
            return False
        supplied = str(self.headers.get("Authorization", "") or "")
        prefix = "Bearer "
        if not INTERNAL_TOKEN or not supplied.startswith(prefix):
            return False
        return secrets.compare_digest(supplied[len(prefix):], INTERNAL_TOKEN)

    def require_internal(self):
        if self.is_internal_authorized():
            return True
        self.send_json(403, {"error": "x_internal_auth_failed", "message": "内部鉴权失败"})
        return False

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            raise ServiceError("invalid_request", "Content-Length无效", 400) from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise ServiceError("invalid_request", "请求体过大", 413)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise ServiceError("invalid_request", "JSON请求体无效", 400) from None
        if not isinstance(payload, dict):
            raise ServiceError("invalid_request", "JSON请求体必须是对象", 400)
        return payload

    def send_service_error(self, exc):
        code = exc.code if isinstance(exc, ServiceError) else "x_accounts_unavailable"
        status = exc.status if isinstance(exc, ServiceError) else 503
        message = clean_text(str(exc) if isinstance(exc, ServiceError) else "X账号服务暂不可用")
        self.send_json(status, {"error": code, "message": message})

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
        if parsed.path == "/internal/accounts":
            if self.require_internal():
                try:
                    self.send_json(200, list_accounts())
                except ServiceError as exc:
                    self.send_service_error(exc)
                except Exception:
                    self.send_service_error(ServiceError("x_accounts_unavailable", "X账号服务暂不可用", 503))
            return
        self.send_text(404, "not found\n")

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith("/internal/"):
            self.send_text(404, "not found\n")
            return
        if not self.require_internal():
            return
        try:
            payload = self.read_json()
            if parsed.path == "/internal/authorize":
                self.send_json(200, create_authorization(payload.get("actor", {})))
                return
            match = re.fullmatch(r"/internal/accounts/([0-9]+)/verify", parsed.path)
            if match:
                self.send_json(200, {"item": verify_account(match.group(1))})
                return
            self.send_text(404, "not found\n")
        except ServiceError as exc:
            self.send_service_error(exc)
        except Exception:
            self.send_service_error(ServiceError("x_accounts_unavailable", "X账号服务暂不可用", 503))


def serve():
    if LISTEN_HOST not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("X sidecar must listen on loopback")
    if not INTERNAL_TOKEN:
        raise RuntimeError("X_INTERNAL_TOKEN is required")
    require_oauth_config()
    os.umask(0o077)
    ensure_storage()
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
