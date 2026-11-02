"""Bounded sidecar client for the main API's private identity verifier."""
from __future__ import annotations

import json
import os
import re

import requests

from features.fb_auto_posts.validation import valid_internal_bearer
from .client import GatewayError
from .identity_broker import ACTOR_KEYS, IDENTITY_PATH, identity_keys


class IdentityClient:
    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.token = str(env.get("FB_AUTO_POST_INTERNAL_TOKEN") or "")

    def resolve(self, tenant_key, user_id, *, navigation_key="fbPageManagement"):
        identity_keys(tenant_key, user_id)
        if navigation_key not in {"fbPageManagement", "fbPostMaterialPool"}:
            raise GatewayError("invalid_identity", "发布入口无效", 400)
        payload = {"tenant_key": tenant_key, "user_id": user_id}
        if navigation_key != "fbPageManagement":
            payload["navigation_key"] = navigation_key
        if not valid_internal_bearer(self.token):
            raise GatewayError("identity_unavailable", "发布人身份验证暂不可用", 503)
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.post("http://127.0.0.1:8787" + IDENTITY_PATH,
                                  json=payload,
                                  headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
                                  timeout=(3, 35), allow_redirects=False, stream=True) as response:
                    raw = bytearray()
                    for chunk in response.iter_content(4096):
                        raw.extend(chunk)
                        if len(raw) > 16384:
                            raise ValueError()
                    data = json.loads(raw)
                    if response.status_code != 200:
                        code = data.get("code") if isinstance(data, dict) else ""
                        if response.status_code == 403 and code in {
                            "identity_inactive", "identity_permission_denied", "identity_attribution_invalid",
                        }:
                            raise GatewayError(code, "原发布人身份已失效，请重新启用计划", 403)
                        raise ValueError()
                    actor = data.get("actor") if isinstance(data, dict) and data.get("ok") is True else None
                    if (not isinstance(actor, dict) or set(actor) != ACTOR_KEYS
                            or actor["tenant_key"] != tenant_key or actor["user_id"] != user_id
                            or type(actor["is_admin"]) is not bool or not isinstance(actor["user_name"], str)
                            or len(actor["user_name"]) > 200
                            or not isinstance(actor["sub_user_id"], str)
                            or not re.fullmatch(r"[1-9][0-9]{0,30}", actor["sub_user_id"])
                            or actor["business_owner_id"] != actor["sub_user_id"]):
                        raise ValueError()
                    return actor
        except GatewayError:
            raise
        except Exception:
            raise GatewayError("identity_unavailable", "发布人身份验证暂不可用", 503) from None
