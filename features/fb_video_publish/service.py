"""Manual Page configuration, read-only confirmation and atomic batch API."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import timedelta

from .core import DomainError, actor_checked, canonical, digest, id_text, normalize_batch, parse_time, stamp
from .templates import render_preview

PREFIX = "/api/fb-video-publish"
PLACEHOLDER = "[提交后生成独立短链]"
LANGUAGES = {"en": "英语", "es": "西班牙语", "zh": "中文", "zh-tw": "繁体中文", "zh-cn": "简体中文",
             "pt": "葡萄牙语", "id": "印度尼西亚语", "th": "泰语", "vi": "越南语", "fr": "法语",
             "de": "德语", "ja": "日语", "ko": "韩语", "ar": "阿拉伯语", "ru": "俄语", "tl": "菲律宾语",
             "it": "意大利语", "tr": "土耳其语", "hi": "印地语", "ms": "马来语", "pl": "波兰语"}


class Service:
    def __init__(self, store, sources, signing_key, *, live_enabled=False, prepare_enabled=True):
        if len(str(signing_key)) < 32:
            raise ValueError("manual preflight signing key is required")
        self.store, self.sources = store, sources
        self.signing_key, self.live_enabled = str(signing_key).encode(), live_enabled
        self.prepare_enabled = bool(prepare_enabled)

    @staticmethod
    def _keys(payload, required):
        if not isinstance(payload, dict) or set(payload) != set(required):
            raise DomainError("invalid_request", "请求字段不完整或包含未知字段")

    def _token(self, facts):
        body = canonical({"facts": digest(facts), "expires": int(self.store.now_fn().timestamp()) + 900}).encode()
        encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
        signature = hmac.new(self.signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + "." + signature

    def _verify(self, token):
        try:
            if not isinstance(token, str) or len(token) > 1000:
                raise ValueError()
            encoded, signature = token.split(".")
            expected = hmac.new(self.signing_key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError()
            body = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if set(body) != {"facts", "expires"} or type(body["expires"]) is not int or not re.fullmatch("[a-f0-9]{64}", body["facts"]):
                raise ValueError()
            if body["expires"] <= self.store.now_fn().timestamp():
                raise DomainError("preview_expired", "确认预览已过期，请重新预览", 409)
            return body["facts"]
        except DomainError:
            raise
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise DomainError("invalid_preview", "确认预览校验失败，请重新预览", 409) from None

    def resolve(self, actor, request):
        if not re.fullmatch("[1-9][0-9]{0,30}", actor["sub_user_id"]):
            raise DomainError("actor_mapping_missing", "发布人邮箱未唯一映射到有效sub_user_id", 403)
        pages = self.sources.get_pages(actor, request["page_ids"])
        if request["processing_mode"] == "random_overlay" and not self.prepare_enabled:
            raise DomainError("prepare_gate_closed", "视频加工服务未启用，请选择不加工", 409)
        materials = self.sources.get_materials(request["material_ids"], need_desc=False)
        if set(pages) != set(request["page_ids"]) or set(materials) != set(request["material_ids"]):
            raise DomainError("source_scope_changed", "素材或Page不再可用", 409)
        return {"pages": pages, "materials": materials}

    def preflight(self, actor, raw):
        request = normalize_batch(raw)
        resolved = self.resolve(actor, request)
        now = self.store.now_fn()
        start = now if request["schedule_mode"] == "now" else parse_time(request["scheduled_at_utc"])
        if request["schedule_mode"] == "scheduled" and start <= now:
            raise DomainError("invalid_schedule", "预约时间必须晚于当前时间")
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            configs, repeats = self.store.check_plan(conn, actor, request, resolved)
            facts = {"actor": actor, "request": request, "resolved": resolved, "configs": configs, "repeats": repeats}
        counts, items = {}, []
        for p in request["pairs"]:
            pid, mid = p["page_id"], p["material_id"]
            counts[pid] = counts.get(pid, 0) + 1
            material, page = resolved["materials"][mid], resolved["pages"][pid]
            items.append({"material_id": mid, "page_id": pid, "material_name": material["material_name"],
                          "page_name": page["page_name"], "config_version": p["config_version"],
                          "caption": render_preview(p["caption_template"], material, PLACEHOLDER),
                          "short_url": PLACEHOLDER, "short_url_is_placeholder": True,
                          "long_url_parameters": {
                              "c": "yingliang_post_CLV_VL_facebook_" + pid
                                   + "*[提交创建秒时间戳]none" + material["language"] + "*"
                                   + material["drama_name"] + "*" + str(material.get("material_tag") or "none") + "*[提交记录ID]",
                              "af_adset": page["page_name"], "af_adset_id": pid,
                              "af_ad": material["material_name"] + "_contentid[" + str(material["content_id"]) + "]",
                              "af_ad_id": mid, "af_channel": actor["sub_user_id"],
                              "af_c_id": "[提交任务ID]", "af_dp": str(material["content_id"])},
                          "planned_at_utc": stamp(start + timedelta(minutes=(counts[pid] - 1) * request["interval_minutes"])),
                          "repeat_required": pid + ":" + mid in repeats})
        return {"ok": True, "preview_token": self._token(facts), "expires_at_utc": stamp(now + timedelta(minutes=15)),
                "preview_hash": digest(facts), "items": items, "repeat_required": bool(repeats),
                "repeat_fingerprint": digest(repeats), "landing_url": "https://www.dramawavew2a.com/ads/101/2310/view"}

    def configured_pages(self, actor):
        visible = {p["page_id"]: p for p in self.sources.list_pages(actor)}
        return {"ok": True, "items": [{**c, **visible[pid], "language": c["language"]} for pid, c in self.store.configs(actor).items() if pid in visible]}

    def _config_access(self, actor, ids):
        visible = {p["page_id"] for p in self.sources.list_pages(actor)}
        if any(pid not in visible for pid in ids):
            raise DomainError("page_not_found", "Page不存在或不在当前访问范围", 404)

    def handle(self, method, path, actor, payload=None, query=None):
        actor = actor_checked(actor)
        query = query or {}
        query = {k: (v[0] if isinstance(v, list) and v else v) for k, v in query.items()}
        payload = {} if payload is None else payload
        try:
            return self._handle(method, path, actor, payload, query)
        except Exception as exc:
            # Explicit domain validation failures cannot follow successful commit.
            # Unexpected faults retain an ambiguous result for operation lookup.
            if hasattr(exc, "code") and hasattr(exc, "status"):
                exc.accepted = False
            raise

    def _handle(self, method, path, actor, payload, query):
        route = path.removeprefix(PREFIX)
        if not path.startswith(PREFIX + "/"):
            raise DomainError("not_found", "接口不存在", 404)
        if method == "GET":
            if route == "/languages":
                return {"ok": True, "items": [{"value": v, "label": label} for v, label in LANGUAGES.items()]}
            if route == "/pages/available":
                return {"ok": True, "items": self.sources.list_pages(actor, q=str(query.get("q", ""))[:200])}
            if route == "/pages":
                return self.configured_pages(actor)
            if route == "/materials":
                return {"ok": True, **self.sources.search_materials({**query, "search": query.get("q", query.get("search", ""))})}
            if route == "/draft":
                return self.store.draft(actor)
            if route in {"/batches", "/tasks"}:
                try:
                    limit, cursor = int(query.get("limit", 50)), str(query.get("cursor", ""))
                    if not 1 <= limit <= 100 or (cursor and not re.fullmatch("[1-9][0-9]{0,18}", cursor)):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise DomainError("invalid_request", "分页参数无效") from None
                if route == "/tasks":
                    status = str(query.get("status", ""))
                    if status not in {"", "queued", "running", "submitted", "published", "unknown", "failed", "cancelled"}:
                        raise DomainError("invalid_request", "任务状态无效")
                    return self.store.list_tasks(actor, limit, cursor, status)
                return self.store.list_batches(actor, limit, cursor)
            match = re.fullmatch(r"/tasks/([a-f0-9]{32})", route)
            if match:
                return self.store.get_task(actor, match[1])
            match = re.fullmatch(r"/batches/([a-f0-9]{32})", route)
            if match:
                return self.store.get_batch(actor, match[1])
            match = re.fullmatch(r"/operations/([A-Za-z0-9_-]{1,70})", route)
            if match:
                result = self.store.existing(actor, match[1])
                if result is None:
                    raise DomainError("operation_not_found", "尚未查到本次操作；请保留操作标识重试", 404)
                return result
        if method == "POST":
            if route == "/draft":
                self._keys(payload, {"draft"})
                return self.store.draft(actor, payload["draft"])
            if route == "/pages":
                self._keys(payload, {"page_ids", "language", "caption_template"})
                if not isinstance(payload["page_ids"], list):
                    raise DomainError("invalid_request", "Page列表无效")
                ids = [id_text(v) for v in payload["page_ids"]]
                self._config_access(actor, ids)
                return {"ok": True, "items": self.store.save_configs(actor, ids, payload["language"], payload["caption_template"])}
            match = re.fullmatch(r"/pages/([1-9][0-9]{0,30})(/delete)?", route)
            if match:
                expected = {"config_instance_id", "expected_version"}
                self._keys(payload, expected if match[2] else expected | {"language", "caption_template"})
                self._config_access(actor, [match[1]])
                if type(payload["expected_version"]) is not int or payload["expected_version"] < 1:
                    raise DomainError("invalid_request", "配置版本无效")
                if match[2]:
                    return self.store.delete_config(actor, match[1], payload["config_instance_id"], payload["expected_version"])
                return {"ok": True, "items": self.store.save_configs(actor, [match[1]], payload["language"], payload["caption_template"],
                         expected=(payload["config_instance_id"], payload["expected_version"]))}
            if route == "/preflight":
                return self.preflight(actor, payload)
            if route == "/batches":
                if not isinstance(payload, dict) or type(payload.get("allow_repeat")) is not bool:
                    raise DomainError("invalid_request", "提交确认字段无效")
                request = normalize_batch({k: v for k, v in payload.items() if k not in {"preview_token", "allow_repeat"}})
                existing = self.store.existing(actor, request["operation_id"], digest(request))
                if existing:
                    return existing
                if not self.live_enabled:
                    raise DomainError("manual_live_gate_closed", "手动发布开关关闭，未创建任务", 409)
                facts = self._verify(payload.get("preview_token"))
                resolved = self.resolve(actor, request)
                return self.store.submit(actor, request, resolved, facts, payload["allow_repeat"])
            match = re.fullmatch(r"/tasks/([a-f0-9]{32})/cancel", route)
            if match:
                self._keys(payload, set())
                return self.store.cancel(actor, match[1])
        raise DomainError("not_found", "接口不存在", 404)
