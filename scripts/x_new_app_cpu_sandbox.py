#!/usr/bin/env python3
"""Independent CPU X App login test, using existing AI admin authentication."""
from __future__ import annotations

import argparse
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer

import x_new_app_sandbox as local
import x_ads_oauth as ads

SITE = "https://ai.yingliangads.com"
PUBLIC = SITE + "/x-app-test"
STATE_PREFIX = "xtest_20261019_"
COOKIE_NAME = "x_cpu_app_test"
ADS_COOKIE = "__Host-x-ads-flow"


def read_actor(cookie):
    """Read existing Feishu login status over loopback; never import the main app."""
    request = urllib.request.Request(
        "http://127.0.0.1:8787/api/auth/status",
        headers={"Cookie": cookie, "Host": "ai.yingliangads.com", "X-Forwarded-Proto": "https"},
    )
    try:
        with local.oauth._NO_REDIRECT_OPENER.open(request, timeout=5) as response:
            payload = json.loads(response.read(128 * 1024))
    except Exception:
        raise local.oauth.ServiceError("auth_service_unavailable", "后台登录校验暂不可用", 503) from None
    user = payload.get("user") or {}
    if not payload.get("enabled") or not payload.get("authenticated") or not user.get("user_id"):
        raise local.oauth.ServiceError("auth_required", "请先登录 AI 后台", 401)
    if user.get("role") != "admin":
        raise local.oauth.ServiceError("admin_required", "仅后台管理员可使用新 App 测试环境", 403)
    return {key: user.get(key, "") for key in ("tenant_key", "user_id", "name", "email", "role")}


def channel_for_ads(cookie, handle):
    handle = str(handle or "").strip().lstrip("@")
    if not ads.HANDLE.fullmatch(handle):
        raise ads.AdsError("invalid_handle", "请输入后台管理的 X 频道用户名。")
    request = urllib.request.Request(
        "http://127.0.0.1:8787/api/admin/x-accounts",
        headers={"Cookie": cookie, "Host": "ai.yingliangads.com", "X-Forwarded-Proto": "https"})
    try:
        with local.oauth._NO_REDIRECT_OPENER.open(request, timeout=12) as response:
            rows = json.loads(response.read(8 * 1024 * 1024))["items"]
    except Exception:
        raise ads.AdsError("channels_unavailable", "当前后台账号无法读取 X 频道列表。", 503) from None
    for row in rows:
        if str(row.get("username", "")).lower() == handle.lower() and str(row.get("x_user_id", "")).isdigit():
            return {"username": row["username"], "x_user_id": str(row["x_user_id"])}
    raise ads.AdsError("channel_not_found", "后台未找到这个 X 频道。", 404)


def create_test_authorization(actor):
    with local.CONFIG_LOCK:
        if local.accounts():
            raise local.oauth.ServiceError("one_account_only", "已有一个测试账号，请先完成该账号验证", 409)
        result = local.oauth.create_authorization(actor)
        parsed = urllib.parse.urlsplit(result["authorization_url"])
        params = urllib.parse.parse_qsl(parsed.query)
        raw = next(value for key, value in params if key == "state")
        prefixed = STATE_PREFIX + raw
        with local.oauth._DB_LOCK:
            conn = local.oauth.db_connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute("UPDATE x_oauth_state SET state_hash=? WHERE state_hash=?",
                                       (local.oauth.state_digest(prefixed), local.oauth.state_digest(raw)))
                if changed.rowcount != 1:
                    raise RuntimeError("authorization state update failed")
                conn.commit()
            finally:
                conn.close()
        params = [(key, prefixed if key == "state" else value) for key, value in params]
        result["authorization_url"] = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(params)))
        return result


def prepare(data_dir):
    local.BASE = PUBLIC
    local.prepare(data_dir)
    local.oauth.ADMIN_RETURN_URL = PUBLIC + "/"


