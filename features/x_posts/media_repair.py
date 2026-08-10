"""Fail-closed GPU media repair for the Dramawave X post workflow.

The worker is intentionally independent from the X publisher.  It accepts only
the deterministic codec, dimension, and over-limit-duration failures covered by
the repair contract, downloads the exact caller-preflighted object, normalizes
it with NVENC, trims only an over-limit tail, uploads the immutable result to
COS, and returns only after a COS HEAD verification.

Production callers use the loopback HTTP wrapper in
``scripts/x_post_media_repair_worker.py``.  Collaborators are injectable so the
contract can be tested without ffmpeg, a GPU, network access, or COS credentials.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .service import DEFAULT_MAX_MEDIA_BYTES, XPostError, download_media

try:  # Linux production uses flock; the fallback keeps offline Windows tests usable.
    import fcntl
except ImportError:  # pragma: no cover - exercised only outside Linux.
    fcntl = None


REPAIR_PROFILE = "x-h264-nvenc-720-duration-policy-v3"
REPAIR_PATH = "/internal/x-post-media-repair"
HEALTH_PATH = "/health"
REPAIRABLE_TRIGGER_CODES = frozenset(
    {
        "invalid_media_codec",
        "invalid_media_dimensions",
        "invalid_media_duration",
    }
)
MIN_DURATION_SECONDS = 0.5
STANDARD_MAX_DURATION_SECONDS = 140.0
PREMIUM_MAX_DURATION_SECONDS = 600.0
STANDARD_TRIM_TARGET_SECONDS = 139.0
PREMIUM_TRIM_TARGET_SECONDS = 599.0
# Backward-compatible names remain the strict standard-account contract.
MAX_DURATION_SECONDS = STANDARD_MAX_DURATION_SECONDS
TRIM_TARGET_SECONDS = STANDARD_TRIM_TARGET_SECONDS
DURATION_POLICIES = {
    "standard": (
        STANDARD_MAX_DURATION_SECONDS,
        STANDARD_TRIM_TARGET_SECONDS,
    ),
    "premium": (
        PREMIUM_MAX_DURATION_SECONDS,
        PREMIUM_TRIM_TARGET_SECONDS,
    ),
}
TRIM_DURATION_TOLERANCE_SECONDS = 0.5
REQUEST_FIELDS = frozenset(
    {
        "job_key",
        "material_id",
        "pool_item_id",
        "source_url",
        "source_sha256",
        "source_size",
        "trigger_code",
        "profile",
        "duration_policy",
    }
)
HEX_64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
POSITIVE_ID_RE = re.compile(r"\A[1-9][0-9]{0,30}\Z")
DRAMA_RESOURCE_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
SAFE_PREFIX_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,240}\Z")
MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_OUTPUT_MAX_BYTES = DEFAULT_MAX_MEDIA_BYTES
DEFAULT_WORK_ROOT = Path("/data/x-post-media-repair")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8820
DEFAULT_COS_PREFIX = "x-post-media-repair"
DEFAULT_FFMPEG_BIN = "/opt/ffmpeg-nvenc/ffmpeg"
DEFAULT_FFPROBE_BIN = "/opt/ffmpeg-nvenc/ffprobe"
DEFAULT_TRANSCODE_TIMEOUT = 1800
DEFAULT_PROBE_TIMEOUT = 120
DEFAULT_DOWNLOAD_TIMEOUT = 120

_FALLBACK_LOCKS = {}
_FALLBACK_LOCKS_GUARD = threading.Lock()


class MediaRepairError(RuntimeError):
    """Stable, sanitized worker failure."""

    def __init__(self, code, message, status=400):
        self.code = _clean_error_token(code)
        self.status = int(status or 500)
        super().__init__(_clean_message(message))


def _clean_error_token(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", value):
        return "media_repair_error"
    return value


def _clean_message(value, limit=300):
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)(secret(?:id|key)?|token|authorization)(\s*[:=]\s*)\S+",
        r"\1\2[redacted]",
        text,
    )
    return text[: int(limit)]


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _env_bool(name, default=False):
    raw = str(os.environ.get(name, "1" if default else "0") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise MediaRepairError("invalid_configuration", "%s must be 0 or 1" % name, 500)


def _env_int(name, default, minimum, maximum):
    raw = str(os.environ.get(name, str(default)) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise MediaRepairError("invalid_configuration", "%s must be an integer" % name, 500) from None
    if value < minimum or value > maximum:
        raise MediaRepairError(
            "invalid_configuration",
            "%s must be between %s and %s" % (name, minimum, maximum),
            500,
        )
    return value


def _require_absolute_binary(value, name):
    value = str(value or "").strip()
    path = Path(value)
    if not value or "\x00" in value or not path.is_absolute():
        raise MediaRepairError("invalid_configuration", "%s must be an absolute path" % name, 500)
    return value


def _parse_allowed_hosts(value):
    hosts = []
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
            raise MediaRepairError(
                "invalid_configuration",
                "X_POST_MEDIA_REPAIR_ALLOWED_HOSTS contains an invalid host",
                500,
            )
        hosts.append(host)
    if not hosts:
        raise MediaRepairError(
            "invalid_configuration",
            "X_POST_MEDIA_REPAIR_ALLOWED_HOSTS is required",
            500,
        )
    return tuple(dict.fromkeys(hosts))


def _normalize_cos_domain(value):
    value = str(value or "").strip().rstrip("/")
    if not value:
        raise MediaRepairError("invalid_configuration", "COS_DOMAIN is required", 500)
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise MediaRepairError(
            "invalid_configuration",
            "COS_DOMAIN must be an HTTPS origin",
            500,
        )
    return value


def _normalize_cos_prefix(value):
    value = str(value or "").strip().strip("/")
    if not value:
        value = DEFAULT_COS_PREFIX
    if ".." in value.split("/") or not SAFE_PREFIX_RE.fullmatch(value):
        raise MediaRepairError(
            "invalid_configuration",
            "X_POST_MEDIA_REPAIR_COS_PREFIX is invalid",
            500,
        )
    return value


@dataclass(frozen=True)
class WorkerConfig:
    """Validated, fail-closed worker configuration."""

    enabled: bool
    host: str
    port: int
    token: str = field(repr=False)
    allowed_hosts: tuple
    work_root: Path
    ffmpeg_bin: str
    ffprobe_bin: str
    cos_secret_id: str = field(repr=False)
    cos_secret_key: str = field(repr=False)
    cos_bucket: str
    cos_region: str
    cos_domain: str
    cos_prefix: str
    max_source_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    max_output_bytes: int = DEFAULT_OUTPUT_MAX_BYTES
    download_timeout: int = DEFAULT_DOWNLOAD_TIMEOUT
    probe_timeout: int = DEFAULT_PROBE_TIMEOUT
    transcode_timeout: int = DEFAULT_TRANSCODE_TIMEOUT
    profile: str = REPAIR_PROFILE

    @classmethod
    def from_env(cls):
        enabled = _env_bool("X_POST_MEDIA_REPAIR_ENABLED", False)
        if not enabled:
            raise MediaRepairError(
                "media_repair_disabled",
                "X post media repair is disabled",
                503,
            )
        host = str(os.environ.get("X_POST_MEDIA_REPAIR_HOST", DEFAULT_HOST) or "").strip()
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("not loopback")
        except ValueError:
            raise MediaRepairError(
                "invalid_configuration",
                "X_POST_MEDIA_REPAIR_HOST must be a loopback address",
                500,
            ) from None
        token = str(os.environ.get("X_POST_MEDIA_REPAIR_TOKEN", "") or "").strip()
        if len(token) < 32 or len(token) > 512 or re.search(r"[\s\x00-\x1f]", token):
            raise MediaRepairError(
                "invalid_configuration",
                "X_POST_MEDIA_REPAIR_TOKEN must be a non-whitespace secret of at least 32 characters",
                500,
            )
        work_root = Path(
            str(os.environ.get("X_POST_MEDIA_REPAIR_WORK_ROOT", DEFAULT_WORK_ROOT) or "")
        )
        if not work_root.is_absolute():
            raise MediaRepairError(
                "invalid_configuration",
                "X_POST_MEDIA_REPAIR_WORK_ROOT must be absolute",
                500,
            )
        secret_id = str(os.environ.get("COS_SECRET_ID", "") or "").strip()
        secret_key = str(os.environ.get("COS_SECRET_KEY", "") or "").strip()
        bucket = str(os.environ.get("COS_BUCKET", "") or "").strip()
        region = str(os.environ.get("COS_REGION", "") or "").strip()
        if not all((secret_id, secret_key, bucket, region)):
            raise MediaRepairError(
                "invalid_configuration",
                "COS credentials, bucket and region are required",
                500,
            )
        return cls(
            enabled=True,
            host=host,
            port=_env_int("X_POST_MEDIA_REPAIR_PORT", DEFAULT_PORT, 1, 65535),
            token=token,
            allowed_hosts=_parse_allowed_hosts(
                os.environ.get("X_POST_MEDIA_REPAIR_ALLOWED_HOSTS", "")
            ),
            work_root=work_root,
            ffmpeg_bin=_require_absolute_binary(
                os.environ.get("X_POST_MEDIA_REPAIR_FFMPEG_BIN", DEFAULT_FFMPEG_BIN),
                "X_POST_MEDIA_REPAIR_FFMPEG_BIN",
            ),
            ffprobe_bin=_require_absolute_binary(
                os.environ.get("X_POST_MEDIA_REPAIR_FFPROBE_BIN", DEFAULT_FFPROBE_BIN),
                "X_POST_MEDIA_REPAIR_FFPROBE_BIN",
            ),
            cos_secret_id=secret_id,
            cos_secret_key=secret_key,
            cos_bucket=bucket,
            cos_region=region,
            cos_domain=_normalize_cos_domain(os.environ.get("COS_DOMAIN", "")),
            cos_prefix=_normalize_cos_prefix(
                os.environ.get("X_POST_MEDIA_REPAIR_COS_PREFIX", DEFAULT_COS_PREFIX)
            ),
            max_source_bytes=_env_int(
                "X_POST_MEDIA_REPAIR_MAX_SOURCE_BYTES",
                DEFAULT_MAX_MEDIA_BYTES,
                1024,
                4 * 1024 * 1024 * 1024,
            ),
            max_output_bytes=_env_int(
                "X_POST_MEDIA_REPAIR_MAX_OUTPUT_BYTES",
                DEFAULT_OUTPUT_MAX_BYTES,
                1024,
                4 * 1024 * 1024 * 1024,
            ),
            download_timeout=_env_int(
                "X_POST_MEDIA_REPAIR_DOWNLOAD_TIMEOUT",
                DEFAULT_DOWNLOAD_TIMEOUT,
                1,
                120,
            ),
            probe_timeout=_env_int(
                "X_POST_MEDIA_REPAIR_PROBE_TIMEOUT",
                DEFAULT_PROBE_TIMEOUT,
                1,
                300,
            ),
            transcode_timeout=_env_int(
                "X_POST_MEDIA_REPAIR_TRANSCODE_TIMEOUT",
                DEFAULT_TRANSCODE_TIMEOUT,
                30,
                14400,
            ),
        )


def _ensure_private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise MediaRepairError("storage_error", "repair storage path is invalid", 500)
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise MediaRepairError("storage_error", "cannot secure repair storage: %s" % exc, 500) from None
    return path


def _fsync_directory(path):
    if os.name == "nt":  # Windows cannot open directories for fsync.
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_manifest(path, payload):
    path = Path(path)
    _ensure_private_directory(path.parent)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(8)))
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor = None
    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _job_flock(lock_path):
    """Cross-process exclusive lock; Linux production always takes flock."""

    lock_path = Path(lock_path)
    _ensure_private_directory(lock_path.parent)
    descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    fallback = None
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows-only fallback for local unit tests.
            key = str(lock_path.resolve())
            with _FALLBACK_LOCKS_GUARD:
                fallback = _FALLBACK_LOCKS.setdefault(key, threading.Lock())
            fallback.acquire()
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        elif fallback is not None:  # pragma: no cover
            fallback.release()
        os.close(descriptor)


def _parse_positive_id(value, name):
    value = str(value or "").strip()
    if not POSITIVE_ID_RE.fullmatch(value):
        raise MediaRepairError("invalid_request", "%s must be a positive integer" % name, 400)
    return value


def _parse_material_id(value):
    value = str(value or "").strip()
    if POSITIVE_ID_RE.fullmatch(value):
        return value
    if DRAMA_RESOURCE_ID_RE.fullmatch(value):
        return value
    raise MediaRepairError(
        "invalid_request",
        "material_id must be a positive integer or a 32-character hexadecimal resource ID",
        400,
    )


def _parse_sha256(value, name):
    value = str(value or "").strip().lower()
    if not HEX_64_RE.fullmatch(value):
        raise MediaRepairError("invalid_request", "%s must be a lowercase SHA-256" % name, 400)
    return value


def _parse_source_size(value):
    if isinstance(value, bool):
        raise MediaRepairError("invalid_request", "source_size must be an integer", 400)
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        raise MediaRepairError("invalid_request", "source_size must be an integer", 400) from None
    if value <= 0:
        raise MediaRepairError("invalid_request", "source_size must be positive", 400)
    return value


def validate_request(payload, profile=REPAIR_PROFILE):
    if not isinstance(payload, dict):
        raise MediaRepairError("invalid_request", "request body must be a JSON object", 400)
    keys = frozenset(payload)
    missing = REQUEST_FIELDS - keys
    unknown = keys - REQUEST_FIELDS
    if missing or unknown:
        raise MediaRepairError(
            "invalid_request",
            "request fields do not match the media repair contract",
            400,
        )
    job_key = _parse_sha256(payload.get("job_key"), "job_key")
    source_sha256 = _parse_sha256(payload.get("source_sha256"), "source_sha256")
    material_id = _parse_material_id(payload.get("material_id"))
    pool_item_id = _parse_positive_id(payload.get("pool_item_id"), "pool_item_id")
    source_url = str(payload.get("source_url") or "").strip()
    parsed = urllib.parse.urlsplit(source_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise MediaRepairError("invalid_request", "source_url must be HTTPS", 400)
    trigger_code = str(payload.get("trigger_code") or "").strip()
    if trigger_code not in REPAIRABLE_TRIGGER_CODES:
        raise MediaRepairError(
            "trigger_not_repairable",
            "trigger_code is not eligible for automatic media repair",
            422,
        )
    request_profile = str(payload.get("profile") or "").strip()
    if not secrets.compare_digest(request_profile, profile):
        raise MediaRepairError(
            "profile_mismatch",
            "repair profile does not match the worker",
            409,
        )
    duration_policy = str(payload.get("duration_policy") or "").strip().lower()
    if duration_policy not in DURATION_POLICIES:
        raise MediaRepairError(
            "invalid_request",
            "duration_policy must be standard or premium",
            400,
        )
    return {
        "job_key": job_key,
        "material_id": material_id,
        "pool_item_id": pool_item_id,
        "source_url": source_url,
        "source_sha256": source_sha256,
        "source_size": _parse_source_size(payload.get("source_size")),
        "trigger_code": trigger_code,
        "profile": request_profile,
        "duration_policy": duration_policy,
    }


def _safe_subprocess_environment():
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }


def _run_command(runner, command, timeout, error_code):
    try:
        completed = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(timeout),
            check=False,
            close_fds=True,
            env=_safe_subprocess_environment(),
        )
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise MediaRepairError(error_code, "%s execution failed: %s" % (error_code, exc), 422) from None
    if int(getattr(completed, "returncode", 1)) != 0:
        stderr = _clean_message(getattr(completed, "stderr", ""), 180)
        detail = ": %s" % stderr if stderr else ""
        raise MediaRepairError(error_code, "%s failed%s" % (error_code, detail), 422)
    return completed


def _probe_payload(config, path, runner):
    command = [
        config.ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = _run_command(runner, command, config.probe_timeout, "media_probe_failed")
    try:
        payload = json.loads(str(getattr(completed, "stdout", "") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise MediaRepairError("media_probe_failed", "ffprobe returned invalid JSON", 422) from None
    if not isinstance(payload, dict):
        raise MediaRepairError("media_probe_failed", "ffprobe returned invalid data", 422)
    return payload


def _streams(payload, kind):
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return []
    return [
        item
        for item in streams
        if isinstance(item, dict) and str(item.get("codec_type") or "") == kind
    ]


def _positive_number(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _frame_rate(value):
    value = str(value or "").strip()
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator = float(denominator)
            return float(numerator) / denominator if denominator else 0.0
        return float(value)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return 0.0


def _rotation(video):
    candidates = []
    tags = video.get("tags")
    if isinstance(tags, dict):
        candidates.append(tags.get("rotate"))
    side_data = video.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict):
                candidates.append(item.get("rotation"))
    for value in candidates:
        try:
            return int(round(float(value))) % 360
        except (TypeError, ValueError, OverflowError):
            continue
    return 0


def inspect_source(payload, trigger_code="", duration_policy="standard"):
    duration_policy = str(duration_policy or "").strip().lower()
    if duration_policy not in DURATION_POLICIES:
        raise MediaRepairError(
            "invalid_request", "duration policy is invalid", 400
        )
    max_duration, trim_target = DURATION_POLICIES[duration_policy]
    videos = _streams(payload, "video")
    if len(videos) != 1:
        raise MediaRepairError(
            "source_not_repairable",
            "source must contain exactly one video stream",
            422,
        )
    video = videos[0]
    try:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError, OverflowError):
        width = height = 0
    if width <= 0 or height <= 0:
        raise MediaRepairError(
            "source_not_repairable",
            "source video dimensions are unavailable",
            422,
        )
    rotation = _rotation(video)
    field_order = str(video.get("field_order") or "").strip().lower()
    if field_order and field_order != "progressive":
        raise MediaRepairError(
            "source_not_repairable",
            "source video has a non-repairable scan mode",
            422,
        )
    frame_rate = _frame_rate(
        video.get("avg_frame_rate") or video.get("r_frame_rate")
    )
    if frame_rate <= 0 or frame_rate > 60.0:
        raise MediaRepairError(
            "source_not_repairable",
            "source video frame rate is outside the X post contract",
            422,
        )
    display_width, display_height = width, height
    if rotation in {90, 270}:
        display_width, display_height = height, width
    if display_width == display_height:
        canvas = (720, 720)
    elif display_width > display_height:
        canvas = (1280, 720)
    else:
        canvas = (720, 1280)
    format_data = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _positive_number(format_data.get("duration") or video.get("duration"))
    if duration < MIN_DURATION_SECONDS:
        raise MediaRepairError(
            "source_not_repairable",
            "source duration is outside the X post contract",
            422,
        )
    trim_applied = duration > max_duration
    if (
        str(trigger_code or "") == "invalid_media_duration"
        and not trim_applied
    ):
        raise MediaRepairError(
            "source_not_repairable",
            "source does not have an over-limit duration",
            422,
        )
    return {
        "width": width,
        "height": height,
        "display_width": display_width,
        "display_height": display_height,
        "rotation": rotation,
        "duration": duration,
        "output_duration": (
            trim_target if trim_applied else duration
        ),
        "trim_applied": trim_applied,
        "duration_policy": duration_policy,
        "max_duration": max_duration,
        "trim_target": trim_target,
        "frame_rate": frame_rate,
        "has_audio": bool(_streams(payload, "audio")),
        "canvas": canvas,
    }


def build_ffmpeg_command(config, source_path, output_path, source_info):
    canvas_width, canvas_height = source_info["canvas"]
    video_filter = (
        "yadif=mode=send_frame:parity=auto:deint=interlaced,"
        "scale=w=%d:h=%d:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1,fps=30,format=yuv420p"
        % (canvas_width, canvas_height, canvas_width, canvas_height)
    )
    command = [
        config.ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
    ]
    if not source_info["has_audio"]:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
        )
    command.extend(["-map", "0:v:0", "-map", "0:a:0" if source_info["has_audio"] else "1:a:0"])
    command.extend(
        [
            "-vf",
            video_filter,
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-fps_mode",
            "cfr",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-flags",
            "+cgop",
            "-rc",
            "vbr",
            "-cq",
            "20",
            "-b:v",
            "5M",
            "-maxrate",
            "6M",
            "-bufsize",
            "10M",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "128k",
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
    if not source_info["has_audio"]:
        command.append("-shortest")
    if source_info.get("trim_applied"):
        command.extend(["-t", "%.3f" % source_info["trim_target"]])
    command.append(str(output_path))
    return command


def _file_sha256(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def validate_output(
    payload,
    output_path,
    expected_canvas,
    max_output_bytes,
    expected_duration=None,
    trim_applied=False,
    max_duration_seconds=STANDARD_MAX_DURATION_SECONDS,
):
    try:
        max_duration_seconds = float(max_duration_seconds)
    except (TypeError, ValueError, OverflowError):
        raise MediaRepairError(
            "invalid_configuration", "output duration policy is invalid", 500
        ) from None
    if max_duration_seconds not in {
        STANDARD_MAX_DURATION_SECONDS,
        PREMIUM_MAX_DURATION_SECONDS,
    }:
        raise MediaRepairError(
            "invalid_configuration", "output duration policy is invalid", 500
        )
    videos = _streams(payload, "video")
    audios = _streams(payload, "audio")
    if len(videos) != 1 or len(audios) != 1:
        raise MediaRepairError(
            "repaired_media_invalid",
            "repaired media must have exactly one video and one audio stream",
            500,
        )
    video, audio = videos[0], audios[0]
    try:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        sample_rate = int(audio.get("sample_rate") or 0)
        channels = int(audio.get("channels") or 0)
    except (TypeError, ValueError, OverflowError):
        raise MediaRepairError(
            "repaired_media_invalid",
            "repaired media metadata is invalid",
            500,
        ) from None
    frame_rate = _frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    r_frame_rate = _frame_rate(video.get("r_frame_rate") or video.get("avg_frame_rate"))
    field_order = str(video.get("field_order") or "").strip().lower()
    video_profile = str(video.get("profile") or "").strip().lower()
    audio_profile = str(audio.get("profile") or "").strip().lower()
    channel_layout = str(audio.get("channel_layout") or "").strip().lower()
    if (
        str(video.get("codec_name") or "").strip().lower() != "h264"
        or video_profile != "high"
        or str(video.get("pix_fmt") or "").strip().lower() != "yuv420p"
        or field_order != "progressive"
        or (width, height) != tuple(expected_canvas)
        or abs(frame_rate - 30.0) > 0.01
        or abs(r_frame_rate - 30.0) > 0.01
    ):
        raise MediaRepairError(
            "repaired_media_invalid",
            "repaired video does not match the NVENC output profile",
            500,
        )
    if (
        str(audio.get("codec_name") or "").strip().lower() != "aac"
        or audio_profile != "lc"
        or sample_rate != 48000
        or channels != 2
        or channel_layout not in {"", "stereo"}
    ):
        raise MediaRepairError(
            "repaired_media_invalid",
            "repaired audio does not match AAC-LC 48k stereo",
            500,
        )
    format_data = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _positive_number(format_data.get("duration") or video.get("duration"))
    if duration < MIN_DURATION_SECONDS or duration > max_duration_seconds:
        raise MediaRepairError(
            "repaired_media_invalid",
            "repaired media duration is outside the X post contract",
            500,
        )
    source_duration = _positive_number(expected_duration)
    duration_tolerance = (
        TRIM_DURATION_TOLERANCE_SECONDS
        if trim_applied
        else max(0.5, source_duration * 0.02)
    )
    if source_duration and abs(duration - source_duration) > duration_tolerance:
        raise MediaRepairError(
            "repaired_media_invalid",
            "repaired media duration does not preserve the source",
            500,
        )
    try:
        size = Path(output_path).stat().st_size
    except OSError:
        raise MediaRepairError("repaired_media_invalid", "repaired media is missing", 500) from None
    if size <= 0 or size > int(max_output_bytes):
        raise MediaRepairError(
            "repaired_media_invalid",
            "repaired media size is outside the configured limit",
            500,
        )
    return {
        "codec": "h264",
        "profile": "high",
        "pixel_format": "yuv420p",
        "field_order": "progressive",
        "width": width,
        "height": height,
        "frame_rate": 30.0,
        "gop": 60,
        "duration": duration,
        "size": size,
        "audio_codec": "aac",
        "audio_profile": "lc",
        "audio_sample_rate": 48000,
        "audio_channels": 2,
        "audio_channel_layout": "stereo",
    }


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
    """Minimal COS adapter with content-address verification."""

    def __init__(self, config, client=None):
        self.config = config
        self.client = client or self._create_client()

    def _create_client(self):
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError:
            raise MediaRepairError(
                "cos_sdk_unavailable",
                "qcloud_cos is required by the media repair worker",
                500,
            ) from None
        cos_config = CosConfig(
            Region=self.config.cos_region,
            SecretId=self.config.cos_secret_id,
            SecretKey=self.config.cos_secret_key,
            Timeout=max(60, self.config.transcode_timeout),
            KeepAlive=False,
        )
        return CosS3Client(cos_config)

    def head(self, key):
        try:
            return self.client.head_object(Bucket=self.config.cos_bucket, Key=key)
        except Exception as exc:
            if _cos_status_code(exc) == 404:
                return None
            raise MediaRepairError(
                "cos_head_failed",
                "COS object verification failed: %s" % exc.__class__.__name__,
                502,
            ) from None

    @staticmethod
    def validate_head(head, expected_size, expected_sha256):
        if not isinstance(head, dict):
            return False
        raw_size = _head_value(head, "Content-Length", "content_length")
        remote_sha = _head_value(head, "x-cos-meta-sha256", "sha256")
        try:
            remote_size = int(raw_size)
        except (TypeError, ValueError, OverflowError):
            return False
        return (
            remote_size == int(expected_size)
            and isinstance(remote_sha, str)
            and secrets.compare_digest(remote_sha.strip().lower(), expected_sha256)
        )

    def upload(self, key, local_path, sha256_value, size):
        existing = self.head(key)
        if existing is not None:
            if self.validate_head(existing, size, sha256_value):
                return True
            raise MediaRepairError(
                "cos_object_conflict",
                "existing content-addressed COS object failed integrity verification",
                409,
            )
        try:
            self.client.upload_file(
                Bucket=self.config.cos_bucket,
                Key=key,
                LocalFilePath=str(local_path),
                PartSize=8,
                MAXThread=4,
                EnableMD5=True,
                ACL="public-read",
                ContentType="video/mp4",
                Metadata={
                    "x-cos-meta-sha256": sha256_value,
                    "x-cos-meta-profile": self.config.profile,
                },
            )
        except Exception as exc:
            raise MediaRepairError(
                "cos_upload_failed",
                "COS upload failed: %s" % exc.__class__.__name__,
                502,
            ) from None
        verified = self.head(key)
        if not self.validate_head(verified, size, sha256_value):
            raise MediaRepairError(
                "cos_verification_failed",
                "uploaded COS object failed HEAD integrity verification",
                502,
            )
        return False

    def url(self, key):
        quoted = "/".join(urllib.parse.quote(part, safe="") for part in key.split("/"))
        result = self.config.cos_domain.rstrip("/") + "/" + quoted
        if urllib.parse.urlsplit(result).scheme != "https":
            raise MediaRepairError("cos_verification_failed", "COS result URL is not HTTPS", 500)
        return result


def _manifest_matches(manifest, request):
    if not isinstance(manifest, dict) or manifest.get("status") != "ready":
        return False
    stored_request = manifest.get("request")
    if not isinstance(stored_request, dict):
        return False
    return all(stored_request.get(name) == request.get(name) for name in REQUEST_FIELDS)


def _read_manifest(path):
    path = Path(path)
    try:
        if path.is_symlink():
            return None
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _response_from_manifest(manifest, reused):
    result = manifest.get("result") if isinstance(manifest, dict) else None
    if not isinstance(result, dict):
        raise MediaRepairError("manifest_invalid", "repair manifest is invalid", 500)
    required = {
        "job_key",
        "profile",
        "output_url",
        "output_sha256",
        "output_size",
        "probe",
    }
    if not required.issubset(result):
        raise MediaRepairError("manifest_invalid", "repair manifest result is incomplete", 500)
    return {
        "status": "ready",
        "reused": bool(reused),
        "job_key": result["job_key"],
        "profile": result["profile"],
        "output_url": result["output_url"],
        "output_sha256": result["output_sha256"],
        "output_size": result["output_size"],
        "probe": result["probe"],
    }


class MediaRepairProcessor:
    """Synchronous, idempotent repair processor."""

    def __init__(
        self,
        config,
        *,
        runner=None,
        cos_client=None,
        downloader=None,
        http_client=None,
    ):
        self.config = config
        self.runner = runner or subprocess.run
        self.downloader = downloader or download_media
        self.http_client = http_client
        self.cos = CosObjectStore(config, client=cos_client)
        self._gpu_slot = threading.Lock()
        self.manifest_root = _ensure_private_directory(config.work_root / "manifests")
        self.lock_root = _ensure_private_directory(config.work_root / "locks")
        self.job_root = _ensure_private_directory(config.work_root / "work")

    def _cos_key(self, request, output_sha256):
        material_id = _parse_material_id(request.get("material_id"))
        material_segment = (
            "material-%s" % material_id
            if POSITIVE_ID_RE.fullmatch(material_id)
            else "drama-resource-%s" % material_id
        )
        return (
            "%s/%s/%s/source-%s/output-%s.mp4"
            % (
                self.config.cos_prefix,
                self.config.profile,
                material_segment,
                request["source_sha256"],
                output_sha256,
            )
        )

    def _reuse_manifest(self, manifest, request):
        if not _manifest_matches(manifest, request):
            return None
        result = manifest.get("result") or {}
        output_sha256 = str(result.get("output_sha256") or "")
        output_size = result.get("output_size")
        cos_key = str(manifest.get("cos_key") or "")
        output_url = str(result.get("output_url") or "")
        if (
            not HEX_64_RE.fullmatch(output_sha256)
            or not cos_key
            or urllib.parse.urlsplit(output_url).scheme != "https"
        ):
            return None
        try:
            output_size = int(output_size)
        except (TypeError, ValueError, OverflowError):
            return None
        head = self.cos.head(cos_key)
        if not self.cos.validate_head(head, output_size, output_sha256):
            return None
        return _response_from_manifest(manifest, True)

    def repair(self, payload):
        request = validate_request(payload, self.config.profile)
        if request["source_size"] > int(self.config.max_source_bytes):
            raise MediaRepairError(
                "source_too_large",
                "source_size exceeds the configured limit",
                413,
            )
        manifest_path = self.manifest_root / (request["job_key"] + ".json")
        lock_path = self.lock_root / (request["job_key"] + ".lock")
        with _job_flock(lock_path):
            manifest = _read_manifest(manifest_path)
            if manifest_path.exists() and manifest is None:
                raise MediaRepairError(
                    "manifest_invalid",
                    "existing repair manifest cannot be verified",
                    500,
                )
            if manifest is not None and not _manifest_matches(manifest, request):
                raise MediaRepairError(
                    "job_key_conflict",
                    "job_key is already bound to a different repair request",
                    409,
                )
            reusable = self._reuse_manifest(manifest, request)
            if reusable is not None:
                return reusable
            with self._gpu_slot:
                manifest = _read_manifest(manifest_path)
                if manifest_path.exists() and manifest is None:
                    raise MediaRepairError(
                        "manifest_invalid",
                        "existing repair manifest cannot be verified",
                        500,
                    )
                if manifest is not None and not _manifest_matches(manifest, request):
                    raise MediaRepairError(
                        "job_key_conflict",
                        "job_key is already bound to a different repair request",
                        409,
                    )
                reusable = self._reuse_manifest(manifest, request)
                if reusable is not None:
                    return reusable
                return self._repair_locked(request, manifest_path)

    def _repair_locked(self, request, manifest_path):
        job_dir = self.job_root / request["job_key"]
        if job_dir.exists():
            shutil.rmtree(job_dir)
        _ensure_private_directory(job_dir)
        source_path = job_dir / "source.media"
        output_path = job_dir / "repaired.mp4"
        try:
            try:
                downloaded = self.downloader(
                    request["source_url"],
                    source_path,
                    self.config.allowed_hosts,
                    max_bytes=self.config.max_source_bytes,
                    timeout=self.config.download_timeout,
                    http_client=self.http_client,
                )
            except XPostError as exc:
                raise MediaRepairError(exc.code, str(exc), exc.status) from None
            actual_size = int(downloaded.get("size") or 0)
            actual_sha256 = str(downloaded.get("sha256") or "").lower()
            if (
                actual_size != request["source_size"]
                or not secrets.compare_digest(actual_sha256, request["source_sha256"])
            ):
                raise MediaRepairError(
                    "source_integrity_mismatch",
                    "downloaded source does not match caller preflight",
                    409,
                )
            source_probe = _probe_payload(self.config, source_path, self.runner)
            source_info = inspect_source(
                source_probe,
                request["trigger_code"],
                request["duration_policy"],
            )
            command = build_ffmpeg_command(
                self.config,
                source_path,
                output_path,
                source_info,
            )
            _run_command(
                self.runner,
                command,
                self.config.transcode_timeout,
                "media_transcode_failed",
            )
            output_sha256, output_size = _file_sha256(output_path)
            if output_size <= 0 or output_size > int(self.config.max_output_bytes):
                raise MediaRepairError(
                    "repaired_media_invalid",
                    "repaired media size is outside the configured limit",
                    500,
                )
            output_probe = _probe_payload(self.config, output_path, self.runner)
            probe = validate_output(
                output_probe,
                output_path,
                source_info["canvas"],
                self.config.max_output_bytes,
                expected_duration=source_info["output_duration"],
                trim_applied=source_info["trim_applied"],
                max_duration_seconds=source_info["max_duration"],
            )
            if probe["size"] != output_size:
                raise MediaRepairError(
                    "repaired_media_invalid",
                    "repaired media size changed during validation",
                    500,
                )
            cos_key = self._cos_key(request, output_sha256)
            self.cos.upload(cos_key, output_path, output_sha256, output_size)
            output_url = self.cos.url(cos_key)
            result = {
                "job_key": request["job_key"],
                "profile": self.config.profile,
                "output_url": output_url,
                "output_sha256": output_sha256,
                "output_size": output_size,
                "probe": probe,
            }
            manifest = {
                "version": 3,
                "status": "ready",
                "request": request,
                "cos_key": cos_key,
                "result": result,
                "repair": {
                    "source_duration": source_info["duration"],
                    "target_duration": source_info["output_duration"],
                    "trim_applied": source_info["trim_applied"],
                },
                "completed_at": _utc_now(),
            }
            _atomic_write_manifest(manifest_path, manifest)
            return _response_from_manifest(manifest, False)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)


def _is_loopback_client(address):
    try:
        return ipaddress.ip_address(str(address or "")).is_loopback
    except ValueError:
        return False


class MediaRepairHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, processor, token):
        super().__init__(address, MediaRepairRequestHandler)
        self.processor = processor
        self.token = token


class MediaRepairRequestHandler(BaseHTTPRequestHandler):
    """Loopback-only JSON transport.  Request bodies and bearer values are never logged."""

    protocol_version = "HTTP/1.1"
    server_version = "XPostMediaRepair/1"
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

    def _allow_client(self):
        return _is_loopback_client(self.client_address[0] if self.client_address else "")

    def _authorized(self):
        header = str(self.headers.get("Authorization") or "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        expected = str(getattr(self.server, "token", "") or "")
        return bool(token and expected and secrets.compare_digest(token, expected))

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if not self._allow_client():
            self._send_json(403, {"code": "forbidden", "message": "forbidden"})
            return
        if parsed.path != HEALTH_PATH or parsed.query or parsed.fragment:
            self._send_json(404, {"code": "not_found", "message": "not found"})
            return
        self._send_json(
            200,
            {
                "status": "ok",
                "profile": getattr(self.server.processor.config, "profile", REPAIR_PROFILE),
            },
        )

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if not self._allow_client() or not self._authorized():
            self._send_json(403, {"code": "forbidden", "message": "forbidden"})
            return
        if parsed.path != REPAIR_PATH or parsed.query or parsed.fragment:
            self._send_json(404, {"code": "not_found", "message": "not found"})
            return
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(
                415,
                {"code": "invalid_content_type", "message": "application/json required"},
            )
            return
        if str(self.headers.get("Transfer-Encoding") or "").strip():
            self._send_json(
                400,
                {"code": "invalid_request", "message": "chunked requests are not supported"},
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError, OverflowError):
            content_length = 0
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(
                413,
                {"code": "invalid_request", "message": "request body size is invalid"},
            )
            return
        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            self._send_json(
                400,
                {"code": "invalid_json", "message": "request body must be valid JSON"},
            )
            return
        try:
            result = self.server.processor.repair(payload)
        except MediaRepairError as exc:
            self._send_json(
                exc.status,
                {"code": exc.code, "message": str(exc)},
            )
            return
        except Exception:
            self._send_json(
                500,
                {"code": "internal_error", "message": "media repair failed"},
            )
            return
        self._send_json(200, result)
