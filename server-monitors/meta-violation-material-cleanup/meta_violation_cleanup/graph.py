"""Bounded, credential-safe Meta reads and single-attempt object deletion.

An incomplete inventory is never returned as a successful partial inventory.
Only a read of the exact object/account with status DELETED verifies deletion.
"""
import datetime as dt
import email.utils
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter


AD_FIELDS = (
    "id,account_id,status,effective_status,issues_info,ad_review_feedback,"
    "creative{id},adset{id,status},campaign{id,status}"
)
CREATIVE_FIELDS = "id,account_id,status"
# Meta's Ad.EffectiveStatus enum, excluding DELETED and ARCHIVED. The official
# AdAccount.get_ads interface accepts this as effective_status: list<string>.
CURRENT_STATUSES = (
    "ACTIVE", "PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED", "DISAPPROVED",
    "IN_PROCESS", "PENDING_REVIEW", "PREAPPROVED", "PENDING_BILLING_INFO", "WITH_ISSUES",
)
ERROR_FIELDS = (
    "code", "error_subcode", "fbtrace_id", "message",
    "error_user_title", "error_user_msg",
)
_AD_KEYS = (
    "id", "account_id", "status", "effective_status", "issues_info",
    "ad_review_feedback", "creative", "adset", "campaign",
)
_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_SECRET_KEYS = {"access_token", "authorization", "appsecret_proof", "client_secret"}


def _object_id(value, account=False):
    if isinstance(value, bool):
        raise GraphError("invalid_id", "Meta object identifier is invalid")
    value = str(value or "")
    if account and value.startswith("act_"):
        value = value[4:]
    if not _ID.fullmatch(value):
        raise GraphError("invalid_id", "Meta object identifier is invalid")
    return value


class GraphError(Exception):
    """Only safe, bounded diagnostics are exposed to the caller or logs."""

    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


