"""Offline featured-drama cache for the public TikTok bridge."""

from .service import (
    DATA_DISK_UUID,
    FeaturedCacheError,
    FeaturedConfig,
    FeaturedDramaRepository,
    FeaturedRefreshError,
    atomic_write_snapshot,
    build_snapshot,
    ensure_safe_data_disk_target,
    previous_source_date,
    shanghai_now,
)

__all__ = [
    "DATA_DISK_UUID",
    "FeaturedCacheError",
    "FeaturedConfig",
    "FeaturedDramaRepository",
    "FeaturedRefreshError",
    "atomic_write_snapshot",
    "build_snapshot",
    "ensure_safe_data_disk_target",
    "previous_source_date",
    "shanghai_now",
]
