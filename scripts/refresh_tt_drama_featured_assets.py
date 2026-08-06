#!/usr/bin/env python3
"""Build locale-specific Featured JSON and best-effort WebP thumbnails."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_drama_featured_assets import (  # noqa: E402
    AssetConfig,
    FeaturedAssetError,
    build_featured_assets,
)


DEFAULT_INPUT_PATH = (
    "/mnt/data-disk/tt-drama-featured/public/current-by-language.json"
)
DEFAULT_LOCALE_OUTPUT_DIR = (
    "/mnt/data-disk/tt-drama-featured/public/by-language"
)
DEFAULT_COVER_OUTPUT_DIR = (
    "/mnt/data-disk/tt-drama-featured/public/covers"
)
DEFAULT_LOCK_PATH = "/run/tt-drama-featured-assets/refresh.lock"


def _env(name, default):
    return str(os.environ.get(name, default) or default).strip()


def _env_int(name, default, minimum, maximum):
    try:
        value = int(_env(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(value, int(maximum)))


@contextmanager
def _exclusive_lock(path):
    try:
        import fcntl
    except ImportError:
        raise FeaturedAssetError("fcntl is required for the asset refresh lock")
    lock_path = Path(path)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(str(lock_path.parent), 0o700)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise FeaturedAssetError(
            "another featured asset refresh is already running"
        ) from None
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _parser():
    parser = argparse.ArgumentParser(
        description="Build locale Featured JSON and cached WebP covers."
    )
    parser.add_argument(
        "--input-path",
        default=_env("TT_DRAMA_FEATURED_LANGUAGE_CACHE_PATH", DEFAULT_INPUT_PATH),
    )
    parser.add_argument(
        "--locale-output-dir",
        default=_env(
            "TT_DRAMA_FEATURED_ASSET_LOCALE_DIR",
            DEFAULT_LOCALE_OUTPUT_DIR,
        ),
    )
    parser.add_argument(
        "--cover-output-dir",
        default=_env(
            "TT_DRAMA_FEATURED_ASSET_COVER_DIR",
            DEFAULT_COVER_OUTPUT_DIR,
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=_env_int("TT_DRAMA_FEATURED_ASSET_WORKERS", 4, 1, 4),
    )
    parser.add_argument(
        "--http-timeout-seconds",
        type=float,
        default=float(
            _env_int("TT_DRAMA_FEATURED_ASSET_HTTP_TIMEOUT_SECONDS", 10, 1, 30)
        ),
    )
    parser.add_argument(
        "--lock-path",
        default=_env("TT_DRAMA_FEATURED_ASSET_LOCK_PATH", DEFAULT_LOCK_PATH),
    )
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        config = AssetConfig(
            input_path=Path(args.input_path),
            locale_output_dir=Path(args.locale_output_dir),
            cover_output_dir=Path(args.cover_output_dir),
            workers=args.workers,
            http_timeout_seconds=args.http_timeout_seconds,
        )
        with _exclusive_lock(args.lock_path):
            result = build_featured_assets(config)
    except (FeaturedAssetError, OSError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "unexpected asset refresh failure: %s"
                    % type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
