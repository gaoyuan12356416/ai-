"""Offline featured-drama cache for the public TikTok bridge."""

from .service import (
    DATA_DISK_UUID,
    DEFAULT_FEATURED_LANGUAGE,
    FeaturedCacheError,
    FeaturedConfig,
    FeaturedDramaRepository,
    FeaturedRefreshError,
    atomic_write_language_snapshot,
    atomic_write_snapshot,
    build_language_snapshot,
    build_snapshot,
    ensure_safe_data_disk_target,
    normalize_featured_language,
    previous_source_date,
    resolve_ranked_resources,
    resolve_ranked_resources_by_language,
    shanghai_now,
)

__all__ = [
    "DATA_DISK_UUID",
    "DEFAULT_FEATURED_LANGUAGE",
    "FeaturedCacheError",
    "FeaturedConfig",
    "FeaturedDramaRepository",
    "FeaturedRefreshError",
    "atomic_write_language_snapshot",
    "atomic_write_snapshot",
    "build_language_snapshot",
    "build_snapshot",
    "ensure_safe_data_disk_target",
    "normalize_featured_language",
    "previous_source_date",
    "resolve_ranked_resources",
    "resolve_ranked_resources_by_language",
    "shanghai_now",
]
