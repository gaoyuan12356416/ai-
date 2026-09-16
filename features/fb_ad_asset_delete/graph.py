"""Meta object adapter. GET uncertainty never implies successful deletion."""
import json
import re
import time
from copy import deepcopy
import requests
from .core import AssetError, account_id, creative_video_ids, meta_id, now, redact

CREATIVE_FIELDS = "id,account_id,status,video_id,object_story_spec,asset_feed_spec"
AD_FIELDS = "id,account_id,status,effective_status,creative{%s}" % CREATIVE_FIELDS
ACCOUNT_FIELDS = "id,account_id,account_status,user_tasks,disable_reason"
VIDEO_CREDENTIAL_CREATIVE_FIELDS = "id,object_story_spec,asset_feed_spec,video_id"
VIDEO_CREDENTIAL_MAX_CREATIVES = 2
VIDEO_CREDENTIAL_LOOKUP_SECONDS = 10
ERROR_FIELDS = ("code", "error_subcode", "type", "fbtrace_id", "is_transient",
                "message", "error_user_title", "error_user_msg")
LIVE_STATUSES = ["ACTIVE", "PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED", "DISAPPROVED", "PENDING_REVIEW", "PREAPPROVED", "PENDING_BILLING_INFO", "WITH_ISSUES", "IN_PROCESS", "ARCHIVED"]


def safe_error_detail(error, token):
    """Keep only bounded scalar diagnostics, never echoed credentials/payloads."""
    detail = {}
    for key in ERROR_FIELDS:
        if key not in error:
            continue
        value = error[key]
        if isinstance(value, str):
            detail[key] = redact(value.replace(token, "[redacted]") if token else value)
        elif value is None or isinstance(value, (int, float, bool)):
            detail[key] = value
        else:
            detail[key] = "[invalid field omitted]"
    return detail


class GraphError(AssetError):
    def __init__(self, code, message, uncertain=False, detail=None):
        super().__init__(str(code), redact(message), 400)
        self.uncertain = uncertain
        self.detail = detail or {}


