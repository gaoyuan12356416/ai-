"""Same-origin settings endpoint authenticated by the existing CPU session service."""

import json
import os
import re
import socketserver
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from . import store

NAV = {
    "fb": {
        "fbAutoPublishTemplates",
        "fbPageManagement",
        "fbVideoPublish",
        "fbPostMaterialPool",
    },
    "x_material": {"xTagMaterialPool", "xPostMaterialPool"},
    "x_drama": {"xPostDramaPool"},
    "x_tag_auto": {"xTagAutoPublishTemplates"},
}
EDIT = dict(
    NAV, fb={"fbAutoPublishTemplates", "fbPageManagement", "fbPostMaterialPool"}
)


def access(user, nav, keys):
    admin = user.get("role") == "admin"
    perms = user.get("permissions") or {}
    for group in nav:
        for item in group.get("items", []):
            if item.get("key") not in keys:
                continue
            if all(
                n.get("enabled") is not False
                and (
                    admin
                    or (
                        not n.get("adminOnly")
                        and (not n.get("module") or perms.get(n["module"]))
                    )
                )
                for n in (group, item)
            ):
                return True
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, **fields):
        body = store.dump(fields).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        endpoint = urlsplit(self.path).path.removeprefix("/api/story-copy/")
        if endpoint not in NAV:
            return self.reply(404, error="unknown_endpoint")
        cookie = self.headers.get("Cookie", "")
        if not cookie or len(cookie) > 16384:
            return self.reply(401, error="请先登录")
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8787/api/auth/status", headers={"Cookie": cookie}
            )
            with urllib.request.urlopen(req, timeout=3) as response:
                auth = json.load(response)
            user = auth.get("user") or {}
            if (
                not auth.get("authenticated")
                or not user.get("tenant_key")
                or not user.get("user_id")
                or user.get("auth_type") == "api_token"
            ):
                return self.reply(401, error="请先使用飞书登录")
            nav = json.loads(
                Path("/usr/share/nginx/html/navigation.json").read_text(
                    encoding="utf-8-sig"
                )
            )
            if not access(user, nav, NAV[endpoint]):
                return self.reply(403, error="没有此发布端权限")
            editable = access(user, nav, EDIT[endpoint])
            if self.command == "POST":
                if not editable:
                    return self.reply(403, error="没有此发布端配置权限")
                origin = self.headers.get("Origin", "")
                if (
                    origin != "https://ai.yingliangads.com"
                    or self.headers.get("Sec-Fetch-Site", "") == "cross-site"
                ):
                    return self.reply(403, error="请从本站配置页面保存")
                if (
                    self.headers.get_content_type() != "application/json"
                    or not re.fullmatch(
                        r"[0-9]{1,4}", self.headers.get("Content-Length", "")
                    )
                ):
                    return self.reply(400, error="请求格式无效")
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if (
                    not isinstance(data, dict)
                    or set(data) != {"enabled"}
                    or type(data["enabled"]) is not bool
                ):
                    return self.reply(400, error="开关值无效")
                enabled = store.change_setting(
                    endpoint,
                    data["enabled"],
                    str(user["tenant_key"]) + "/" + str(user["user_id"]),
                )
            else:
                enabled = store.setting(endpoint)
            return self.reply(
                200, endpoint=endpoint, enabled=enabled, editable=editable
            )
        except (ValueError, TypeError):
            return self.reply(400, error="请求无效")
        except Exception:
            return self.reply(503, error="暂时无法读取或保存文案配置")

    do_GET = dispatch
    do_POST = dispatch


if __name__ == "__main__":

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    store.initialize()
    sock = Path("/run/story-copy/settings.sock")
    sock.unlink(missing_ok=True)
    with Server(str(sock), Handler) as server:
        os.chmod(sock, 0o666)
        server.serve_forever()
