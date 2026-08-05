#!/usr/bin/env python3
"""Refresh the local public cache of yesterday's top DramaWave W2A dramas."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_drama_featured import (  # noqa: E402
    DATA_DISK_UUID,
    FeaturedCacheError,
    FeaturedConfig,
    FeaturedDramaRepository,
    FeaturedRefreshError,
    atomic_write_language_snapshot,
    atomic_write_snapshot,
    build_language_snapshot,
    build_snapshot,
    ensure_safe_data_disk_target,
    previous_source_date,
    resolve_ranked_resources,
    resolve_ranked_resources_by_language,
    shanghai_now,
)
from features.tt_drama_resources import (  # noqa: E402
    DEFAULT_LANDING_ID,
    ResourceError,
    SQLiteResourceCache,
    W2AHTMLClient,
    W2AResourceService,
)


DEFAULT_CACHE_PATH = (
    "/mnt/data-disk/tt-drama-featured/public/current.json"
)
DEFAULT_LANGUAGE_CACHE_PATH = (
    "/mnt/data-disk/tt-drama-featured/public/current-by-language.json"
)
DEFAULT_RESOURCE_DB_PATH = (
    "/mnt/data-disk/tt-drama-resource-cache/state/resources.sqlite3"
)
DEFAULT_LOCK_PATH = "/run/tt-drama-featured/refresh.lock"


def _env(name, default=""):
    return str(os.environ.get(name, default) or default).strip()


def _env_int(name, default, minimum, maximum):
    try:
        value = int(_env(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(value, int(maximum)))


def _mysql_settings():
    return {
        "host": _env("DRAMA_DB_HOST") or _env("ADMIN_MAPPING_MYSQL_HOST"),
        "port": (
            _env("DRAMA_DB_PORT")
            or _env("ADMIN_MAPPING_MYSQL_PORT")
            or "0"
        ),
        "user": _env("DRAMA_DB_USER") or _env("ADMIN_MAPPING_MYSQL_USER"),
        "password": (
            os.environ.get("DRAMA_DB_PASSWORD")
            or os.environ.get("ADMIN_MAPPING_MYSQL_PASSWORD")
            or ""
        ),
        "database": (
            _env("DRAMA_DB_NAME")
            or _env("ADMIN_MAPPING_MYSQL_DATABASE")
            or "kunlunads_dev"
        ),
    }


def _mount_info(path):
    command = [
        "/usr/bin/findmnt",
        "-n",
        "-o",
        "UUID",
        "--target",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            universal_newlines=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FeaturedCacheError(
            "data disk UUID check failed: %s" % type(exc).__name__
        ) from None
    return {"uuid": str(completed.stdout or "").strip()}


def _make_public_parent(target, mount_path="/mnt/data-disk"):
    parent = Path(target).parent
    if not parent.is_dir():
        raise FeaturedCacheError(
            "featured public directory must be provisioned before refresh"
        )
    mount_real = Path(os.path.realpath(mount_path))
    current = parent
    while current != mount_real:
        if current.is_symlink():
            raise FeaturedCacheError(
                "featured cache directory must not be a symlink"
            )
        if not os.access(str(current), os.R_OK | os.X_OK):
            raise FeaturedCacheError(
                "featured cache directory is not traversable"
            )
        if current.parent == current:
            raise FeaturedCacheError(
                "featured cache directory escaped the data disk"
            )
        current = current.parent
    if not os.access(str(parent), os.W_OK):
        raise FeaturedCacheError("featured public directory is not writable")


@contextmanager
def _exclusive_lock(path):
    try:
        import fcntl
    except ImportError:
        raise FeaturedCacheError("fcntl is required for the refresh lock") from None
    lock_path = Path(path)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(str(lock_path.parent), 0o700)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise FeaturedCacheError("another featured refresh is already running") from None
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _build_config(settings):
    cover_hosts = _env(
        "TT_DRAMA_FEATURED_COVER_HOSTS",
        _env("TT_DRAMA_RESOLVER_COVER_HOSTS"),
    )
    return FeaturedConfig(
        database=settings["database"],
        insight_table=_env(
            "TT_DRAMA_FEATURED_INSIGHT_TABLE",
            "ads_custom_source_insight",
        ),
        insight_index=_env("TT_DRAMA_FEATURED_INSIGHT_INDEX", "as"),
        product=_env("TT_DRAMA_FEATURED_PRODUCT", "Dramawave"),
        source_app_id=_env(
            "TT_DRAMA_FEATURED_SOURCE_APP_ID",
            "[w2a]drama-double",
        ),
        data_source=_env_int("TT_DRAMA_FEATURED_DATA_SOURCE", 6, 0, 100),
        candidate_limit=_env_int(
            "TT_DRAMA_FEATURED_CANDIDATE_LIMIT",
            20,
            5,
            20,
        ),
        item_limit=_env_int("TT_DRAMA_FEATURED_ITEM_LIMIT", 5, 5, 5),
        allowed_cover_hosts=cover_hosts,
    )


def _build_resource_service():
    landing_id = _env_int(
        "TT_DRAMA_RESOURCE_LANDING_ID",
        DEFAULT_LANDING_ID,
        1,
        9999999999,
    )
    if landing_id != DEFAULT_LANDING_ID:
        raise FeaturedRefreshError(
            "featured resources must use W2A landing_id 2049"
        )
    cache = SQLiteResourceCache(
        _env("TT_DRAMA_RESOURCE_DB_PATH", DEFAULT_RESOURCE_DB_PATH),
        busy_timeout_seconds=_env_int(
            "TT_DRAMA_RESOURCE_SQLITE_BUSY_TIMEOUT_SECONDS",
            5,
            1,
            30,
        ),
    )
    client = W2AHTMLClient(
        landing_id=landing_id,
        timeout_seconds=_env_int(
            "TT_DRAMA_RESOURCE_HTTP_TIMEOUT_SECONDS",
            5,
            1,
            10,
        ),
        max_html_bytes=_env_int(
            "TT_DRAMA_RESOURCE_HTTP_MAX_BYTES",
            512 * 1024,
            16 * 1024,
            2 * 1024 * 1024,
        ),
        allowed_cover_hosts=_env(
            "TT_DRAMA_RESOURCE_COVER_HOSTS",
            "cdn.usrgrow.com",
        ),
    )
    service = W2AResourceService(
        cache=cache,
        client=client,
        landing_id=landing_id,
        positive_ttl_seconds=_env_int(
            "TT_DRAMA_RESOURCE_POSITIVE_TTL_SECONDS",
            86400,
            300,
            604800,
        ),
        negative_ttl_seconds=_env_int(
            "TT_DRAMA_RESOURCE_NEGATIVE_TTL_SECONDS",
            900,
            30,
            3600,
        ),
        stale_ttl_seconds=_env_int(
            "TT_DRAMA_RESOURCE_STALE_TTL_SECONDS",
            604800,
            3600,
            2592000,
        ),
        lease_seconds=_env_int(
            "TT_DRAMA_RESOURCE_LEASE_SECONDS",
            15,
            5,
            60,
        ),
        wait_timeout_seconds=_env_int(
            "TT_DRAMA_RESOURCE_WAIT_TIMEOUT_SECONDS",
            5,
            1,
            10,
        ),
    )
    try:
        service.warmup()
    except Exception:
        service.close()
        raise
    return service


def refresh(
    source_date,
    cache_path,
    dry_run=False,
    *,
    language_cache_path=DEFAULT_LANGUAGE_CACHE_PATH,
    repository=None,
    resource_service=None,
    generated_at=None,
):
    if os.path.realpath(str(cache_path)) == os.path.realpath(
        str(language_cache_path)
    ):
        raise FeaturedCacheError(
            "v1 and language featured cache paths must be different"
        )
    settings = _mysql_settings()
    config = _build_config(settings)
    ranking_repository = repository or FeaturedDramaRepository(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        config=config,
        connect_timeout=_env_int(
            "TT_DRAMA_FEATURED_DB_CONNECT_TIMEOUT_SECONDS",
            5,
            1,
            10,
        ),
        read_timeout=_env_int(
            "TT_DRAMA_FEATURED_DB_READ_TIMEOUT_SECONDS",
            30,
            5,
            60,
        ),
    )
    owns_resource_service = resource_service is None
    resources = resource_service or _build_resource_service()
    try:
        spend_rows = ranking_repository.fetch_ranked(source_date)
        cover_hosts = (
            getattr(getattr(resources, "client", None), "allowed_cover_hosts", None)
            or config.allowed_cover_hosts
        )
        resource_items = resolve_ranked_resources(
            spend_rows,
            resources,
            item_limit=config.item_limit,
            allowed_cover_hosts=cover_hosts,
        )
        generated = generated_at or shanghai_now()
        snapshot = build_snapshot(
            source_date=source_date,
            generated_at=generated,
            spend_rows=spend_rows,
            resource_items=resource_items,
            item_limit=config.item_limit,
            allowed_cover_hosts=cover_hosts,
        )
        changed = False
        if not dry_run:
            target = ensure_safe_data_disk_target(
                cache_path,
                mount_path="/mnt/data-disk",
                expected_uuid=DATA_DISK_UUID,
                mount_info=_mount_info,
            )
            _make_public_parent(target)
            ensure_safe_data_disk_target(
                target,
                mount_path="/mnt/data-disk",
                expected_uuid=DATA_DISK_UUID,
                mount_info=_mount_info,
            )
            changed = atomic_write_snapshot(target, snapshot)

        # Keep the legacy v1 refresh independent: the original file is fully
        # generated (and, outside dry-run, published) before the new language
        # ranking performs any extra query or resource validation.
        ranked_by_language = ranking_repository.fetch_ranked_by_language(
            source_date
        )
        language_resource_items = resolve_ranked_resources_by_language(
            ranked_by_language,
            resources,
            item_limit=config.item_limit,
            allowed_cover_hosts=cover_hosts,
        )
        language_snapshot = build_language_snapshot(
            source_date=source_date,
            generated_at=generated,
            rankings=language_resource_items,
            item_limit=config.item_limit,
            allowed_cover_hosts=cover_hosts,
        )
        language_changed = False
        if not dry_run:
            language_target = ensure_safe_data_disk_target(
                language_cache_path,
                mount_path="/mnt/data-disk",
                expected_uuid=DATA_DISK_UUID,
                mount_info=_mount_info,
            )
            _make_public_parent(language_target)
            ensure_safe_data_disk_target(
                language_target,
                mount_path="/mnt/data-disk",
                expected_uuid=DATA_DISK_UUID,
                mount_info=_mount_info,
            )
            language_changed = atomic_write_language_snapshot(
                language_target,
                language_snapshot,
            )
    finally:
        if owns_resource_service:
            resources.close()
    return {
        "status": "ok",
        "source_date": snapshot["source_date"],
        "generated_at": snapshot["generated_at"],
        "item_count": len(snapshot["items"]),
        "changed": bool(changed),
        "language_count": len(language_snapshot["rankings"]),
        "language_changed": bool(language_changed),
        "dry_run": bool(dry_run),
    }


def _parser():
    parser = argparse.ArgumentParser(
        description="Refresh the local TT featured-drama cache."
    )
    parser.add_argument(
        "--source-date",
        default="",
        help="Shanghai business date in YYYY-MM-DD; defaults to yesterday.",
    )
    parser.add_argument(
        "--cache-path",
        default=_env("TT_DRAMA_FEATURED_CACHE_PATH", DEFAULT_CACHE_PATH),
    )
    parser.add_argument(
        "--language-cache-path",
        default=_env(
            "TT_DRAMA_FEATURED_LANGUAGE_CACHE_PATH",
            DEFAULT_LANGUAGE_CACHE_PATH,
        ),
    )
    parser.add_argument(
        "--lock-path",
        default=_env("TT_DRAMA_FEATURED_LOCK_PATH", DEFAULT_LOCK_PATH),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Query and validate without replacing the public featured JSON files; "
            "the shared resource cache may still be filled."
        ),
    )
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    source_date = args.source_date or previous_source_date()
    try:
        with _exclusive_lock(args.lock_path):
            result = refresh(
                source_date=source_date,
                cache_path=args.cache_path,
                language_cache_path=args.language_cache_path,
                dry_run=args.dry_run,
            )
    except (
        FeaturedRefreshError,
        FeaturedCacheError,
        ResourceError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
