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
        self.verified_times = {}
        self.publication_membership = {}
        self.acknowledged_posts = set()

    def close(self):
        self.session.close()

    def _request(self, method, node, fields=None, query=None):
        if node not in ("me", self.page_id + "/posts") and not ID.fullmatch(str(node)):
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
                params=dict(query or {}, **({"fields": fields} if fields else {})),
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
            if "data" in body:
                if not isinstance(body["data"], list) or any(not isinstance(row, dict) for row in body["data"]):
                    safe["invalid_data"] = True
                else:
                    safe["data"] = [{"id": str(row.get("id", "")), "created_time": row.get("created_time")}
                                    for row in body["data"]]
                    paging = body.get("paging", {})
                    safe["has_next"] = bool(paging.get("next"))
                    safe["after"] = paging.get("cursors", {}).get("after")
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
            if result["id"] == post_id and result["page_id"] == self.page_id and result["created_at"]:
                self.verified_times[post_id] = result["created_at"]
        elif response.get("error", {}).get("code") in (10, 100) and post_id in self.verified_times:
            identity = self.identity()
            baseline = self.publication_membership.get(post_id, {})
            covered_before = baseline.get("complete") and baseline.get("present")
            listing = (self.window_presence(post_id, self.verified_times[post_id])
                       if identity["ok"] and covered_before else {"complete": False, "reason": "no_predelete_list_membership"})
            result.update(absent=bool(identity["ok"] and covered_before and listing.get("complete") and not listing.get("present")),
                          page_identity=identity, published_window=listing,
                          receipt_validated=bool(post_id in self.acknowledged_posts and identity["ok"]
                                                 and not listing.get("present")))
        else:
            result["error"] = response.get("error", response.get("transport_error", "graph_read_unconfirmed"))
        return result

    def window_presence(self, post_id, created_at):
        """Complete bounded Page publication listing, not an error-as-absence test."""
        try:
            text = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", str(created_at).replace("Z", "+00:00"))
            created = dt.datetime.fromisoformat(text)
            if created.tzinfo is None:
                raise ValueError()
            center = int(created.timestamp())
        except (ValueError, TypeError):
            return {"complete": False, "reason": "verified_post_time_invalid"}
        query = {"since": center - 86400, "until": center + 86400, "limit": 100}
        observed, cursors = set(), set()
        def incomplete(reason, **details):
            return dict(complete=False, present=post_id in observed, reason=reason, **details)
        for page in range(10):
            response = self._request("GET", self.page_id + "/posts", "id,created_time", query)
            if response.get("http_status") != 200 or not isinstance(response.get("data"), list):
                return incomplete("publication_list_unreadable", response=response)
            for row in response["data"]:
                if not re.fullmatch(re.escape(self.page_id) + r"_[0-9]+", row["id"]):
                    return incomplete("publication_list_identity_invalid")
                observed.add(row["id"])
                try:
                    parsed = dt.datetime.fromisoformat(re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", str(row["created_time"]).replace("Z", "+00:00")))
                    if parsed.tzinfo is None:
                        raise ValueError()
                    timestamp = parsed.timestamp()
                except (ValueError, TypeError):
                    return incomplete("publication_list_time_invalid")
                if timestamp < query["since"] - 1 or timestamp > query["until"] + 1:
                    return incomplete("publication_list_time_filter_unconfirmed")
            if not response.get("has_next"):
                return {"complete": True, "present": post_id in observed, "pages": page + 1,
                        "since": query["since"], "until": query["until"], "post_ids": sorted(observed)}
            cursor = response.get("after")
            if not cursor or cursor in cursors:
                return incomplete("publication_pagination_unconfirmed")
            cursors.add(cursor)
            query["after"] = cursor
        return incomplete("publication_page_limit_reached")

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
            self.acknowledged_posts.add(post_id)
        elif response.get("uncertain") or response.get("http_status", 0) >= 500:
            kind = "unknown"
        elif response.get("error"):
            kind = "failure"
        else:
            kind = "unknown"
        return {"kind": kind, "response": response}