class GraphClient:
    def __init__(self, token_provider, version="v25.0", transport=None, timeout=20, max_inventory=50000,
                 video_credential_provider=None, video_account_credential_provider=None):
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
            raise ValueError("invalid graph version")
        self.token_provider = token_provider
        self.video_credential_provider = video_credential_provider
        self.video_account_credential_provider = video_account_credential_provider
        self.base = "https://graph.facebook.com/" + version + "/"
        self.http = transport or requests.Session()
        self.timeout = timeout
        self.max_inventory = max_inventory
        self.tokens, self.inventory_cache, self.video_library_cache = {}, {}, {}
        self.credential_contexts = {}
        self.video_credential_creatives = {}

    def request(self, method, path, token, params=None, timeout=None):
        if not re.fullmatch(r"(?:act_)?[1-9][0-9]{0,31}(?:/(?:ads|advideos))?", path):
            raise GraphError("invalid_graph_path", "Meta 请求对象无效")
        try:
            response = self.http.request(method, self.base + path, params=params or {},
                headers={"Authorization": "Bearer " + token}, timeout=self.timeout if timeout is None else timeout, allow_redirects=False)
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
            detail = safe_error_detail(error, token)
            message = detail.get("message") or "Meta 请求失败"
            detail["message"] = redact(message)
            detail["http_status"] = response.status_code
            uncertain = method == "DELETE" and (response.status_code >= 500 or response.status_code in (301, 302, 307, 308))
            raise GraphError(detail.get("code", "graph_error"), message, uncertain, detail)
        return data

    @staticmethod
    def _credential_key(obj, account=None):
        accounts = obj.get("account_ids") or []
        aid = account_id(account or (accounts[0] if accounts else ""))
        users = [str(x) for x in obj.get("user_ids", []) if str(x).isdigit() and str(x) != "0"]
        return aid, tuple(users)

    @staticmethod
    def _account_diagnostic(data, aid):
        # Status 2 is Meta's disabled account state. Do not infer the meaning of
        # a DELETE error subcode from account state or the advertised task list.
        raw_status = data.get("account_status")
        status = int(raw_status) if not isinstance(raw_status, bool) and re.fullmatch(r"[0-9]{1,3}", str(raw_status)) else None
        tasks = data.get("user_tasks")
        tasks = sorted({value for value in tasks if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z_]{0,63}", value)}) if isinstance(tasks, list) else []
        diagnostic = {"account_id": aid, "account_status": status,
                      "account_state": "disabled" if status == 2 else "active" if status == 1 else "unknown",
                      "user_tasks": tasks, "readable": True, "delete_permission": "unverified", "checked_at": now(),
                      "message": "仅确认广告账户可读；user_tasks 不足以确认具体对象的删除权限"}
        if status == 2:
            diagnostic["message"] = "广告账户已停用（account_status=2），请由管理员核查账户限制；账户可读不代表具有对象删除权限"
        reason = data.get("disable_reason")
        if not isinstance(reason, bool) and re.fullmatch(r"[0-9]{1,6}", str(reason)):
            diagnostic["disable_reason"] = int(reason)
        return diagnostic

    def credential_context(self, obj, account=None):
        """Return the selected credential's safe snapshot without any network I/O."""
        return deepcopy(self.credential_contexts.get(self._credential_key(obj, account), {}))

    def credential(self, obj, account=None):
        cache_key = self._credential_key(obj, account)
        aid, users = cache_key
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
                data = self.request("GET", "act_" + aid, token, {"fields": ACCOUNT_FIELDS})
                if not isinstance(data, dict) or account_id(data.get("account_id") or data.get("id")) != aid:
                    raise GraphError("account_mismatch", "Meta Token 返回的广告账户不匹配")
                self.tokens[cache_key] = token
                self.credential_contexts[cache_key] = {"credential_user_id": uid,
                    "account_diagnostic": self._account_diagnostic(data, aid)}
                return token
            except GraphError as exc:
                exc.detail.update(credential_probe_user_id=uid, account_id=aid)
                last = exc
        raise last or GraphError("missing_token", "所配置用户没有可用的 Meta Token")

    def diagnose(self, obj, account=None, fresh=False):
        """Read account diagnostics; a refresh never rotates a selected token."""
        cache_key = self._credential_key(obj, account)
        aid = cache_key[0]
        had_context = cache_key in self.credential_contexts
        try:
            token = self.credential(obj, aid)
            if fresh and had_context:
                data = self.request("GET", "act_" + aid, token, {"fields": ACCOUNT_FIELDS})
                if not isinstance(data, dict) or account_id(data.get("account_id") or data.get("id")) != aid:
                    raise GraphError("account_mismatch", "Meta Token 返回的广告账户不匹配")
                self.credential_contexts[cache_key]["account_diagnostic"] = self._account_diagnostic(data, aid)
            return dict(self.credential_context(obj, aid), read_only=True)
        except GraphError as exc:
            context = self.credential_context(obj, aid)
            context["account_diagnostic"] = {"account_id": aid, "account_status": None, "account_state": "unknown",
                "readable": False, "delete_permission": "unverified", "checked_at": now(),
                "code": exc.code, "message": exc.message, "detail": exc.detail}
            if had_context:
                self.credential_contexts[cache_key] = deepcopy(context)
            return dict(context, read_only=True)

    def video_credential(self, obj):
        """Choose an existing configured token without any Graph read probe.

        Token absence may select the next configured candidate. A DELETE error
        never rotates credentials or causes an automatic second write.
        """
        users = tuple(dict.fromkeys(str(x) for x in obj.get("user_ids", [])
                                   if str(x).isdigit() and str(x) != "0"))
        key = ("video_id_direct", users)
        if key in self.tokens:
            return self.tokens[key], deepcopy(self.credential_contexts[key])
        for uid in users:
            token = self.token_provider([uid])
            if token:
                context = {"credential_user_id": uid, "credential_kind": "user", "delete_mode": "video_id_direct",
                           "credential_relation": "configured_user"}
                self.tokens[key], self.credential_contexts[key] = token, context
                return token, deepcopy(context)
        raise GraphError("missing_token", "所配置用户没有可用的 Meta Token")

    @staticmethod
    def _identity_id(value):
        text = str(value or "")
        return text if re.fullmatch(r"[1-9][0-9]{0,31}", text) else ""

    def _remember_video_creative(self, creative_id, data):
        if not isinstance(data, dict) or str(data.get("id", "")) != creative_id:
            return None
        spec = data.get("object_story_spec")
        relation = {"page_id": self._identity_id(spec.get("page_id")) if isinstance(spec, dict) else "",
                    "video_ids": creative_video_ids(data)}
        # Relation facts may be reused after the Creative stage deletes its
        # node. Credentials themselves must be read afresh for every Video.
        self.video_credential_creatives[creative_id] = relation
        return relation

    def _video_identity_credential(self, users, identity_id, relation):
        credential = self.video_credential_provider(list(users), identity_id, relation)
        if not isinstance(credential, dict):
            return None
        token = credential.get("token")
        uid = self._identity_id(credential.get("credential_user_id"))
        fbid = self._identity_id(credential.get("credential_fb_user_id"))
        kind = credential.get("credential_kind")
        if not isinstance(token, str) or not token.strip() or uid not in users or not fbid:
            return None
        context = {"delete_mode": "video_id_direct", "credential_kind": kind,
                   "credential_user_id": uid, "credential_fb_user_id": fbid,
                   "credential_relation": relation, "credential_lookup": "matched"}
        if kind == "page":
            page = self._identity_id(credential.get("credential_page_id"))
            row = self._identity_id(credential.get("credential_row_id"))
            if page != identity_id or not row:
                return None
            context.update(credential_page_id=page, credential_row_id=row)
        elif kind != "user" or relation != "video_from" or fbid != identity_id:
            return None
        return token, context

    def prepare_video_delete(self, obj):
        """Resolve one credential before a durable audit and a single DELETE.

        These bounded GETs only improve credential selection. Missing identity,
        unreadable nodes, or unavailable Page credentials always keep the
        configured User Token path; none is a Video deletion gate.
        """
        users = tuple(dict.fromkeys(str(x) for x in obj.get("user_ids", []) if self._identity_id(x)))
        # This relation comes only from this ledger object's previous attempt,
        # never from execute payload fields. A still-configured Page credential
        # must remain usable if its linked User Token has since been cleared.
        previous = obj.get("result")
        if self.video_credential_provider is not None and isinstance(previous, dict):
            page = self._identity_id(previous.get("credential_page_id"))
            relation = previous.get("credential_relation")
            if previous.get("credential_kind") == "page" and page and relation in ("video_from", "creative_page"):
                try:
                    selected = self._video_identity_credential(users, page, relation)
                    if selected and selected[1]["credential_kind"] == "page":
                        selected[1]["credential_lookup_message"] = (
                            "沿用本对象历史 Video.from 身份，重新读取当前 Page 凭证" if relation == "video_from" else
                            "沿用本对象历史 Creative 关联 Page，重新读取当前凭证；关联 Page 不代表已确认上传者")
                        return selected
                except Exception:
                    pass
        token, fallback = self.video_credential(obj)
        if self.video_credential_provider is None:
            return token, fallback
        video_id = meta_id(obj["object_id"])
        deadline = time.monotonic() + VIDEO_CREDENTIAL_LOOKUP_SECONDS
        notes = []

        def note(message):
            if len(notes) < 8:
                notes.append(message)

        def read_identity(oid, fields):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GraphError("identity_lookup_timeout", "身份解析已达到时限")
            data = self.request("GET", oid, token, {"fields": fields}, timeout=min(self.timeout, 3, remaining))
            if not isinstance(data, dict) or str(data.get("id", "")) != oid:
                raise GraphError("object_mismatch", "身份解析返回对象不匹配")
            return data

        def choose(identity_id, relation, message):
            if time.monotonic() >= deadline:
                return None
            try:
                selected = self._video_identity_credential(users, identity_id, relation)
            except Exception:
                # Never expose query/credential exceptions, which may contain
                # credentials, and never turn this optional lookup into a gate.
                note("对应身份的凭证查询未完成")
                return None
            if selected:
                selected[1]["credential_lookup_message"] = message
                return selected
            note("对应身份没有冻结候选用户的可用凭证")
            return None

        try:
            data = read_identity(video_id, "id,from")
            owner = data.get("from")
            owner_id = self._identity_id(owner.get("id")) if isinstance(owner, dict) else ""
            if owner_id:
                selected = choose(owner_id, "video_from", "按 Video.from 身份选择冻结候选用户的凭证")
                if selected:
                    return selected
            else:
                note("Video.from 未返回可用身份")
        except Exception:
            note("Video 身份读取未完成")

        creatives = list(dict.fromkeys(self._identity_id(x) for x in obj.get("creative_ids", []) if self._identity_id(x)))
        seen_pages = set()
        for cid in creatives[:VIDEO_CREDENTIAL_MAX_CREATIVES]:
            if time.monotonic() >= deadline:
                note("身份解析达到时限")
                break
            try:
                relation = self.video_credential_creatives.get(cid)
                if relation is None:
                    relation = self._remember_video_creative(cid, read_identity(cid, VIDEO_CREDENTIAL_CREATIVE_FIELDS))
                if not relation or video_id not in relation["video_ids"]:
                    note("Creative 未精确引用本 Video")
                    continue
                page_id = relation["page_id"]
                if not page_id or page_id in seen_pages:
                    continue
                seen_pages.add(page_id)
                selected = choose(page_id, "creative_page",
                    "Creative %s 精确引用本 Video，使用其关联 Page 的凭证；关联 Page 不代表已确认上传者" % cid)
                if selected:
                    return selected
            except Exception:
                note("关联 Creative 读取未完成")
        if len(creatives) > VIDEO_CREDENTIAL_MAX_CREATIVES:
            note("关联 Creative 解析达到数量上限")
        fallback.update(credential_lookup="fallback", credential_lookup_message=
            "；".join(notes + ["使用原冻结候选用户的 User Token，继续单次删除"])[:800])
        return token, fallback

    def read_node(self, obj, fields):
        token = self.credential(obj)
        try:
            data = self.request("GET", meta_id(obj["object_id"]), token, {"fields": fields})
        except GraphError as exc:
            exc.detail.update(self.credential_context(obj))
            raise
        if not isinstance(data, dict) or str(data.get("id", "")) != obj["object_id"]:
            raise GraphError("object_mismatch", "Meta 未返回可核验的对象 ID", detail=self.credential_context(obj))
        if self.video_credential_provider is not None and obj.get("kind") == "creative":
            self._remember_video_creative(obj["object_id"], data)
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
            try:
                data = self.request("GET", "act_" + aid + "/" + edge, token, params)
            except GraphError as exc:
                exc.detail.update(self.credential_context(obj, aid))
                raise
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise GraphError("reference_check_incomplete", "Meta 账户引用清单未完整返回", detail=self.credential_context(obj, aid))
            result.extend(data["data"])
            if len(result) > self.max_inventory:
                raise GraphError("reference_check_incomplete", "Meta 账户引用超过核验上限，不能执行该素材删除", detail=self.credential_context(obj, aid))
            paging = data.get("paging") or {}
            if not paging.get("next"):
                break
            after = (paging.get("cursors") or {}).get("after")
            if not after or after in cursors:
                raise GraphError("reference_check_incomplete", "Meta 引用清单分页不完整", detail=self.credential_context(obj, aid))
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
                    raise GraphError("reference_check_incomplete", "账户内存在无法读取 Creative 的广告，无法排除共享引用", detail=self.credential_context(obj, aid))
                referenced = str(creative["id"]) == obj["object_id"] if obj["kind"] == "creative" else obj["object_id"] in creative_video_ids(creative)
                if referenced:
                    raise GraphError("shared_outside_scope", "该素材仍被范围外 Ad %s 引用" % ad["id"], detail=self.credential_context(obj, aid))

    def inspect(self, obj, allowed_ads=None, deleted_creatives=None, check_references=True):
        kind = obj["kind"]
        fields = AD_FIELDS if kind == "ad" else CREATIVE_FIELDS if kind == "creative" else "id,from"
        data = self.read_node(obj, fields)
        if kind in ("ad", "creative"):
            if account_id(data.get("account_id", "")) not in obj["account_ids"]:
                raise GraphError("account_mismatch", "Meta 对象所属账户与预览不一致", detail=self.credential_context(obj))
            if str(data.get("status", "")).upper() == "DELETED" or str(data.get("effective_status", "")).upper() == "DELETED":
                return "already_deleted", dict(self.credential_context(obj), confirmed_deleted=True,
                    proof={"id": obj["object_id"], "status": "DELETED"}, checked_at=now())
        if kind == "ad":
            creative = data.get("creative") or {}
            expected = set(obj.get("creative_ids") or [])
            actual = str(creative.get("id") or "")
            if expected and actual not in expected:
                if actual or not expected.issubset(set(deleted_creatives or [])):
                    raise GraphError("creative_changed", "Ad 当前 Creative 与预览范围不一致，请重新预览", detail=self.credential_context(obj))
        if kind == "video":
            # Library membership verifies the selected ad-account association;
            # actual deletion below still targets /{video_id}, never the edge.
            member = False
            for aid in obj["account_ids"]:
                if any(str(row.get("id")) == obj["object_id"] for row in self.inventory(obj, aid, edge="advideos")):
                    member = True
                    break
            if not member:
                raise GraphError("video_owner_unverified", "无法确认该视频属于预览中的广告账户素材库", detail=self.credential_context(obj))
        if kind in ("creative", "video") and check_references:
            self.check_references(obj, set(allowed_ads or []))
        proof = dict(self.credential_context(obj), id=obj["object_id"], account_ids=obj["account_ids"], checked_at=now())
        if kind == "ad":
            proof["creative_id"] = str((data.get("creative") or {}).get("id") or "")
        if kind == "creative":
            proof["video_ids"] = creative_video_ids(data)
            if "verified_video_ids" in obj and set(proof["video_ids"]) != set(obj["verified_video_ids"]):
                raise GraphError("creative_video_changed", "Creative 当前视频关系与冻结预览不一致，请重新预览", detail=self.credential_context(obj))
        return "pending", proof

    @staticmethod
    def _video_account_target(obj, account):
        if obj.get("kind") != "video":
            raise GraphError("invalid_video_kind", "账户视频接口只接受冻结 Video")
        vid, aid = meta_id(obj["object_id"]), account_id(account)
        allowed = {account_id(value) for value in obj.get("account_ids", [])}
        if aid not in allowed:
            raise GraphError("account_outside_preview", "广告账户不在该 Video 的冻结范围内")
        return vid, aid

    def prepare_video_account_delete(self, obj, account_id):
        """Select the queue's current credential; no Meta GET is a write gate."""
        _, aid = self._video_account_target(obj, account_id)
        users = list(dict.fromkeys(str(x) for x in obj.get("user_ids", []) if self._identity_id(x)))
        context = {"delete_mode": "ad_account_video", "delete_account_id": aid,
                   "delete_endpoint": "act_" + aid + "/advideos", "credential_kind": "user"}
        if self.video_account_credential_provider is not None:
            route_fields = ("credential_source_row_id", "credential_ad_id", "credential_product_id",
                            "credential_source_user_id", "credential_publish_queue_id", "credential_default_token")
            try:
                credential = self.video_account_credential_provider(obj, aid)
                if isinstance(credential, dict):
                    uid = self._identity_id(credential.get("credential_user_id"))
                    token = credential.get("token")
                    relation = credential.get("credential_relation")
                    route = {key: str(credential.get(key) or "") for key in route_fields}
                    source_uid = route["credential_source_user_id"]
                    source_id, product_id, ad_id = (route[key] for key in
                        ("credential_source_row_id", "credential_product_id", "credential_ad_id"))
                    scope_ok = (source_uid in users and product_id in obj.get("product_ids", []) and
                        ad_id in obj.get("ad_ids", []) and self._identity_id(source_id) and
                        self._identity_id(route["credential_publish_queue_id"]))
                    frozen = obj.get("video_account_sources")
                    if isinstance(frozen, list):
                        scope_ok = scope_ok and any(isinstance(row, dict) and
                            str(row.get("account_id", "")).removeprefix("act_") == aid and
                            all(str(row.get(key)) == value for key, value in
                                (("source_row_id", source_id), ("product_id", product_id),
                                 ("ad_id", ad_id), ("user_id", source_uid))) for row in frozen)
                    rule_ok = (relation == "product_default_user" and route["credential_default_token"] == "1" or
                        relation == "publish_queue_user" and route["credential_default_token"] == "-1" and uid == source_uid)
                    if (uid and scope_ok and rule_ok and credential.get("credential_kind") == "user" and
                            isinstance(token, str) and token.strip()):
                        context.update(route)
                        context.update(credential_user_id=uid, credential_relation=relation,
                            credential_lookup="matched", credential_lookup_message=
                                "发布队列启用默认 Token，使用该产品当前配置的默认用户凭证" if relation == "product_default_user" else
                                "发布队列未启用默认 Token，使用发布用户自己的凭证")
                        fbid = self._identity_id(credential.get("credential_fb_user_id"))
                        if fbid:
                            context["credential_fb_user_id"] = fbid
                        return token, context
            except AssetError as exc:
                detail = getattr(exc, "detail", {})
                for key in route_fields + ("credential_user_id", "credential_relation", "credential_fb_user_id"):
                    value = detail.get(key)
                    if isinstance(value, (str, int)):
                        context[key] = value
                raise GraphError(exc.code, exc.message, detail=context) from None
            except Exception:
                raise GraphError("credential_read_unavailable", "发布队列或指定 Token 读取未完成；未改用其他用户", detail=context) from None
            raise GraphError("publishing_token_unavailable", "发布凭证与队列规则或冻结范围不匹配；未发送删除请求", detail=context)
        for uid in users:
            try:
                token = self.token_provider([uid])
            except Exception:
                raise GraphError("credential_read_unavailable", "User Token 读取未完成", detail=context) from None
            if token:
                context.update(credential_user_id=uid, credential_relation="fallback", credential_lookup="fallback",
                               credential_lookup_message="使用原冻结候选用户的现有凭证，未进行 Meta 读取预检")
                return token, context
        raise GraphError("missing_token", "冻结候选用户没有可用的 Meta User Token", detail=context)

    def delete_video_account(self, obj, account_id, prepared=None):
        """One DELETE of the frozen (account, video), without token rotation."""
        context, delete_sent = {}, False
        try:
            vid, aid = self._video_account_target(obj, account_id)
            token, context = prepared if prepared is not None else self.prepare_video_account_delete(obj, aid)
            context = deepcopy(context)
            endpoint = "act_" + aid + "/advideos"
            if (context.get("delete_mode") != "ad_account_video" or context.get("delete_account_id") != aid or
                    context.get("delete_endpoint") != endpoint or context.get("credential_kind") != "user"):
                raise GraphError("prepared_account_mismatch", "已审计凭证与目标广告账户不一致")
            result = dict(context, account_id=aid, video_id=vid, delete_scope="ad_account_video")
            delete_sent = True
            data = self.request("DELETE", endpoint, token, {"video_id": vid})
            if data is True or isinstance(data, dict) and data.get("success") is True:
                return "deleted", dict(result, success=True, checked_at=now())
            return "unknown", dict(result, code="unconfirmed_delete_response", message="Meta 未明确确认账户视频删除结果，需要核实", checked_at=now())
        except AssetError as exc:
            state = "unknown" if getattr(exc, "uncertain", False) else "failed"
            result = dict(context, account_id=str(account_id).removeprefix("act_"), video_id=str(obj.get("object_id", "")),
                delete_mode="ad_account_video", delete_scope="ad_account_video", code=exc.code,
                message=exc.message, detail=getattr(exc, "detail", {}), checked_at=now())
            if delete_sent and self._invalid_account_video_id(exc):
                # A rejected ID may have been removed manually. The DELETE
                # error itself is not proof: read the complete account library
                # with this exact prepared credential, without another DELETE.
                delete_error = {key: deepcopy(result[key]) for key in ("code", "message", "detail", "checked_at")}
                verified_state, verification = self._read_video_account_absence(vid, aid, (token, context), seconds=10)
                evidence = dict(verification, status=verified_state)
                if verified_state == "already_deleted":
                    return verified_state, dict(verification, delete_error=delete_error, verification=evidence,
                        message="Meta 拒绝此 Video ID；完整读取该账户视频库后确认已无此 Video")
                result.update(delete_error=delete_error, verification=evidence,
                    message=redact(result["message"] + "；删除后核实：" + verification["message"]))
            return state, result

    @staticmethod
    def _invalid_account_video_id(exc):
        """Match the explicit parameter error, never a generic code 100 denial."""
        return (isinstance(exc, GraphError) and not exc.uncertain and exc.code == "100" and
            re.fullmatch(r"(?:\(#100\)\s*)?Param video_id is not a valid video ID\.?", exc.message.strip(), re.IGNORECASE) is not None)

    def reconcile_video_account(self, obj, account_id):
        """Only a complete account-library read can prove this pair is absent."""
        context = {}
        try:
            vid, aid = self._video_account_target(obj, account_id)
            token, context = self.prepare_video_account_delete(obj, aid)
            return self._read_video_account_absence(vid, aid, (token, context))
        except AssetError as exc:
            return "unknown", dict(context, account_id=str(account_id).removeprefix("act_"), video_id=str(obj.get("object_id", "")),
                delete_mode="ad_account_video", delete_scope="ad_account_video", code=exc.code,
                message=exc.message, detail=getattr(exc, "detail", {}), checked_at=now())

    def _read_video_account_absence(self, vid, aid, prepared, seconds=30):
        """GET only; retain the caller's credential and bounded account proof."""
        token, context = prepared
        result = dict(context, account_id=aid, video_id=vid, delete_scope="ad_account_video")
        try:
            params, seen, cursors, pages = {"fields": "id", "limit": "100"}, set(), set(), 0
            deadline = time.monotonic() + seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GraphError("account_video_read_limit", "账户视频完整核实已达到时限")
                data = self.request("GET", "act_" + aid + "/advideos", token, params, timeout=min(self.timeout, 5, remaining))
                pages += 1
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise GraphError("account_video_read_incomplete", "账户视频列表未完整返回")
                for item in data["data"]:
                    ident = self._identity_id(item.get("id")) if isinstance(item, dict) else ""
                    if not ident or ident in seen:
                        raise GraphError("account_video_read_incomplete", "账户视频列表含无法核实或重复的记录")
                    if ident == vid:
                        return "unknown", dict(result, code="account_video_present", message="该账户下仍能读取此 Video，不能确认该账户视频已移除", checked_at=now())
                    seen.add(ident)
                    if len(seen) > self.max_inventory:
                        raise GraphError("account_video_read_limit", "账户视频列表超过完整核实上限")
                paging = data.get("paging", {})
                if not isinstance(paging, dict):
                    raise GraphError("account_video_read_incomplete", "账户视频分页信息无效")
                if not paging.get("next"):
                    proof = {"account_id": aid, "video_id": vid, "complete": True, "pages": pages, "items": len(seen)}
                    return "already_deleted", dict(result, confirmed_absent=True, proof=proof,
                        message="完整读取该账户视频库后确认已无此 Video", checked_at=now())
                after = (paging.get("cursors") or {}).get("after") if isinstance(paging.get("cursors", {}), dict) else None
                if not isinstance(after, str) or not after or len(after) > 4096 or after in cursors or pages >= min(self.max_inventory, 500):
                    raise GraphError("account_video_read_incomplete", "账户视频分页不完整或已达到上限")
                cursors.add(after)
                params["after"] = after
        except AssetError as exc:
            return "unknown", dict(result, code=exc.code,
                message=exc.message, detail=getattr(exc, "detail", {}), checked_at=now())
        except Exception:
            # A failed follow-up read cannot replace an explicit DELETE failure
            # with a new unknown write outcome or interrupt later objects.
            return "unknown", dict(result, code="account_video_read_incomplete",
                message="账户视频读取未完整结束，不能确认该账户已无此 Video", checked_at=now())

    def delete(self, obj, prepared=None):
        """Call only after a durable attempt has been claimed by the service."""
        context = {"delete_mode": "video_id_direct"} if obj["kind"] == "video" else {}
        try:
            if obj["kind"] == "video":
                token, context = prepared if prepared is not None else self.prepare_video_delete(obj)
                context = deepcopy(context)
            else:
                token = self.credential(obj)
                context = self.credential_context(obj)
            data = self.request("DELETE", meta_id(obj["object_id"]), token)
            if data is True or (isinstance(data, dict) and data.get("success") is True):
                return "deleted", dict(context, success=True, object_id=obj["object_id"], checked_at=now())
            return "unknown", dict(context, code="unconfirmed_delete_response", message="Meta 未返回明确删除成功，需要核实", checked_at=now())
        except GraphError as exc:
            if obj["kind"] != "video":
                context = self.credential_context(obj)
            return "unknown" if exc.uncertain else "failed", dict(context, code=exc.code, message=exc.message, detail=exc.detail, checked_at=now())

    def reconcile(self, obj):
        # No unqualified GET error, including code 100/subcode 33 or missing
        # permissions, can turn an unknown write outcome into a successful one.
        context = {}
        try:
            fields = "id,account_id,status,effective_status" if obj["kind"] == "ad" else "id,account_id,status" if obj["kind"] == "creative" else "id"
            if obj["kind"] == "video":
                token, context = self.prepare_video_delete(obj)
                data = self.request("GET", meta_id(obj["object_id"]), token, {"fields": fields})
                if not isinstance(data, dict) or str(data.get("id", "")) != obj["object_id"]:
                    raise GraphError("object_mismatch", "Meta 未返回可核验的对象 ID")
            else:
                data = self.read_node(obj, fields)
                context = self.credential_context(obj)
            if obj["kind"] in ("ad", "creative") and account_id(data.get("account_id", "")) not in obj["account_ids"]:
                raise GraphError("account_mismatch", "Meta 对象所属账户不匹配", detail=self.credential_context(obj))
            if obj["kind"] in ("ad", "creative") and (str(data.get("status", "")).upper() == "DELETED" or str(data.get("effective_status", "")).upper() == "DELETED"):
                return "already_deleted", dict(context, confirmed_deleted=True,
                    proof={"id": obj["object_id"], "status": "DELETED"}, checked_at=now())
            return "unknown", dict(context, code="still_readable", message="对象仍可读取；原删除请求的最终结果尚未确认，保持禁止重试", checked_at=now())
        except (GraphError, AssetError) as exc:
            if obj["kind"] != "video":
                context = self.credential_context(obj)
            return "unknown", dict(context, code=exc.code, message=exc.message,
                detail=getattr(exc, "detail", {}), checked_at=now())
