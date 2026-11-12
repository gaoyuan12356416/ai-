"""Main API boundary: only the logged-in Cookie determines promotion ownership."""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import parse_qs
from .settings import read_env_file


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def trusted_actor(handler, app):
    session = app["load_session"](handler._cookies().get(app["SESSION_COOKIE_NAME"], ""))
    if not session or session.get("auth_type") == "api_token":
        return None, 401, {"error": "cookie_auth_required", "message": "请先使用飞书登录"}
    if not session.get("tenant_key") or not session.get("user_id"):
        return None, 401, {"error": "cookie_auth_required", "message": "登录用户身份不完整，请重新登录"}
    if not app["has_module_permission"](session, "moboreels_library"):
        return None, 403, {"error": "permission_denied", "module": "moboreels_library", "message": "请联系管理员开通 Moboreels 短剧库权限"}
    try:
        access = app["navigation_item_access"](session, "moboreelsLibrary", app["load_navigation_config"]())
    except (ValueError, TypeError, OSError):
        return None, 503, {"error": "navigation_unavailable", "message": "导航配置暂不可用"}
    if not access.get("allowed"):
        return None, 403, {"error": "permission_denied", "message": "当前账号不能访问此剧库"}
    return {key: str(session.get(key) or "") for key in ("tenant_key", "user_id", "name", "role")}, None, None


def dispatch(handler, parsed, app):
    respond = app["json_response"]
    actor, status, error = trusted_actor(handler, app)
    if error:
        respond(handler, status, error, no_store=True)
        return
    body = {}
    if handler.command == "POST":
        if not handler._require_same_origin_json():
            return
        try:
            lengths = handler.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or handler.headers.get("Transfer-Encoding"):
                raise ValueError()
            length = int(lengths[0])
            if not 0 <= length <= 16384:
                raise ValueError()
            body = json.loads(handler.rfile.read(length).decode("utf-8")) if length else {}
            if body != {}:
                raise ValueError()
        except (ValueError, UnicodeError):
            respond(handler, 400, {"error": "invalid_request", "message": "请求内容应为空对象；推广身份由当前登录用户确定"}, no_store=True)
            return
    config = read_env_file(os.environ.get("MOBOREELS_ENV_FILE", "/etc/moboreels-library.env"))
    token = os.environ.get("MOBOREELS_INTERNAL_TOKEN", config.get("MOBOREELS_INTERNAL_TOKEN", ""))
    if not token:
        respond(handler, 503, {"error": "service_unavailable", "message": "Moboreels 服务尚未配置"}, no_store=True)
        return
    envelope = {"method": handler.command, "path": parsed.path[len("/api/moboreels"):],
                "query": {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()},
                "body": body, "actor": actor}
    request = urllib.request.Request("http://127.0.0.1:8864/internal/dispatch",
        data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with OPENER.open(request, timeout=180) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError()
            result = json.loads(raw.decode("utf-8"))
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read(65536).decode("utf-8"))
            status = exc.code if exc.code in (400, 401, 403, 404, 409, 429, 500, 502, 503, 504) else 503
        except (ValueError, UnicodeError):
            result, status = {"error": "service_unavailable", "message": "Moboreels 服务暂不可用"}, 503
        finally:
            exc.close()
    except (OSError, ValueError, urllib.error.URLError):
        result, status = {"error": "service_unavailable", "message": "Moboreels 服务响应暂不可用；生成结果可稍后通过查看链接核对"}, 503
    respond(handler, status, result, no_store=True)
