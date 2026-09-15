"""Meta object adapter. GET uncertainty never implies successful deletion."""
import json
import re
import time
import requests
from .core import AssetError, account_id, creative_video_ids, meta_id, now, redact

CREATIVE_FIELDS = "id,account_id,status,video_id,object_story_spec,asset_feed_spec"
AD_FIELDS = "id,account_id,status,effective_status,creative{%s}" % CREATIVE_FIELDS
LIVE_STATUSES = ["ACTIVE", "PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED", "DISAPPROVED", "PENDING_REVIEW", "PREAPPROVED", "PENDING_BILLING_INFO", "WITH_ISSUES", "IN_PROCESS", "ARCHIVED"]


class GraphError(AssetError):
    def __init__(self, code, message, uncertain=False, detail=None):
        super().__init__(str(code), redact(message), 400)
        self.uncertain = uncertain
        self.detail = detail or {}


class GraphClient:
    def __init__(self, token_provider, version="v25.0", transport=None, timeout=20, max_inventory=50000):
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
            raise ValueError("invalid graph version")
        self.token_provider = token_provider
        self.base = "https://graph.facebook.com/" + version + "/"
        self.http = transport or requests.Session()
        self.timeout = timeout
        self.max_inventory = max_inventory
        self.tokens, self.inventory_cache, self.video_library_cache = {}, {}, {}

    def request(self, method, path, token, params=None):
        if not re.fullmatch(r"(?:act_)?[1-9][0-9]{0,31}(?:/(?:ads|advideos))?", path):
            raise GraphError("invalid_graph_path", "Meta 请求对象无效")
        try:
            response = self.http.request(method, self.base + path, params=params or {},
                headers={"Authorization": "Bearer " + token}, timeout=self.timeout, allow_redirects=False)
        except (requests.Timeout, requests.ConnectionError):
            raise GraphError("network_uncertain" if method == "DELETE" else "read_unavailable",
                "Meta 请求超时或连接中断，删除结果需要核实" if method == "DELETE" else "Meta 读取未完成", method == "DELETE") from None
        except Exception:
            raise GraphError("request_unavailable", "Meta 请求未完成", method == "DELETE") from None
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise GraphError("invalid_graph_response", "Meta 返回无法解析，不能确认结果", method == "DELETE") from None
        error = data.get("error") if isinstance(data, dict) else None
        if response.status_code >= 300 or error:
            error = error if isinstance(error, dict) else {}
            message = str(error.get("message") or "Meta 请求失败").replace(token, "[redacted]")
            detail = {k: error[k] for k in ("code", "error_subcode", "type", "fbtrace_id", "is_transient") if k in error}
            detail["message"] = redact(message)
            uncertain = method == "DELETE" and (response.status_code >= 500 or response.status_code in (301, 302, 307, 308))
            raise GraphError(error.get("code", "graph_error"), message, uncertain, detail)
        return data

    def credential(self, obj, account=None):
        accounts = obj.get("account_ids") or []
        aid = account_id(account or (accounts[0] if accounts else ""))
        users = [str(x) for x in obj.get("user_ids", []) if str(x).isdigit() and str(x) != "0"]
        cache_key = (aid, tuple(users))
        if cache_key in self.tokens:
            return self.tokens[cache_key]
        if not users:
            raise GraphError("missing_token", "没有配置可用的 Meta Token 用户")
        last = None
        # Only credential selection uses GET probes. A failed DELETE is never
        # replayed automatically with another user's token.
        for uid in dict.fromkeys(users):
            token = self.token_provider([uid])
            if not token:
                continue
            try:
                data = self.request("GET", "act_" + aid, token, {"fields": "id,account_id"})
                if not isinstance(data, dict) or account_id(data.get("account_id") or data.get("id")) != aid:
                    raise GraphError("account_mismatch", "Meta Token 返回的广告账户不匹配")
                self.tokens[cache_key] = token
                return token
            except GraphError as exc:
                last = exc
        raise last or GraphError("missing_token", "所配置用户没有可用的 Meta Token")

    def read_node(self, obj, fields):
        token = self.credential(obj)
        data = self.request("GET", meta_id(obj["object_id"]), token, {"fields": fields})
        if not isinstance(data, dict) or str(data.get("id", "")) != obj["object_id"]:
            raise GraphError("object_mismatch", "Meta 未返回可核验的对象 ID")
        return data

    def inventory(self, obj, account, edge="ads", fresh=False):
        aid = account_id(account)
        cache = self.inventory_cache if edge == "ads" else self.video_library_cache
        cached = cache.get(aid)
        if not fresh and cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        token = self.credential(obj, aid)
        params = {"fields": AD_FIELDS if edge == "ads" else "id", "limit": "100"}
        if edge == "ads":
            params["effective_status"] = json.dumps(LIVE_STATUSES)
        result, cursors = [], set()
        while True:
            data = self.request("GET", "act_" + aid + "/" + edge, token, params)
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise GraphError("reference_check_incomplete", "Meta 账户引用清单未完整返回")
            result.extend(data["data"])
            if len(result) > self.max_inventory:
                raise GraphError("reference_check_incomplete", "Meta 账户引用超过核验上限，不能执行该素材删除")
            paging = data.get("paging") or {}
            if not paging.get("next"):
                break
            after = (paging.get("cursors") or {}).get("after")
            if not after or after in cursors:
                raise GraphError("reference_check_incomplete", "Meta 引用清单分页不完整")
            cursors.add(after)
            params["after"] = after
        cache[aid] = (time.monotonic(), result)
        return result

    def check_references(self, obj, allowed_ads):
        for aid in obj.get("account_ids", []):
            for ad in self.inventory(obj, aid):
                if str(ad.get("status", "")).upper() == "DELETED" or str(ad.get("effective_status", "")).upper() == "DELETED":
                    continue
                if str(ad.get("id", "")) in allowed_ads:
                    continue
                creative = ad.get("creative")
                if not isinstance(creative, dict) or not creative.get("id"):
                    raise GraphError("reference_check_incomplete", "账户内存在无法读取 Creative 的广告，无法排除共享引用")
                referenced = str(creative["id"]) == obj["object_id"] if obj["kind"] == "creative" else obj["object_id"] in creative_video_ids(creative)
                if referenced:
                    raise GraphError("shared_outside_scope", "该素材仍被范围外 Ad %s 引用" % ad["id"])

    def inspect(self, obj, allowed_ads=None, deleted_creatives=None, check_references=True):
        kind = obj["kind"]
        fields = AD_FIELDS if kind == "ad" else CREATIVE_FIELDS if kind == "creative" else "id,from"
        data = self.read_node(obj, fields)
        if kind in ("ad", "creative"):
            if account_id(data.get("account_id", "")) not in obj["account_ids"]:
                raise GraphError("account_mismatch", "Meta 对象所属账户与预览不一致")
            if str(data.get("status", "")).upper() == "DELETED" or str(data.get("effective_status", "")).upper() == "DELETED":
                return "already_deleted", {"confirmed_deleted": True, "proof": {"id": obj["object_id"], "status": "DELETED"}, "checked_at": now()}
        if kind == "ad":
            creative = data.get("creative") or {}
            expected = set(obj.get("creative_ids") or [])
            actual = str(creative.get("id") or "")
            if expected and actual not in expected:
                if actual or not expected.issubset(set(deleted_creatives or [])):
                    raise GraphError("creative_changed", "Ad 当前 Creative 与预览范围不一致，请重新预览")
        if kind == "video":
            # Library membership verifies the selected ad-account association;
            # actual deletion below still targets /{video_id}, never the edge.
            member = False
            for aid in obj["account_ids"]:
                if any(str(row.get("id")) == obj["object_id"] for row in self.inventory(obj, aid, edge="advideos")):
                    member = True
                    break
            if not member:
                raise GraphError("video_owner_unverified", "无法确认该视频属于预览中的广告账户素材库")
        if kind in ("creative", "video") and check_references:
            self.check_references(obj, set(allowed_ads or []))
        proof = {"id": obj["object_id"], "account_ids": obj["account_ids"], "checked_at": now()}
        if kind == "ad":
            proof["creative_id"] = str((data.get("creative") or {}).get("id") or "")
        if kind == "creative":
            proof["video_ids"] = creative_video_ids(data)
            if "verified_video_ids" in obj and set(proof["video_ids"]) != set(obj["verified_video_ids"]):
                raise GraphError("creative_video_changed", "Creative 当前视频关系与冻结预览不一致，请重新预览")
        return "pending", proof

    def delete(self, obj):
        """Call only after a durable attempt has been claimed by the service."""
        token = self.credential(obj)
        try:
            data = self.request("DELETE", meta_id(obj["object_id"]), token)
            if data is True or (isinstance(data, dict) and data.get("success") is True):
                return "deleted", {"success": True, "object_id": obj["object_id"], "checked_at": now()}
            return "unknown", {"code": "unconfirmed_delete_response", "message": "Meta 未返回明确删除成功，需要核实", "checked_at": now()}
        except GraphError as exc:
            return "unknown" if exc.uncertain else "failed", {"code": exc.code, "message": exc.message, "detail": exc.detail, "checked_at": now()}

    def reconcile(self, obj):
        # No unqualified GET error, including code 100/subcode 33 or missing
        # permissions, can turn an unknown write outcome into a successful one.
        try:
            fields = "id,account_id,status,effective_status" if obj["kind"] == "ad" else "id,account_id,status" if obj["kind"] == "creative" else "id"
            data = self.read_node(obj, fields)
            if obj["kind"] in ("ad", "creative") and account_id(data.get("account_id", "")) not in obj["account_ids"]:
                raise GraphError("account_mismatch", "Meta 对象所属账户不匹配")
            if str(data.get("status", "")).upper() == "DELETED" or str(data.get("effective_status", "")).upper() == "DELETED":
                return "already_deleted", {"confirmed_deleted": True, "proof": {"id": obj["object_id"], "status": "DELETED"}, "checked_at": now()}
            return "unknown", {"code": "still_readable", "message": "对象仍可读取；原删除请求的最终结果尚未确认，保持禁止重试", "checked_at": now()}
        except (GraphError, AssetError) as exc:
            return "unknown", {"code": exc.code, "message": exc.message, "checked_at": now()}