def make_handler():
    class CPUHandler(local.oauth.Handler):
        server_version = "XCPUAppTest/1.0"

        def end_headers(self):
            self.send_header("X-X-App-Test", "cpu-20261019")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            super().end_headers()

        def check(self, *, session=True):
            if self.headers.get("Host") != "ai.yingliangads.com":
                raise local.oauth.ServiceError("invalid_host", "Host 无效", 403)
            actor = read_actor(self.headers.get("Cookie", ""))
            if session:
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                    value = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ""
                except Exception:
                    value = ""
                if not secrets.compare_digest(value, local.SESSION):
                    raise local.oauth.ServiceError("test_session_required", "请从测试首页重新开始", 403)
            return actor

        def ads_browser(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                value = cookie[ADS_COOKIE].value if ADS_COOKIE in cookie else ""
            except Exception:
                value = ""
            return value if len(value) == 43 and all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in value) else ""

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/health":
                self.send_json(200, {"ok": True, "environment": "cpu-isolated", "publishing_enabled": False})
                return
            try:
                if parsed.path in ("/", "/ads"):
                    ads_page = parsed.path == "/ads"
                    try:
                        self.check(session=False)
                    except local.oauth.ServiceError as exc:
                        if exc.code != "auth_required":
                            raise
                        target = "/x-app-test/"
                        if ads_page:
                            handle = urllib.parse.parse_qs(parsed.query).get("channel", [""])[0]
                            target += "ads" + ("?channel=" + handle if ads.HANDLE.fullmatch(handle) else "")
                        self.send_redirect(SITE + "/api/auth/feishu/login?" + urllib.parse.urlencode({"next": target}))
                        return
                    name = "x-ads-authorization.html" if ads_page else "x-new-app-cpu-test.html"
                    body = (local.ROOT / "static" / name).read_text(encoding="utf-8").replace("__CSRF__", local.CSRF).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Set-Cookie", COOKIE_NAME + "=" + local.SESSION + "; HttpOnly; Secure; SameSite=Lax; Path=/")
                    if ads_page and not self.ads_browser():
                        self.send_header("Set-Cookie", ADS_COOKIE + "=" + secrets.token_urlsafe(32) + "; HttpOnly; Secure; SameSite=Lax; Path=/")
                    self.end_headers()
                    self.wfile.write(body)
                elif parsed.path == "/api/status":
                    self.check()
                    self.send_json(200, {"environment": "CPU 独立测试环境", "configured": bool(local.oauth.CLIENT_ID and local.oauth.CLIENT_SECRET),
                                         "callback_url": local.oauth.callback_url(), "accounts": local.accounts(),
                                         "scopes": list(local.oauth.SCOPES), "publishing_enabled": False})
                elif parsed.path == "/callback":
                    actor = self.check()
                    params = urllib.parse.parse_qs(parsed.query)
                    if "state" not in params and ("oauth_token" in params or "denied" in params):
                        _, handle = ads.finish(local.DATA, local.CONFIG, actor, self.ads_browser(), params)
                        self.send_redirect(PUBLIC + "/ads?" + urllib.parse.urlencode({"channel": handle, "linked": "1"}))
                        return
                    if not params.get("state", [""])[0].startswith(STATE_PREFIX):
                        raise local.oauth.ServiceError("invalid_test_state", "非本次测试授权", 400)
                    with local.CONFIG_LOCK:
                        if local.accounts():
                            raise local.oauth.ServiceError("one_account_only", "已有一个测试账号", 409)
                        super().do_GET()
                else:
                    self.send_json(404, {"error": "test_route_disabled"})
            except ads.AdsError as exc:
                self.send_text(exc.status, str(exc))
            except local.oauth.ServiceError as exc:
                self.send_json(exc.status, {"error": exc.code, "message": "请返回测试首页检查登录或授权状态"})
            except Exception:
                self.send_json(503, {"error": "test_service_unavailable"})

        def do_POST(self):
            try:
                payload = self.read_json()
                actor = self.check()
                if (self.headers.get("Origin") != SITE
                        or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                        or not secrets.compare_digest(self.headers.get("X-Sandbox-CSRF", ""), local.CSRF)):
                    raise local.oauth.ServiceError("csrf_failed", "请求来源校验失败", 403)
                path = urllib.parse.urlsplit(self.path).path
                if path in ("/api/ads/authorize", "/api/ads/status", "/api/ads/check"):
                    channel = channel_for_ads(self.headers.get("Cookie", ""), payload.get("channel"))
                    x_id = channel["x_user_id"]
                    if path == "/api/ads/authorize":
                        browser = self.ads_browser()
                        if not browser:
                            raise ads.AdsError("browser_required", "请重新打开频道授权页。", 403)
                        url = ads.start(local.DATA, local.CONFIG, local.oauth.callback_url(), actor, browser, channel)
                        self.send_json(200, {"authorization_url": url})
                    elif path == "/api/ads/check":
                        self.send_json(200, {"authorized": True, "report": ads.probe(local.DATA, local.CONFIG, x_id)})
                    else:
                        self.send_json(200, ads.status(local.DATA, local.CONFIG, x_id))
                elif path == "/api/authorize":
                    self.send_json(200, create_test_authorization(actor))
                elif path == "/api/verify":
                    with local.CONFIG_LOCK:
                        items = local.accounts()
                        if len(items) != 1:
                            raise local.oauth.ServiceError("one_account_required", "请先授权一个测试账号", 409)
                        local.oauth.verify_account(items[0]["id"], actor, preserve_transient_status=True)
                        self.send_json(200, {"ok": True, "accounts": local.accounts()})
                elif path == "/api/app-probe":
                    self.send_json(200, local.app_probe())
                else:
                    self.send_json(404, {"error": "test_route_disabled"})
            except ads.AdsError as exc:
                self.send_json(exc.status, {"error": exc.code, "message": str(exc)})
            except local.oauth.ServiceError as exc:
                self.send_json(exc.status, {"error": exc.code, "message": "操作未完成（" + exc.code + "）"})
            except Exception:
                self.send_json(503, {"error": "test_service_unavailable"})

    return CPUHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=18881)
    args = parser.parse_args()
    prepare(args.data_dir)
    local.oauth.require_oauth_config()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler())
    server.daemon_threads = True
    print("CPU isolated X App test ready on loopback", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
