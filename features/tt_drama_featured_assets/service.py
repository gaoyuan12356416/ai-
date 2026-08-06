"""Build small locale snapshots and best-effort WebP cover thumbnails.

The schema-v2 language bundle remains authoritative. This module only derives
public cache artifacts from that already validated source and never queries a
database or changes ranking order.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
from urllib import request
from urllib.parse import urlsplit
import warnings


INPUT_SCHEMA_VERSION = 2
OUTPUT_SCHEMA_VERSION = 3
ITEMS_PER_LANGUAGE = 5
MAX_LANGUAGE_BUCKETS = 32
MAX_INPUT_BYTES = 256 * 1024
MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_COVER_PIXELS = 20_000_000
MAX_WEBP_BYTES = 512 * 1024
THUMBNAIL_WIDTH = 236
THUMBNAIL_HEIGHT = 338
THUMBNAIL_QUALITY = 78
MAX_WORKERS = 4
TRANSFORM_VERSION = b"tt-featured-webp-236x338-q78-v1"
DEFAULT_THUMBNAIL_BASE_URL = "/tt-featured-covers"
DEFAULT_ALLOWED_COVER_HOSTS = (
    "ads-cdn.yingliang.tech",
    "cdn.usrgrow.com",
    "static.mydramawave.com",
    "static-v1.mydramawave.com",
    "static-v2.mydramawave.com",
)
ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp"}
)
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")
CONTENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,32}$")
THUMBNAIL_NAME_PATTERN = re.compile(r"^[a-f0-9]{64}\.webp$")
LOCALE_FILE_PATTERN = re.compile(
    r"^(?P<language>[a-z]{2,3}(?:-[a-z0-9]{2,8})?)\.json$"
)
INPUT_ITEM_KEYS = frozenset(
    {"content_id", "title", "cover_url", "language", "episode_count"}
)
OUTPUT_ITEM_KEYS = frozenset(INPUT_ITEM_KEYS | {"thumbnail_url"})
_PILLOW_LIMIT_LOCK = Lock()
_THUMBNAIL_WRITE_LOCK = Lock()


class FeaturedAssetError(RuntimeError):
    """Base error for a failed derived-featured-assets run."""


class FeaturedAssetValidationError(FeaturedAssetError):
    """The input or a derived public artifact violated the strict contract."""


class ThumbnailBuildError(FeaturedAssetError):
    """One cover could not be downloaded or converted."""


@dataclass(frozen=True)
class AssetConfig:
    input_path: Path
    locale_output_dir: Path
    cover_output_dir: Path
    thumbnail_base_url: str = DEFAULT_THUMBNAIL_BASE_URL
    allowed_cover_hosts: tuple = DEFAULT_ALLOWED_COVER_HOSTS
    workers: int = MAX_WORKERS
    http_timeout_seconds: float = 10.0
    maximum_cover_bytes: int = MAX_COVER_BYTES
    maximum_cover_pixels: int = MAX_COVER_PIXELS

    def __post_init__(self):
        object.__setattr__(self, "input_path", Path(self.input_path))
        object.__setattr__(
            self,
            "locale_output_dir",
            Path(self.locale_output_dir),
        )
        object.__setattr__(
            self,
            "cover_output_dir",
            Path(self.cover_output_dir),
        )
        hosts = tuple(
            sorted(
                {
                    str(host or "").strip().lower()
                    for host in self.allowed_cover_hosts
                    if str(host or "").strip()
                }
            )
        )
        if not hosts or not set(hosts).issubset(DEFAULT_ALLOWED_COVER_HOSTS):
            raise ValueError("cover host allowlist cannot be expanded")
        object.__setattr__(self, "allowed_cover_hosts", hosts)
        if not 1 <= int(self.workers) <= MAX_WORKERS:
            raise ValueError("thumbnail workers must be between 1 and 4")
        if not 1 <= float(self.http_timeout_seconds) <= 30:
            raise ValueError("thumbnail HTTP timeout is invalid")
        if not 1024 <= int(self.maximum_cover_bytes) <= MAX_COVER_BYTES:
            raise ValueError("maximum cover bytes is invalid")
        if not 1_000_000 <= int(self.maximum_cover_pixels) <= MAX_COVER_PIXELS:
            raise ValueError("maximum cover pixels is invalid")
        _validate_thumbnail_base_url(self.thumbnail_base_url)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FeaturedAssetValidationError("JSON contains a duplicate key")
        result[key] = value
    return result


def _contains_private_spend_key(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key or "").strip().lower() in {"spend", "spend_n"}:
                return True
            if _contains_private_spend_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_private_spend_key(item) for item in value)
    return False


def _validate_source_date(value):
    raw = str(value or "")
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise FeaturedAssetValidationError(
            "featured source_date must be YYYY-MM-DD"
        ) from None
    if parsed.strftime("%Y-%m-%d") != raw:
        raise FeaturedAssetValidationError("featured source_date is not canonical")
    return raw


def _validate_generated_at(value):
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise FeaturedAssetValidationError(
            "featured generated_at is invalid"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FeaturedAssetValidationError(
            "featured generated_at must include a timezone"
        )
    return raw


def _validate_language(value):
    language = str(value or "")
    if not LANGUAGE_PATTERN.fullmatch(language):
        raise FeaturedAssetValidationError("featured language is invalid")
    return language


def _validate_cover_url(value, allowed_hosts):
    raw = str(value or "")
    if not raw or raw != raw.strip() or len(raw) > 4096:
        raise FeaturedAssetValidationError("featured cover URL is invalid")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise FeaturedAssetValidationError("featured cover URL is invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in set(allowed_hosts)
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise FeaturedAssetValidationError("featured cover URL is not allowlisted")
    return raw


def _validate_thumbnail_base_url(value):
    raw = str(value or "").rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise ValueError("thumbnail base URL is invalid") from None
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path != "/tt-featured-covers"
    ):
        raise ValueError("thumbnail base URL must use the fixed same-origin path")
    return raw


def _validate_thumbnail_url(value, thumbnail_base_url):
    raw = str(value or "")
    if not raw:
        return ""
    base = _validate_thumbnail_base_url(thumbnail_base_url)
    prefix = base + "/"
    if not raw.startswith(prefix):
        raise FeaturedAssetValidationError("thumbnail URL is not same-origin")
    name = raw[len(prefix):]
    if not THUMBNAIL_NAME_PATTERN.fullmatch(name):
        raise FeaturedAssetValidationError("thumbnail URL hash is invalid")
    return raw


def _normalize_input_item(item, language, seen_content_ids, allowed_hosts):
    if not isinstance(item, dict) or set(item) != INPUT_ITEM_KEYS:
        raise FeaturedAssetValidationError(
            "featured language item fields are incomplete"
        )
    content_id = str(item.get("content_id") or "")
    title = item.get("title")
    episode_count = item.get("episode_count")
    if (
        not CONTENT_ID_PATTERN.fullmatch(content_id)
        or content_id in seen_content_ids
    ):
        raise FeaturedAssetValidationError(
            "featured content_id is invalid or duplicated"
        )
    if (
        not isinstance(title, str)
        or not title
        or title != title.strip()
        or len(title) > 240
    ):
        raise FeaturedAssetValidationError("featured title is invalid")
    if item.get("language") != language:
        raise FeaturedAssetValidationError("featured item language is invalid")
    if (
        isinstance(episode_count, bool)
        or not isinstance(episode_count, int)
        or episode_count < 0
    ):
        raise FeaturedAssetValidationError("featured episode_count is invalid")
    cover_url = _validate_cover_url(item.get("cover_url"), allowed_hosts)
    seen_content_ids.add(content_id)
    return {
        "content_id": content_id,
        "title": title,
        "cover_url": cover_url,
        "language": language,
        "episode_count": episode_count,
    }


def load_language_bundle(path, allowed_cover_hosts=DEFAULT_ALLOWED_COVER_HOSTS):
    """Load and strictly validate the authoritative schema-v2 bundle."""
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise FeaturedAssetValidationError(
            "featured language bundle must be a regular file"
        )
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise FeaturedAssetValidationError(
            "featured language bundle stat failed: %s" % type(exc).__name__
        ) from None
    if size <= 0 or size > MAX_INPUT_BYTES:
        raise FeaturedAssetValidationError(
            "featured language bundle exceeds the size limit"
        )
    try:
        raw = source.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except FeaturedAssetValidationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise FeaturedAssetValidationError(
            "featured language bundle is unreadable: %s" % type(exc).__name__
        ) from None

    expected_keys = {
        "schema_version",
        "source_date",
        "generated_at",
        "default_language",
        "rankings",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise FeaturedAssetValidationError(
            "featured language bundle fields are invalid"
        )
    if payload.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise FeaturedAssetValidationError(
            "featured language bundle schema is invalid"
        )
    if payload.get("default_language") != "en":
        raise FeaturedAssetValidationError("English fallback is required")
    if _contains_private_spend_key(payload):
        raise FeaturedAssetValidationError(
            "featured language bundle contains private spend data"
        )
    source_date = _validate_source_date(payload.get("source_date"))
    generated_at = _validate_generated_at(payload.get("generated_at"))
    rankings = payload.get("rankings")
    if (
        not isinstance(rankings, dict)
        or not rankings
        or len(rankings) > MAX_LANGUAGE_BUCKETS
        or "en" not in rankings
    ):
        raise FeaturedAssetValidationError(
            "featured language rankings are invalid"
        )

    normalized_rankings = {}
    seen_content_ids = set()
    for raw_language in sorted(rankings):
        language = _validate_language(raw_language)
        items = rankings.get(raw_language)
        if not isinstance(items, list) or len(items) != ITEMS_PER_LANGUAGE:
            raise FeaturedAssetValidationError(
                "each featured language must contain exactly five items"
            )
        normalized_rankings[language] = [
            _normalize_input_item(
                item,
                language,
                seen_content_ids,
                allowed_cover_hosts,
            )
            for item in items
        ]
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "source_date": source_date,
        "generated_at": generated_at,
        "default_language": "en",
        "rankings": normalized_rankings,
    }


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ThumbnailBuildError("cover redirect is not allowed")


def download_cover_bytes(
    cover_url,
    *,
    allowed_hosts=DEFAULT_ALLOWED_COVER_HOSTS,
    timeout_seconds=10,
    maximum_bytes=MAX_COVER_BYTES,
    opener=None,
):
    """Download one bounded HTTPS image without following redirects."""
    try:
        safe_url = _validate_cover_url(cover_url, allowed_hosts)
    except FeaturedAssetValidationError as exc:
        raise ThumbnailBuildError(str(exc)) from None
    client = opener or request.build_opener(_NoRedirectHandler())
    req = request.Request(
        safe_url,
        headers={
            "Accept": "image/webp,image/png,image/jpeg,image/*;q=0.8",
            "User-Agent": "tt-drama-featured-assets/1.0",
        },
        method="GET",
    )
    try:
        with client.open(req, timeout=float(timeout_seconds)) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            if status != 200:
                raise ThumbnailBuildError("cover response status is invalid")
            try:
                _validate_cover_url(response.geturl(), allowed_hosts)
            except FeaturedAssetValidationError as exc:
                raise ThumbnailBuildError(
                    "cover response URL is not allowlisted"
                ) from exc
            content_type = str(
                response.headers.get("Content-Type", "")
            ).split(";", 1)[0].strip().lower()
            if content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
                raise ThumbnailBuildError("cover response type is invalid")
            content_length = str(response.headers.get("Content-Length", "")).strip()
            if content_length:
                try:
                    declared = int(content_length)
                except ValueError:
                    raise ThumbnailBuildError(
                        "cover response length is invalid"
                    ) from None
                if declared <= 0 or declared > int(maximum_bytes):
                    raise ThumbnailBuildError("cover response is too large")
            body = response.read(int(maximum_bytes) + 1)
    except ThumbnailBuildError:
        raise
    except Exception as exc:
        raise ThumbnailBuildError(
            "cover download failed: %s" % type(exc).__name__
        ) from None
    if not body or len(body) > int(maximum_bytes):
        raise ThumbnailBuildError("cover response is empty or too large")
    return body


def validate_image_runtime():
    """Fail before publishing locale files if Pillow/WebP is unavailable."""
    try:
        from PIL import Image, features
    except Exception as exc:
        raise FeaturedAssetError(
            "Pillow runtime is unavailable: %s" % type(exc).__name__
        ) from None
    if not features.check("webp"):
        raise FeaturedAssetError("Pillow WebP support is unavailable")
    try:
        output = io.BytesIO()
        Image.new("RGB", (1, 1), (0, 0, 0)).save(
            output,
            format="WEBP",
            quality=THUMBNAIL_QUALITY,
            method=0,
        )
        _validate_webp_bytes(output.getvalue())
    except FeaturedAssetError:
        raise
    except Exception as exc:
        raise FeaturedAssetError(
            "Pillow WebP smoke test failed: %s" % type(exc).__name__
        ) from None
    return True


def encode_cover_webp(
    source_bytes,
    *,
    width=THUMBNAIL_WIDTH,
    height=THUMBNAIL_HEIGHT,
    quality=THUMBNAIL_QUALITY,
    maximum_pixels=MAX_COVER_PIXELS,
):
    """Decode and resize one cover; Pillow is imported only on demand."""
    try:
        from PIL import Image, ImageFile, ImageOps, features
    except Exception as exc:
        raise ThumbnailBuildError(
            "Pillow is unavailable: %s" % type(exc).__name__
        ) from None
    if not features.check("webp"):
        raise ThumbnailBuildError("Pillow WebP support is unavailable")
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    # MAX_IMAGE_PIXELS is process-global in Pillow. Serialize the bounded
    # decode section so worker threads cannot restore one another's limit.
    with _PILLOW_LIMIT_LOCK:
        previous_max_pixels = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = int(maximum_pixels)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(source_bytes)) as source:
                    source.load()
                    if (
                        source.width <= 0
                        or source.height <= 0
                        or source.width * source.height > int(maximum_pixels)
                    ):
                        raise ThumbnailBuildError("cover dimensions are invalid")
                    normalized = ImageOps.exif_transpose(source).convert("RGB")
                    resized = ImageOps.fit(
                        normalized,
                        (int(width), int(height)),
                        method=Image.Resampling.LANCZOS,
                        centering=(0.5, 0.5),
                    )
                    output = io.BytesIO()
                    resized.save(
                        output,
                        format="WEBP",
                        quality=int(quality),
                        method=6,
                        lossless=False,
                    )
                    payload = output.getvalue()
        except ThumbnailBuildError:
            raise
        except Exception as exc:
            raise ThumbnailBuildError(
                "cover conversion failed: %s" % type(exc).__name__
            ) from None
        finally:
            Image.MAX_IMAGE_PIXELS = previous_max_pixels
    _validate_webp_bytes(payload)
    return payload


def _validate_webp_bytes(payload):
    if (
        not isinstance(payload, (bytes, bytearray))
        or len(payload) < 12
        or len(payload) > MAX_WEBP_BYTES
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
    ):
        raise ThumbnailBuildError("generated WebP is invalid")


def _require_output_directory(path, label):
    target = Path(path)
    if target.is_symlink() or not target.is_dir():
        raise FeaturedAssetValidationError(
            "%s must be a real pre-provisioned directory" % label
        )
    if not os.access(str(target), os.W_OK | os.X_OK):
        raise FeaturedAssetValidationError("%s is not writable" % label)
    return target


def _atomic_write_bytes(path, payload):
    target = Path(path)
    parent = _require_output_directory(target.parent, "asset output directory")
    if target.parent.resolve() != parent.resolve():
        raise FeaturedAssetValidationError("asset output path escaped its directory")
    if target.is_symlink():
        raise FeaturedAssetValidationError("asset output target must not be a symlink")
    try:
        if target.is_file() and target.read_bytes() == payload:
            os.chmod(str(target), 0o644)
            return False
    except OSError:
        pass

    descriptor = -1
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % target.name,
            suffix=".tmp",
            dir=str(parent),
        )
        temporary = Path(temporary_name)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if not hasattr(os, "fchmod"):
            os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(target))
        temporary = None
        os.chmod(str(target), 0o644)
        if hasattr(os, "O_DIRECTORY"):
            try:
                directory_fd = os.open(
                    str(parent),
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return True
    except FeaturedAssetError:
        raise
    except Exception as exc:
        raise FeaturedAssetError(
            "atomic asset write failed: %s" % type(exc).__name__
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _thumbnail_url(base_url, digest):
    return _validate_thumbnail_base_url(base_url) + "/" + digest + ".webp"


def _build_one_thumbnail(cover_url, config, downloader, encoder):
    source = downloader(
        cover_url,
        allowed_hosts=config.allowed_cover_hosts,
        timeout_seconds=config.http_timeout_seconds,
        maximum_bytes=config.maximum_cover_bytes,
    )
    digest = hashlib.sha256(
        TRANSFORM_VERSION + b"\0" + source
    ).hexdigest()
    target = config.cover_output_dir / (digest + ".webp")
    with _THUMBNAIL_WRITE_LOCK:
        if target.is_symlink():
            raise ThumbnailBuildError("thumbnail target must not be a symlink")
        if target.is_file():
            try:
                existing = target.read_bytes()
                _validate_webp_bytes(existing)
                os.chmod(str(target), 0o644)
                return _thumbnail_url(config.thumbnail_base_url, digest), False
            except (OSError, ThumbnailBuildError):
                pass
    payload = encoder(
        source,
        width=THUMBNAIL_WIDTH,
        height=THUMBNAIL_HEIGHT,
        quality=THUMBNAIL_QUALITY,
        maximum_pixels=config.maximum_cover_pixels,
    )
    _validate_webp_bytes(payload)
    # Different allowlisted URLs may resolve to identical bytes. Recheck under
    # one write lock after conversion so same-digest workers cannot race an
    # atomic replacement or surface a false best-effort failure.
    with _THUMBNAIL_WRITE_LOCK:
        if target.is_symlink():
            raise ThumbnailBuildError("thumbnail target must not be a symlink")
        if target.is_file():
            try:
                existing = target.read_bytes()
                _validate_webp_bytes(existing)
                os.chmod(str(target), 0o644)
                return _thumbnail_url(config.thumbnail_base_url, digest), False
            except (OSError, ThumbnailBuildError):
                pass
        changed = _atomic_write_bytes(target, bytes(payload))
    return _thumbnail_url(config.thumbnail_base_url, digest), changed


def _validate_locale_snapshot(payload, thumbnail_base_url):
    expected = {
        "schema_version",
        "source_date",
        "generated_at",
        "language",
        "items",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise FeaturedAssetValidationError("locale snapshot fields are invalid")
    if payload.get("schema_version") != OUTPUT_SCHEMA_VERSION:
        raise FeaturedAssetValidationError("locale snapshot schema is invalid")
    language = _validate_language(payload.get("language"))
    _validate_source_date(payload.get("source_date"))
    _validate_generated_at(payload.get("generated_at"))
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != ITEMS_PER_LANGUAGE:
        raise FeaturedAssetValidationError(
            "locale snapshot must contain exactly five items"
        )
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != OUTPUT_ITEM_KEYS:
            raise FeaturedAssetValidationError("locale item fields are invalid")
        normalized = _normalize_input_item(
            {key: item[key] for key in INPUT_ITEM_KEYS},
            language,
            seen,
            DEFAULT_ALLOWED_COVER_HOSTS,
        )
        if normalized["content_id"] != item["content_id"]:
            raise FeaturedAssetValidationError("locale item content_id changed")
        _validate_thumbnail_url(item.get("thumbnail_url"), thumbnail_base_url)
    if _contains_private_spend_key(payload):
        raise FeaturedAssetValidationError(
            "locale snapshot contains private spend data"
        )
    return payload


def _serialize_locale_snapshot(payload, thumbnail_base_url):
    _validate_locale_snapshot(payload, thumbnail_base_url)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > 32 * 1024:
        raise FeaturedAssetValidationError("locale snapshot exceeds 32 KiB")
    return encoded


def write_locale_snapshots(
    output_dir,
    snapshots,
    *,
    thumbnail_base_url=DEFAULT_THUMBNAIL_BASE_URL,
):
    """Validate all locale payloads, then atomically replace each file."""
    directory = _require_output_directory(output_dir, "locale output directory")
    if not isinstance(snapshots, dict) or not snapshots:
        raise FeaturedAssetValidationError("locale snapshots are empty")
    serialized = {}
    for raw_language in sorted(snapshots):
        language = _validate_language(raw_language)
        if snapshots[raw_language].get("language") != language:
            raise FeaturedAssetValidationError("locale snapshot language mismatch")
        serialized[language] = _serialize_locale_snapshot(
            snapshots[raw_language],
            thumbnail_base_url,
        )

    changed = 0
    for language, payload in serialized.items():
        if _atomic_write_bytes(directory / (language + ".json"), payload):
            changed += 1

    expected_names = {language + ".json" for language in serialized}
    pruned = 0
    for candidate in directory.iterdir():
        match = LOCALE_FILE_PATTERN.fullmatch(candidate.name)
        if not match or candidate.name in expected_names:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise FeaturedAssetValidationError(
                "stale locale target must be a regular file"
            )
        candidate.unlink()
        pruned += 1
    return {"changed": changed, "pruned": pruned}


def build_featured_assets(
    config,
    *,
    downloader=download_cover_bytes,
    encoder=encode_cover_webp,
):
    """Derive locale files and best-effort thumbnails from one v2 bundle."""
    if not isinstance(config, AssetConfig):
        raise TypeError("config must be AssetConfig")
    _require_output_directory(config.locale_output_dir, "locale output directory")
    _require_output_directory(config.cover_output_dir, "cover output directory")
    validate_image_runtime()
    bundle = load_language_bundle(
        config.input_path,
        allowed_cover_hosts=config.allowed_cover_hosts,
    )
    cover_urls = sorted(
        {
            item["cover_url"]
            for items in bundle["rankings"].values()
            for item in items
        }
    )
    thumbnail_urls = {}
    changed_thumbnails = 0
    failure_types = Counter()
    with ThreadPoolExecutor(max_workers=int(config.workers)) as executor:
        futures = {
            executor.submit(
                _build_one_thumbnail,
                cover_url,
                config,
                downloader,
                encoder,
            ): cover_url
            for cover_url in cover_urls
        }
        for future in as_completed(futures):
            cover_url = futures[future]
            try:
                thumbnail_url, changed = future.result()
                thumbnail_urls[cover_url] = thumbnail_url
                changed_thumbnails += int(bool(changed))
            except Exception as exc:
                thumbnail_urls[cover_url] = ""
                failure_types[type(exc).__name__] += 1

    failure_count = sum(failure_types.values())
    if len(cover_urls) >= ITEMS_PER_LANGUAGE and failure_count == len(cover_urls):
        raise FeaturedAssetError(
            "all featured thumbnails failed; locale LKG was preserved"
        )

    snapshots = {}
    for language, items in bundle["rankings"].items():
        snapshots[language] = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "source_date": bundle["source_date"],
            "generated_at": bundle["generated_at"],
            "language": language,
            "items": [
                {
                    **item,
                    "thumbnail_url": thumbnail_urls.get(item["cover_url"], ""),
                }
                for item in items
            ],
        }
    locale_result = write_locale_snapshots(
        config.locale_output_dir,
        snapshots,
        thumbnail_base_url=config.thumbnail_base_url,
    )
    return {
        "status": "ok",
        "source_date": bundle["source_date"],
        "generated_at": bundle["generated_at"],
        "language_count": len(snapshots),
        "unique_cover_count": len(cover_urls),
        "thumbnail_success_count": len(cover_urls) - failure_count,
        "thumbnail_failure_count": failure_count,
        "thumbnail_failure_types": dict(sorted(failure_types.items())),
        "thumbnail_changed_count": changed_thumbnails,
        "locale_changed_count": locale_result["changed"],
        "locale_pruned_count": locale_result["pruned"],
    }
