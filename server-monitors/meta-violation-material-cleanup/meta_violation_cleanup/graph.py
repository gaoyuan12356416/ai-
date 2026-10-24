"""Bounded, credential-safe Meta reads and single-attempt object deletion.

An incomplete inventory is never returned as a successful partial inventory.
Only a read of the exact object/account with status DELETED verifies deletion.
"""
import datetime as dt
import email.utils
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter
from .policy import _feedback_empty


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
_RATE_LIMIT_CODES = (4, 17, 32, 613, 80004)
GRAPH_METRIC_FIELDS = (
    "read_workers", "get_requests", "get_retries", "rate_limit_responses", "rate_limit_retries",
    "usage_app_max_percent", "usage_ad_account_max_percent", "usage_business_max_percent",
    "usage_app_observations", "usage_ad_account_observations", "usage_business_observations",
)
_USAGE_HEADERS = {
    "x-app-usage": "usage_app_max_percent",
    "x-ad-account-usage": "usage_ad_account_max_percent",
    "x-business-use-case-usage": "usage_business_max_percent",
}
_USAGE_PERCENT_KEYS = {"call_count", "total_cputime", "total_time", "acc_id_util_pct"}


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
                 sleeper=None, read_workers=4):
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
        if isinstance(read_workers, bool) or not isinstance(read_workers, int) or not 1 <= read_workers <= 16:
            raise ValueError("Read workers must be an integer between 1 and 16")
        self.tokens = {str(uid): value for uid, value in tokens.items()}
        self.version = version
        self.base = "https://graph.facebook.com/" + version + "/"
        self.timeout = timeout
        self.get_retries = get_retries
        self.max_pages = max_pages
        self.max_objects = max_objects
        self.sleep = sleeper or time.sleep
        self.read_workers = 1 if session is not None else read_workers
        self._metrics = {key: 0 for key in GRAPH_METRIC_FIELDS if key != "read_workers"}
        self._metrics_lock = threading.Lock()
        self.stop = lambda: False
        self.delete_stop = lambda: False
        self._read_local = threading.local()
        self.http = session if session is not None else requests.Session()
        # No transport retries, especially not for DELETE. GET retry policy is
        # implemented explicitly below and never follows a redirect.
        if hasattr(self.http, "mount"):
            self.http.mount("https://", HTTPAdapter(max_retries=0))
            self.http.mount("http://", HTTPAdapter(max_retries=0))

    def close(self):
        if hasattr(self.http, "close"):
            self.http.close()

    @property
    def safe_metrics(self):
        with self._metrics_lock:
            return dict(self._metrics, read_workers=self.read_workers)

    def share_read_metrics(self, parent):
        """Use one numeric counter for inventory and serial execution readers."""
        self._metrics, self._metrics_lock = parent._metrics, parent._metrics_lock

    def _count_read(self, retry=False, rate_limit_retry=False):
        with self._metrics_lock:
            self._metrics['get_requests'] += 1
            self._metrics['get_retries'] += int(retry)
            self._metrics['rate_limit_retries'] += int(rate_limit_retry)

    def _observe_usage(self, response):
        # Keep only finite numeric percentages. Do not retain header payloads,
        # business/account identifiers, URLs, credentials, or arbitrary keys.
        observed = {}
        for name, raw in (getattr(response, 'headers', {}) or {}).items():
            metric = _USAGE_HEADERS.get(str(name).lower())
            if metric is None or not isinstance(raw, str) or len(raw) > 65536:
                continue
            try:
                value = json.loads(raw)
            except (ValueError, TypeError, RecursionError):
                continue
            nodes = [(value, 0)]
            for _ in range(2048):
                if not nodes:
                    break
                node, depth = nodes.pop()
                if isinstance(node, dict):
                    for key, item in node.items():
                        if (key in _USAGE_PERCENT_KEYS and isinstance(item, (int, float))
                                and not isinstance(item, bool) and 0 <= item <= 1000000):
                            observed[metric] = max(observed.get(metric, 0), item)
                        elif isinstance(item, (dict, list)) and depth < 8:
                            nodes.append((item, depth + 1))
                elif isinstance(node, list) and depth < 8:
                    nodes.extend((item, depth + 1) for item in node if isinstance(item, (dict, list)))
        with self._metrics_lock:
            for metric, value in observed.items():
                self._metrics[metric] = max(self._metrics[metric], value)
                self._metrics[metric.replace('_max_percent', '_observations')] += 1

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
        rate_limited = False
        for attempt in range(self.get_retries + 1):
            if self.stop():
                raise InterruptedError('stop_requested')
            transient = False
            delay = float(2 ** attempt)
            self._count_read(retry=attempt > 0, rate_limit_retry=attempt > 0 and rate_limited)
            rate_limited = False
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
                self._observe_usage(response)
                try:
                    data = response.json()
                except (TypeError, ValueError):
                    data = None
                detail = self._detail(data, response)
                rate_limited = response.status_code == 429 or detail.get("code") in _RATE_LIMIT_CODES
                if rate_limited:
                    with self._metrics_lock:
                        self._metrics['rate_limit_responses'] += 1
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
            if self.stop():
                raise InterruptedError('stop_requested')
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
        registry = getattr(self._read_local, 'registry', None)
        reader = getattr(self._read_local, 'reader', None)
        if registry is not None and getattr(self._read_local, 'reader_registry', None) is registry:
            return reader
        reader = GraphClient(self.tokens, self.version, self.timeout,
                           get_retries=self.get_retries, max_pages=self.max_pages,
                           max_objects=self.max_objects)
        reader.share_read_metrics(self)
        reader.stop = getattr(self._read_local, 'stop', self.stop)
        if registry is not None:
            # Reuse this thread's pool for subsequent identities/accounts.
            # The owning executor closes every reader after its workers exit.
            with self._read_local.registry_lock:
                registry.append(reader)
            self._read_local.reader = reader
            self._read_local.reader_registry = registry
        return reader

    def _release_reader(self, reader):
        if reader is not self and reader is not getattr(self._read_local, 'reader', None):
            reader.close()

    def _bounded_reads(self, function, items, stop=lambda: False):
        items = iter(items)
        readers = []
        registry_lock = threading.Lock()
        cancelled = threading.Event()
        should_stop = lambda: cancelled.is_set() or stop() or self.stop()
        def invoke(item):
            if should_stop():
                raise InterruptedError('stop_requested')
            self._read_local.registry = readers
            self._read_local.registry_lock = registry_lock
            self._read_local.stop = should_stop
            return function(item)
        pool = ThreadPoolExecutor(max_workers=self.read_workers)
        pending = set()
        try:
            for _ in range(self.read_workers):
                if should_stop():
                    raise InterruptedError('stop_requested')
                try:
                    pending.add(pool.submit(invoke, next(items)))
                except StopIteration:
                    break
            while pending:
                if should_stop():
                    raise InterruptedError('stop_requested')
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    if should_stop():
                        raise InterruptedError('stop_requested')
                    value = future.result()
                    # Schedule one replacement BEFORE yielding to serial SQL
                    # consumption. Active+ready work stays bounded by workers;
                    # HTTP latency overlaps processing of this account.
                    try:
                        pending.add(pool.submit(invoke, next(items)))
                    except StopIteration:
                        pass
                    yield value
        finally:
            cancelled.set()
            for future in pending:
                future.cancel()
            pool.shutdown(wait=True)
            for reader in readers:
                reader.close()

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
            self._release_reader(reader)

    def list_accounts(self):
        accounts, errors = {}, []
        finished = 0
        for user_id, per_user, error in self._bounded_reads(self._user_accounts, sorted(self.tokens), self.stop):
            finished += 1
            if error:
                errors.append(error)
            else:
                # Publish an identity's accounts only after all of its pages finish.
                for aid in sorted(per_user):
                    accounts.setdefault(aid, {"user_ids": []})["user_ids"].append(user_id)
            progress = getattr(self, 'on_discovery_progress', None)
            if callable(progress):
                progress({'identities_total': len(self.tokens), 'identities_finished': finished,
                          'identities_failed': len(errors), 'accounts_discovered': len(accounts)})
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
            self._release_reader(reader)

    def iter_account_ads(self, accounts, stop=lambda: False):
        """Bounded read-only account scans; never parallelize DELETE or SQL."""
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

    def _target_ads(self, item):
        user, account, identifiers = item
        reader = self._reader()
        try:
            try:
                data = reader._get('', user, {'ids': ','.join(identifiers), 'fields': AD_FIELDS})
            except GraphError as exc:
                return item, {}, {identifier: exc.as_dict() for identifier in identifiers}
            records, errors = {}, {}
            if not isinstance(data, dict):
                return item, {}, {identifier: {'code': 'invalid_batch'} for identifier in identifiers}
            for identifier in identifiers:
                value = data.get(identifier)
                if isinstance(value, dict) and value.get('error'):
                    error = value['error']
                    errors[identifier] = {'code': error.get('code'),
                        'detail': reader._clean({k: error[k] for k in ERROR_FIELDS if k in error})}
                    continue
                try:
                    records[identifier] = reader._record(value, identifier, account)
                except GraphError as exc:
                    errors[identifier] = exc.as_dict()
            return item, records, errors
        finally:
            self._release_reader(reader)

    def iter_target_ads(self, targets, stop=lambda: False):
        """Only exact SQL-selected IDs; batch GETs share one selected identity."""
        groups = {}
        for target in targets:
            user = _object_id(target['user_id'])
            account = _object_id(target['account_id'], account=True)
            groups.setdefault((user, account), set()).add(_object_id(target['ad_id']))
        items = []
        for (user, account), ids in sorted(groups.items()):
            ids = sorted(ids)
            items.extend((user, account, ids[n:n+50]) for n in range(0, len(ids), 50))
        yield from self._bounded_reads(self._target_ads, items, stop)

    def account_diagnostic(self, account_id, user_id):
        account = _object_id(account_id, account=True)
        data = self._get('act_' + account, user_id,
                         {'fields': 'id,account_id,account_status,disable_reason'})
        if not isinstance(data, dict) or _object_id(data.get('account_id') or data.get('id'), account=True) != account:
            raise GraphError('account_mismatch', 'Account diagnostic identity mismatch')
        return self._clean(data)

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
            if kind == 'ad':
                # This is the final identity GET immediately before DELETE.
                # Never ignore a newly observed rejection after runner preflight.
                try:
                    clear = _feedback_empty(record.get('ad_review_feedback'))
                except (TypeError, ValueError):
                    clear = False
                if (not clear or record.get('effective_status') not in
                        ('ACTIVE', 'PAUSED', 'ADSET_PAUSED', 'CAMPAIGN_PAUSED', 'WITH_ISSUES')
                        or record.get('status') not in ('ACTIVE', 'PAUSED')):
                    return {'status': 'failed', 'code': 'current_review_changed', 'request_sent': False}
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
