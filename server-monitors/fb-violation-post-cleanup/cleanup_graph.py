"""Bounded Page-only Graph reads and one-shot DELETE transport.

Secrets live only in Credential/transport memory. There is deliberately no
automatic retry, redirect, token rotation, or video-delete fallback on DELETE.
"""
import datetime as dt
import re
import threading
import time

import requests


RATE_CODES = {4, 17, 32, 613, 80004, 80006}
ID = re.compile(r"^[0-9]+(?:_[0-9]+)?$")


class RateBudget:
    def __init__(self, interval=0.5):
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.cooldown_until = 0.0
        self.interval = interval

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                delay = max(self.next_at, self.cooldown_until) - now
                if delay <= 0:
                    self.next_at = now + self.interval
                    return
            time.sleep(min(delay, 1.0))

    def cool(self, seconds=30):
        with self.lock:
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + seconds)


class PageGraph:
    def __init__(self, page_id, token, context, budget=None, version="v25.0", session=None):
        if not str(page_id).isdigit() or not re.fullmatch(r"v[0-9]+\.0", version):
            raise ValueError("invalid_graph_identity")
        self.page_id = str(page_id)
        self._token = str(token)
        self.context = dict(context)
        self.budget = budget or RateBudget()
        self.version = version
        self.session = session or requests.Session()
        self.before_delete = None

    def close(self):
        self.session.close()

    def _request(self, method, node, fields=None):
        if node != "me" and not ID.fullmatch(str(node)):
            raise ValueError("invalid_graph_node")
        self.budget.wait()
        if method == "DELETE" and self.before_delete is not None:
            source_check = self.before_delete()
            if source_check.get("ok") is not True:
                return {"sent": False, "source_check": source_check}
        try:
            response = self.session.request(
                method, "https://graph.facebook.com/" + self.version + "/" + str(node),
                headers={"Authorization": "Bearer " + self._token},
                params={"fields": fields} if fields else None,
                timeout=(5, 20), allow_redirects=False,
            )
        except requests.RequestException as exc:
            return {"transport_error": type(exc).__name__, "uncertain": True}
        try:
            body = response.json()
        except ValueError:
            return {"http_status": response.status_code, "invalid_json": True,
                    "uncertain": True}
        safe = {"http_status": response.status_code}
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            err = body["error"]
            safe["error"] = {k: err.get(k) for k in (
                "code", "error_subcode", "type", "fbtrace_id", "is_transient")}
            # Store useful platform error text without any token or URL query.
            msg = str(err.get("message", "")).replace(self._token, "[REDACTED]")
            safe["error"]["message"] = re.sub(r"access_token=[^&\s]+", "access_token=[REDACTED]", msg)[:1000]
            if err.get("code") in RATE_CODES or response.status_code == 429:
                self.budget.cool(30)
            return safe
        if response.status_code >= 500 or not 200 <= response.status_code < 300:
            safe["uncertain"] = True
            return safe
        if body is True:
            safe["success"] = True
        elif isinstance(body, dict):
            for key in ("id", "post_id", "created_time", "success"):
                if key in body:
                    safe[key] = body[key]
            if isinstance(body.get("from"), dict):
                safe["from"] = {"id": str(body["from"].get("id", ""))}
        else:
            safe["unexpected_body"] = True
        return safe

    def identity(self):
        result = self._request("GET", "me", "id")
        return {"ok": str(result.get("id", "")) == self.page_id,
                "context": self.context, "response": result}

    def inspect(self, post_id):
        if not re.fullmatch(re.escape(self.page_id) + r"_[0-9]+", str(post_id)):
            return {"ok": False, "error": "post_page_mismatch"}
        response = self._request("GET", post_id, "id,from,created_time")
        result = {"ok": False, "response": response}
        if response.get("id"):
            result.update(ok=True, id=str(response["id"]),
                          page_id=str(response.get("from", {}).get("id", "")),
                          created_at=response.get("created_time"))
        elif response.get("error", {}).get("code") == 100 and response.get("error", {}).get("error_subcode") == 33:
            # Absence alone is not proof of deletion. Core requires a prior
            # durable, explicit DELETE success and retains unknown otherwise.
            identity = self.identity()
            result.update(absent=identity["ok"], page_identity=identity)
        else:
            result["error"] = response.get("error", response.get("transport_error", "graph_read_unconfirmed"))
        return result

    def resolve(self, raw_id, allow_post_suffix=True):
        """Read both documented video linkage and a possible stored Post suffix.

        A concatenated candidate becomes a Post identity only after Graph
        confirms its exact id and owning Page. It is never deleted here.
        """
        raw_id = str(raw_id)
        if not raw_id.isdigit():
            return {"ok": False, "error": "invalid_raw_id"}
        video = self._request("GET", raw_id, "id,post_id,created_time")
        linked = str(video.get("post_id", ""))
        if linked and str(video.get("id", "")) != raw_id:
            return {"ok": False, "error": "video_identity_conflict", "video_linkage": video}
        if linked and linked.isdigit():
            linked = self.page_id + "_" + linked
        candidates = [linked] if linked else []
        fallback = self.page_id + "_" + raw_id
        if not linked and allow_post_suffix and fallback not in candidates:
            candidates.append(fallback)
        for candidate in candidates:
            result = self.inspect(candidate)
            if result.get("ok") and result.get("id") == candidate and result.get("page_id") == self.page_id:
                return dict(result, video_linkage=video, resolution="graph_verified")
        return {"ok": False, "error": "raw_id_unresolved", "video_linkage": video}

    def delete(self, post_id):
        if not re.fullmatch(re.escape(self.page_id) + r"_[0-9]+", str(post_id)):
            raise ValueError("post_page_mismatch")
        response = self._request("DELETE", post_id)
        if response.get("sent") is False:
            kind = "failure"
        elif response.get("success") is True and 200 <= response.get("http_status", 0) < 300:
            kind = "success"
        elif response.get("uncertain") or response.get("http_status", 0) >= 500:
            kind = "unknown"
        elif response.get("error"):
            kind = "failure"
        else:
            kind = "unknown"
        return {"kind": kind, "response": response}