class GraphClient:
    def __init__(self, tokens, version="v25.0", timeout=20, *, session=None,
                 get_retries=2, max_pages=10000, max_objects=1000000,
                 sleeper=None):
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
            raise ValueError("Invalid Graph version")
        if not isinstance(tokens, dict):
            raise ValueError("Tokens must be a user-id to token mapping")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise ValueError("Timeout must be between 0 and 120 seconds")
        if not isinstance(get_retries, int) or not 0 <= get_retries <= 5:
            raise ValueError("GET retries must be between 0 and 5")
        if not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages must be positive")
        if not isinstance(max_objects, int) or max_objects < 1:
            raise ValueError("max_objects must be positive")
        self.tokens = {str(uid): value for uid, value in tokens.items()}
        self.version = version
        self.base = "https://graph.facebook.com/" + version + "/"
        self.timeout = timeout
        self.get_retries = get_retries
        self.max_pages = max_pages
        self.max_objects = max_objects
        self.sleep = sleeper or time.sleep
        self.read_workers = 1 if session is not None else 4
        self.stop = lambda: False
        self.delete_stop = lambda: False
        self.http = session if session is not None else requests.Session()
        # No transport retries, especially not for DELETE. GET retry policy is
        # implemented explicitly below and never follows a redirect.
        if hasattr(self.http, "mount"):
            self.http.mount("https://", HTTPAdapter(max_retries=0))
            self.http.mount("http://", HTTPAdapter(max_retries=0))

    def close(self):
        if hasattr(self.http, "close"):
            self.http.close()

    def _redact(self, value, limit=2048):
        text = str(value)
        for token in self.tokens.values():
            if isinstance(token, str) and token:
                text = text.replace(token, "[redacted]")
        text = re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [redacted]", text)
        text = re.sub(
            r"(?i)((?:access_token|appsecret_proof|client_secret|authorization)"
            r"\s*[=:]\s*)[^&\s\"'<>]+", r"\1[redacted]", text,
        )
        text = re.sub(r"\bEA[A-Za-z0-9]{20,}\b", "[redacted]", text)
        return text[:limit] if limit is not None else text

    def _clean(self, value):
        if isinstance(value, dict):
            return {str(key): self._clean(item) for key, item in value.items()
                    if str(key).lower() not in _SECRET_KEYS}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if isinstance(value, str):
            return self._redact(value, limit=None)
        return value

    def _token(self, user_id):
        token = self.tokens.get(str(user_id))
        if not isinstance(token, str) or not token.strip():
            raise GraphError("missing_token", "No configured Meta token for this identity")
        if "\r" in token or "\n" in token:
            raise GraphError("invalid_token", "Configured Meta token is invalid")
        return token

    def _detail(self, data, response):
        detail = {"http_status": response.status_code}
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            for key in ERROR_FIELDS:
                value = error.get(key)
                if isinstance(value, str):
                    detail[key] = self._redact(value)
                elif value is not None and isinstance(value, (int, float, bool)):
                    detail[key] = value
        retry = self._retry_after(response)
        if retry is not None:
            detail["retry_after_seconds"] = retry
        return detail

    @staticmethod
    def _retry_after(response):
        headers = getattr(response, "headers", {}) or {}
        raw = next((value for key, value in headers.items()
                    if str(key).lower() == "retry-after"), None)
        if raw is None:
            return None
        try:
            seconds = float(raw)
            if not math.isfinite(seconds):
                return None
            return max(0.0, seconds)
        except (TypeError, ValueError):
            try:
                until = email.utils.parsedate_to_datetime(str(raw))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=dt.timezone.utc)
                return max(0.0, (until - dt.datetime.now(dt.timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _get(self, path, user_id, params=None):
        token = self._token(user_id)
        for attempt in range(self.get_retries + 1):
            if self.stop():
                raise InterruptedError('stop_requested')
            transient = False
            delay = float(2 ** attempt)
            try:
                response = self.http.request(
                    "GET", self.base + path, params=params or {},
                    headers={"Authorization": "Bearer " + token},
                    timeout=self.timeout, allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError):
                error = GraphError("read_unavailable", "Meta read timed out or disconnected")
                transient = True
            except requests.RequestException:
                raise GraphError("read_unavailable", "Meta read failed") from None
            else:
                try:
                    data = response.json()
                except (TypeError, ValueError):
                    data = None
                detail = self._detail(data, response)
                has_error = isinstance(data, dict) and data.get("error") is not None
                if 200 <= response.status_code < 300 and not has_error and data is not None:
                    return data
                error = GraphError(
                    detail.get("code", "graph_read_failed"),
                    detail.get("message", "Meta read returned an invalid or failed response"),
                    detail,
                )
                transient = (response.status_code == 429 or response.status_code >= 500
                             or detail.get("code") in (1, 2, 4, 17, 32, 613, 80004))
                delay = max(delay, detail.get("retry_after_seconds", 0.0))
            # A long platform cooldown is propagated instead of retrying early
            # or blocking the worker for an unbounded time.
            if not transient or attempt >= self.get_retries or delay > 60:
                raise error
            self.sleep(delay)
        raise GraphError("read_unavailable", "Meta read did not finish")

    def _pages(self, path, user_id, params):
        params = dict(params)
        seen_cursors = set()
        total = 0
        for page_number in range(self.max_pages):
            data = self._get(path, user_id, params)
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise GraphError("invalid_page", "Meta inventory page has no valid data list")
            rows = data["data"]
            if any(not isinstance(row, dict) for row in rows):
                raise GraphError("invalid_page", "Meta inventory contains an invalid object")
            total += len(rows)
            if total > self.max_objects:
                raise GraphError("inventory_limit", "Meta inventory exceeded the configured object limit")
            yield rows
            paging = data.get("paging")
            if paging is None:
                return
            if not isinstance(paging, dict):
                raise GraphError("invalid_page", "Meta inventory pagination is invalid")
            next_url = paging.get("next")
            if next_url is None:
                return
            if not isinstance(next_url, str) or not rows:
                raise GraphError("invalid_page", "Meta inventory has an invalid continuation")
            # Never follow a returned URL: it can carry an access_token or an
            # unintended host/path. Only its validated after cursor is reused.
            try:
                parsed = urlsplit(next_url)
                expected_path = "/" + self.version + "/" + path
                valid_path = parsed.path.rstrip("/") == expected_path.rstrip("/")
                if path == "me/adaccounts":
                    # Graph can canonicalize /me to the token's Facebook user
                    # id. Requests still use our original /me/adaccounts path.
                    valid_path = valid_path or bool(re.fullmatch(
                        "/" + re.escape(self.version) + r"/[1-9][0-9]{0,31}/adaccounts/?",
                        parsed.path,
                    ))
                if (parsed.scheme != "https" or parsed.hostname != "graph.facebook.com"
                        or parsed.port not in (None, 443) or parsed.username or parsed.password
                        or not valid_path or parsed.fragment):
                    raise ValueError("invalid continuation")
                values = parse_qs(parsed.query, keep_blank_values=True)
                after = values.get("after", [])
                if len(after) != 1 or not after[0] or len(after[0]) > 8192:
                    raise ValueError("invalid cursor")
                cursor = after[0]
                cursors = paging.get("cursors")
                if isinstance(cursors, dict) and cursors.get("after") not in (None, cursor):
                    raise ValueError("cursor mismatch")
            except (TypeError, ValueError):
                raise GraphError("invalid_page", "Meta inventory continuation is invalid") from None
            if cursor in seen_cursors:
                raise GraphError("pagination_loop", "Meta inventory pagination repeated a cursor")
            seen_cursors.add(cursor)
            params["after"] = cursor
        raise GraphError("inventory_limit", "Meta inventory exceeded the configured page limit")

    def _reader(self):
        """Each production read worker owns its HTTP session and connection pool."""
        if self.read_workers == 1:
            return self
        reader = GraphClient(self.tokens, self.version, self.timeout,
                           get_retries=self.get_retries, max_pages=self.max_pages,
                           max_objects=self.max_objects)
        reader.stop = self.stop
        return reader

    def _bounded_reads(self, function, items, stop=lambda: False):
        items = iter(items)
        with ThreadPoolExecutor(max_workers=self.read_workers) as pool:
            pending = set()
            exhausted = False
            while pending or not exhausted:
                while not exhausted and len(pending) < self.read_workers:
                    if stop():
                        raise InterruptedError('stop_requested')
                    try:
                        item = next(items)
                    except StopIteration:
                        exhausted = True
                        break
                    pending.add(pool.submit(function, item))
                if pending:
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in finished:
                        yield future.result()

    def _user_accounts(self, user_id):
        reader = self._reader()
        try:
            per_user = set()
            try:
                for rows in reader._pages("me/adaccounts", user_id,
                                        {"fields": "id,account_id", "limit": 100}):
                    for row in rows:
                        aid = _object_id(row.get("account_id") or row.get("id"), account=True)
                        if row.get("id") and _object_id(row["id"], account=True) != aid:
                            raise GraphError("account_mismatch", "Meta account identifiers disagree")
                        per_user.add(aid)
            except GraphError as error:
                return user_id, set(), dict(user_id=user_id, **error.as_dict())
            return user_id, per_user, None
        finally:
            if reader is not self:
                reader.close()

    def list_accounts(self):
        accounts, errors = {}, []
        finished = 0
        for user_id, per_user, error in self._bounded_reads(self._user_accounts, sorted(self.tokens), self.stop):
            finished += 1
            progress = getattr(self, 'on_discovery_progress', None)
            if callable(progress):
                progress({'identities_total': len(self.tokens), 'identities_finished': finished,
                          'identities_failed': len(errors) + bool(error), 'accounts_discovered': len(accounts)})
            if error:
                errors.append(error)
                continue
            # Publish an identity's accounts only after all of its pages finish.
            for aid in sorted(per_user):
                accounts.setdefault(aid, {"user_ids": []})["user_ids"].append(user_id)
        for info in accounts.values():
            info['user_ids'].sort()
        errors.sort(key=lambda item: item['user_id'])
        return {"accounts": accounts, "errors": errors}

    def _account_ads(self, item):
        account, info = item
        reader = self._reader()
        errors = []
        try:
            for uid in info['user_ids']:
                try:
                    return account, reader.list_ads(account, uid), uid, errors
                except GraphError as exc:
                    errors.append({'user_id': str(uid), 'error': exc.as_dict()})
            return account, None, None, errors
        finally:
            if reader is not self:
                reader.close()

    def iter_account_ads(self, accounts, stop=lambda: False):
        """At most four read-only account scans; never parallelize DELETE or SQL."""
        yield from self._bounded_reads(self._account_ads, sorted(accounts.items()), stop)

    def _record(self, data, object_id=None, account_id=None, kind="ad"):
        if not isinstance(data, dict):
            raise GraphError("invalid_object", "Meta did not return a valid object")
        actual_id = _object_id(data.get("id"))
        actual_account = _object_id(data.get("account_id"), account=True)
        if object_id is not None and actual_id != object_id:
            raise GraphError("object_mismatch", "Meta returned a different object")
        if account_id is not None and actual_account != account_id:
            raise GraphError("account_mismatch", "Meta returned a different account")
        keys = _AD_KEYS if kind == "ad" else ("id", "account_id", "status")
        result = self._clean({key: data[key] for key in keys if key in data})
        result["id"], result["account_id"] = actual_id, actual_account
        return result

    def list_ads(self, account_id, user_id):
        aid = _object_id(account_id, account=True)
        ads = {}
        # Fetch every current state, including review and issues, without
        # scanning deleted historical ads. Do not fall back to an unfiltered
        # history scan if the platform rejects this supported parameter.
        for rows in self._pages("act_" + aid + "/ads", user_id,
                                {"fields": AD_FIELDS, "limit": 100,
                                 "effective_status": json.dumps(CURRENT_STATUSES)}):
            for row in rows:
                record = self._record(row, account_id=aid)
                identifier = record["id"]
                if identifier in ads and ads[identifier] != record:
                    raise GraphError("inventory_changed", "Meta inventory changed during pagination")
                ads[identifier] = record
        return [record for record in ads.values()
                if record.get("status") not in ("DELETED", "ARCHIVED")
                and record.get("effective_status") not in ("DELETED", "ARCHIVED")]

    def get_ad(self, ad_id, user_id):
        identifier = _object_id(ad_id)
        return self._record(self._get(identifier, user_id, {"fields": AD_FIELDS}), identifier)

    def get_creative(self, creative_id, user_id):
        identifier = _object_id(creative_id)
        return self._record(self._get(identifier, user_id, {"fields": CREATIVE_FIELDS}),
                            identifier, kind="creative")

    def _object(self, kind, identifier, user_id, account_id):
        if kind not in ("ad", "creative"):
            raise GraphError("invalid_kind", "Only Ad and Creative objects are supported")
        record = (self.get_ad(identifier, user_id) if kind == "ad"
                  else self.get_creative(identifier, user_id))
        if record["account_id"] != account_id:
            raise GraphError("account_mismatch", "Meta returned a different account")
        return record

    def delete(self, kind, object_id, user_id, account_id):
        """Verify identity, then send at most one DELETE; never rotate tokens."""
        if self.delete_stop():
            return {'status': 'failed', 'code': 'cancelled_before_delete', 'request_sent': False}
        try:
            identifier = _object_id(object_id)
            aid = _object_id(account_id, account=True)
            token = self._token(user_id)
            record = self._object(kind, identifier, user_id, aid)
            if record.get("status") == "DELETED":
                return {"status": "deleted_verified", "code": "already_deleted",
                        "confirmed_deleted": True, "request_sent": False, "object": record}
        except GraphError as error:
            return {"status": "failed", "request_sent": False, "error": error.as_dict()}
        if self.delete_stop():
            return {'status': 'failed', 'code': 'cancelled_before_delete', 'request_sent': False}
        try:
            response = self.http.request(
                "DELETE", self.base + identifier, params={},
                headers={"Authorization": "Bearer " + token},
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException:
            return {"status": "unknown", "request_sent": True, "code": "transport_uncertain"}
        try:
            data = response.json()
        except (TypeError, ValueError):
            data = None
        detail = self._detail(data, response)
        result = {"status": "unknown", "request_sent": True, "response": detail}
        if response.status_code >= 500 or response.status_code == 408 or 300 <= response.status_code < 400:
            return result
        if response.status_code == 429:
            result.update(status="failed", code="rate_limited")
            return result
        has_error = isinstance(data, dict) and data.get("error") is not None
        if has_error:
            result["status"] = "failed"
            return result
        if 200 <= response.status_code < 300:
            if data is True or (isinstance(data, dict) and data.get("success") is True):
                result.update(status="api_accepted", response={"http_status": response.status_code, "success": True})
            elif data is False or (isinstance(data, dict) and data.get("success") is False):
                result.update(status="failed", response={"http_status": response.status_code, "success": False})
        return result

    def reconcile(self, kind, object_id, user_id, account_id):
        try:
            identifier = _object_id(object_id)
            aid = _object_id(account_id, account=True)
            record = self._object(kind, identifier, user_id, aid)
        except GraphError as error:
            return {"status": "unknown", "error": error.as_dict()}
        if record.get("status") == "DELETED":
            return {"status": "deleted_verified", "object": record}
        if isinstance(record.get("status"), str) and record["status"]:
            return {"status": "present", "object": record}
        return {"status": "unknown", "code": "missing_status", "object": record}
