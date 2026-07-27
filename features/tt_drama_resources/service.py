"""Combined disk cache, single-flight, lease and W2A source service."""

import logging
import os
import threading
import time
import uuid

from .client import W2AHTMLClient
from .models import (
    ResourceBusyError,
    ResourceContentMismatchError,
    ResourceNotFoundError,
    ResourceOutcome,
    ResourceSourceError,
    ResourceStorageError,
    compact_text,
    normalize_content_id,
    normalize_landing_id,
    public_content_hash,
    sanitize_cover_url,
    utc_iso_from_epoch,
)


class _Flight:
    def __init__(self):
        self.event = threading.Event()
        self.outcome = None
        self.error = None


class W2AResourceService:
    """Resolve W2A resources with memory and cross-process de-duplication."""

    def __init__(
        self,
        cache,
        client=None,
        landing_id=2049,
        positive_ttl_seconds=86400,
        negative_ttl_seconds=900,
        stale_ttl_seconds=604800,
        lease_seconds=15,
        wait_timeout_seconds=5.0,
        poll_interval_seconds=0.05,
        clock=None,
        sleep=None,
        owner_id=None,
    ):
        if cache is None:
            raise ValueError("resource cache is required")
        self.cache = cache
        self.landing_id = normalize_landing_id(landing_id)
        self.client = client or W2AHTMLClient(landing_id=self.landing_id)
        if normalize_landing_id(self.client.landing_id) != self.landing_id:
            raise ValueError("resource client landing_id does not match")
        self.positive_ttl_seconds = max(1, int(positive_ttl_seconds))
        self.negative_ttl_seconds = max(1, int(negative_ttl_seconds))
        self.stale_ttl_seconds = max(
            self.positive_ttl_seconds,
            int(stale_ttl_seconds),
        )
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.wait_timeout_seconds = max(0.1, float(wait_timeout_seconds))
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.owner_id = str(
            owner_id
            or "%s:%s:%s" % (os.getpid(), threading.get_ident(), uuid.uuid4().hex)
        )
        self._lock = threading.RLock()
        self._flights = {}
        self._closed = False

    def warmup(self):
        if self._closed:
            raise ResourceStorageError("resource service is closed")
        return self.cache.warmup()

    def close(self):
        self._closed = True
        self.cache.close()

    @staticmethod
    def _follower_outcome(outcome):
        if outcome.cache_state == "STALE":
            state = "STALE"
        elif outcome.found:
            state = "DISK_HIT"
        else:
            state = "NEGATIVE_HIT"
        return ResourceOutcome(outcome.found, outcome.item, state)

    def _wait_for_cross_process_result(
        self,
        content_id,
        *,
        allow_stale,
        deadline,
    ):
        while float(self.clock()) < deadline:
            cached = self.cache.peek(
                self.landing_id,
                content_id,
                allow_stale=allow_stale,
            )
            if cached is not None:
                return cached
            remaining = max(0.0, deadline - float(self.clock()))
            if remaining <= 0:
                break
            self.sleep(min(self.poll_interval_seconds, remaining))
        return None

    def _normalize_source_item(self, content_id, item):
        if not isinstance(item, dict):
            raise ResourceSourceError("W2A source returned invalid resource data")
        resolved = str(item.get("resolved_content_id") or "")
        if resolved != content_id:
            raise ResourceContentMismatchError(content_id, resolved)
        if str(item.get("content_id") or "") != content_id:
            raise ResourceSourceError("W2A source returned a mismatched cache key")
        if "description" not in item:
            raise ResourceSourceError("W2A source omitted the description field")
        title = compact_text(item.get("title"), 240)
        description = compact_text(item.get("description"), 2000)
        cover_hosts = getattr(self.client, "allowed_cover_hosts", None)
        cover_url = sanitize_cover_url(
            item.get("cover_url"),
            cover_hosts,
        )
        if not title or not cover_url:
            raise ResourceSourceError(
                "W2A source omitted a required public resource field"
            )
        normalized = {
            "landing_id": self.landing_id,
            "content_id": content_id,
            "resolved_content_id": content_id,
            "title": title,
            "description": description,
            "cover_url": cover_url,
            "country": "",
            "language": "",
            "episode_count": 0,
        }
        normalized["content_hash"] = public_content_hash(normalized)
        return normalized

    def _resolve_as_leader(
        self,
        content_id,
        *,
        force_refresh,
        allow_stale,
        stale,
    ):
        lease_owner = "%s:%s" % (self.owner_id, threading.get_ident())
        if not self.cache.acquire_lease(
            self.landing_id,
            content_id,
            lease_owner,
            lease_seconds=self.lease_seconds,
        ):
            if stale is not None and allow_stale:
                return stale
            waited = self._wait_for_cross_process_result(
                content_id,
                allow_stale=allow_stale,
                deadline=float(self.clock()) + self.wait_timeout_seconds,
            )
            if waited is not None:
                return waited
            raise ResourceBusyError("W2A resource refresh is busy")

        try:
            if not force_refresh:
                refreshed = self.cache.peek(
                    self.landing_id,
                    content_id,
                    allow_stale=False,
                )
                if refreshed is not None:
                    return refreshed
            try:
                item = self._normalize_source_item(
                    content_id,
                    self.client.fetch(content_id),
                )
                fetched_epoch = float(self.clock())
                fetched_at = utc_iso_from_epoch(fetched_epoch)
                item = dict(item)
                item["fetched_at"] = fetched_at
                item["source_updated_at"] = fetched_at
                item.setdefault("country", "")
                item.setdefault("language", "")
                item.setdefault("episode_count", 0)
                self.cache.put_ready(
                    self.landing_id,
                    content_id,
                    item,
                    positive_ttl_seconds=self.positive_ttl_seconds,
                    stale_ttl_seconds=self.stale_ttl_seconds,
                    now=fetched_epoch,
                )
                return ResourceOutcome(True, item, "ORIGIN_FILL")
            except (ResourceContentMismatchError, ResourceNotFoundError) as exc:
                error_code = (
                    "resolved_content_mismatch"
                    if isinstance(exc, ResourceContentMismatchError)
                    else "not_found"
                )
                current = float(self.clock())
                self.cache.put_negative(
                    self.landing_id,
                    content_id,
                    negative_ttl_seconds=self.negative_ttl_seconds,
                    error_code=error_code,
                    now=current,
                    updated_at=utc_iso_from_epoch(current),
                )
                return ResourceOutcome(False, None, "NEGATIVE_FILL")
            except ResourceSourceError as exc:
                current = float(self.clock())
                try:
                    self.cache.mark_error(
                        self.landing_id,
                        content_id,
                        type(exc).__name__,
                        error_at=utc_iso_from_epoch(current),
                    )
                except ResourceStorageError:
                    logging.warning(
                        "W2A resource cache could not record source error for %s",
                        content_id,
                    )
                if stale is not None and allow_stale:
                    return stale
                raise
            except Exception as exc:
                logging.warning(
                    "W2A resource source failed for %s: %s",
                    content_id,
                    type(exc).__name__,
                )
                if stale is not None and allow_stale:
                    return stale
                raise ResourceSourceError(
                    "W2A resource source is unavailable"
                ) from None
        finally:
            try:
                self.cache.release_lease(
                    self.landing_id,
                    content_id,
                    lease_owner,
                )
            except ResourceStorageError:
                logging.warning(
                    "W2A resource lease release failed for %s",
                    content_id,
                )

    def resolve(
        self,
        content_id,
        force_refresh=False,
        allow_stale=True,
    ):
        if self._closed:
            raise ResourceStorageError("resource service is closed")
        normalized = normalize_content_id(content_id)
        cached = self.cache.peek(
            self.landing_id,
            normalized,
            allow_stale=allow_stale,
        )
        if (
            cached is not None
            and not force_refresh
            and cached.cache_state in ("DISK_HIT", "NEGATIVE_HIT")
        ):
            return cached
        stale = (
            cached
            if cached is not None and cached.cache_state == "STALE"
            else None
        )

        with self._lock:
            flight = self._flights.get(normalized)
            if flight is None:
                flight = _Flight()
                self._flights[normalized] = flight
                leader = True
            else:
                leader = False

        if not leader:
            if not flight.event.wait(self.wait_timeout_seconds):
                if stale is not None and allow_stale:
                    return stale
                raise ResourceBusyError("W2A resource refresh timed out")
            if flight.outcome is not None:
                return self._follower_outcome(flight.outcome)
            if stale is not None and allow_stale:
                return stale
            if isinstance(flight.error, ResourceSourceError):
                raise flight.error
            raise ResourceSourceError("W2A resource refresh failed")

        outcome = None
        error = None
        try:
            outcome = self._resolve_as_leader(
                normalized,
                force_refresh=bool(force_refresh),
                allow_stale=bool(allow_stale),
                stale=stale,
            )
            return outcome
        except Exception as exc:
            error = exc
            raise
        finally:
            with self._lock:
                current = self._flights.pop(normalized, None)
                if current is not None:
                    current.outcome = outcome
                    current.error = error
                    current.event.set()

    def fetch_and_cache(self, content_id, force=False):
        return self.resolve(
            content_id,
            force_refresh=bool(force),
            allow_stale=True,
        )
