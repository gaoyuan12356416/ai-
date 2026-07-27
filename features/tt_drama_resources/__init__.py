"""Lightweight W2A source resource cache for the TikTok drama bridge."""

from .cache import (
    DATA_DISK_ROOT,
    DATA_DISK_UUID,
    MIN_DATA_DISK_FREE_BYTES,
    SQLiteResourceCache,
    validate_resource_cache_path,
)
from .client import W2AHTMLClient
from .models import (
    DEFAULT_COVER_HOSTS,
    DEFAULT_LANDING_ID,
    InvalidContentIdError,
    InvalidLandingIdError,
    ResourceBusyError,
    ResourceContentMismatchError,
    ResourceError,
    ResourceNotFoundError,
    ResourceOutcome,
    ResourceParseError,
    ResourceSourceError,
    ResourceStorageError,
    build_source_url,
    normalize_content_id,
    normalize_landing_id,
    sanitize_cover_url,
)
from .parser import extract_resolved_content_id, parse_w2a_resource_html
from .service import W2AResourceService


__all__ = [
    "DATA_DISK_ROOT",
    "DATA_DISK_UUID",
    "DEFAULT_COVER_HOSTS",
    "DEFAULT_LANDING_ID",
    "MIN_DATA_DISK_FREE_BYTES",
    "InvalidContentIdError",
    "InvalidLandingIdError",
    "ResourceBusyError",
    "ResourceContentMismatchError",
    "ResourceError",
    "ResourceNotFoundError",
    "ResourceOutcome",
    "ResourceParseError",
    "ResourceSourceError",
    "ResourceStorageError",
    "SQLiteResourceCache",
    "W2AHTMLClient",
    "W2AResourceService",
    "build_source_url",
    "extract_resolved_content_id",
    "normalize_content_id",
    "normalize_landing_id",
    "parse_w2a_resource_html",
    "sanitize_cover_url",
    "validate_resource_cache_path",
]
