"""Fail-closed GPU compositor and TikTok Direct Post sidecar.

The service has four responsibilities:

* prepare an immutable vertical video with one explicit, versioned media mode:
  the legacy branded-preview compositor, a clean Direct Post normalizer, or a
  reviewed Direct Post compositor with the fixed tutorial outro;
* keep the branded-preview Logo/tutorial-outro profile permanently ineligible
  for formal Direct Post while allowing the clean and reviewed-outro profiles
  to carry separate auditable identities;
* query TikTok creator capabilities using a short-lived encrypted credential;
* initialize and reconcile a TikTok Direct Post using ``PULL_FROM_URL``.

The normal publish path remains fail-closed behind all compliance gates.  A
separate expiring SELF_ONLY canary route can authorize one immutable target
without changing those production gate assertions.

The worker listens on loopback only.  Its transport bearer is distinct from
the AES-GCM credential-seal key.  Plaintext TikTok tokens are never accepted as
normal fields and are never written to disk, logs, manifests, or exception
messages.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .credentials import CredentialEnvelopeError, decode_seal_key, open_access_token

try:  # Production Linux uses flock; Windows tests use the in-process fallback.
    import fcntl
except ImportError:  # pragma: no cover - Windows-only branch.
    fcntl = None


OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280
HEVC_VIDEO_BITRATE = "900k"
HEVC_VIDEO_MAXRATE = "1350k"
HEVC_VIDEO_BUFSIZE = "1800k"
H264_VIDEO_BITRATE = "1500k"
H264_VIDEO_MAXRATE = "2200k"
H264_VIDEO_BUFSIZE = "3000k"
AUDIO_BITRATE = "128k"
MAX_DELIVERY_AVERAGE_BITRATE_BPS = 1_900_000
PROFILE = "tt-post-hevc-720x1280-v2"
H264_FALLBACK_PROFILE = "tt-post-h264-720x1280-v2"
DIRECT_CLEAN_PROFILE = "tt-post-direct-clean-hevc-720x1280-v1"
DIRECT_CLEAN_H264_PROFILE = "tt-post-direct-clean-h264-720x1280-v1"
DIRECT_OUTRO_PROFILE = "tt-post-direct-outro-hevc-720x1280-v1"
DIRECT_OUTRO_H264_PROFILE = "tt-post-direct-outro-h264-720x1280-v1"
BRANDED_PREVIEW_MEDIA_MODE = "branded_preview"
DIRECT_CLEAN_MEDIA_MODE = "direct_clean"
DIRECT_OUTRO_MEDIA_MODE = "direct_outro"
DEFAULT_MEDIA_MODE = BRANDED_PREVIEW_MEDIA_MODE
HEALTH_PATH = "/health"
CREATOR_INFO_PATH = "/internal/tt-post/creator-info"
PREPARE_PATH = "/internal/tt-post/prepare"
PUBLISH_PATH = "/internal/tt-post/publish"
CANARY_PUBLISH_PATH = "/internal/tt-post/canary-publish"
RECONCILE_PATH = "/internal/tt-post/reconcile"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8830
DEFAULT_MEDIA_HOST = "127.0.0.1"
DEFAULT_MEDIA_PORT = 8831
DEFAULT_WORK_ROOT = Path("/data/tt-post-publisher")
DEFAULT_FFMPEG_BIN = "/opt/ffmpeg-nvenc/ffmpeg"
DEFAULT_FFPROBE_BIN = "/opt/ffmpeg-nvenc/ffprobe"
DEFAULT_FONT_FILE = "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"
DEFAULT_TRANSITION_SECONDS = 0.9
DEFAULT_COS_PREFIX = "tt-post-prepared"
DEFAULT_LOCAL_MEDIA_PREFIX = "tt-post-media/v1"
DEFAULT_STORAGE_BACKEND = "cos"
DEFAULT_TERMINAL_MEDIA_GRACE_SECONDS = 3600
DEFAULT_LOCAL_MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024
LOCAL_PREPARE_OVERHEAD_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
# TikTok Content Posting accepts video media up to 4 GiB. Long sources can
# expand after normalization, so the prepared artifact
# ceiling follows that platform boundary while source downloads stay at 2 GiB.
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024 * 1024
# TikTok creator_info currently allows up to 3,600 seconds for some accounts.
# The CPU still enforces the selected account's live creator limit before a
# queue can be frozen, so the GPU-wide ceiling only needs to be broad enough to
# prepare a valid long-form candidate.
DEFAULT_MAX_DURATION_SECONDS = 3600
DEFAULT_DOWNLOAD_TIMEOUT = 120
DEFAULT_PROBE_TIMEOUT = 120
DEFAULT_TRANSCODE_TIMEOUT = 3600
DEFAULT_COS_TIMEOUT = 120
DEFAULT_PREPARE_TOTAL_TIMEOUT = 8700
COS_PART_SIZE_BYTES = 8 * 1024 * 1024
COS_UPLOAD_THREADS = 4
_COS_UPLOAD_SLOTS = threading.BoundedSemaphore(COS_UPLOAD_THREADS)
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
TIKTOK_API_ORIGIN = "https://open.tiktokapis.com"
TIKTOK_CREATOR_INFO_PATH = "/v2/post/publish/creator_info/query/"
TIKTOK_VIDEO_INIT_PATH = "/v2/post/publish/video/init/"
TIKTOK_STATUS_FETCH_PATH = "/v2/post/publish/status/fetch/"
MANUAL_CANARY_ACKNOWLEDGEMENT = (
    "I_ACCEPT_ONE_SHOT_PRIVATE_TIKTOK_CANARY_20260731"
)
JOB_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{11,127}\Z")
CONTENT_ID_RE = re.compile(r"\A[A-Za-z0-9]{4,64}\Z")
ACCOUNT_ID_RE = re.compile(r"\A[1-9][0-9]{0,30}\Z")
HEX_64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
PUBLISH_ID_RE = re.compile(r"\A[A-Za-z0-9._~:+/-]{1,512}\Z")
SAFE_PREFIX_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,240}\Z")
SAFE_UPSTREAM_CODE_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,100}\Z")
SAFE_LOG_ID_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,200}\Z")
PRIVACY_LEVELS = frozenset(
    {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    }
)
PREPARE_REQUIRED_FIELDS = frozenset(
    {"job_id", "content_id", "source_url", "expected_profile"}
)
PREPARE_OPTIONAL_FIELDS = frozenset(
    {"source_sha256", "source_size", "source_trim_tail_seconds"}
)
CREATOR_INFO_FIELDS = frozenset(
    {"job_id", "source_account_id", "credential_envelope"}
)
PUBLISH_REQUIRED_FIELDS = frozenset(
    {
        "job_id",
        "source_account_id",
        "credential_envelope",
        "title",
        "privacy_level",
        "disable_comment",
        "disable_duet",
        "disable_stitch",
    }
)
PUBLISH_OPTIONAL_FIELDS = frozenset(
    {
        "video_cover_timestamp_ms",
        "brand_content_toggle",
        "brand_organic_toggle",
        "is_aigc",
        "manual_canary_id",
        "material_id",
    }
)
RECONCILE_FIELDS = CREATOR_INFO_FIELDS
_FALLBACK_LOCKS = {}
_FALLBACK_LOCKS_GUARD = threading.Lock()


class TTGPUError(RuntimeError):
    """Stable, redacted sidecar failure."""

    def __init__(self, code, message, status=400, details=None):
        code = str(code or "tt_gpu_error").strip()
        if not re.fullmatch(r"[a-z0-9_]{1,80}", code):
            code = "tt_gpu_error"
        self.code = code
        self.status = int(status or 500)
        self.details = _safe_error_details(details)
        super().__init__(_clean_message(message))


def _clean_message(value, limit=300):
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)(access[_ -]?token|secret(?:id|key)?|authorization)(\s*[:=]\s*)\S+",
        r"\1\2[redacted]",
        text,
    )
    return text[: int(limit)]


def _safe_error_details(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in (
        "log_id",
        "publish_id",
        "state",
        "upstream_code",
        "received_at",
    ):
        item = value.get(key)
        if isinstance(item, str) and len(item) <= 512 and "\x00" not in item:
            result[key] = item
    upstream_message = value.get("upstream_message")
    if isinstance(upstream_message, str):
        result["upstream_message"] = _clean_message(
            upstream_message,
            300,
        )
    http_status = value.get("upstream_http_status")
    if isinstance(http_status, int) and 100 <= http_status <= 599:
        result["upstream_http_status"] = http_status
    if type(value.get("message_redacted")) is bool:
        result["message_redacted"] = value["message_redacted"]
    return result


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _env_bool(name, default=False):
    raw = str(os.environ.get(name, "1" if default else "0") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise TTGPUError("invalid_configuration", "%s must be 0 or 1" % name, 500)


def _env_int(name, default, minimum, maximum):
    raw = str(os.environ.get(name, str(default)) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise TTGPUError(
            "invalid_configuration",
            "%s must be an integer" % name,
            500,
        ) from None
    if value < minimum or value > maximum:
        raise TTGPUError(
            "invalid_configuration",
            "%s must be between %s and %s" % (name, minimum, maximum),
            500,
        )
    return value


def _env_float(name, default, minimum, maximum):
    raw = str(os.environ.get(name, str(default)) or "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise TTGPUError(
            "invalid_configuration",
            "%s must be a number" % name,
            500,
        ) from None
    if value < minimum or value > maximum or value == float("inf"):
        raise TTGPUError(
            "invalid_configuration",
            "%s must be between %s and %s" % (name, minimum, maximum),
            500,
        )
    return value


def _absolute_path(value, name):
    text = str(value or "").strip()
    path = Path(text)
    if not text or "\x00" in text or not path.is_absolute():
        raise TTGPUError(
            "invalid_configuration",
            "%s must be an absolute path" % name,
            500,
        )
    return path


def _parse_allowed_hosts(value):
    result = []
    for raw in str(value or "").split(","):
        host = raw.strip().lower().rstrip(".")
        if not host:
            continue
        check = host[2:] if host.startswith("*.") else host
        if (
            not check
            or "/" in check
            or ":" in check
            or "@" in check
            or not re.fullmatch(r"[a-z0-9.-]+", check)
        ):
            raise TTGPUError(
                "invalid_configuration",
                "TT_POST_GPU_ALLOWED_SOURCE_HOSTS contains an invalid host",
                500,
            )
        result.append(host)
    if not result:
        raise TTGPUError(
            "invalid_configuration",
            "TT_POST_GPU_ALLOWED_SOURCE_HOSTS is required",
            500,
        )
    return tuple(dict.fromkeys(result))


def _normalize_https_origin(value, name):
    text = str(value or "").strip().rstrip("/")
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlsplit(text)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise TTGPUError(
            "invalid_configuration",
            "%s must be an HTTPS origin" % name,
            500,
        )
    return "https://" + parsed.hostname.lower()


def _normalize_prefix(value, name, default):
    text = str(value or default).strip().strip("/")
    parts = text.split("/")
    if (
        not text
        or any(part in {"", ".", ".."} for part in parts)
        or not SAFE_PREFIX_RE.fullmatch(text)
    ):
        raise TTGPUError(
            "invalid_configuration",
            "%s is invalid" % name,
            500,
        )
    return text


def _decode_local_signing_key(value):
    text = str(value or "").strip()
    if not text or len(text) > 512 or re.search(r"\s", text):
        raise TTGPUError(
            "invalid_configuration",
            "TT_POST_GPU_LOCAL_URL_SIGNING_KEY_B64 is required",
            500,
        )
    try:
        padding = "=" * ((4 - len(text) % 4) % 4)
        decoded = base64.urlsafe_b64decode(text + padding)
    except Exception:
        decoded = b""
    if len(decoded) != 32:
        raise TTGPUError(
            "invalid_configuration",
            "TT_POST_GPU_LOCAL_URL_SIGNING_KEY_B64 must encode exactly 32 bytes",
            500,
        )
    return decoded


def _loopback_host(value, name):
    host = str(value or "").strip()
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("not loopback")
    except ValueError:
        raise TTGPUError(
            "invalid_configuration",
            "%s must be a loopback address" % name,
            500,
        ) from None
    return host


def _selected_media_profile(video_encoder, media_mode):
    if media_mode == BRANDED_PREVIEW_MEDIA_MODE:
        return (
            PROFILE
            if video_encoder == "hevc_nvenc"
            else H264_FALLBACK_PROFILE
        )
    if media_mode == DIRECT_CLEAN_MEDIA_MODE:
        return (
            DIRECT_CLEAN_PROFILE
            if video_encoder == "hevc_nvenc"
            else DIRECT_CLEAN_H264_PROFILE
        )
    if media_mode == DIRECT_OUTRO_MEDIA_MODE:
        return (
            DIRECT_OUTRO_PROFILE
            if video_encoder == "hevc_nvenc"
            else DIRECT_OUTRO_H264_PROFILE
        )
    raise TTGPUError(
        "invalid_configuration",
        "TT_POST_GPU_MEDIA_MODE is not supported",
        500,
    )


@dataclass(frozen=True)
class WorkerConfig:
    """Validated runtime configuration with secrets excluded from repr."""

    enabled: bool
    host: str
    port: int
    internal_token: str = field(repr=False)
    credential_seal_key: bytes = field(repr=False)
    credential_max_ttl_seconds: int
    work_root: Path
    fixed_outro_path: Path
    logo_path: Path
    font_file: Path
    allowed_source_hosts: tuple
    ffmpeg_bin: str
    ffprobe_bin: str
    video_encoder: str
    cos_secret_id: str = field(repr=False)
    cos_secret_key: str = field(repr=False)
    cos_bucket: str
    cos_region: str
    cos_domain: str
    cos_prefix: str
    storage_backend: str = DEFAULT_STORAGE_BACKEND
    media_host: str = DEFAULT_MEDIA_HOST
    media_port: int = DEFAULT_MEDIA_PORT
    local_media_origin: str = ""
    local_media_prefix: str = DEFAULT_LOCAL_MEDIA_PREFIX
    local_media_signing_key: bytes = field(default=b"", repr=False)
    terminal_media_grace_seconds: int = DEFAULT_TERMINAL_MEDIA_GRACE_SECONDS
    local_min_free_bytes: int = DEFAULT_LOCAL_MIN_FREE_BYTES
    url_property_verified_origin: str = ""
    live_enabled: bool = False
    direct_audit_approved: bool = False
    url_property_verified: bool = False
    manual_canary_enabled: bool = False
    manual_canary_acknowledged: bool = False
    manual_canary_id: str = ""
    manual_canary_expires_at_utc: str = ""
    manual_canary_account_id: str = ""
    manual_canary_material_id: str = ""
    manual_canary_content_id: str = ""
    manual_canary_gpu_job_id: str = ""
    manual_canary_output_sha256: str = ""
    manual_canary_output_size: int = 0
    manual_canary_profile: str = ""
    manual_canary_origin: str = ""
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS
    default_source_trim_tail_seconds: float = 4.333333
    download_timeout: int = DEFAULT_DOWNLOAD_TIMEOUT
    probe_timeout: int = DEFAULT_PROBE_TIMEOUT
    transcode_timeout: int = DEFAULT_TRANSCODE_TIMEOUT
    cos_timeout: int = DEFAULT_COS_TIMEOUT
    prepare_total_timeout: int = DEFAULT_PREPARE_TOTAL_TIMEOUT
    media_mode: str = DEFAULT_MEDIA_MODE
    profile: str = PROFILE
    fixed_outro_sha256: str = ""
    logo_sha256: str = ""

    @classmethod
    def from_env(cls):
        if not _env_bool("TT_POST_GPU_ENABLED", False):
            raise TTGPUError(
                "tt_gpu_disabled",
                "TikTok GPU publisher is disabled",
                503,
            )
        host = _loopback_host(
            os.environ.get("TT_POST_GPU_HOST", DEFAULT_HOST),
            "TT_POST_GPU_HOST",
        )
        internal_token = str(os.environ.get("TT_POST_GPU_INTERNAL_TOKEN", "") or "")
        if (
            len(internal_token) < 32
            or len(internal_token) > 512
            or re.search(r"[\s\x00-\x1f]", internal_token)
        ):
            raise TTGPUError(
                "invalid_configuration",
                "TT_POST_GPU_INTERNAL_TOKEN must be a dedicated secret of at least 32 characters",
                500,
            )
        try:
            seal_key = decode_seal_key(
                os.environ.get("TT_POST_GPU_CREDENTIAL_SEAL_KEY_B64", "")
            )
        except CredentialEnvelopeError as exc:
            raise TTGPUError(exc.code, str(exc), exc.status) from None
        work_root = _absolute_path(
            os.environ.get("TT_POST_GPU_WORK_ROOT", DEFAULT_WORK_ROOT),
            "TT_POST_GPU_WORK_ROOT",
        )
        fixed_outro_path = _absolute_path(
            os.environ.get("TT_POST_GPU_FIXED_OUTRO_PATH", ""),
            "TT_POST_GPU_FIXED_OUTRO_PATH",
        )
        font_file = _absolute_path(
            os.environ.get("TT_POST_GPU_FONT_FILE", DEFAULT_FONT_FILE),
            "TT_POST_GPU_FONT_FILE",
        )
        logo_path = _absolute_path(
            os.environ.get("TT_POST_GPU_LOGO_PATH", ""),
            "TT_POST_GPU_LOGO_PATH",
        )
        if not fixed_outro_path.is_file() or fixed_outro_path.is_symlink():
            raise TTGPUError(
                "invalid_configuration",
                "configured fixed outro is missing or unsafe",
                500,
            )
        if not font_file.is_file() or font_file.is_symlink():
            raise TTGPUError(
                "invalid_configuration",
                "configured overlay font is missing or unsafe",
                500,
            )
        if not logo_path.is_file() or logo_path.is_symlink():
            raise TTGPUError(
                "invalid_configuration",
                "configured DramaWave logo is missing or unsafe",
                500,
            )
        ffmpeg = str(
            _absolute_path(
                os.environ.get("TT_POST_GPU_FFMPEG_BIN", DEFAULT_FFMPEG_BIN),
                "TT_POST_GPU_FFMPEG_BIN",
            )
        )
        ffprobe = str(
            _absolute_path(
                os.environ.get("TT_POST_GPU_FFPROBE_BIN", DEFAULT_FFPROBE_BIN),
                "TT_POST_GPU_FFPROBE_BIN",
            )
        )
        encoder = str(
            os.environ.get("TT_POST_GPU_VIDEO_ENCODER", "hevc_nvenc") or ""
        ).strip()
        if encoder not in {"hevc_nvenc", "h264_nvenc", "libx264"}:
            raise TTGPUError(
                "invalid_configuration",
                (
                    "TT_POST_GPU_VIDEO_ENCODER must be hevc_nvenc, "
                    "h264_nvenc, or libx264"
                ),
                500,
            )
        media_mode = str(
            os.environ.get("TT_POST_GPU_MEDIA_MODE", DEFAULT_MEDIA_MODE)
            or ""
        ).strip().lower()
        if media_mode not in {
            BRANDED_PREVIEW_MEDIA_MODE,
            DIRECT_CLEAN_MEDIA_MODE,
            DIRECT_OUTRO_MEDIA_MODE,
        }:
            raise TTGPUError(
                "invalid_configuration",
                (
                    "TT_POST_GPU_MEDIA_MODE must be branded_preview, "
                    "direct_clean, or direct_outro"
                ),
                500,
            )
        selected_profile = _selected_media_profile(encoder, media_mode)
        fixed_outro_sha256 = str(
            os.environ.get("TT_POST_GPU_FIXED_OUTRO_SHA256", "") or ""
        ).strip().lower()
        logo_sha256 = str(
            os.environ.get("TT_POST_GPU_LOGO_SHA256", "") or ""
        ).strip().lower()
        if media_mode == DIRECT_OUTRO_MEDIA_MODE and (
            not HEX_64_RE.fullmatch(fixed_outro_sha256)
            or not HEX_64_RE.fullmatch(logo_sha256)
        ):
            raise TTGPUError(
                "invalid_configuration",
                (
                    "direct_outro requires approved "
                    "TT_POST_GPU_FIXED_OUTRO_SHA256 and "
                    "TT_POST_GPU_LOGO_SHA256 fingerprints"
                ),
                500,
            )
        if media_mode == DIRECT_OUTRO_MEDIA_MODE:
            try:
                actual_outro_sha256, _ = _file_sha256(fixed_outro_path)
                actual_logo_sha256, _ = _file_sha256(logo_path)
            except OSError:
                raise TTGPUError(
                    "invalid_configuration",
                    "approved direct_outro assets are unavailable",
                    500,
                ) from None
            if (
                not secrets.compare_digest(
                    actual_outro_sha256,
                    fixed_outro_sha256,
                )
                or not secrets.compare_digest(
                    actual_logo_sha256,
                    logo_sha256,
                )
            ):
                raise TTGPUError(
                    "invalid_configuration",
                    "direct_outro assets do not match approved fingerprints",
                    500,
                )
        secret_id = str(os.environ.get("TT_POST_GPU_COS_SECRET_ID", "") or "").strip()
        secret_key = str(os.environ.get("TT_POST_GPU_COS_SECRET_KEY", "") or "").strip()
        bucket = str(os.environ.get("TT_POST_GPU_COS_BUCKET", "") or "").strip()
        region = str(os.environ.get("TT_POST_GPU_COS_REGION", "") or "").strip()
        storage_backend = str(
            os.environ.get(
                "TT_POST_GPU_STORAGE_BACKEND",
                DEFAULT_STORAGE_BACKEND,
            )
            or ""
        ).strip().lower()
        if storage_backend not in {"cos", "local"}:
            raise TTGPUError(
                "invalid_configuration",
                "TT_POST_GPU_STORAGE_BACKEND must be cos or local",
                500,
            )
        if storage_backend == "cos" and not all(
            (secret_id, secret_key, bucket, region)
        ):
            raise TTGPUError(
                "invalid_configuration",
                "dedicated TT GPU COS configuration is required",
                500,
            )
        cos_domain_raw = str(
            os.environ.get("TT_POST_GPU_COS_DOMAIN", "") or ""
        ).strip()
        cos_domain = (
            _normalize_https_origin(
                cos_domain_raw,
                "TT_POST_GPU_COS_DOMAIN",
            )
            if cos_domain_raw
            else ""
        )
        if storage_backend == "cos" and not cos_domain:
            raise TTGPUError(
                "invalid_configuration",
                "TT_POST_GPU_COS_DOMAIN is required for COS storage",
                500,
            )
        local_media_origin = ""
        local_media_signing_key = b""
        local_media_origin_raw = str(
            os.environ.get("TT_POST_GPU_LOCAL_MEDIA_ORIGIN", "") or ""
        ).strip()
        local_signing_key_raw = str(
            os.environ.get(
                "TT_POST_GPU_LOCAL_URL_SIGNING_KEY_B64",
                "",
            )
            or ""
        ).strip()
        control_port = _env_int(
            "TT_POST_GPU_PORT",
            DEFAULT_PORT,
            1,
            65535,
        )
        media_host = _loopback_host(
            os.environ.get("TT_POST_GPU_MEDIA_HOST", DEFAULT_MEDIA_HOST),
            "TT_POST_GPU_MEDIA_HOST",
        )
        media_port = _env_int(
            "TT_POST_GPU_MEDIA_PORT",
            DEFAULT_MEDIA_PORT,
            1,
            65535,
        )
        local_origin_requested = bool(
            storage_backend == "local"
            or local_media_origin_raw
            or local_signing_key_raw
        )
        if local_origin_requested and (
            media_host == host and media_port == control_port
        ):
            raise TTGPUError(
                "invalid_configuration",
                "TT_POST_GPU_MEDIA_PORT must differ from TT_POST_GPU_PORT",
                500,
            )
        if local_origin_requested:
            local_media_origin = _normalize_https_origin(
                local_media_origin_raw,
                "TT_POST_GPU_LOCAL_MEDIA_ORIGIN",
            )
            local_media_signing_key = _decode_local_signing_key(
                local_signing_key_raw
            )
        url_property_verified_origin_raw = str(
            os.environ.get(
                "TT_POST_URL_PROPERTY_VERIFIED_ORIGIN",
                "",
            )
            or ""
        ).strip()
        url_property_verified_origin = (
            _normalize_https_origin(
                url_property_verified_origin_raw,
                "TT_POST_URL_PROPERTY_VERIFIED_ORIGIN",
            )
            if url_property_verified_origin_raw
            else ""
        )
        manual_canary_enabled = _env_bool(
            "TT_POST_MANUAL_CANARY_ENABLED",
            False,
        )
        manual_canary_values = {
            "manual_canary_enabled": False,
            "manual_canary_acknowledged": False,
            "manual_canary_id": "",
            "manual_canary_expires_at_utc": "",
            "manual_canary_account_id": "",
            "manual_canary_material_id": "",
            "manual_canary_content_id": "",
            "manual_canary_gpu_job_id": "",
            "manual_canary_output_sha256": "",
            "manual_canary_output_size": 0,
            "manual_canary_profile": "",
            "manual_canary_origin": "",
        }
        if manual_canary_enabled:
            acknowledgement = str(
                os.environ.get(
                    "TT_POST_MANUAL_CANARY_ACKNOWLEDGEMENT",
                    "",
                )
                or ""
            )
            if not secrets.compare_digest(
                acknowledgement,
                MANUAL_CANARY_ACKNOWLEDGEMENT,
            ):
                raise TTGPUError(
                    "invalid_configuration",
                    "TT manual canary acknowledgement is invalid",
                    500,
                )
            canary_id = str(
                os.environ.get("TT_POST_MANUAL_CANARY_ID", "") or ""
            ).strip()
            account_id = str(
                os.environ.get(
                    "TT_POST_MANUAL_CANARY_ACCOUNT_ID",
                    "",
                )
                or ""
            ).strip()
            material_id = str(
                os.environ.get(
                    "TT_POST_MANUAL_CANARY_MATERIAL_ID",
                    "",
                )
                or ""
            ).strip()
            content_id = str(
                os.environ.get(
                    "TT_POST_MANUAL_CANARY_CONTENT_ID",
                    "",
                )
                or ""
            ).strip()
            gpu_job_id = str(
                os.environ.get(
                    "TT_POST_MANUAL_CANARY_GPU_JOB_ID",
                    "",
                )
                or ""
            ).strip()
            output_sha256 = str(
                os.environ.get(
                    "TT_POST_MANUAL_CANARY_OUTPUT_SHA256",
                    "",
                )
                or ""
            ).strip().lower()
            profile = str(
                os.environ.get(
                    "TT_POST_MANUAL_CANARY_PROFILE",
                    "",
                )
                or ""
            ).strip()
            origin = _normalize_https_origin(
                os.environ.get(
                    "TT_POST_MANUAL_CANARY_ORIGIN",
                    "",
                ),
                "TT_POST_MANUAL_CANARY_ORIGIN",
            )
            expiry_raw = str(
                os.environ.get(
                    "TT_POST_MANUAL_CANARY_EXPIRES_AT_UTC",
                    "",
                )
                or ""
            ).strip()
            try:
                expiry = datetime.fromisoformat(
                    expiry_raw.replace("Z", "+00:00")
                )
                if expiry.tzinfo is None:
                    raise ValueError("naive expiry")
                expiry = expiry.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                raise TTGPUError(
                    "invalid_configuration",
                    "TT manual canary expiry is invalid",
                    500,
                ) from None
            now = datetime.now(timezone.utc)
            expected_origin = (
                local_media_origin
                if storage_backend == "local"
                else cos_domain
            )
            if (
                not re.fullmatch(r"[A-Za-z0-9._-]{8,80}", canary_id)
                or not ACCOUNT_ID_RE.fullmatch(account_id)
                or not re.fullmatch(r"[1-9][0-9]{0,18}", material_id)
                or not CONTENT_ID_RE.fullmatch(content_id)
                or not JOB_ID_RE.fullmatch(gpu_job_id)
                or not HEX_64_RE.fullmatch(output_sha256)
                or profile != selected_profile
                or origin != expected_origin
                or expiry > now + timedelta(hours=24)
            ):
                raise TTGPUError(
                    "invalid_configuration",
                    "TT manual canary target configuration is invalid",
                    500,
                )
            manual_canary_values = {
                "manual_canary_enabled": True,
                "manual_canary_acknowledged": True,
                "manual_canary_id": canary_id,
                "manual_canary_expires_at_utc": (
                    expiry.isoformat().replace("+00:00", "Z")
                ),
                "manual_canary_account_id": account_id,
                "manual_canary_material_id": material_id,
                "manual_canary_content_id": content_id,
                "manual_canary_gpu_job_id": gpu_job_id,
                "manual_canary_output_sha256": output_sha256,
                "manual_canary_output_size": _env_int(
                    "TT_POST_MANUAL_CANARY_OUTPUT_SIZE",
                    0,
                    1,
                    DEFAULT_MAX_OUTPUT_BYTES,
                ),
                "manual_canary_profile": profile,
                "manual_canary_origin": origin,
            }
        return cls(
            enabled=True,
            host=host,
            port=control_port,
            internal_token=internal_token,
            credential_seal_key=seal_key,
            credential_max_ttl_seconds=_env_int(
                "TT_POST_GPU_CREDENTIAL_MAX_TTL_SECONDS",
                300,
                30,
                900,
            ),
            work_root=work_root,
            fixed_outro_path=fixed_outro_path,
            logo_path=logo_path,
            font_file=font_file,
            allowed_source_hosts=_parse_allowed_hosts(
                os.environ.get("TT_POST_GPU_ALLOWED_SOURCE_HOSTS", "")
            ),
            ffmpeg_bin=ffmpeg,
            ffprobe_bin=ffprobe,
            video_encoder=encoder,
            media_mode=media_mode,
            profile=selected_profile,
            fixed_outro_sha256=fixed_outro_sha256,
            logo_sha256=logo_sha256,
            cos_secret_id=secret_id,
            cos_secret_key=secret_key,
            cos_bucket=bucket,
            cos_region=region,
            cos_domain=cos_domain,
            cos_prefix=_normalize_prefix(
                os.environ.get("TT_POST_GPU_COS_PREFIX", DEFAULT_COS_PREFIX),
                "TT_POST_GPU_COS_PREFIX",
                DEFAULT_COS_PREFIX,
            ),
            storage_backend=storage_backend,
            media_host=media_host,
            media_port=media_port,
            local_media_origin=local_media_origin,
            local_media_prefix=_normalize_prefix(
                os.environ.get(
                    "TT_POST_GPU_LOCAL_MEDIA_PREFIX",
                    DEFAULT_LOCAL_MEDIA_PREFIX,
                ),
                "TT_POST_GPU_LOCAL_MEDIA_PREFIX",
                DEFAULT_LOCAL_MEDIA_PREFIX,
            ),
            local_media_signing_key=local_media_signing_key,
            terminal_media_grace_seconds=_env_int(
                "TT_POST_GPU_TERMINAL_MEDIA_GRACE_SECONDS",
                DEFAULT_TERMINAL_MEDIA_GRACE_SECONDS,
                3600,
                86400,
            ),
            local_min_free_bytes=_env_int(
                "TT_POST_GPU_LOCAL_MIN_FREE_BYTES",
                DEFAULT_LOCAL_MIN_FREE_BYTES,
                1024 * 1024 * 1024,
                1024 * 1024 * 1024 * 1024,
            ),
            url_property_verified_origin=url_property_verified_origin,
            live_enabled=_env_bool("TT_POST_LIVE_ENABLED", False),
            direct_audit_approved=_env_bool(
                "TT_POST_DIRECT_AUDIT_APPROVED",
                False,
            ),
            url_property_verified=_env_bool(
                "TT_POST_URL_PROPERTY_VERIFIED",
                False,
            ),
            **manual_canary_values,
            max_source_bytes=_env_int(
                "TT_POST_GPU_MAX_SOURCE_BYTES",
                DEFAULT_MAX_SOURCE_BYTES,
                1024,
                4 * 1024 * 1024 * 1024,
            ),
            max_output_bytes=_env_int(
                "TT_POST_GPU_MAX_OUTPUT_BYTES",
                DEFAULT_MAX_OUTPUT_BYTES,
                1024,
                4 * 1024 * 1024 * 1024,
            ),
            max_duration_seconds=_env_int(
                "TT_POST_GPU_MAX_DURATION_SECONDS",
                DEFAULT_MAX_DURATION_SECONDS,
                5,
                3600,
            ),
            default_source_trim_tail_seconds=_env_float(
                "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS",
                4.333333,
                0.0,
                60.0,
            ),
            download_timeout=_env_int(
                "TT_POST_GPU_DOWNLOAD_TIMEOUT",
                DEFAULT_DOWNLOAD_TIMEOUT,
                1,
                600,
            ),
            probe_timeout=_env_int(
                "TT_POST_GPU_PROBE_TIMEOUT",
                DEFAULT_PROBE_TIMEOUT,
                1,
                600,
            ),
            transcode_timeout=_env_int(
                "TT_POST_GPU_TRANSCODE_TIMEOUT",
                DEFAULT_TRANSCODE_TIMEOUT,
                30,
                14400,
            ),
            cos_timeout=_env_int(
                "TT_POST_GPU_COS_TIMEOUT",
                DEFAULT_COS_TIMEOUT,
                60,
                300,
            ),
            prepare_total_timeout=_env_int(
                "TT_POST_GPU_PREPARE_TOTAL_TIMEOUT",
                DEFAULT_PREPARE_TOTAL_TIMEOUT,
                600,
                8700,
            ),
        )

    def brand_overlay_review_required(self):
        return self.media_mode == BRANDED_PREVIEW_MEDIA_MODE

    def direct_post_eligible(self):
        return self.media_mode in {
            DIRECT_CLEAN_MEDIA_MODE,
            DIRECT_OUTRO_MEDIA_MODE,
        }

    def uses_outro_pipeline(self):
        return self.media_mode in {
            BRANDED_PREVIEW_MEDIA_MODE,
            DIRECT_OUTRO_MEDIA_MODE,
        }

    def asset_identity_ready(self):
        if self.media_mode != DIRECT_OUTRO_MEDIA_MODE:
            return True
        if (
            not HEX_64_RE.fullmatch(self.fixed_outro_sha256)
            or not HEX_64_RE.fullmatch(self.logo_sha256)
        ):
            return False
        try:
            actual_outro_sha256, _ = _file_sha256(self.fixed_outro_path)
            actual_logo_sha256, _ = _file_sha256(self.logo_path)
        except OSError:
            return False
        return bool(
            secrets.compare_digest(
                actual_outro_sha256,
                self.fixed_outro_sha256,
            )
            and secrets.compare_digest(
                actual_logo_sha256,
                self.logo_sha256,
            )
        )

    def preparation_transition(self):
        return (
            "phone-match-0.9s"
            if self.uses_outro_pipeline()
            else "none"
        )

    def gate_state(self):
        expected_origin = (
            self.local_media_origin
            if self.storage_backend == "local"
            else self.cos_domain
        )
        verified_origin_matches = bool(
            expected_origin
            and self.url_property_verified_origin
            and secrets.compare_digest(
                expected_origin,
                self.url_property_verified_origin,
            )
        )
        return {
            "TT_POST_LIVE_ENABLED": bool(self.live_enabled),
            "TT_POST_DIRECT_AUDIT_APPROVED": bool(
                self.direct_audit_approved
            ),
            "TT_POST_URL_PROPERTY_VERIFIED": bool(
                self.url_property_verified
            ),
            "ready": bool(
                self.live_enabled
                and self.direct_audit_approved
                and self.url_property_verified
                and verified_origin_matches
            ),
        }

    def manual_canary_state(self):
        active = False
        if (
            self.manual_canary_enabled
            and self.manual_canary_acknowledged
            and self.manual_canary_expires_at_utc
        ):
            try:
                expires_at = datetime.fromisoformat(
                    self.manual_canary_expires_at_utc.replace(
                        "Z",
                        "+00:00",
                    )
                ).astimezone(timezone.utc)
                active = datetime.now(timezone.utc) < expires_at
            except (TypeError, ValueError, OverflowError):
                active = False
        return {
            "active": bool(active),
            "enabled": bool(self.manual_canary_enabled),
            "privacy_level": "SELF_ONLY",
            "test_bypass": bool(active),
        }


class PrepareDeadline:
    """One monotonic budget shared by every stage of a prepare request."""

    def __init__(self, timeout_seconds, monotonic_fn=time.monotonic):
        self._monotonic_fn = monotonic_fn
        self._expires_at = (
            float(monotonic_fn()) + float(timeout_seconds)
        )

    def remaining(self):
        return max(0.0, self._expires_at - float(self._monotonic_fn()))

    def check(self):
        if self.remaining() <= 0:
            raise TTGPUError(
                "prepare_timeout",
                "GPU prepare exceeded the total execution budget",
                504,
            )

    def stage_timeout(self, configured_timeout):
        self.check()
        return max(
            1,
            min(
                int(configured_timeout),
                int(math.ceil(self.remaining())),
            ),
        )


def _ensure_private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise TTGPUError("storage_error", "TT GPU storage path is invalid", 500)
    try:
        path.chmod(0o700)
    except OSError:
        raise TTGPUError("storage_error", "TT GPU storage cannot be secured", 500) from None
    return path


def _fsync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_regular_readonly(path, expected_size=None):
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (
                expected_size is not None
                and int(metadata.st_size) != int(expected_size)
            )
        ):
            raise OSError("not a regular file with the expected size")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _fsync_regular_file(path, expected_size):
    flags = os.O_RDWR
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or int(metadata.st_size) != int(expected_size)
        ):
            raise OSError("not a regular file with the expected size")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path, payload):
    path = Path(path)
    _ensure_private_directory(path.parent)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(8)))
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path):
    path = Path(path)
    try:
        if path.is_symlink():
            return None
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (FileNotFoundError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


@contextmanager
def _job_lock(path, deadline=None):
    path = Path(path)
    _ensure_private_directory(path.parent)
    if fcntl is None:
        key = str(path.resolve())
        with _FALLBACK_LOCKS_GUARD:
            lock = _FALLBACK_LOCKS.setdefault(key, threading.Lock())
        if deadline is not None:
            deadline.check()
        acquired = (
            lock.acquire(timeout=deadline.remaining())
            if deadline is not None
            else lock.acquire()
        )
        if not acquired:
            raise TTGPUError(
                "prepare_timeout",
                "GPU prepare exceeded the total execution budget",
                504,
            )
        try:
            if deadline is not None:
                deadline.check()
            yield
        finally:
            lock.release()
        return
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        if deadline is None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            while True:
                deadline.check()
                try:
                    fcntl.flock(
                        handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    break
                except BlockingIOError:
                    time.sleep(min(0.1, deadline.remaining()))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _file_sha256(path, deadline=None):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            if deadline is not None:
                deadline.check()
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _host_matches(host, allowed_hosts):
    host = str(host or "").lower().rstrip(".")
    for allowed in allowed_hosts:
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif secrets.compare_digest(host, allowed):
            return True
    return False


def validate_source_url(value, allowed_hosts):
    text = str(value or "").strip()
    if len(text) > 4096 or "\x00" in text:
        raise TTGPUError("invalid_request", "source URL is invalid")
    parsed = urllib.parse.urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or not parsed.path.startswith("/")
        or not _host_matches(parsed.hostname, allowed_hosts)
    ):
        raise TTGPUError("source_url_not_allowed", "source URL is not allowed", 400)
    return text


def _resolve_public_host(hostname):
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError:
        raise TTGPUError(
            "source_download_failed",
            "source host could not be resolved",
            502,
        ) from None
    if not addresses:
        raise TTGPUError(
            "source_download_failed",
            "source host could not be resolved",
            502,
        )
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise TTGPUError(
                    "source_url_not_allowed",
                    "source host resolved to a non-public address",
                    400,
                )
        except ValueError:
            raise TTGPUError(
                "source_url_not_allowed",
                "source host address is invalid",
                400,
            ) from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect(),
)


def download_source(
    url,
    destination,
    expected_sha256,
    expected_size,
    config,
    deadline=None,
):
    url = validate_source_url(url, config.allowed_source_hosts)
    parsed = urllib.parse.urlsplit(url)
    _resolve_public_host(parsed.hostname)
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "video/mp4,video/*;q=0.9,application/octet-stream;q=0.8",
            "User-Agent": "DramawaveTTPostGPU/1",
        },
    )
    destination = Path(destination)
    digest = hashlib.sha256()
    size = 0
    try:
        with _NO_REDIRECT_OPENER.open(
            request,
            timeout=(
                deadline.stage_timeout(config.download_timeout)
                if deadline is not None
                else config.download_timeout
            ),
        ) as response:
            raw_length = response.headers.get("Content-Length")
            if raw_length not in (None, ""):
                try:
                    announced = int(raw_length)
                except (TypeError, ValueError, OverflowError):
                    raise TTGPUError(
                        "source_download_failed",
                        "source response length is invalid",
                        502,
                    ) from None
                if expected_size is not None and announced != int(expected_size):
                    raise TTGPUError(
                        "source_integrity_mismatch",
                        "source response size does not match the preflight",
                        409,
                    )
            with destination.open("xb") as handle:
                os.chmod(destination, 0o600)
                while True:
                    if deadline is not None:
                        deadline.check()
                    chunk = response.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > int(config.max_source_bytes):
                        raise TTGPUError(
                            "source_too_large",
                            "source exceeds the configured size limit",
                            413,
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
    except TTGPUError:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise TTGPUError(
            "source_download_failed",
            "source download failed",
            502,
        ) from None
    actual_sha = digest.hexdigest()
    size_mismatch = expected_size is not None and size != int(expected_size)
    sha_mismatch = (
        expected_sha256 is not None
        and not secrets.compare_digest(actual_sha, expected_sha256)
    )
    if size_mismatch or sha_mismatch:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise TTGPUError(
            "source_integrity_mismatch",
            "downloaded source failed SHA-256 or size verification",
            409,
        )
    return {"sha256": actual_sha, "size": size}


def _run_command(
    runner,
    command,
    timeout,
    error_code,
    *,
    timeout_error_code=None,
):
    try:
        completed = runner(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(timeout),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TTGPUError(
            timeout_error_code or error_code,
            "media command exceeded its execution budget",
            504 if timeout_error_code else 500,
        ) from None
    except (OSError, subprocess.SubprocessError):
        raise TTGPUError(error_code, "media command failed", 500) from None
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        raise TTGPUError(error_code, "media command failed", 500)
    return completed


def probe_media(config, path, runner=subprocess.run, deadline=None):
    command = [
        config.ffprobe_bin,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-print_format",
        "json",
        str(path),
    ]
    completed = _run_command(
        runner,
        command,
        (
            deadline.stage_timeout(config.probe_timeout)
            if deadline is not None
            else config.probe_timeout
        ),
        "media_probe_failed",
        timeout_error_code="prepare_timeout" if deadline is not None else None,
    )
    try:
        payload = json.loads(completed.stdout or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        raise TTGPUError(
            "media_probe_failed",
            "ffprobe returned invalid metadata",
            500,
        ) from None
    if not isinstance(payload, dict):
        raise TTGPUError(
            "media_probe_failed",
            "ffprobe returned invalid metadata",
            500,
        )
    return payload


def _stream_items(payload, kind):
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        return []
    return [
        item
        for item in streams
        if isinstance(item, dict)
        and str(item.get("codec_type") or "").lower() == kind
    ]


def _positive_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if result > 0 and result < float("inf") else 0.0


def inspect_input(payload, max_duration):
    videos = _stream_items(payload, "video")
    audios = _stream_items(payload, "audio")
    if len(videos) != 1 or len(audios) > 1:
        raise TTGPUError(
            "input_media_invalid",
            "input must contain exactly one video and at most one audio stream",
            400,
        )
    video = videos[0]
    try:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError, OverflowError):
        width = height = 0
    format_payload = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _positive_float(
        format_payload.get("duration") or video.get("duration")
    )
    if (
        width < 16
        or height < 16
        or width > 8192
        or height > 8192
        or duration < 0.5
        or duration > float(max_duration)
    ):
        raise TTGPUError(
            "input_media_invalid",
            "input media dimensions or duration are outside the contract",
            400,
        )
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "has_audio": bool(audios),
    }


def _frame_rate(value):
    try:
        result = float(Fraction(str(value or "0/1")))
    except (ValueError, ZeroDivisionError, OverflowError):
        return 0.0
    return result if result > 0 else 0.0


def validate_prepared_output(config, payload, path, max_size, expected_duration):
    video_contract = _delivery_video_contract(config)
    videos = _stream_items(payload, "video")
    audios = _stream_items(payload, "audio")
    if len(videos) != 1 or len(audios) != 1:
        raise TTGPUError(
            "prepared_media_invalid",
            "prepared media must contain one video and one audio stream",
            500,
        )
    video, audio = videos[0], audios[0]
    try:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        sample_rate = int(audio.get("sample_rate") or 0)
        channels = int(audio.get("channels") or 0)
    except (TypeError, ValueError, OverflowError):
        raise TTGPUError(
            "prepared_media_invalid",
            "prepared stream metadata is invalid",
            500,
        ) from None
    rate = _frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    format_payload = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _positive_float(
        format_payload.get("duration") or video.get("duration")
    )
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    profile = str(video.get("profile") or "").strip().lower()
    codec_tag = str(video.get("codec_tag_string") or "").strip().lower()
    audio_profile = str(audio.get("profile") or "").strip().lower()
    average_bitrate = size * 8.0 / duration if duration > 0 else math.inf
    if (
        str(video.get("codec_name") or "").lower() != video_contract["codec"]
        or profile != video_contract["profile"]
        or codec_tag != video_contract["codec_tag"]
        or str(video.get("pix_fmt") or "").lower() != "yuv420p"
        or (width, height) != (OUTPUT_WIDTH, OUTPUT_HEIGHT)
        or abs(rate - 30.0) > 0.01
        or str(audio.get("codec_name") or "").lower() != "aac"
        or audio_profile != "lc"
        or sample_rate != 48000
        or channels != 2
        or size <= 0
        or size > int(max_size)
        or duration <= 0
        or average_bitrate > MAX_DELIVERY_AVERAGE_BITRATE_BPS
        or abs(duration - float(expected_duration)) > max(
            1.0,
            float(expected_duration) * 0.02,
        )
    ):
        raise TTGPUError(
            "prepared_media_invalid",
            "prepared output does not match the TikTok media profile",
            500,
        )
    return {
        "audio_channels": channels,
        "audio_codec": "aac",
        "audio_profile": "lc",
        "audio_sample_rate": sample_rate,
        "duration": round(duration, 3),
        "frame_rate": 30.0,
        "height": height,
        "pixel_format": "yuv420p",
        "profile": video_contract["profile"],
        "size": size,
        "video_codec": video_contract["codec"],
        "video_codec_tag": video_contract["codec_tag"],
        "width": width,
    }


def _ffmpeg_filter_path(path):
    value = str(Path(path))
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
    )


def _base_video_filter():
    return (
        "scale=w=%d:h=%d:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1,fps=30,format=yuv420p"
    ) % (OUTPUT_WIDTH, OUTPUT_HEIGHT, OUTPUT_WIDTH, OUTPUT_HEIGHT)


def build_outro_filter(config, drama_id_text_path, tutorial_text_path):
    font = _ffmpeg_filter_path(config.font_file)
    drama_text = _ffmpeg_filter_path(drama_id_text_path)
    tutorial_text = _ffmpeg_filter_path(tutorial_text_path)
    return ",".join(
        [
            _base_video_filter(),
            "drawbox=x=28:y=40:w=664:h=114:color=black@0.78:t=fill",
            "drawbox=x=28:y=40:w=10:h=114:color=0xFF2E88@1.0:t=fill",
            (
                "drawtext=fontfile='%s':textfile='%s':"
                "fontcolor=white:fontsize=43:x=53:y=59"
            )
            % (font, drama_text),
            (
                "drawtext=fontfile='%s':textfile='%s':"
                "fontcolor=white:fontsize=20:x=(w-text_w)/2:y=h-95:"
                "box=1:boxcolor=black@0.72:boxborderw=12"
            )
            % (font, tutorial_text),
        ]
    )


def _delivery_video_contract(config):
    expected_profile = _selected_media_profile(
        config.video_encoder,
        config.media_mode,
    )
    if config.profile != expected_profile:
        raise TTGPUError(
            "invalid_configuration",
            "video encoder and media mode require their exact media profile",
            500,
        )
    if config.video_encoder == "hevc_nvenc":
        return {
            "codec": "hevc",
            "codec_tag": "hvc1",
            "profile": "main",
        }
    if config.video_encoder in {"h264_nvenc", "libx264"}:
        return {
            "codec": "h264",
            "codec_tag": "avc1",
            "profile": "high",
        }
    raise TTGPUError(
        "invalid_configuration",
        "unsupported TikTok delivery encoder",
        500,
    )


def _encoder_arguments(config):
    if config.video_encoder in {"hevc_nvenc", "h264_nvenc"}:
        is_hevc = config.video_encoder == "hevc_nvenc"
        return [
            "-c:v",
            config.video_encoder,
            "-preset",
            "p6",
            "-tune",
            "hq",
            "-profile:v",
            "main" if is_hevc else "high",
            "-rc",
            "vbr",
            "-b:v",
            HEVC_VIDEO_BITRATE if is_hevc else H264_VIDEO_BITRATE,
            "-maxrate",
            HEVC_VIDEO_MAXRATE if is_hevc else H264_VIDEO_MAXRATE,
            "-bufsize",
            HEVC_VIDEO_BUFSIZE if is_hevc else H264_VIDEO_BUFSIZE,
            "-multipass",
            "fullres",
            "-rc-lookahead",
            "32",
            "-spatial-aq",
            "1",
            "-temporal-aq",
            "1",
            "-aq-strength",
            "8",
            "-bf",
            "3",
            "-b_ref_mode",
            "middle",
            "-tag:v",
            "hvc1" if is_hevc else "avc1",
        ]
    if config.video_encoder == "libx264":
        return [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-profile:v",
            "high",
            "-level:v",
            "4.1",
            "-b:v",
            H264_VIDEO_BITRATE,
            "-maxrate",
            H264_VIDEO_MAXRATE,
            "-bufsize",
            H264_VIDEO_BUFSIZE,
            "-tag:v",
            "avc1",
        ]
    _delivery_video_contract(config)
    raise AssertionError("unreachable")


def build_normalize_command(
    config,
    input_path,
    output_path,
    input_info,
    video_filter,
    logo_path=None,
    output_duration=None,
):
    command = [
        config.ffmpeg_bin,
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
    ]
    if not input_info["has_audio"]:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
        )
    audio_map = "0:a:0" if input_info["has_audio"] else "1:a:0"
    if logo_path is not None:
        logo_input_index = 1 if input_info["has_audio"] else 2
        command.extend(["-loop", "1", "-i", str(logo_path)])
        command.extend(
            [
                "-filter_complex",
                (
                    "[0:v]%s[base];[%s:v]scale=132:132:flags=lanczos[logo];"
                    "[base][logo]overlay=48:72:format=auto[v]"
                )
                % (video_filter, logo_input_index),
                "-map",
                "[v]",
            ]
        )
    else:
        command.extend(["-map", "0:v:0", "-vf", video_filter])
    command.extend(
        [
            "-map",
            audio_map,
            "-af",
            "aresample=48000:async=1:first_pts=0,apad",
            "-shortest",
        ]
    )
    command.extend(_encoder_arguments(config))
    command.extend(
        [
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-flags",
            "+cgop",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            AUDIO_BITRATE,
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-movflags",
            "+faststart",
            "-max_muxing_queue_size",
            "2048",
        ]
    )
    if output_duration is not None:
        command.extend(["-t", "%.6f" % float(output_duration)])
    command.append(str(output_path))
    return command


def build_phone_match_command(
    config,
    source_path,
    outro_path,
    output_path,
    source_duration,
    outro_duration,
    transition_seconds=DEFAULT_TRANSITION_SECONDS,
    source_has_audio=True,
    logo_path=None,
):
    """Normalize the source once and overlap its ending into the playing outro."""

    transition = min(
        float(transition_seconds),
        max(0.1, float(source_duration) - 0.1),
        max(0.1, float(outro_duration) - 0.1),
    )
    transition_start = float(source_duration) - transition
    fade_start = max(0.0, transition - 0.25)
    source_duration_text = "%.6f" % float(source_duration)
    outro_duration_text = "%.6f" % float(outro_duration)
    transition_text = "%.6f" % transition
    transition_start_text = "%.6f" % transition_start
    fade_start_text = "%.6f" % fade_start
    scale_progress = "min(t/%s\\,1)" % transition_text
    source_audio_values = {
        "source_end": source_duration_text,
        "start": transition_start_text,
        "transition": transition_text,
    }
    if source_has_audio:
        source_audio = (
            "[0:a]aresample=48000:async=1:first_pts=0,apad,"
            "atrim=start=0:end=%(source_end)s,asetpts=PTS-STARTPTS,"
            "afade=t=out:st=%(start)s:d=%(transition)s[sa];"
        ) % source_audio_values
    else:
        source_audio = (
            "anullsrc=channel_layout=stereo:sample_rate=48000,"
            "atrim=start=0:end=%(source_end)s,asetpts=PTS-STARTPTS,"
            "afade=t=out:st=%(start)s:d=%(transition)s[sa];"
        ) % source_audio_values
    logo_path = Path(logo_path or config.logo_path)
    video_graph = (
        "[0:v]trim=start=0:end=%(source_end)s,setpts=PTS-STARTPTS,"
        "%(source_filter)s[base];"
        "[2:v]scale=132:132:flags=lanczos[logo];"
        "[base][logo]overlay=48:72:shortest=1:format=auto,"
        "format=yuv420p[source];"
        "[source]split=2[source_pre][source_bridge];"
        "[source_pre]trim=start=0:end=%(start)s,"
        "setpts=PTS-STARTPTS[pre];"
        "[source_bridge]trim=start=%(start)s:end=%(source_end)s,"
        "setpts=PTS-STARTPTS,"
        "scale=w='trunc((720-214*%(progress)s)/2)*2':"
        "h='trunc((1280-378*%(progress)s)/2)*2':"
        "eval=frame,format=rgba,"
        "fade=t=out:st=%(fade_start)s:d=0.250000:alpha=1[foreground];"
        "[1:v]trim=start=0:end=%(transition)s,setpts=PTS-STARTPTS[background];"
        "[background][foreground]overlay=x=(W-w)/2:y=(H-h)/2:"
        "shortest=1:format=auto,format=yuv420p[bridge];"
        "[1:v]trim=start=%(transition)s:end=%(outro_end)s,"
        "setpts=PTS-STARTPTS[post];"
        "[pre][bridge][post]concat=n=3:v=1:a=0[outv];"
        "%(source_audio)s"
        "[1:a]atrim=start=0:end=%(outro_end)s,asetpts=PTS-STARTPTS,"
        "adelay=%(delay)d|%(delay)d[oa];"
        "[sa][oa]amix=inputs=2:duration=longest:normalize=0,"
        "alimiter=limit=0.95[outa]"
    ) % {
        "delay": int(round(transition_start * 1000)),
        "fade_start": fade_start_text,
        "outro_end": outro_duration_text,
        "progress": scale_progress,
        "source_audio": source_audio,
        "source_end": source_duration_text,
        "source_filter": _base_video_filter(),
        "start": transition_start_text,
        "transition": transition_text,
    }
    command = [
        config.ffmpeg_bin,
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-i",
        str(outro_path),
        "-loop",
        "1",
        "-i",
        str(logo_path),
        "-filter_complex",
        video_graph,
        "-map",
        "[outv]",
        "-map",
        "[outa]",
    ]
    command.extend(_encoder_arguments(config))
    command.extend(
        [
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-flags",
            "+cgop",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            AUDIO_BITRATE,
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    return command


def _cos_status_code(exc):
    getter = getattr(exc, "get_status_code", None)
    if callable(getter):
        try:
            return int(getter())
        except (TypeError, ValueError, OverflowError):
            pass
    for name in ("status_code", "status", "code"):
        try:
            value = int(getattr(exc, name))
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if 100 <= value <= 599:
            return value
    return None


def _head_value(payload, *names):
    if not isinstance(payload, dict):
        return None
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    metadata = payload.get("Metadata") or payload.get("metadata")
    if isinstance(metadata, dict):
        lowered_meta = {str(key).lower(): value for key, value in metadata.items()}
        for name in names:
            simple = name.lower().replace("x-cos-meta-", "")
            if simple in lowered_meta:
                return lowered_meta[simple]
    return None


class CosObjectStore:
    """Content-addressed public COS adapter for TikTok PULL_FROM_URL."""

    def __init__(self, config, client=None):
        self.config = config
        self.client = client or self._create_client()

    def _create_client(self):
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError:
            raise TTGPUError(
                "cos_sdk_unavailable",
                "qcloud_cos is required by the TT GPU worker",
                500,
            ) from None
        cos_config = CosConfig(
            Region=self.config.cos_region,
            SecretId=self.config.cos_secret_id,
            SecretKey=self.config.cos_secret_key,
            Timeout=max(60, self.config.cos_timeout),
            KeepAlive=False,
        )
        # The SDK timeout is per HTTP request. Disable SDK retries so one
        # stalled part cannot silently multiply the prepare wall-clock budget.
        return CosS3Client(cos_config, retry=0)

    @staticmethod
    def _deadline_call(deadline, callback):
        if deadline is None:
            return callback()
        deadline.check()
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tt-cos-control",
        )
        future = executor.submit(callback)
        try:
            return future.result(timeout=deadline.remaining())
        except FutureTimeoutError:
            future.cancel()
            raise TTGPUError(
                "prepare_timeout",
                "GPU prepare exceeded the total execution budget",
                504,
            ) from None
        finally:
            # Never use the executor as a context manager here: its implicit
            # wait=True shutdown would defeat the outer wall-clock deadline.
            executor.shutdown(wait=False, cancel_futures=True)

    def head(self, key, deadline=None):
        try:
            return self._deadline_call(
                deadline,
                lambda: self.client.head_object(
                    Bucket=self.config.cos_bucket,
                    Key=key,
                ),
            )
        except TTGPUError:
            raise
        except Exception as exc:
            if _cos_status_code(exc) == 404:
                return None
            raise TTGPUError(
                "cos_head_failed",
                "COS object verification failed",
                502,
            ) from None

    @staticmethod
    def _head_matches(head, size, sha256_value):
        try:
            remote_size = int(_head_value(head, "Content-Length", "content_length"))
        except (TypeError, ValueError, OverflowError):
            return False
        remote_sha = str(
            _head_value(head, "x-cos-meta-sha256", "sha256") or ""
        ).strip().lower()
        return remote_size == int(size) and secrets.compare_digest(
            remote_sha,
            sha256_value,
        )

    def _upload_part_batch(
        self,
        key,
        upload_id,
        batch,
        deadline,
    ):
        executor = ThreadPoolExecutor(
            max_workers=len(batch),
            thread_name_prefix="tt-cos-part",
        )
        futures = [
            (
                part_number,
                executor.submit(
                    self._upload_part,
                    key,
                    upload_id,
                    part_number,
                    body,
                    deadline,
                ),
            )
            for part_number, body in batch
        ]
        completed = False
        try:
            parts = []
            for part_number, future in futures:
                try:
                    response = future.result(timeout=deadline.remaining())
                except FutureTimeoutError:
                    raise TTGPUError(
                        "prepare_timeout",
                        "GPU prepare exceeded the total execution budget",
                        504,
                    ) from None
                except TTGPUError:
                    raise
                except Exception:
                    raise TTGPUError(
                        "cos_upload_failed",
                        "COS upload failed",
                        502,
                    ) from None
                etag = str(_head_value(response, "ETag", "etag") or "")
                if not etag or len(etag) > 512:
                    raise TTGPUError(
                        "cos_upload_failed",
                        "COS upload returned an invalid part receipt",
                        502,
                    )
                parts.append(
                    {
                        "ETag": etag,
                        "PartNumber": int(part_number),
                    }
                )
            completed = True
            return parts
        finally:
            for _part_number, future in futures:
                if not completed:
                    future.cancel()
            # A timed-out SDK call may still be unwinding its socket. Returning
            # without waiting keeps the HTTP handler inside the total deadline;
            # at most COS_UPLOAD_THREADS calls can remain in flight.
            executor.shutdown(
                wait=completed,
                cancel_futures=not completed,
            )

    def _upload_part(
        self,
        key,
        upload_id,
        part_number,
        body,
        deadline,
    ):
        if deadline is not None:
            deadline.check()
            acquired = _COS_UPLOAD_SLOTS.acquire(
                timeout=deadline.remaining()
            )
        else:
            acquired = _COS_UPLOAD_SLOTS.acquire()
        if not acquired:
            raise TTGPUError(
                "prepare_timeout",
                "GPU prepare exceeded the total execution budget",
                504,
            )
        try:
            if deadline is not None:
                deadline.check()
            return self.client.upload_part(
                Bucket=self.config.cos_bucket,
                Key=key,
                Body=body,
                PartNumber=part_number,
                UploadId=upload_id,
                EnableMD5=True,
            )
        finally:
            _COS_UPLOAD_SLOTS.release()

    def _abort_async(self, key, upload_id):
        def abort():
            try:
                self.client.abort_multipart_upload(
                    Bucket=self.config.cos_bucket,
                    Key=key,
                    UploadId=upload_id,
                )
            except Exception:
                pass

        threading.Thread(
            target=abort,
            name="tt-cos-abort",
            daemon=True,
        ).start()

    def upload(
        self,
        key,
        local_path,
        sha256_value,
        size,
        deadline=None,
    ):
        existing = self.head(key, deadline=deadline)
        if existing is not None:
            if self._head_matches(existing, size, sha256_value):
                return True
            raise TTGPUError(
                "cos_object_conflict",
                "existing prepared object failed integrity verification",
                409,
            )
        upload_id = ""
        complete_started = False
        try:
            created = self._deadline_call(
                deadline,
                lambda: self.client.create_multipart_upload(
                    Bucket=self.config.cos_bucket,
                    Key=key,
                    ACL="public-read",
                    ContentType="video/mp4",
                    Metadata={
                        "x-cos-meta-sha256": sha256_value,
                        "x-cos-meta-profile": self.config.profile,
                    },
                ),
            )
            upload_id = str(
                _head_value(created, "UploadId", "upload_id") or ""
            )
            if not upload_id or len(upload_id) > 2048:
                raise TTGPUError(
                    "cos_upload_failed",
                    "COS upload did not return a valid multipart ID",
                    502,
                )
            completed_parts = []
            part_number = 1
            with Path(local_path).open("rb") as handle:
                while True:
                    if deadline is not None:
                        deadline.check()
                    batch = []
                    for _index in range(COS_UPLOAD_THREADS):
                        body = handle.read(COS_PART_SIZE_BYTES)
                        if not body:
                            break
                        batch.append((part_number, body))
                        part_number += 1
                    if not batch:
                        break
                    completed_parts.extend(
                        self._upload_part_batch(
                            key,
                            upload_id,
                            batch,
                            deadline or PrepareDeadline(
                                max(60, self.config.cos_timeout)
                            ),
                        )
                    )
            if not completed_parts:
                raise TTGPUError(
                    "cos_upload_failed",
                    "COS upload source was empty",
                    502,
                )
            complete_started = True
            try:
                self._deadline_call(
                    deadline,
                    lambda: self.client.complete_multipart_upload(
                        Bucket=self.config.cos_bucket,
                        Key=key,
                        UploadId=upload_id,
                        MultipartUpload={
                            "Part": sorted(
                                completed_parts,
                                key=lambda item: item["PartNumber"],
                            )
                        },
                    ),
                )
            except TTGPUError:
                # A timed-out Future cannot stop an in-flight complete call.
                # Its result is unknown, so aborting here could delete an
                # object that is about to become durable. The next idempotent
                # retry recovers through the content-addressed HEAD check.
                raise
            except Exception:
                recovered = None
                try:
                    recovered = self.head(key, deadline=deadline)
                except TTGPUError:
                    pass
                if not self._head_matches(
                    recovered,
                    size,
                    sha256_value,
                ):
                    raise TTGPUError(
                        "cos_complete_outcome_unknown",
                        "COS multipart completion outcome is unknown",
                        502,
                    ) from None
            upload_id = ""
        except TTGPUError:
            if upload_id and not complete_started:
                self._abort_async(key, upload_id)
            raise
        except Exception:
            if upload_id and not complete_started:
                self._abort_async(key, upload_id)
            raise TTGPUError("cos_upload_failed", "COS upload failed", 502) from None
        if not self._head_matches(
            self.head(key, deadline=deadline),
            size,
            sha256_value,
        ):
            raise TTGPUError(
                "cos_verification_failed",
                "uploaded prepared object failed integrity verification",
                502,
            )
        return False

    def url(self, key):
        quoted = "/".join(
            urllib.parse.quote(part, safe="") for part in key.split("/")
        )
        return self.config.cos_domain.rstrip("/") + "/" + quoted


class LocalMediaStore:
    """Private GPU blob store exposed only through the loopback media server."""

    backend = "local"

    def __init__(self, config):
        self.config = config
        self.root = _ensure_private_directory(config.work_root / "media")

    @staticmethod
    def key(job_id, sha256_value):
        if not JOB_ID_RE.fullmatch(str(job_id or "")):
            raise TTGPUError(
                "local_media_invalid",
                "local media job identity is invalid",
                500,
            )
        sha256_value = str(sha256_value or "").lower()
        if not HEX_64_RE.fullmatch(sha256_value):
            raise TTGPUError(
                "local_media_invalid",
                "local media fingerprint is invalid",
                500,
            )
        return "%s/%s.mp4" % (job_id, sha256_value)

    @staticmethod
    def _split_key(key):
        parts = str(key or "").split("/")
        if (
            len(parts) != 2
            or not JOB_ID_RE.fullmatch(parts[0])
            or not parts[1].endswith(".mp4")
            or not HEX_64_RE.fullmatch(parts[1][:-4])
        ):
            raise TTGPUError(
                "local_media_invalid",
                "local media key is invalid",
                500,
            )
        return parts[0], parts[1][:-4]

    def _path(self, key):
        job_id, sha256_value = self._split_key(key)
        return self.root / job_id / ("%s.mp4" % sha256_value)

    def _signature(self, job_id, sha256_value):
        if len(self.config.local_media_signing_key) != 32:
            raise TTGPUError(
                "invalid_configuration",
                "local media URL signing key is unavailable",
                500,
            )
        message = ("v1\n%s\n%s" % (job_id, sha256_value)).encode("ascii")
        return hmac.new(
            self.config.local_media_signing_key,
            message,
            hashlib.sha256,
        ).hexdigest()

    def url(self, key):
        job_id, sha256_value = self._split_key(key)
        signature = self._signature(job_id, sha256_value)
        components = (
            self.config.local_media_prefix.strip("/").split("/")
            + [job_id, sha256_value, signature + ".mp4"]
        )
        quoted = "/".join(
            urllib.parse.quote(part, safe="") for part in components
        )
        return self.config.local_media_origin.rstrip("/") + "/" + quoted

    def admit_prepare(self, required_bytes):
        try:
            free_bytes = int(shutil.disk_usage(self.root).free)
        except OSError:
            raise TTGPUError(
                "local_media_storage_unavailable",
                "GPU local media storage is unavailable",
                503,
            ) from None
        required_free = (
            int(self.config.local_min_free_bytes)
            + max(0, int(required_bytes))
        )
        if free_bytes < required_free:
            raise TTGPUError(
                "local_media_storage_full",
                "GPU local media storage cannot preserve the configured free-space reserve",
                507,
            )

    def verify(self, key, sha256_value, size, *, full_hash):
        path = self._path(key)
        try:
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or int(metadata.st_size) != int(size)
            ):
                raise OSError("unsafe local media")
            descriptor = _open_regular_readonly(path, size)
        except FileNotFoundError:
            return None
        except OSError:
            raise TTGPUError(
                "local_media_verification_failed",
                "GPU local media could not be verified",
                500,
            ) from None
        try:
            if full_hash:
                digest = hashlib.sha256()
                actual_size = 0
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        actual_size += len(chunk)
                actual_sha = digest.hexdigest()
            else:
                actual_sha = sha256_value
                actual_size = int(size)
        finally:
            os.close(descriptor)
        if full_hash:
            if (
                actual_size != int(size)
                or not secrets.compare_digest(actual_sha, sha256_value)
            ):
                raise TTGPUError(
                    "local_media_verification_failed",
                    "GPU local media failed integrity verification",
                    500,
                )
        return path

    def upload(
        self,
        key,
        local_path,
        sha256_value,
        size,
        deadline=None,
    ):
        if deadline is not None:
            deadline.check()
        target = self.verify(
            key,
            sha256_value,
            size,
            full_hash=True,
        )
        if target is not None:
            return True
        source = Path(local_path)
        try:
            source_metadata = source.lstat()
        except OSError:
            raise TTGPUError(
                "local_media_persist_failed",
                "prepared media is unavailable for local persistence",
                500,
            ) from None
        if (
            stat.S_ISLNK(source_metadata.st_mode)
            or not stat.S_ISREG(source_metadata.st_mode)
            or int(source_metadata.st_size) != int(size)
        ):
            raise TTGPUError(
                "local_media_persist_failed",
                "prepared media is unsafe for local persistence",
                500,
            )
        target = self._path(key)
        target_parent_existed = target.parent.exists()
        _ensure_private_directory(target.parent)
        if not target_parent_existed:
            _fsync_directory(self.root)
        try:
            os.replace(str(source), str(target))
            os.chmod(target, 0o600)
            _fsync_regular_file(target, size)
            _fsync_directory(target.parent)
        except OSError:
            raise TTGPUError(
                "local_media_persist_failed",
                "prepared media could not be persisted on GPU storage",
                500,
            ) from None
        if deadline is not None:
            deadline.check()
        if self.verify(
            key,
            sha256_value,
            size,
            full_hash=True,
        ) is None:
            raise TTGPUError(
                "local_media_verification_failed",
                "GPU local media failed integrity verification",
                500,
            )
        return False

    def resolve_request_path(self, request_path, manifests_root):
        expected_prefix = self.config.local_media_prefix.strip("/").split("/")
        parts = str(request_path or "").strip("/").split("/")
        if len(parts) != len(expected_prefix) + 3:
            return None
        if parts[: len(expected_prefix)] != expected_prefix:
            return None
        job_id, sha256_value, signed_name = parts[-3:]
        if (
            not JOB_ID_RE.fullmatch(job_id)
            or not HEX_64_RE.fullmatch(sha256_value)
            or not signed_name.endswith(".mp4")
        ):
            return None
        signature = signed_name[:-4]
        if not re.fullmatch(r"[0-9a-f]{64}", signature):
            return None
        expected = self._signature(job_id, sha256_value)
        if not secrets.compare_digest(signature, expected):
            return None
        manifest = _read_json(Path(manifests_root) / ("%s.json" % job_id))
        result = manifest.get("result") if isinstance(manifest, dict) else None
        storage = manifest.get("storage") if isinstance(manifest, dict) else None
        key = self.key(job_id, sha256_value)
        if (
            not isinstance(manifest, dict)
            or manifest.get("status") != "ready"
            or not isinstance(result, dict)
            or not isinstance(storage, dict)
            or storage.get("backend") != "local"
            or storage.get("key") != key
            or result.get("job_id") != job_id
            or result.get("output_sha256") != sha256_value
            or result.get("output_url") != self.url(key)
        ):
            return None
        try:
            size = int(result.get("output_size"))
        except (TypeError, ValueError, OverflowError):
            return None
        path = self.verify(key, sha256_value, size, full_hash=False)
        if path is None:
            return None
        return {
            "path": path,
            "sha256": sha256_value,
            "size": size,
        }

    def release(self, key, sha256_value, size):
        path = self.verify(
            key,
            sha256_value,
            size,
            full_hash=True,
        )
        if path is None:
            return True
        try:
            path.unlink()
            _fsync_directory(path.parent)
            try:
                path.parent.rmdir()
            except OSError:
                pass
            else:
                _fsync_directory(self.root)
        except OSError:
            raise TTGPUError(
                "local_media_release_failed",
                "GPU local media could not be released",
                500,
            ) from None
        return False


class TikTokContentPostingAPI:
    """Minimal no-redirect client for TikTok Content Posting API v2."""

    def __init__(self, opener=None, timeout=30):
        self.opener = opener or _NO_REDIRECT_OPENER
        self.timeout = max(1, min(int(timeout or 30), 120))

    def _request(self, path, token, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            TIKTOK_API_ORIGIN + path,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + str(token),
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise TTGPUError(
                        "tt_upstream_invalid",
                        "TikTok API response exceeded the limit",
                        502,
                    )
        except urllib.error.HTTPError as exc:
            http_status = int(getattr(exc, "code", 0) or 0)
            try:
                raw = exc.read(MAX_RESPONSE_BYTES)
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {}
            finally:
                exc.close()
            details = _upstream_error_details(data, http_status)
            if (
                http_status >= 500
                or http_status in {408, 409, 425, 429}
            ):
                raise TTGPUError(
                    "tt_upstream_unavailable",
                    _upstream_error_summary(
                        details,
                        unavailable=True,
                    ),
                    503,
                    details,
                ) from None
            raise TTGPUError(
                "tt_upstream_rejected",
                _upstream_error_summary(details),
                502,
                details,
            ) from None
        except TTGPUError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TTGPUError(
                "tt_upstream_unavailable",
                "TikTok API request outcome is unavailable",
                503,
            ) from None
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise TTGPUError(
                "tt_upstream_invalid",
                "TikTok API returned invalid JSON",
                502,
            ) from None
        if not isinstance(data, dict):
            raise TTGPUError(
                "tt_upstream_invalid",
                "TikTok API returned an invalid response",
                502,
            )
        error = data.get("error")
        log_id = _upstream_log_id(data)
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            if code and code.lower() != "ok":
                details = _upstream_error_details(data, 200)
                raise TTGPUError(
                    "tt_upstream_rejected",
                    _upstream_error_summary(details),
                    502,
                    details,
                )
        result = data.get("data") if isinstance(data.get("data"), dict) else {}
        return result, log_id

    def creator_info(self, token):
        data, log_id = self._request(TIKTOK_CREATOR_INFO_PATH, token, {})
        result = normalize_creator_info(data)
        result["log_id"] = log_id
        return result

    def initialize_video(self, token, post_info, video_url):
        data, log_id = self._request(
            TIKTOK_VIDEO_INIT_PATH,
            token,
            {
                "post_info": dict(post_info),
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "video_url": str(video_url),
                },
            },
        )
        publish_id = str(data.get("publish_id") or "")
        if not PUBLISH_ID_RE.fullmatch(publish_id):
            raise TTGPUError(
                "tt_upstream_invalid",
                "TikTok API did not return a valid publish ID",
                502,
            )
        return {"log_id": log_id, "publish_id": publish_id}

    def fetch_status(self, token, publish_id):
        data, log_id = self._request(
            TIKTOK_STATUS_FETCH_PATH,
            token,
            {"publish_id": str(publish_id)},
        )
        result = normalize_publish_status(data)
        result["log_id"] = log_id
        return result


def _upstream_error_code(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    code = str(error.get("code") or "") if isinstance(error, dict) else ""
    return code if SAFE_UPSTREAM_CODE_RE.fullmatch(code) else "http_error"


def _upstream_error_details(payload, http_status=0):
    error = payload.get("error") if isinstance(payload, dict) else None
    raw_message = (
        str(error.get("message") or "")
        if isinstance(error, dict)
        else ""
    )
    safe_message = _clean_message(raw_message, 300)
    details = {
        "upstream_code": _upstream_error_code(payload),
        "upstream_message": safe_message,
        "log_id": _upstream_log_id(payload),
        "message_redacted": bool(safe_message != raw_message[:300]),
        "received_at": _utc_now(),
    }
    try:
        normalized_status = int(http_status)
    except (TypeError, ValueError, OverflowError):
        normalized_status = 0
    if 100 <= normalized_status <= 599:
        details["upstream_http_status"] = normalized_status
    return details


def _upstream_error_summary(details, *, unavailable=False):
    prefix = (
        "TikTok API request outcome is unavailable"
        if unavailable
        else "TikTok API rejected the request"
    )
    parts = []
    http_status = details.get("upstream_http_status")
    if http_status:
        parts.append("HTTP %s" % http_status)
    code = str(details.get("upstream_code") or "")
    if code:
        parts.append("code=%s" % code)
    log_id = str(details.get("log_id") or "")
    if log_id:
        parts.append("log_id=%s" % log_id)
    message = str(details.get("upstream_message") or "")
    summary = prefix + ((" [" + ", ".join(parts) + "]") if parts else "")
    if message:
        summary += ": " + message
    return _clean_message(summary, 300)


def _upstream_log_id(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    value = str(error.get("log_id") or "") if isinstance(error, dict) else ""
    return value if SAFE_LOG_ID_RE.fullmatch(value) else ""


def normalize_creator_info(data):
    data = data if isinstance(data, dict) else {}
    privacy = data.get("privacy_level_options")
    privacy = [
        item
        for item in privacy
        if isinstance(item, str) and item in PRIVACY_LEVELS
    ] if isinstance(privacy, list) else []
    try:
        max_duration = int(data.get("max_video_post_duration_sec") or 0)
    except (TypeError, ValueError, OverflowError):
        max_duration = 0
    result = {
        "comment_disabled": bool(data.get("comment_disabled")),
        "creator_nickname": str(data.get("creator_nickname") or "")[:200],
        "creator_username": str(data.get("creator_username") or "")[:200],
        "duet_disabled": bool(data.get("duet_disabled")),
        "max_video_post_duration_sec": max(0, min(max_duration, 86400)),
        "privacy_level_options": privacy,
        "stitch_disabled": bool(data.get("stitch_disabled")),
    }
    log_id = str(data.get("log_id") or "")
    result["log_id"] = log_id if SAFE_LOG_ID_RE.fullmatch(log_id) else ""
    avatar = str(data.get("creator_avatar_url") or "")
    parsed = urllib.parse.urlsplit(avatar)
    if (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    ):
        result["creator_avatar_url"] = avatar[:2048]
    else:
        result["creator_avatar_url"] = ""
    return result


def normalize_publish_status(data):
    data = data if isinstance(data, dict) else {}
    status = str(data.get("status") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{1,80}", status):
        status = "UNKNOWN"
    fail_reason = str(data.get("fail_reason") or "").strip().upper()
    if fail_reason and not re.fullmatch(r"[A-Z0-9_]{1,120}", fail_reason):
        fail_reason = "UPSTREAM_FAILURE"
    post_ids = data.get("publicaly_available_post_id")
    if not isinstance(post_ids, list):
        post_ids = data.get("publicly_available_post_id")
    safe_ids = []
    if isinstance(post_ids, list):
        for value in post_ids[:20]:
            item = str(value or "")
            if re.fullmatch(r"[A-Za-z0-9._~:-]{1,256}", item):
                safe_ids.append(item)
    result = {
        "fail_reason": fail_reason,
        "post_ids": safe_ids,
        "status": status,
    }
    log_id = str(data.get("log_id") or "")
    result["log_id"] = log_id if SAFE_LOG_ID_RE.fullmatch(log_id) else ""
    for field in ("downloaded_bytes", "uploaded_bytes"):
        try:
            value = int(data.get(field))
        except (TypeError, ValueError, OverflowError):
            continue
        if value >= 0:
            result[field] = value
    return result


def _exact_fields(payload, allowed, required=None):
    if not isinstance(payload, dict):
        raise TTGPUError("invalid_request", "request body must be an object")
    required = set(allowed if required is None else required)
    keys = set(payload)
    if keys - set(allowed) or required - keys:
        raise TTGPUError("invalid_request", "request fields do not match the contract")
    return payload


def _job_id(value):
    value = str(value or "").strip()
    if not JOB_ID_RE.fullmatch(value):
        raise TTGPUError("invalid_request", "job_id is invalid")
    return value


def _account_id(value):
    value = str(value or "").strip()
    if not ACCOUNT_ID_RE.fullmatch(value):
        raise TTGPUError("invalid_request", "source_account_id is invalid")
    return value


def validate_prepare_request(payload, config):
    payload = _exact_fields(
        payload,
        PREPARE_REQUIRED_FIELDS | PREPARE_OPTIONAL_FIELDS,
        PREPARE_REQUIRED_FIELDS,
    )
    content_id = str(payload.get("content_id") or "").strip()
    if not CONTENT_ID_RE.fullmatch(content_id):
        raise TTGPUError("invalid_request", "content_id is invalid")
    expected_profile = str(payload.get("expected_profile") or "").strip()
    if not secrets.compare_digest(expected_profile, config.profile):
        raise TTGPUError(
            "prepare_profile_mismatch",
            "requested media profile does not match the GPU worker",
            409,
        )
    result = {
        "content_id": content_id,
        "expected_profile": expected_profile,
        "job_id": _job_id(payload.get("job_id")),
        "source_url": validate_source_url(
            payload.get("source_url"),
            config.allowed_source_hosts,
        ),
    }
    if "source_sha256" in payload:
        source_sha = str(payload.get("source_sha256") or "").strip().lower()
        if not HEX_64_RE.fullmatch(source_sha):
            raise TTGPUError("invalid_request", "source_sha256 is invalid")
        result["source_sha256"] = source_sha
    if "source_size" in payload:
        try:
            source_size = int(payload.get("source_size"))
        except (TypeError, ValueError, OverflowError):
            source_size = 0
        if source_size <= 0 or source_size > int(config.max_source_bytes):
            raise TTGPUError("invalid_request", "source_size is outside the contract")
        result["source_size"] = source_size
    trim_value = payload.get(
        "source_trim_tail_seconds",
        config.default_source_trim_tail_seconds,
    )
    try:
        trim_seconds = float(trim_value)
    except (TypeError, ValueError, OverflowError):
        trim_seconds = -1.0
    if (
        trim_seconds < 0.0
        or trim_seconds > 60.0
        or trim_seconds == float("inf")
    ):
        raise TTGPUError(
            "invalid_request",
            "source_trim_tail_seconds is outside the contract",
        )
    result["source_trim_tail_seconds"] = round(trim_seconds, 6)
    return result


def validate_credential_request(payload, allowed_fields=CREATOR_INFO_FIELDS):
    payload = _exact_fields(payload, allowed_fields)
    envelope = str(payload.get("credential_envelope") or "").strip()
    if not envelope or len(envelope.encode("utf-8")) > 16 * 1024:
        raise TTGPUError("invalid_request", "credential_envelope is invalid")
    return {
        "credential_envelope": envelope,
        "job_id": _job_id(payload.get("job_id")),
        "source_account_id": _account_id(payload.get("source_account_id")),
    }


def validate_publish_request(payload):
    payload = _exact_fields(
        payload,
        PUBLISH_REQUIRED_FIELDS | PUBLISH_OPTIONAL_FIELDS,
        PUBLISH_REQUIRED_FIELDS,
    )
    result = validate_credential_request(
        {
            key: payload[key]
            for key in CREATOR_INFO_FIELDS
        }
    )
    title = str(payload.get("title") or "")
    try:
        title_units = len(title.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        title_units = 2201
    if not title.strip() or title_units > 2200 or "\x00" in title:
        raise TTGPUError("invalid_request", "title is invalid")
    privacy = str(payload.get("privacy_level") or "").strip()
    if privacy not in PRIVACY_LEVELS:
        raise TTGPUError("invalid_request", "privacy_level is invalid")
    result.update(
        {
            "disable_comment": _required_bool(payload, "disable_comment"),
            "disable_duet": _required_bool(payload, "disable_duet"),
            "disable_stitch": _required_bool(payload, "disable_stitch"),
            "privacy_level": privacy,
            "title": title,
        }
    )
    for field in ("brand_content_toggle", "brand_organic_toggle", "is_aigc"):
        if field in payload:
            result[field] = _required_bool(payload, field)
    if "manual_canary_id" in payload:
        canary_id = str(payload.get("manual_canary_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,80}", canary_id):
            raise TTGPUError(
                "invalid_request",
                "manual_canary_id is invalid",
            )
        result["manual_canary_id"] = canary_id
    if "material_id" in payload:
        material_id = str(payload.get("material_id") or "").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,18}", material_id):
            raise TTGPUError(
                "invalid_request",
                "material_id is invalid",
            )
        result["material_id"] = material_id
    if "video_cover_timestamp_ms" in payload:
        try:
            cover = int(payload["video_cover_timestamp_ms"])
        except (TypeError, ValueError, OverflowError):
            cover = -1
        if cover < 0 or cover > 86_400_000:
            raise TTGPUError(
                "invalid_request",
                "video_cover_timestamp_ms is invalid",
            )
        result["video_cover_timestamp_ms"] = cover
    return result


def _required_bool(payload, name):
    value = payload.get(name)
    if not isinstance(value, bool):
        raise TTGPUError("invalid_request", "%s must be boolean" % name)
    return value


def _stable_hash(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_response(manifest, reused, config, expected_job_id):
    video_contract = _delivery_video_contract(config)
    result = manifest.get("result") if isinstance(manifest, dict) else None
    if not isinstance(result, dict):
        raise TTGPUError("manifest_invalid", "prepare manifest is invalid", 500)
    required = {
        "content_id",
        "job_id",
        "output_sha256",
        "output_size",
        "output_url",
        "probe",
        "profile",
    }
    if not required.issubset(result):
        raise TTGPUError("manifest_invalid", "prepare manifest is incomplete", 500)
    probe = result.get("probe")
    stored_request = manifest.get("request")
    try:
        output_size = int(result.get("output_size"))
        probe_size = int(probe.get("size")) if isinstance(probe, dict) else 0
        duration = (
            float(probe.get("duration")) if isinstance(probe, dict) else 0.0
        )
        frame_rate = (
            float(probe.get("frame_rate")) if isinstance(probe, dict) else 0.0
        )
        width = int(probe.get("width")) if isinstance(probe, dict) else 0
        height = int(probe.get("height")) if isinstance(probe, dict) else 0
        audio_channels = (
            int(probe.get("audio_channels"))
            if isinstance(probe, dict)
            else 0
        )
        audio_sample_rate = (
            int(probe.get("audio_sample_rate"))
            if isinstance(probe, dict)
            else 0
        )
    except (TypeError, ValueError, OverflowError):
        raise TTGPUError(
            "prepared_media_invalid",
            "stored prepared media metadata is invalid",
            500,
        ) from None
    output_sha_raw = str(result.get("output_sha256") or "").strip()
    output_sha = output_sha_raw.lower()
    output_url = str(result.get("output_url") or "").strip()
    storage = manifest.get("storage")
    if isinstance(storage, dict):
        storage_backend = str(storage.get("backend") or "")
        storage_key = str(storage.get("key") or "")
    else:
        # Version 1 manifests predate the backend discriminator and are COS.
        storage_backend = "cos"
        storage_key = str(manifest.get("cos_key") or "")
    if storage_backend == "local":
        expected_storage_key = LocalMediaStore.key(
            expected_job_id,
            output_sha,
        )
        local_store = LocalMediaStore(config)
        expected_output_url = local_store.url(expected_storage_key)
    elif storage_backend == "cos":
        expected_storage_key = "%s/%s/%s.mp4" % (
            config.cos_prefix.strip("/"),
            output_sha[:2],
            output_sha,
        )
        expected_output_url = "%s/%s" % (
            config.cos_domain.rstrip("/"),
            expected_storage_key,
        )
    else:
        expected_storage_key = ""
        expected_output_url = ""
    request_content_id = (
        str(stored_request.get("content_id") or "")
        if isinstance(stored_request, dict)
        else ""
    )
    request_media_mode = (
        str(stored_request.get("media_mode") or "")
        if isinstance(stored_request, dict)
        else ""
    )
    expected_brand_review = config.brand_overlay_review_required()
    expected_direct_eligible = config.direct_post_eligible()
    request_contract_match = True
    response_assets = None
    if config.media_mode == DIRECT_OUTRO_MEDIA_MODE:
        try:
            current_outro_sha, current_outro_size = _file_sha256(
                config.fixed_outro_path
            )
            current_logo_sha, current_logo_size = _file_sha256(
                config.logo_path
            )
            request_source_size = int(stored_request.get("source_size"))
            request_trim = float(
                stored_request.get("source_trim_tail_seconds")
            )
        except OSError:
            raise TTGPUError(
                "prepared_media_invalid",
                "current Direct Post outro assets are unavailable",
                500,
            ) from None
        except (AttributeError, TypeError, ValueError, OverflowError):
            request_source_size = 0
            request_trim = -1.0
        expected_assets = {
            "logo_sha256": current_logo_sha,
            "logo_size": current_logo_size,
            "outro_sha256": current_outro_sha,
            "outro_size": current_outro_size,
        }
        response_assets = dict(expected_assets)
        request_source_sha = str(
            stored_request.get("source_sha256") or ""
        ) if isinstance(stored_request, dict) else ""
        request_source_url_sha = str(
            stored_request.get("source_url_sha256") or ""
        ) if isinstance(stored_request, dict) else ""
        request_contract_match = bool(
            isinstance(stored_request, dict)
            and manifest.get("version") == 4
            and HEX_64_RE.fullmatch(config.fixed_outro_sha256)
            and HEX_64_RE.fullmatch(config.logo_sha256)
            and secrets.compare_digest(
                current_outro_sha,
                config.fixed_outro_sha256,
            )
            and secrets.compare_digest(
                current_logo_sha,
                config.logo_sha256,
            )
            and stored_request.get("transition")
            == config.preparation_transition()
            and stored_request.get("profile") == config.profile
            and HEX_64_RE.fullmatch(request_source_url_sha)
            and HEX_64_RE.fullmatch(request_source_sha)
            and request_source_size > 0
            and request_source_size <= int(config.max_source_bytes)
            and math.isfinite(request_trim)
            and 0.0 <= request_trim <= 60.0
            and all(
                stored_request.get(key) == value
                for key, value in expected_assets.items()
            )
        )
    if (
        str(result.get("job_id") or "") != str(expected_job_id or "")
        or not CONTENT_ID_RE.fullmatch(request_content_id)
        or str(result.get("content_id") or "") != request_content_id
        or not CONTENT_ID_RE.fullmatch(str(result.get("content_id") or ""))
        or (
            config.media_mode == BRANDED_PREVIEW_MEDIA_MODE
            and request_media_mode
            not in {"", BRANDED_PREVIEW_MEDIA_MODE}
        )
        or (
            config.media_mode != BRANDED_PREVIEW_MEDIA_MODE
            and request_media_mode != config.media_mode
        )
        or not request_contract_match
        or str(result.get("profile") or "") != str(config.profile)
        or (
            result.get("brand_overlay_review_required")
            is not expected_brand_review
        )
        or (
            result.get("direct_post_eligible")
            is not expected_direct_eligible
        )
        or not HEX_64_RE.fullmatch(output_sha)
        or output_sha_raw != output_sha
        or output_size <= 0
        or output_size > int(config.max_output_bytes)
        or probe_size != output_size
        or not math.isfinite(duration)
        or duration <= 0
        or (
            output_size * 8.0 / duration
            > MAX_DELIVERY_AVERAGE_BITRATE_BPS
        )
        or duration > float(config.max_duration_seconds)
        or not math.isfinite(frame_rate)
        or abs(frame_rate - 30.0) > 0.01
        or (width, height) != (OUTPUT_WIDTH, OUTPUT_HEIGHT)
        or (
            str(probe.get("video_codec") or "").lower()
            != video_contract["codec"]
        )
        or (
            str(probe.get("video_codec_tag") or "").lower()
            != video_contract["codec_tag"]
        )
        or (
            str(probe.get("profile") or "").lower()
            != video_contract["profile"]
        )
        or str(probe.get("pixel_format") or "").lower() != "yuv420p"
        or str(probe.get("audio_codec") or "").lower() != "aac"
        or str(probe.get("audio_profile") or "").lower() != "lc"
        or audio_channels != 2
        or audio_sample_rate != 48000
        or storage_key != expected_storage_key
        or output_url != expected_output_url
    ):
        raise TTGPUError(
            "prepared_media_invalid",
            "stored prepared media does not match the current contract",
            500,
        )
    if storage_backend == "local":
        if (
            local_store.verify(
                storage_key,
                output_sha,
                output_size,
                full_hash=True,
            )
            is None
        ):
            raise TTGPUError(
                "prepared_artifact_not_found",
                "prepared GPU media is no longer available",
                409,
            )
    response = {
        "brand_overlay_review_required": expected_brand_review,
        "content_id": result["content_id"],
        "direct_post_eligible": expected_direct_eligible,
        "job_id": result["job_id"],
        "media_mode": config.media_mode,
        "output_sha256": result["output_sha256"],
        "output_size": result["output_size"],
        "output_url": result["output_url"],
        "probe": result["probe"],
        "profile": result["profile"],
        "reused": bool(reused),
        "storage_backend": storage_backend,
        "status": "ready",
        "transition": config.preparation_transition(),
    }
    if response_assets is not None:
        response["assets"] = response_assets
    return response


class TTPostGPUProcessor:
    """Synchronous, idempotent compositor and Direct Post coordinator."""

    def __init__(
        self,
        config,
        *,
        runner=None,
        downloader=None,
        object_store=None,
        tiktok_api=None,
        monotonic_fn=None,
    ):
        self.config = config
        self.runner = runner or subprocess.run
        self.downloader = downloader or download_source
        self._object_store_instance = object_store
        self._local_media_store_instance = (
            object_store if isinstance(object_store, LocalMediaStore) else None
        )
        self.tiktok_api = tiktok_api or TikTokContentPostingAPI()
        self._monotonic_fn = monotonic_fn or time.monotonic
        self._prepare_slot = threading.Lock()
        self._gpu_slot = threading.Lock()
        self._cleanup_state_lock = threading.Lock()
        self._cleanup_state = {"status": "not_started"}
        self.manifest_root = _ensure_private_directory(config.work_root / "manifests")
        self.publish_root = _ensure_private_directory(config.work_root / "publishes")
        self.lock_root = _ensure_private_directory(config.work_root / "locks")
        self.jobs_root = _ensure_private_directory(config.work_root / "jobs")

    def record_cleanup_state(self, result=None, failed=False):
        state = {
            "last_run_at": _utc_now(),
            "status": "failed" if failed else "ok",
        }
        if isinstance(result, dict):
            for key in ("failed", "released", "scanned"):
                try:
                    state[key] = max(0, int(result.get(key, 0)))
                except (TypeError, ValueError, OverflowError):
                    state[key] = 0
        with self._cleanup_state_lock:
            self._cleanup_state = state

    def storage_health(self):
        state = {
            "backend": self.config.storage_backend,
            "local_origin_enabled": bool(
                self.config.local_media_origin
                and len(self.config.local_media_signing_key) == 32
            ),
        }
        if state["local_origin_enabled"] or self.config.storage_backend == "local":
            try:
                usage = shutil.disk_usage(self.config.work_root)
                required = (
                    int(self.config.local_min_free_bytes)
                    + int(self.config.max_source_bytes)
                    + int(self.config.max_output_bytes)
                    + LOCAL_PREPARE_OVERHEAD_BYTES
                )
                state.update(
                    {
                        "free_bytes": int(usage.free),
                        "local_prepare_admission_ready": bool(
                            int(usage.free) >= required
                        ),
                        "next_prepare_required_free_bytes": required,
                        "reserve_bytes": int(
                            self.config.local_min_free_bytes
                        ),
                        "total_bytes": int(usage.total),
                    }
                )
            except OSError:
                state["local_prepare_admission_ready"] = False
        with self._cleanup_state_lock:
            state["cleanup"] = dict(self._cleanup_state)
        return state

    def _object_store(self):
        if self._object_store_instance is None:
            if self.config.storage_backend == "local":
                self._object_store_instance = self._local_media_store()
            else:
                self._object_store_instance = CosObjectStore(self.config)
        return self._object_store_instance

    def _local_media_store(self):
        if self._local_media_store_instance is None:
            self._local_media_store_instance = LocalMediaStore(self.config)
        return self._local_media_store_instance

    def _prepare_manifest_path(self, job_id):
        return self.manifest_root / ("%s.json" % job_id)

    def _publish_ledger_path(self, job_id):
        return self.publish_root / ("%s.json" % job_id)

    def _lock_path(self, job_id):
        return self.lock_root / ("%s.lock" % job_id)

    def _storage_key(self, job_id, output_sha):
        if self.config.storage_backend == "local":
            return LocalMediaStore.key(job_id, output_sha)
        return "%s/%s/%s.mp4" % (
            self.config.cos_prefix,
            output_sha[:2],
            output_sha,
        )

    def _release_terminal_media_locked(self, job_id, ledger, ledger_path):
        if str(ledger.get("state") or "") not in {"published", "failed"}:
            return None
        manifest = _read_json(self._prepare_manifest_path(job_id))
        storage = manifest.get("storage") if isinstance(manifest, dict) else None
        result = manifest.get("result") if isinstance(manifest, dict) else None
        if (
            not isinstance(storage, dict)
            or storage.get("backend") != "local"
            or not isinstance(result, dict)
        ):
            return None
        output_sha = str(result.get("output_sha256") or "")
        try:
            output_size = int(result.get("output_size"))
            expected_key = LocalMediaStore.key(job_id, output_sha)
        except (TTGPUError, TypeError, ValueError, OverflowError):
            expected_key = ""
            output_size = 0
        if (
            ledger.get("job_id") != job_id
            or result.get("job_id") != job_id
            or storage.get("key") != expected_key
            or output_size <= 0
        ):
            release = {
                "error": "manifest_invalid",
                "last_attempt_at": _utc_now(),
                "reason": str(ledger.get("state") or ""),
                "state": "release_failed",
            }
            ledger["media_release"] = release
            _atomic_write_json(ledger_path, ledger)
            return dict(release)
        release = ledger.get("media_release")
        if not isinstance(release, dict):
            release = {
                "reason": str(ledger.get("state") or ""),
                "release_after_epoch": int(time.time())
                + int(self.config.terminal_media_grace_seconds),
                "state": "pending",
            }
            ledger["media_release"] = release
            _atomic_write_json(ledger_path, ledger)
        if release.get("state") == "released":
            return dict(release)
        try:
            release_after = int(release.get("release_after_epoch"))
        except (TypeError, ValueError, OverflowError):
            release_after = int(time.time()) + int(
                self.config.terminal_media_grace_seconds
            )
            release["release_after_epoch"] = release_after
            release["state"] = "pending"
            _atomic_write_json(ledger_path, ledger)
        if int(time.time()) < release_after:
            return dict(release)
        try:
            storage_key = str(storage.get("key") or "")
            self._local_media_store().release(
                storage_key,
                output_sha,
                output_size,
            )
        except TTGPUError as exc:
            release.update(
                {
                    "error": exc.code,
                    "last_attempt_at": _utc_now(),
                    "state": "release_failed",
                }
            )
            _atomic_write_json(ledger_path, ledger)
            return dict(release)
        release.update(
            {
                "released_at": _utc_now(),
                "state": "released",
            }
        )
        release.pop("error", None)
        _atomic_write_json(ledger_path, ledger)
        return dict(release)

    def cleanup_due_media(self):
        if (
            not self.config.local_media_origin
            or len(self.config.local_media_signing_key) != 32
        ):
            return {"failed": 0, "released": 0, "scanned": 0}
        counts = {"failed": 0, "released": 0, "scanned": 0}
        for ledger_path in sorted(self.publish_root.glob("*.json")):
            job_id = ledger_path.stem
            if not JOB_ID_RE.fullmatch(job_id):
                continue
            counts["scanned"] += 1
            with _job_lock(self._lock_path(job_id)):
                ledger = _read_json(ledger_path)
                if not isinstance(ledger, dict):
                    continue
                before = (
                    dict(ledger.get("media_release"))
                    if isinstance(ledger.get("media_release"), dict)
                    else None
                )
                release = self._release_terminal_media_locked(
                    job_id,
                    ledger,
                    ledger_path,
                )
                if release and release.get("state") == "released":
                    if not before or before.get("state") != "released":
                        counts["released"] += 1
                elif release and release.get("state") == "release_failed":
                    counts["failed"] += 1
        return counts

    def creator_info(self, payload):
        request = validate_credential_request(payload)
        try:
            with open_access_token(
                request["credential_envelope"],
                self.config.credential_seal_key,
                job_id=request["job_id"],
                source_account_id=request["source_account_id"],
                operation="creator_info",
                max_ttl_seconds=self.config.credential_max_ttl_seconds,
            ) as token:
                result = self.tiktok_api.creator_info(token)
        except CredentialEnvelopeError as exc:
            raise TTGPUError(exc.code, str(exc), exc.status) from None
        return {
            "creator_info": normalize_creator_info(result),
            "job_id": request["job_id"],
            "source_account_id": request["source_account_id"],
            "status": "ok",
        }

    def prepare(self, payload):
        request = validate_prepare_request(payload, self.config)
        job_id = request["job_id"]
        deadline = PrepareDeadline(
            self.config.prepare_total_timeout,
            self._monotonic_fn,
        )
        with _job_lock(self._lock_path(job_id), deadline=deadline):
            return self._prepare_locked(request, deadline)

    def _prepared_reuse(
        self,
        manifest_path,
        reuse_contract,
        job_id,
        deadline,
    ):
        existing = _read_json(manifest_path)
        if existing is None:
            return None
        stored_request = existing.get("request")
        if (
            existing.get("status") == "ready"
            and isinstance(stored_request, dict)
            and all(
                stored_request.get(key) == value
                for key, value in reuse_contract.items()
            )
        ):
            deadline.check()
            return _prepare_response(
                existing,
                True,
                self.config,
                job_id,
            )
        raise TTGPUError(
            "prepare_idempotency_conflict",
            "job_id already belongs to a different prepared artifact",
            409,
        )

    def _prepare_locked(self, request, deadline):
        job_id = request["job_id"]
        manifest_path = self._prepare_manifest_path(job_id)
        reuse_contract = {
            "content_id": request["content_id"],
            "profile": self.config.profile,
            "source_trim_tail_seconds": request[
                "source_trim_tail_seconds"
            ],
            "source_url_sha256": hashlib.sha256(
                request["source_url"].encode("utf-8")
            ).hexdigest(),
        }
        if self.config.media_mode != BRANDED_PREVIEW_MEDIA_MODE:
            reuse_contract["media_mode"] = self.config.media_mode
        if self.config.media_mode == DIRECT_OUTRO_MEDIA_MODE:
            reuse_contract["transition"] = (
                self.config.preparation_transition()
            )
            for key in ("source_sha256", "source_size"):
                if key in request:
                    reuse_contract[key] = request[key]
        if self.config.uses_outro_pipeline():
            outro_sha, outro_size = _file_sha256(
                self.config.fixed_outro_path,
                deadline=deadline,
            )
            logo_sha, logo_size = _file_sha256(
                self.config.logo_path,
                deadline=deadline,
            )
            reuse_contract.update(
                {
                    "logo_sha256": logo_sha,
                    "logo_size": logo_size,
                    "outro_sha256": outro_sha,
                    "outro_size": outro_size,
                }
            )
        reused = self._prepared_reuse(
            manifest_path,
            reuse_contract,
            job_id,
            deadline,
        )
        if reused is not None:
            return reused
        acquired_prepare = self._prepare_slot.acquire(
            timeout=deadline.remaining()
        )
        if not acquired_prepare:
            raise TTGPUError(
                "prepare_timeout",
                "GPU prepare exceeded the total execution budget",
                504,
            )
        try:
            reused = self._prepared_reuse(
                manifest_path,
                reuse_contract,
                job_id,
                deadline,
            )
            if reused is not None:
                return reused
            return self._prepare_new_locked(
                request,
                deadline,
                reuse_contract,
                manifest_path,
            )
        finally:
            self._prepare_slot.release()

    def _prepare_new_locked(
        self,
        request,
        deadline,
        reuse_contract,
        manifest_path,
    ):
        job_id = request["job_id"]
        if self.config.storage_backend == "local":
            self._object_store().admit_prepare(
                int(self.config.max_source_bytes)
                + int(self.config.max_output_bytes)
                + LOCAL_PREPARE_OVERHEAD_BYTES
            )
        job_dir = Path(
            tempfile.mkdtemp(prefix=job_id + ".", dir=str(self.jobs_root))
        )
        os.chmod(job_dir, 0o700)
        source_path = job_dir / "source.mp4"
        outro_normalized = job_dir / "outro-normalized.mp4"
        output_path = job_dir / "prepared.mp4"
        drama_text_path = job_dir / "drama-id.txt"
        tutorial_text_path = job_dir / "tutorial-label.txt"
        outro_input_path = self.config.fixed_outro_path
        logo_input_path = self.config.logo_path
        local_orphan = None
        try:
            if self.config.media_mode == DIRECT_OUTRO_MEDIA_MODE:
                outro_input_path = job_dir / "approved-outro.mp4"
                logo_input_path = job_dir / "approved-logo.png"
                deadline.check()
                try:
                    shutil.copyfile(
                        self.config.fixed_outro_path,
                        outro_input_path,
                    )
                    shutil.copyfile(
                        self.config.logo_path,
                        logo_input_path,
                    )
                except OSError:
                    raise TTGPUError(
                        "direct_outro_asset_unavailable",
                        "approved Direct Post outro assets are unavailable",
                        500,
                    ) from None
                snapshot_outro_sha, snapshot_outro_size = _file_sha256(
                    outro_input_path,
                    deadline=deadline,
                )
                snapshot_logo_sha, snapshot_logo_size = _file_sha256(
                    logo_input_path,
                    deadline=deadline,
                )
                if (
                    not secrets.compare_digest(
                        snapshot_outro_sha,
                        self.config.fixed_outro_sha256,
                    )
                    or not secrets.compare_digest(
                        snapshot_logo_sha,
                        self.config.logo_sha256,
                    )
                    or snapshot_outro_sha
                    != reuse_contract.get("outro_sha256")
                    or snapshot_outro_size
                    != reuse_contract.get("outro_size")
                    or snapshot_logo_sha != reuse_contract.get("logo_sha256")
                    or snapshot_logo_size != reuse_contract.get("logo_size")
                ):
                    raise TTGPUError(
                        "direct_outro_asset_mismatch",
                        (
                            "Direct Post outro assets do not match approved "
                            "fingerprints"
                        ),
                        500,
                    )
                os.chmod(outro_input_path, 0o400)
                os.chmod(logo_input_path, 0o400)
            if self.config.uses_outro_pipeline():
                drama_text_path.write_text(
                    "DRAMA ID: %s" % request["content_id"],
                    encoding="utf-8",
                )
                tutorial_text_path.write_text(
                    "TUTORIAL EXAMPLE  -  Follow the Drama ID shown above",
                    encoding="utf-8",
                )
                os.chmod(drama_text_path, 0o600)
                os.chmod(tutorial_text_path, 0o600)
            source_actual = self.downloader(
                request["source_url"],
                source_path,
                request.get("source_sha256"),
                request.get("source_size"),
                self.config,
                deadline,
            )
            if not isinstance(source_actual, dict):
                source_sha, source_size = _file_sha256(
                    source_path,
                    deadline=deadline,
                )
                source_actual = {"sha256": source_sha, "size": source_size}
            source_sha = str(source_actual.get("sha256") or "").lower()
            try:
                source_size = int(source_actual.get("size"))
            except (TypeError, ValueError, OverflowError):
                source_size = 0
            if (
                not HEX_64_RE.fullmatch(source_sha)
                or source_size <= 0
                or source_size > int(self.config.max_source_bytes)
            ):
                raise TTGPUError(
                    "source_integrity_mismatch",
                    "GPU source fingerprint is invalid",
                    500,
                )
            request_fingerprint = {
                **reuse_contract,
                "source_sha256": source_sha,
                "source_size": source_size,
                "transition": self.config.preparation_transition(),
            }
            source_probe = probe_media(
                self.config,
                source_path,
                self.runner,
                deadline=deadline,
            )
            source_info = inspect_input(
                source_probe,
                self.config.max_duration_seconds,
            )
            effective_source_duration = (
                source_info["duration"]
                - request["source_trim_tail_seconds"]
            )
            if effective_source_duration < 1.0:
                raise TTGPUError(
                    "source_trim_invalid",
                    "source trim would remove the complete usable material",
                    400,
                )
            if not self.config.uses_outro_pipeline():
                outro_info = None
                expected_duration = effective_source_duration
            else:
                outro_probe = probe_media(
                    self.config,
                    outro_input_path,
                    self.runner,
                    deadline=deadline,
                )
                outro_info = inspect_input(
                    outro_probe,
                    min(self.config.max_duration_seconds, 120),
                )
                expected_duration = (
                    effective_source_duration
                    + outro_info["duration"]
                    - DEFAULT_TRANSITION_SECONDS
                )
            if expected_duration > float(self.config.max_duration_seconds):
                raise TTGPUError(
                    "prepared_duration_exceeded",
                    "prepared media exceeds the configured duration",
                    400,
                )
            deadline.check()
            acquired_gpu = self._gpu_slot.acquire(
                timeout=deadline.remaining()
            )
            if not acquired_gpu:
                raise TTGPUError(
                    "prepare_timeout",
                    "GPU prepare exceeded the total execution budget",
                    504,
                )
            try:
                deadline.check()
                if not self.config.uses_outro_pipeline():
                    _run_command(
                        self.runner,
                        build_normalize_command(
                            self.config,
                            source_path,
                            output_path,
                            source_info,
                            _base_video_filter(),
                            output_duration=effective_source_duration,
                        ),
                        deadline.stage_timeout(
                            self.config.transcode_timeout
                        ),
                        "direct_clean_transcode_failed",
                        timeout_error_code="prepare_timeout",
                    )
                else:
                    _run_command(
                        self.runner,
                        build_normalize_command(
                            self.config,
                            outro_input_path,
                            outro_normalized,
                            outro_info,
                            build_outro_filter(
                                self.config,
                                drama_text_path,
                                tutorial_text_path,
                            ),
                        ),
                        deadline.stage_timeout(
                            self.config.transcode_timeout
                        ),
                        "outro_transcode_failed",
                        timeout_error_code="prepare_timeout",
                    )
                    _run_command(
                        self.runner,
                        build_phone_match_command(
                            self.config,
                            source_path,
                            outro_normalized,
                            output_path,
                            effective_source_duration,
                            outro_info["duration"],
                            source_has_audio=source_info["has_audio"],
                            logo_path=logo_input_path,
                        ),
                        deadline.stage_timeout(
                            self.config.transcode_timeout
                        ),
                        "phone_match_transition_failed",
                        timeout_error_code="prepare_timeout",
                    )
            finally:
                self._gpu_slot.release()
            output_sha, output_size = _file_sha256(
                output_path,
                deadline=deadline,
            )
            if output_size <= 0 or output_size > int(self.config.max_output_bytes):
                raise TTGPUError(
                    "prepared_media_invalid",
                    "prepared output size is outside the contract",
                    500,
                )
            output_probe = probe_media(
                self.config,
                output_path,
                self.runner,
                deadline=deadline,
            )
            safe_probe = validate_prepared_output(
                self.config,
                output_probe,
                output_path,
                self.config.max_output_bytes,
                expected_duration,
            )
            storage_key = self._storage_key(job_id, output_sha)
            reused = self._object_store().upload(
                storage_key,
                output_path,
                output_sha,
                output_size,
                deadline=deadline,
            )
            if self.config.storage_backend == "local" and not reused:
                local_orphan = (
                    storage_key,
                    output_sha,
                    output_size,
                )
            output_url = self._object_store().url(storage_key)
            expected_origin = (
                self.config.local_media_origin
                if self.config.storage_backend == "local"
                else self.config.cos_domain
            )
            if not output_url.startswith(expected_origin.rstrip("/") + "/"):
                raise TTGPUError(
                    "prepared_origin_verification_failed",
                    "prepared URL is outside the configured pull origin",
                    500,
                )
            result = {
                "brand_overlay_review_required": (
                    self.config.brand_overlay_review_required()
                ),
                "content_id": request["content_id"],
                "direct_post_eligible": (
                    self.config.direct_post_eligible()
                ),
                "job_id": job_id,
                "output_sha256": output_sha,
                "output_size": output_size,
                "output_url": output_url,
                "probe": safe_probe,
                "profile": self.config.profile,
            }
            manifest = {
                "completed_at": _utc_now(),
                "object_reused": bool(reused),
                "request": request_fingerprint,
                "result": result,
                "status": "ready",
                "storage": {
                    "backend": self.config.storage_backend,
                    "key": storage_key,
                },
                "version": (
                    4
                    if self.config.media_mode == DIRECT_OUTRO_MEDIA_MODE
                    else 3
                    if self.config.media_mode == DIRECT_CLEAN_MEDIA_MODE
                    else 2
                ),
            }
            if self.config.storage_backend == "cos":
                manifest["cos_key"] = storage_key
            deadline.check()
            _atomic_write_json(manifest_path, manifest)
            local_orphan = None
            return _prepare_response(
                manifest,
                False,
                self.config,
                job_id,
            )
        finally:
            if local_orphan is not None:
                try:
                    self._local_media_store().release(*local_orphan)
                except TTGPUError:
                    pass
            shutil.rmtree(job_dir, ignore_errors=True)

    def publish(self, payload):
        request = validate_publish_request(payload)
        gates = self.config.gate_state()
        if not gates["ready"]:
            raise TTGPUError(
                "tt_publish_compliance_gate_closed",
                "TikTok publish init is disabled until all compliance gates are enabled",
                403,
                {"state": "gate_closed"},
            )
        job_id = request["job_id"]
        with _job_lock(self._lock_path(job_id)):
            return self._publish_locked(request, manual_canary=False)

    def canary_publish(self, payload):
        request = validate_publish_request(payload)
        self._assert_manual_canary_request(request)
        job_id = request["job_id"]
        with _job_lock(self._lock_path(job_id)):
            return self._publish_locked(request, manual_canary=True)

    def _assert_manual_canary_request(
        self,
        request,
        prepare_manifest=None,
        prepared=None,
    ):
        config = self.config
        if not config.manual_canary_state()["active"]:
            raise TTGPUError(
                "tt_manual_canary_closed",
                "TT manual canary is disabled or expired",
                403,
            )
        exact_request = bool(
            secrets.compare_digest(
                str(request.get("manual_canary_id") or ""),
                config.manual_canary_id,
            )
            and secrets.compare_digest(
                str(request.get("source_account_id") or ""),
                config.manual_canary_account_id,
            )
            and secrets.compare_digest(
                str(request.get("material_id") or ""),
                config.manual_canary_material_id,
            )
            and secrets.compare_digest(
                str(request.get("job_id") or ""),
                config.manual_canary_gpu_job_id,
            )
            and request.get("privacy_level") == "SELF_ONLY"
            and request.get("disable_comment") is True
            and request.get("disable_duet") is True
            and request.get("disable_stitch") is True
            and request.get("brand_content_toggle") is False
            and request.get("brand_organic_toggle") is False
        )
        if not exact_request:
            raise TTGPUError(
                "tt_manual_canary_target_mismatch",
                "TT manual canary request does not match the configured private target",
                403,
            )
        if prepare_manifest is None or prepared is None:
            return
        storage = (
            prepare_manifest.get("storage")
            if isinstance(prepare_manifest, dict)
            else None
        )
        expected_key = self._storage_key(
            config.manual_canary_gpu_job_id,
            config.manual_canary_output_sha256,
        )
        parsed = urllib.parse.urlsplit(
            str(prepared.get("output_url") or "")
        )
        actual_origin = (
            "https://" + parsed.hostname.lower()
            if (
                parsed.scheme == "https"
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and parsed.port in {None, 443}
            )
            else ""
        )
        exact_artifact = bool(
            prepared.get("content_id") == config.manual_canary_content_id
            and prepared.get("job_id") == config.manual_canary_gpu_job_id
            and prepared.get("output_sha256")
            == config.manual_canary_output_sha256
            and int(prepared.get("output_size") or 0)
            == config.manual_canary_output_size
            and prepared.get("profile") == config.manual_canary_profile
            and actual_origin == config.manual_canary_origin
            and parsed.query == ""
            and parsed.fragment == ""
            and parsed.path == "/" + expected_key
            and isinstance(storage, dict)
            and storage.get("backend") == config.storage_backend
            and storage.get("key") == expected_key
        )
        if not exact_artifact:
            raise TTGPUError(
                "tt_manual_canary_artifact_mismatch",
                "TT manual canary artifact does not match the configured immutable target",
                403,
            )

    def _publish_locked(self, request, *, manual_canary=False):
        job_id = request["job_id"]
        prepare_manifest = _read_json(self._prepare_manifest_path(job_id))
        if not prepare_manifest or prepare_manifest.get("status") != "ready":
            raise TTGPUError(
                "prepared_artifact_not_found",
                "prepare must complete before publish",
                409,
            )
        prepared = _prepare_response(
            prepare_manifest,
            True,
            self.config,
            job_id,
        )
        if manual_canary:
            self._assert_manual_canary_request(
                request,
                prepare_manifest,
                prepared,
            )
        if not manual_canary and not prepared["direct_post_eligible"]:
            raise TTGPUError(
                "tt_media_profile_not_direct_post_eligible",
                "prepared media profile is not eligible for TikTok Direct Post",
                403,
                {"profile": prepared["profile"]},
            )
        parsed_output = urllib.parse.urlsplit(prepared["output_url"])
        actual_origin = (
            "https://" + parsed_output.hostname.lower()
            if (
                parsed_output.scheme == "https"
                and parsed_output.hostname
                and parsed_output.username is None
                and parsed_output.password is None
                and parsed_output.port in {None, 443}
            )
            else ""
        )
        if not manual_canary and (
            not actual_origin
            or not self.config.url_property_verified_origin
            or not secrets.compare_digest(
                actual_origin,
                self.config.url_property_verified_origin,
            )
        ):
            raise TTGPUError(
                "tt_publish_url_property_mismatch",
                "prepared media origin is not the verified TikTok URL Property",
                403,
                {"state": "url_property_mismatch"},
            )
        post_info = {
            "disable_comment": request["disable_comment"],
            "disable_duet": request["disable_duet"],
            "disable_stitch": request["disable_stitch"],
            "privacy_level": request["privacy_level"],
            "title": request["title"],
        }
        for field in (
            "video_cover_timestamp_ms",
            "brand_content_toggle",
            "brand_organic_toggle",
            "is_aigc",
        ):
            if field in request:
                post_info[field] = request[field]
        fingerprint_payload = {
            "job_id": job_id,
            "output_sha256": prepared["output_sha256"],
            "output_url_sha256": hashlib.sha256(
                prepared["output_url"].encode("utf-8")
            ).hexdigest(),
            "post_info": {
                **{k: v for k, v in post_info.items() if k != "title"},
                "title_sha256": hashlib.sha256(
                    post_info["title"].encode("utf-8")
                ).hexdigest(),
            },
            "source_account_id": request["source_account_id"],
        }
        if manual_canary:
            fingerprint_payload["manual_canary_id"] = request[
                "manual_canary_id"
            ]
            fingerprint_payload["material_id"] = request["material_id"]
        request_hash = _stable_hash(fingerprint_payload)
        ledger_path = self._publish_ledger_path(job_id)
        existing = _read_json(ledger_path)
        if existing is not None:
            if existing.get("request_sha256") != request_hash:
                raise TTGPUError(
                    "publish_idempotency_conflict",
                    "job_id already belongs to a different publish request",
                    409,
                )
            publish_id = str(existing.get("publish_id") or "")
            if PUBLISH_ID_RE.fullmatch(publish_id):
                raise TTGPUError(
                    "tt_publish_reconcile_required",
                    "publish was already initialized; use reconcile",
                    409,
                    {"publish_id": publish_id, "state": str(existing.get("state") or "")},
                )
            raise TTGPUError(
                "tt_publish_retry_blocked",
                "a prior init attempt exists without a safe retry path",
                409,
                {"state": str(existing.get("state") or "unknown")},
            )
        ledger = {
            "created_at": _utc_now(),
            "job_id": job_id,
            "request_sha256": request_hash,
            "source_account_id": request["source_account_id"],
            "state": "init_inflight",
            "test_bypass": bool(manual_canary),
            "updated_at": _utc_now(),
            "version": 1,
        }
        try:
            with open_access_token(
                request["credential_envelope"],
                self.config.credential_seal_key,
                job_id=job_id,
                source_account_id=request["source_account_id"],
                operation=(
                    "canary_publish"
                    if manual_canary
                    else "publish"
                ),
                max_ttl_seconds=self.config.credential_max_ttl_seconds,
            ) as token:
                _atomic_write_json(ledger_path, ledger)
                try:
                    init_result = self.tiktok_api.initialize_video(
                        token,
                        post_info,
                        prepared["output_url"],
                    )
                except TTGPUError as exc:
                    ledger.update(
                        {
                            "state": (
                                "init_rejected"
                                if exc.code == "tt_upstream_rejected"
                                else "init_outcome_unknown"
                            ),
                            "upstream_error_code": exc.code,
                            "upstream_log_id": str(
                                exc.details.get("log_id") or ""
                            ),
                            "upstream_code": str(
                                exc.details.get("upstream_code") or ""
                            ),
                            "upstream_message": str(
                                exc.details.get("upstream_message") or ""
                            ),
                            "upstream_http_status": (
                                exc.details.get("upstream_http_status")
                                or 0
                            ),
                            "message_redacted": bool(
                                exc.details.get("message_redacted")
                            ),
                            "received_at": str(
                                exc.details.get("received_at") or _utc_now()
                            ),
                            "updated_at": _utc_now(),
                        }
                    )
                    _atomic_write_json(ledger_path, ledger)
                    raise
        except CredentialEnvelopeError as exc:
            raise TTGPUError(exc.code, str(exc), exc.status) from None
        if isinstance(init_result, dict):
            publish_id = str(init_result.get("publish_id") or "")
            log_id = str(init_result.get("log_id") or "")
        else:  # Injectable tests may return the legacy string form.
            publish_id = str(init_result or "")
            log_id = ""
        if not PUBLISH_ID_RE.fullmatch(publish_id):
            ledger.update(
                {
                    "state": "init_outcome_unknown",
                    "upstream_error_code": "tt_upstream_invalid",
                    "updated_at": _utc_now(),
                }
            )
            _atomic_write_json(ledger_path, ledger)
            raise TTGPUError(
                "tt_upstream_invalid",
                "TikTok API did not return a valid publish ID",
                502,
            )
        if not SAFE_LOG_ID_RE.fullmatch(log_id):
            log_id = ""
        ledger.update(
            {
                "initialized_at": _utc_now(),
                "log_id": log_id,
                "publish_id": publish_id,
                "state": "initialized",
                "updated_at": _utc_now(),
            }
        )
        _atomic_write_json(ledger_path, ledger)
        return {
            "job_id": job_id,
            "log_id": log_id,
            "publish_id": publish_id,
            "source": "PULL_FROM_URL",
            "state": "initialized",
            "status": "ok",
            "test_bypass": bool(manual_canary),
        }

    def reconcile(self, payload):
        request = validate_credential_request(payload, RECONCILE_FIELDS)
        job_id = request["job_id"]
        with _job_lock(self._lock_path(job_id)):
            ledger_path = self._publish_ledger_path(job_id)
            ledger = _read_json(ledger_path)
            if not ledger:
                raise TTGPUError(
                    "publish_ledger_not_found",
                    "publish has not been initialized",
                    404,
                )
            if str(ledger.get("source_account_id") or "") != request["source_account_id"]:
                raise TTGPUError(
                    "credential_binding_mismatch",
                    "publish account does not match the request",
                    403,
                )
            publish_id = str(ledger.get("publish_id") or "")
            if not PUBLISH_ID_RE.fullmatch(publish_id):
                raise TTGPUError(
                    "tt_publish_outcome_unknown",
                    "publish init has no known publish ID and cannot be reconciled automatically",
                    409,
                    {"state": str(ledger.get("state") or "unknown")},
                )
            try:
                with open_access_token(
                    request["credential_envelope"],
                    self.config.credential_seal_key,
                    job_id=job_id,
                    source_account_id=request["source_account_id"],
                    operation="reconcile",
                    max_ttl_seconds=self.config.credential_max_ttl_seconds,
                ) as token:
                    upstream = self.tiktok_api.fetch_status(token, publish_id)
            except CredentialEnvelopeError as exc:
                raise TTGPUError(exc.code, str(exc), exc.status) from None
            upstream = normalize_publish_status(upstream)
            if upstream["status"] == "PUBLISH_COMPLETE":
                state = "published"
            elif upstream["status"] in {"FAILED", "PUBLISH_FAILED"}:
                state = "failed"
            else:
                state = "processing"
            ledger.update(
                {
                    "last_reconciled_at": _utc_now(),
                    "last_status": upstream,
                    "state": state,
                    "updated_at": _utc_now(),
                }
            )
            _atomic_write_json(ledger_path, ledger)
            response = {
                "job_id": job_id,
                "publish_id": publish_id,
                "state": state,
                "status": upstream,
            }
            media_release = self._release_terminal_media_locked(
                job_id,
                ledger,
                ledger_path,
            )
            if media_release is not None:
                response["media_release"] = media_release
            return response


def _is_loopback_client(value):
    try:
        return ipaddress.ip_address(str(value or "")).is_loopback
    except ValueError:
        return False


class TTPostGPUHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, processor, token):
        super().__init__(address, TTPostGPURequestHandler)
        self.processor = processor
        self.token = token


class TTPostGPURequestHandler(BaseHTTPRequestHandler):
    """Loopback-only transport that never logs body or authorization values."""

    protocol_version = "HTTP/1.1"
    server_version = "DramawaveTTPostGPU/1"
    sys_version = ""

    def log_message(self, _format, *_args):
        return

    def _send_json(self, status, payload):
        body = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _allowed_client(self):
        return _is_loopback_client(
            self.client_address[0] if self.client_address else ""
        )

    def _authorized(self):
        header = str(self.headers.get("Authorization") or "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        expected = str(getattr(self.server, "token", "") or "")
        return bool(token and expected and secrets.compare_digest(token, expected))

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if not self._allowed_client():
            self._send_json(403, {"error": "forbidden", "message": "forbidden"})
            return
        if parsed.path != HEALTH_PATH or parsed.query or parsed.fragment:
            self._send_json(404, {"error": "not_found", "message": "not found"})
            return
        self._send_json(
            200,
            {
                "asset_identity_ready": (
                    self.server.processor.config.asset_identity_ready()
                ),
                "brand_overlay_review_required": (
                    self.server.processor.config
                    .brand_overlay_review_required()
                ),
                "direct_post_eligible": (
                    self.server.processor.config.direct_post_eligible()
                ),
                "gates": self.server.processor.config.gate_state(),
                "manual_canary": (
                    self.server.processor.config.manual_canary_state()
                ),
                "local_origin_enabled": bool(
                    self.server.processor.config.local_media_origin
                    and len(
                        self.server.processor.config.local_media_signing_key
                    )
                    == 32
                ),
                "media_mode": self.server.processor.config.media_mode,
                "profile": self.server.processor.config.profile,
                "storage_backend": self.server.processor.config.storage_backend,
                "storage": self.server.processor.storage_health(),
                "status": "ok",
                "transition": (
                    self.server.processor.config.preparation_transition()
                ),
            },
        )

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if not self._allowed_client() or not self._authorized():
            self._send_json(403, {"error": "forbidden", "message": "forbidden"})
            return
        handlers = {
            CREATOR_INFO_PATH: self.server.processor.creator_info,
            PREPARE_PATH: self.server.processor.prepare,
            PUBLISH_PATH: self.server.processor.publish,
            CANARY_PUBLISH_PATH: self.server.processor.canary_publish,
            RECONCILE_PATH: self.server.processor.reconcile,
        }
        handler = handlers.get(parsed.path)
        if handler is None or parsed.query or parsed.fragment:
            self._send_json(404, {"error": "not_found", "message": "not found"})
            return
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(
                415,
                {
                    "error": "invalid_content_type",
                    "message": "application/json required",
                },
            )
            return
        if str(self.headers.get("Transfer-Encoding") or "").strip():
            self._send_json(
                400,
                {
                    "error": "invalid_request",
                    "message": "chunked requests are not supported",
                },
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError, OverflowError):
            content_length = 0
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(
                413,
                {
                    "error": "invalid_request",
                    "message": "request body size is invalid",
                },
            )
            return
        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            self._send_json(
                400,
                {
                    "error": "invalid_json",
                    "message": "request body must be valid JSON",
                },
            )
            return
        try:
            result = handler(payload)
        except TTGPUError as exc:
            response = {"error": exc.code, "message": str(exc)}
            if exc.details:
                response["details"] = exc.details
            self._send_json(exc.status, response)
            return
        except Exception:
            self._send_json(
                500,
                {
                    "error": "internal_error",
                    "message": "TT GPU request failed",
                },
            )
            return
        self._send_json(200, {"item": result})


def _single_byte_range(value, size):
    text = str(value or "").strip()
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", text)
    if not match or "," in text:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    try:
        if not first:
            suffix = int(last)
            if suffix <= 0:
                return None
            start = max(0, int(size) - suffix)
            end = int(size) - 1
        else:
            start = int(first)
            if start < 0 or start >= int(size):
                return None
            end = int(last) if last else int(size) - 1
            if end < start:
                return None
            end = min(end, int(size) - 1)
    except (TypeError, ValueError, OverflowError):
        return None
    return start, end


class TTPostGPUMediaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, processor):
        super().__init__(address, TTPostGPUMediaRequestHandler)
        self.processor = processor


class TTPostGPUMediaRequestHandler(BaseHTTPRequestHandler):
    """Loopback origin used behind the dedicated public HTTPS reverse proxy."""

    protocol_version = "HTTP/1.1"
    server_version = "DramawaveTTPostMedia/1"
    sys_version = ""

    def log_message(self, _format, *_args):
        return

    def _not_found(self):
        self.send_response(404)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _range_not_satisfiable(self, size):
        self.send_response(416)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", "bytes */%s" % int(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _serve_media(self, include_body):
        if not _is_loopback_client(
            self.client_address[0] if self.client_address else ""
        ):
            self._not_found()
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._not_found()
            return
        try:
            store = self.server.processor._local_media_store()
        except TTGPUError:
            self._not_found()
            return
        try:
            resolved = store.resolve_request_path(
                parsed.path,
                self.server.processor.manifest_root,
            )
        except TTGPUError:
            self._not_found()
            return
        if not resolved:
            self._not_found()
            return
        size = int(resolved["size"])
        try:
            descriptor = _open_regular_readonly(
                resolved["path"],
                size,
            )
        except OSError:
            self._not_found()
            return
        try:
            range_header = self.headers.get("Range")
            etag = '"sha256-%s"' % resolved["sha256"]
            if_range = self.headers.get("If-Range")
            if (
                range_header is not None
                and if_range is not None
                and not secrets.compare_digest(
                    str(if_range).strip(),
                    etag,
                )
            ):
                range_header = None
            if range_header is None:
                start, end = 0, size - 1
                status = 200
            else:
                byte_range = _single_byte_range(range_header, size)
                if byte_range is None:
                    self._range_not_satisfiable(size)
                    return
                start, end = byte_range
                status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header(
                "ETag",
                etag,
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 206:
                self.send_header(
                    "Content-Range",
                    "bytes %s-%s/%s" % (start, end, size),
                )
            self.send_header("Connection", "close")
            self.end_headers()
            if include_body:
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = None
                    handle.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self.close_connection = True

    def do_HEAD(self):  # noqa: N802
        self._serve_media(False)

    def do_GET(self):  # noqa: N802
        self._serve_media(True)

    def do_POST(self):  # noqa: N802
        self._not_found()


def _media_cleanup_loop(processor, stop_event):
    while not stop_event.is_set():
        try:
            result = processor.cleanup_due_media()
            processor.record_cleanup_state(result=result)
        except Exception:
            processor.record_cleanup_state(failed=True)
        stop_event.wait(60)


def serve():
    config = WorkerConfig.from_env()
    processor = TTPostGPUProcessor(config)
    control_server = TTPostGPUHTTPServer(
        (config.host, config.port),
        processor,
        config.internal_token,
    )
    media_server = None
    media_thread = None
    cleanup_thread = None
    cleanup_stop = threading.Event()
    if (
        config.local_media_origin
        and len(config.local_media_signing_key) == 32
    ):
        media_server = TTPostGPUMediaHTTPServer(
            (config.media_host, config.media_port),
            processor,
        )
        media_thread = threading.Thread(
            target=media_server.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="tt-post-media-origin",
            daemon=True,
        )
        media_thread.start()
        cleanup_thread = threading.Thread(
            target=_media_cleanup_loop,
            args=(processor, cleanup_stop),
            name="tt-post-media-cleanup",
            daemon=True,
        )
        cleanup_thread.start()
    try:
        control_server.serve_forever(poll_interval=0.5)
    finally:
        control_server.server_close()
        cleanup_stop.set()
        if media_server is not None:
            media_server.shutdown()
            media_server.server_close()
        if media_thread is not None:
            media_thread.join(timeout=5)
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=5)
