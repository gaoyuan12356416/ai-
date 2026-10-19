#!/usr/bin/env python3
"""Loopback-only X App acceptance sandbox; never starts a publisher or worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18880"
CALLBACK_BASE = "https://ai.yingliangads.com/x-oauth"
ACTOR = {
    "tenant_key": "local-x-app-sandbox-20261019",
    "user_id": "local-operator",
    "name": "本机新 App 测试",
    "email": "",
    "role": "admin",
}
SAFE_ACCOUNT_FIELDS = (
    "id", "x_user_id", "username", "display_name", "status", "scopes",
    "missing_scopes", "access_expires_at", "authorization_refreshable",
    "subscription_type", "long_video_eligible", "last_verified_at",
    "last_token_refresh_at", "publish_approved",
)


def prepare(data_dir):
    """Clear inherited X configuration before importing the real OAuth code."""
    global oauth, DATA, CONFIG_FILE, CONFIG, SESSION, CSRF, CONFIG_LOCK, BROWSER_GUARD
    DATA = Path(data_dir).resolve()
    if "sandbox" not in str(DATA).lower() or DATA == ROOT or ROOT in DATA.parents:
        raise ValueError("Use a separate data directory containing 'sandbox'")
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE = DATA / "credentials.json"
    CONFIG = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    for name in tuple(os.environ):
        if name.startswith("X_"):
            del os.environ[name]
    os.environ.update({
        "X_POST_ENV_FILE": str(DATA / "unused.env"),
        "X_CLIENT_ID": CONFIG.get("oauth2_client_id", ""),
        "X_CLIENT_SECRET": CONFIG.get("oauth2_client_secret", ""),
        "X_DATA_DIR": str(DATA / "state"),
        "X_ACCOUNTS_DB": str(DATA / "state" / "accounts.sqlite3"),
        "X_TOKENS_DIR": str(DATA / "state" / "tokens"),
        "X_POST_DB_PATH": str(DATA / "state" / "accounts.sqlite3"),
        "X_PUBLIC_BASE_URL": CALLBACK_BASE,
        "X_ADMIN_RETURN_URL": BASE + "/",
        "X_LISTEN_HOST": "127.0.0.1",
        "X_LISTEN_PORT": "18880",
        "X_POST_STORAGE_ROOT": str(DATA / "unused-media"),
        "X_POST_STORAGE_MOUNT_ROOT": str(DATA),
        "X_POST_PUBLIC_ROOT": str(DATA / "unused-short-links"),
        "X_POST_SHORT_BASE_URL": BASE + "/disabled-short-links",
        "X_OAUTH_SCOPES": "tweet.read tweet.write users.read offline.access media.write",
    })
    sys.path.insert(0, str(ROOT))
    from features.x_accounts import oauth_service
    oauth = oauth_service
    oauth.ensure_storage()
    SESSION, CSRF = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    BROWSER_GUARD = secrets.token_urlsafe(32)
    (DATA / "callback-guard.json").write_text(json.dumps({"guard": BROWSER_GUARD, "server_pid": os.getpid()}), encoding="utf-8")
    CONFIG_LOCK = threading.RLock()


def save_config(payload):
    client_id = str(payload.get("client_id", "")).strip()
    client_secret = str(payload.get("client_secret", "")).strip()
    if not client_id or not client_secret or len(client_id) > 512 or len(client_secret) > 512:
        raise oauth.ServiceError("invalid_config", "请填写新 App 的 OAuth 2.0 Client ID 和 Client Secret", 400)
    with CONFIG_LOCK:
        if accounts() and CONFIG.get("oauth2_client_id") != client_id:
            raise oauth.ServiceError("app_change_blocked", "当前已有测试账号，请为另一 App 建立新的隔离环境", 409)
        updated = {**CONFIG, "oauth2_client_id": client_id, "oauth2_client_secret": client_secret}
        temp = CONFIG_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(updated), encoding="utf-8")
        os.replace(temp, CONFIG_FILE)
        CONFIG.update(updated)
        oauth.CLIENT_ID, oauth.CLIENT_SECRET = client_id, client_secret
        oauth.safe_record_event("sandbox_config", "saved", ACTOR)


def accounts():
    # Do not call list_accounts(): that initializes posting/scheduling schemas.
    with oauth._DB_LOCK:
        conn = oauth.db_connect()
        try:
            rows = conn.execute("SELECT * FROM x_authorized_account ORDER BY id").fetchall()
        finally:
            conn.close()
    return [{key: item.get(key) for key in SAFE_ACCOUNT_FIELDS}
            for item in (oauth.row_to_item(row) for row in rows)]


def app_probe():
    """One bounded public-user read; does not create, refresh, or revoke tokens."""
    token = CONFIG.get("bearer_token", "")
    if not token:
        raise oauth.ServiceError("app_bearer_missing", "尚未配置应用 Bearer Token", 400)
    request = urllib.request.Request(
        "https://api.x.com/2/users/by/username/XDevelopers",
        headers={"Authorization": "Bearer " + token, "User-Agent": "XLocalAppSandbox/1.0"},
    )
    try:
        with oauth._NO_REDIRECT_OPENER.open(request, timeout=20) as response:
            payload = json.load(response)
            result = {"http_status": response.status, "ok": bool(payload.get("data", {}).get("id"))}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(65536))
        except (ValueError, UnicodeError):
            payload = {}
        # Do not return the raw upstream body or headers to the browser or logs.
        title = str(payload.get("title", ""))
        allowed = {"Unauthorized", "Forbidden", "CreditsDepleted", "UsageCapExceeded", "Too Many Requests", "Payment Required"}
        result = {"http_status": exc.code, "ok": False, "upstream_title": title if title in allowed else "upstream_error"}
    except (OSError, TimeoutError):
        result = {"http_status": None, "ok": False, "upstream_title": "network_error"}
    result["checked_at"] = oauth.iso_utc()
    (DATA / "app-probe.json").write_text(json.dumps(result), encoding="utf-8")
    oauth.safe_record_event("sandbox_app_probe", "completed" if result["ok"] else "failed", ACTOR,
                            error_code="" if result["ok"] else str(result.get("upstream_title", "upstream_error")))
    return result


def status():
    probe_file = DATA / "app-probe.json"
    return {
        "environment": "本机隔离测试", "callback_url": oauth.callback_url(),
        "configured": bool(oauth.CLIENT_ID and oauth.CLIENT_SECRET),
        "consumer_key_stored": bool(CONFIG.get("consumer_key")),
        "consumer_secret_stored": bool(CONFIG.get("consumer_secret")),
        "bearer_token_stored": bool(CONFIG.get("bearer_token")),
        "scopes": list(oauth.SCOPES), "accounts": accounts(),
        "app_probe": json.loads(probe_file.read_text(encoding="utf-8")) if probe_file.exists() else None,
        "publishing_enabled": False, "schedulers_started": False,
        "callback_guard_required": True,
        "oauth_source_sha256": hashlib.sha256(Path(oauth.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
    }


def make_handler():
    class SandboxHandler(oauth.Handler):
        server_version = "XLocalSandbox/1.0"

        def valid_host(self):
            if self.headers.get("Host") != "127.0.0.1:18880":
                self.send_json(403, {"error": "invalid_host"})
                return False
            return True

        def valid_session(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                value = cookie["x_sandbox"].value if "x_sandbox" in cookie else ""
            except Exception:
                value = ""
            if not secrets.compare_digest(value, SESSION):
                self.send_json(403, {"error": "local_session_required"})
                return False
            return True

        def guarded_browser(self):
            return secrets.compare_digest(self.headers.get("X-Sandbox-Callback-Guard", ""), BROWSER_GUARD)

        def end_headers(self):
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            super().end_headers()

        def do_GET(self):
            if not self.valid_host():
                return
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/health":
                self.send_json(200, {"ok": True, "environment": "sandbox", "publishing_enabled": False})
            elif parsed.path == "/":
                body = (ROOT / "static" / "x-new-app-sandbox.html").read_text(encoding="utf-8").replace("__CSRF__", CSRF).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Set-Cookie", "x_sandbox=" + SESSION + "; HttpOnly; SameSite=Lax; Path=/")
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/status" and self.valid_session():
                self.send_json(200, {**status(), "callback_guard_active": self.guarded_browser()})
            elif parsed.path == "/x-oauth/callback" and self.valid_session():
                # Only dispatch the existing callback, never its internal routes.
                if not self.guarded_browser():
                    self.send_json(403, {"error": "dedicated_test_browser_required"})
                    return
                with CONFIG_LOCK:
                    if accounts():
                        self.send_redirect(BASE + "/?oauth=error&reason=one_account_only")
                        return
                    self.path = "/callback" + ("?" + parsed.query if parsed.query else "")
                    super().do_GET()
            elif parsed.path not in {"/api/status", "/x-oauth/callback"}:
                self.send_json(404, {"error": "sandbox_route_disabled"})

        def do_POST(self):
            # Drain the bounded body before rejecting; unread bytes can reset Windows sockets.
            try:
                payload = self.read_json()
            except oauth.ServiceError as exc:
                self.send_json(exc.status, {"error": exc.code})
                return
            if not self.valid_host() or not self.valid_session():
                return
            if (self.headers.get("Origin") != BASE
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                    or not secrets.compare_digest(self.headers.get("X-Sandbox-CSRF", ""), CSRF)):
                self.send_json(403, {"error": "csrf_failed"})
                return
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path == "/api/config":
                    save_config(payload)
                    self.send_json(200, {"ok": True})
                elif path == "/api/authorize":
                    with CONFIG_LOCK:
                        extension_transport = self.headers.get("X-Sandbox-Transport") == "extension"
                        if not self.guarded_browser() and not extension_transport:
                            raise oauth.ServiceError("dedicated_test_browser_required", "请在专用测试浏览器中登录", 403)
                        if accounts():
                            raise oauth.ServiceError("one_account_only", "已登录一个测试账号，请先完成该账号验证", 409)
                        conn = oauth.db_connect()
                        try:
                            # Only one outstanding authorization; a new click replaces a zero-account state.
                            conn.execute("DELETE FROM x_oauth_state")
                            conn.commit()
                        finally:
                            conn.close()
                        result = oauth.create_authorization(ACTOR)
                        if extension_transport:
                            # Browser-only bridge credential, released behind Cookie/Origin/CSRF.
                            # The extension installs an exact-state loopback header rule before navigation.
                            result["callback_guard"] = BROWSER_GUARD
                        self.send_json(200, result)
                elif path == "/api/verify":
                    with CONFIG_LOCK:
                        items = accounts()
                        if len(items) != 1:
                            raise oauth.ServiceError("one_account_required", "请先登录一个测试账号", 409)
                        oauth.verify_account(items[0]["id"], ACTOR, preserve_transient_status=True)
                        self.send_json(200, {"ok": True, "accounts": accounts()})
                elif path == "/api/app-probe":
                    self.send_json(200, app_probe())
                else:
                    self.send_json(404, {"error": "sandbox_route_disabled"})
            except oauth.ServiceError as exc:
                # Service error codes are sufficient; raw upstream messages may contain secrets.
                self.send_json(exc.status, {"error": exc.code, "message": "操作未完成，请检查配置或授权结果（" + exc.code + "）"})
            except Exception:
                self.send_json(503, {"error": "sandbox_unavailable"})

    return SandboxHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--probe-app", action="store_true")
    args = parser.parse_args()
    prepare(args.data_dir)
    if args.probe_app:
        print(json.dumps(app_probe()))
        return
    server = ThreadingHTTPServer(("127.0.0.1", 18880), make_handler())
    server.daemon_threads = True
    print("X local sandbox: " + BASE + " (publishing disabled)", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
