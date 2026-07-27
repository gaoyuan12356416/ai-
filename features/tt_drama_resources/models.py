"""Shared models and validation for the lightweight W2A resource cache."""

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlencode, urlsplit, urlunsplit


CONTENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,32}$")
LANDING_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,9}$")
DEFAULT_LANDING_ID = 2049
DEFAULT_COVER_HOSTS = frozenset(("cdn.usrgrow.com",))
W2A_ORIGIN = "https://www.dramawavew2a.com"


class ResourceError(RuntimeError):
    """Base class for resource subsystem errors."""


class InvalidContentIdError(ValueError):
    """A content ID is not an exact supported W2A key."""


class InvalidLandingIdError(ValueError):
    """A landing ID is outside the fixed numeric path contract."""


class ResourceSourceError(ResourceError):
    """The fixed W2A source could not safely provide a resource."""


class ResourceParseError(ResourceSourceError):
    """The W2A source HTML did not satisfy the extraction contract."""


class ResourceStorageError(ResourceSourceError):
    """The persistent cache did not satisfy its storage contract."""


class ResourceBusyError(ResourceSourceError):
    """Another worker owns the resource refresh beyond the wait budget."""


class ResourceNotFoundError(ResourceError):
    """The source explicitly did not resolve the requested content ID."""


class ResourceContentMismatchError(ResourceNotFoundError):
    """The rendered deep link resolved to a different content ID."""

    def __init__(self, requested_content_id, resolved_content_id):
        super().__init__("W2A source resolved a different content_id")
        self.requested_content_id = str(requested_content_id or "")
        self.resolved_content_id = str(resolved_content_id or "")


class ResourceOutcome:
    """Resolver-compatible result returned to the public route and workers."""

    def __init__(self, found, item, cache_state):
        self.found = bool(found)
        self.item = dict(item or {}) if found else None
        self.cache_state = str(cache_state or "MISS")


def normalize_content_id(value):
    text = str(value or "")
    if text != text.strip() or not CONTENT_ID_PATTERN.fullmatch(text):
        raise InvalidContentIdError("invalid DramaWave content_id")
    return text


def normalize_landing_id(value):
    text = str(value if value is not None else "")
    if text != text.strip() or not LANDING_ID_PATTERN.fullmatch(text):
        raise InvalidLandingIdError("invalid W2A landing_id")
    return int(text)


def compact_text(value, limit):
    text = " ".join(str(value or "").split())
    return text[: max(0, int(limit))]


def normalize_cover_hosts(value):
    if value is None:
        return DEFAULT_COVER_HOSTS
    values = value.split(",") if isinstance(value, str) else value
    hosts = {
        str(item or "").strip().lower().rstrip(".")
        for item in values
        if str(item or "").strip()
    }
    return frozenset(hosts or DEFAULT_COVER_HOSTS)


def sanitize_cover_url(value, allowed_hosts=None):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or hostname not in normalize_cover_hosts(allowed_hosts)
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        return ""
    return urlunsplit(("https", hostname, parsed.path or "/", parsed.query, ""))


def build_source_url(landing_id, content_id):
    normalized_landing_id = normalize_landing_id(landing_id)
    normalized_content_id = normalize_content_id(content_id)
    query = urlencode({"af_dp": normalized_content_id})
    return "%s/ads/0/%d/view?%s" % (
        W2A_ORIGIN,
        normalized_landing_id,
        query,
    )


def utc_iso_from_epoch(value):
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def public_content_hash(item):
    """Hash only the fields already intended for public display."""
    payload = {
        "content_id": str(item.get("content_id") or ""),
        "title": str(item.get("title") or ""),
        "description": str(item.get("description") or ""),
        "cover_url": str(item.get("cover_url") or ""),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
