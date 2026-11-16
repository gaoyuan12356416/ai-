"""COS capacity changes authenticated by the existing UI session service."""
import http.client
import json

from .core import DispatchError
from .monitor import MonitorError, capacity_payload
from .views import capacity_dto, invalidate_cache


def session_actor(cookie):
    if not cookie or len(cookie) > 8192:
        raise DispatchError("authentication_required", status=401)
    connection = http.client.HTTPConnection("127.0.0.1", 8787, timeout=5)
    try:
        connection.request("GET", "/api/ui/topbar", headers={"Cookie":cookie, "Connection":"close"})
        response = connection.getresponse()
        raw = response.read(512 * 1024 + 1)
        if response.status not in (200,401,403) or len(raw) > 512 * 1024:
            raise DispatchError("session_service_unavailable", status=503)
        if response.status != 200:
            raise DispatchError("authentication_required", status=401)
        auth = json.loads(raw)
        user = auth.get("user") or {}
        if not auth.get("authenticated") or not user.get("user_id"):
            raise DispatchError("authentication_required", status=401)
        if user.get("is_admin") is not True:
            raise DispatchError("admin_required", status=403)
        return {"is_admin":True,"user_id":user["user_id"],"tenant_key":user.get("tenant_key","")}
    finally:
        connection.close()


def save_capacity(handler, store):
    try:
        actor = session_actor(handler.headers.get("Cookie", ""))
        length = int(handler.headers.get("Content-Length", 0))
        if not 0 < length <= 8192:
            raise DispatchError("request_size_invalid", status=400)
        raw = json.loads(handler.rfile.read(length))
        allowed = {"host_id","stage","max_workers","scaling_mode","min_pending","expected_version"}
        if (not isinstance(raw, dict) or set(raw) - allowed
                or (raw.get("host_id"),raw.get("stage")) != ("cos-cloud","prepare")):
            raise DispatchError("cos_pool_required", status=400)
        config = store.set_capacity(capacity_payload(raw, actor), actor)
        invalidate_cache(store)
        return 200, {"ok":True,"capacity":capacity_dto(config)}
    except (DispatchError, MonitorError) as exc:
        return exc.status, {"error":exc.code}
    except (ValueError,TypeError):
        return 400, {"error":"invalid_request"}
    except (OSError,http.client.HTTPException):
        return 503, {"error":"session_service_unavailable"}
