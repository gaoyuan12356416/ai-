"""Authenticated loopback API for the two independent TT promotion pages."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from . import channels
from .channels import ChannelError

PREFIX = "/api/admin/tt-promotion"
CHANNEL_PAGE = "ttPromotionChannels"
AUTO_PAGE = "ttPromotionAuto"
NAVIGATION = Path("/usr/share/nginx/html/navigation.json")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def session_actor(cookie):
    if not cookie or len(cookie) > 16384:
        raise ChannelError(401, "请先登录")
    req = urllib.request.Request(
        "http://127.0.0.1:8787/api/ui/topbar", headers={"Cookie": cookie, "Accept": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=6) as res:
            auth = json.load(res)
    except (OSError, ValueError) as exc:
        raise ChannelError(503, "暂时无法核对登录状态") from exc
    user = auth.get("user") or {}
    if not auth.get("authenticated") or not user.get("user_id") or not user.get("tenant_key"):
        raise ChannelError(401, "请先登录")
    return {k: user.get(k) for k in ("user_id", "name", "role", "tenant_key", "permissions")}


def permitted(actor, page, nav):
    for group in nav:
        for item in group.get("items", []):
            if item.get("key") != page:
                continue
            rules = (group, item)
            if any(r.get("enabled") is False for r in rules):
                continue
            if actor.get("role") == "admin":
                return True
            if any(r.get("adminOnly") for r in rules):
                continue
            if all(
                (actor.get("permissions") or {}).get(r["module"]) is True for r in rules if r.get("module")
            ):
                return True
    return False


def dispatch(method, path, payload, actor, nav):
    channel_path = path == "/channels" or path.startswith("/channels/")
    pages = (
        (CHANNEL_PAGE, AUTO_PAGE)
        if method == "GET" and path == "/channels"
        else ((CHANNEL_PAGE,) if channel_path else (AUTO_PAGE,))
    )
    if not any(permitted(actor, page, nav) for page in pages):
        raise ChannelError(403, "没有此功能的访问权限")
    if method == "GET" and path == "/channels":
        return channels.channel_data(actor, section=payload.get("section"))
    if method == "GET" and re.fullmatch(r"/channels/[0-9]+", path):
        return channels.channel_data(actor, detail=path.rsplit("/", 1)[1], section=payload.get("section"))
    if method == "POST" and path == "/channels/settings":
        return channels.save_settings(actor, payload)
    if method == "POST" and re.fullmatch(r"/channels/[0-9]+/creator-info", path):
        return {"creator_info": channels.creator_info(path.split("/")[2])}
    if channel_path:
        raise ChannelError(404, "未找到请求的频道操作")
    inventory = re.fullmatch(r"/templates/([0-9]{1,18})/inventory", path)
    board = path in ("/history", "/stats/templates") or bool(inventory)
    # Listing tasks, templates and pages from a frozen preview only requires account visibility.
    # Avoid recomputing every channel's revenue tree for those requests.
    scope_only = (
        board
        or method == "GET"
        or (path.endswith(("/preview", "/drafts")) and bool(payload.get("preview_id")))
    )
    if scope_only:
        identities = channels.public_accounts()["items"]
        ids = {str(c.get("source_account_id") or c["account_id"]) for c in identities}
        ids.update(channels._setting_rows(channels.promotion_db(), actor["tenant_key"]))
        scope = [dict(account_id=account) for account in ids]
    else:
        scope = channels.worker_channels(actor)
    if board:
        from . import operations

        if method == "GET":
            return operations.read(path, payload, actor, ids, channels.promotion_db())
        if method == "POST" and inventory:
            return operations.add_inventory(channels.promotion_db(), int(inventory[1]), payload, actor, ids)
        raise ChannelError(405, "此操作不支持该请求方式")
    from .publishing import Publisher

    pub = Publisher(channels.promotion_db())
    return pub.handle(method, path, payload, actor, scope)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def serve(self):
        path = urllib.parse.urlsplit(self.path)
        if self.command == "GET" and path.path == "/health":
            return self.reply(200, {"ok": True, "service": "tt-promotion"})
        try:
            if not path.path.startswith(PREFIX + "/"):
                raise ChannelError(404, "接口不存在")
            actor = session_actor(self.headers.get("Cookie", ""))
            payload = {k: v[-1] for k, v in urllib.parse.parse_qs(path.query, max_num_fields=40).items()}
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if (
                    self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or origin
                    and origin != "https://ai.yingliangads.com"
                ):
                    raise ChannelError(403, "请求来源无效")
                if self.headers.get_content_type() != "application/json":
                    raise ChannelError(415, "请求须使用 JSON")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 262144:
                        raise ChannelError(413, "请求内容为空或过大")
                    payload = json.loads(self.rfile.read(size))
                except ValueError as exc:
                    raise ChannelError(400, "无效的 JSON 请求") from exc
                if not isinstance(payload, dict):
                    raise ChannelError(400, "无效的请求对象")
            nav = json.loads(NAVIGATION.read_text(encoding="utf-8"))
            result = dispatch(self.command, path.path[len(PREFIX) :], payload, actor, nav)
            return self.reply(200, result)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            status = getattr(exc, "status", None)
            if not isinstance(status, int) or not 400 <= status <= 599:
                logging.exception("TT promotion API failed on %s", path.path)
                status = 503
                message = "推广服务暂不可用，请稍后重试"
            else:
                message = str(exc)
            error = {
                "error": str(getattr(exc, "code", HTTPStatus(status).name.lower())),
                "message": message,
            }
            if getattr(exc, "preview_id", None):
                error["preview_id"] = exc.preview_id
            return self.reply(status, error)

    do_GET = serve
    do_POST = serve


def main():
    channels.initialize(channels.promotion_db())
    ThreadingHTTPServer(("127.0.0.1", 18920), Handler).serve_forever()


if __name__ == "__main__":
    main()
