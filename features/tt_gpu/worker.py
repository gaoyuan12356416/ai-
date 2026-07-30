"""Fail-closed GPU compositor and TikTok Direct Post sidecar.

The service has four responsibilities:

* prepare an immutable vertical video by normalizing a source material and
  appending the configured fixed tutorial outro;
* render the real ``content_id`` as a prominent ``DRAMA ID`` throughout that
  outro while marking the underlying fixed recording as a tutorial example;
* query TikTok creator capabilities using a short-lived encrypted credential;
* initialize and reconcile a TikTok Direct Post using ``PULL_FROM_URL``.

The worker listens on loopback only.  Its transport bearer is distinct from
the AES-GCM credential-seal key.  Plaintext TikTok tokens are never accepted as
normal fields and are never written to disk, logs, manifests, or exception
messages.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .credentials import CredentialEnvelopeError, decode_seal_key, open_access_token

try:  # Production Linux uses flock; Windows tests use the in-process fallback.
    import fcntl
except ImportError:  # pragma: no cover - Windows-only branch.
    fcntl = None


PROFILE = "tt-post-h264-1080x1920-v1"
HEALTH_PATH = "/health"
CREATOR_INFO_PATH = "/internal/tt-post/creator-info"
PREPARE_PATH = "/internal/tt-post/prepare"
PUBLISH_PATH = "/internal/tt-post/publish"
RECONCILE_PATH = "/internal/tt-post/reconcile"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8830
DEFAULT_WORK_ROOT = Path("/data/tt-post-publisher")
DEFAULT_FFMPEG_BIN = "/opt/ffmpeg-nvenc/ffmpeg"
DEFAULT_FFPROBE_BIN = "/opt/ffmpeg-nvenc/ffprobe"
DEFAULT_FONT_FILE = "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"
DEFAULT_TRANSITION_SECONDS = 0.9
DEFAULT_COS_PREFIX = "tt-post-prepared"
DEFAULT_MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
# TikTok creator_info currently allows up to 3,600 seconds for some accounts.
# The CPU still enforces the selected account's live creator limit before a
# queue can be frozen, so the GPU-wide ceiling only needs to be broad enough to
# prepare a valid long-form candidate.
DEFAULT_MAX_DURATION_SECONDS = 3600
DEFAULT_DOWNLOAD_TIMEOUT = 120
DEFAULT_PROBE_TIMEOUT = 120
DEFAULT_TRANSCODE_TIMEOUT = 3600
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
TIKTOK_API_ORIGIN = "https://open.tiktokapis.com"
TIKTOK_CREATOR_INFO_PATH = "/v2/post/publish/creator_info/query/"
TIKTOK_VIDEO_INIT_PATH = "/v2/post/publish/video/init/"
TIKTOK_STATUS_FETCH_PATH = "/v2/post/publish/status/fetch/"
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
PREPARE_REQUIRED_FIELDS = frozenset({"job_id", "content_id", "source_url"})
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
    for key in ("log_id", "publish_id", "state"):
        item = value.get(key)
        if isinstance(item, str) and len(item) <= 512 and "\x00" not in item:
            result[key] = item
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
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
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


def _normalize_prefix(value):
    text = str(value or DEFAULT_COS_PREFIX).strip().strip("/")
    if (
        not text
        or ".." in text.split("/")
        or not SAFE_PREFIX_RE.fullmatch(text)
    ):
        raise TTGPUError(
            "invalid_configuration",
            "TT_POST_GPU_COS_PREFIX is invalid",
            500,
        )
    return text


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
    live_enabled: bool = False
    direct_audit_approved: bool = False
    url_property_verified: bool = False
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS
    default_source_trim_tail_seconds: float = 4.333333
    download_timeout: int = DEFAULT_DOWNLOAD_TIMEOUT
    probe_timeout: int = DEFAULT_PROBE_TIMEOUT
    transcode_timeout: int = DEFAULT_TRANSCODE_TIMEOUT
    profile: str = PROFILE

    @classmethod
    def from_env(cls):
        if not _env_bool("TT_POST_GPU_ENABLED", False):
            raise TTGPUError(
                "tt_gpu_disabled",
                "TikTok GPU publisher is disabled",
                503,
            )
        host = str(os.environ.get("TT_POST_GPU_HOST", DEFAULT_HOST) or "").strip()
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("not loopback")
        except ValueError:
            raise TTGPUError(
                "invalid_configuration",
                "TT_POST_GPU_HOST must be a loopback address",
                500,
            ) from None
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
            os.environ.get("TT_POST_GPU_VIDEO_ENCODER", "h264_nvenc") or ""
        ).strip()
        if encoder not in {"h264_nvenc", "libx264"}:
            raise TTGPUError(
                "invalid_configuration",
                "TT_POST_GPU_VIDEO_ENCODER must be h264_nvenc or libx264",
                500,
            )
        secret_id = str(os.environ.get("TT_POST_GPU_COS_SECRET_ID", "") or "").strip()
        secret_key = str(os.environ.get("TT_POST_GPU_COS_SECRET_KEY", "") or "").strip()
        bucket = str(os.environ.get("TT_POST_GPU_COS_BUCKET", "") or "").strip()
        region = str(os.environ.get("TT_POST_GPU_COS_REGION", "") or "").strip()
        if not all((secret_id, secret_key, bucket, region)):
            raise TTGPUError(
                "invalid_configuration",
                "dedicated TT GPU COS configuration is required",
                500,
            )
        return cls(
            enabled=True,
            host=host,
            port=_env_int("TT_POST_GPU_PORT", DEFAULT_PORT, 1, 65535),
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
            cos_secret_id=secret_id,
            cos_secret_key=secret_key,
            cos_bucket=bucket,
            cos_region=region,
            cos_domain=_normalize_https_origin(
                os.environ.get("TT_POST_GPU_COS_DOMAIN", ""),
                "TT_POST_GPU_COS_DOMAIN",
            ),
            cos_prefix=_normalize_prefix(
                os.environ.get("TT_POST_GPU_COS_PREFIX", DEFAULT_COS_PREFIX)
            ),
            live_enabled=_env_bool("TT_POST_LIVE_ENABLED", False),
            direct_audit_approved=_env_bool(
                "TT_POST_DIRECT_AUDIT_APPROVED",
                False,
            ),
            url_property_verified=_env_bool(
                "TT_POST_URL_PROPERTY_VERIFIED",
                False,
            ),
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
        )

    def gate_state(self):
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
            ),
        }


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
def _job_lock(path):
    path = Path(path)
    _ensure_private_directory(path.parent)
    if fcntl is None:
        key = str(path.resolve())
        with _FALLBACK_LOCKS_GUARD:
            lock = _FALLBACK_LOCKS.setdefault(key, threading.Lock())
        with lock:
            yield
        return
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _file_sha256(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
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
            timeout=config.download_timeout,
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


def _run_command(runner, command, timeout, error_code):
    try:
        completed = runner(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(timeout),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise TTGPUError(error_code, "media command failed", 500) from None
    if int(getattr(completed, "returncode", 1) or 0) != 0:
        raise TTGPUError(error_code, "media command failed", 500)
    return completed


def probe_media(config, path, runner=subprocess.run):
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
        config.probe_timeout,
        "media_probe_failed",
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


def validate_prepared_output(payload, path, max_size, expected_duration):
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
    audio_profile = str(audio.get("profile") or "").strip().lower()
    if (
        str(video.get("codec_name") or "").lower() != "h264"
        or profile != "high"
        or str(video.get("pix_fmt") or "").lower() != "yuv420p"
        or (width, height) != (1080, 1920)
        or abs(rate - 30.0) > 0.01
        or str(audio.get("codec_name") or "").lower() != "aac"
        or audio_profile != "lc"
        or sample_rate != 48000
        or channels != 2
        or size <= 0
        or size > int(max_size)
        or duration <= 0
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
        "profile": "high",
        "size": size,
        "video_codec": "h264",
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
        "scale=w=1080:h=1920:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1,fps=30,format=yuv420p"
    )


def build_outro_filter(config, drama_id_text_path, tutorial_text_path):
    font = _ffmpeg_filter_path(config.font_file)
    drama_text = _ffmpeg_filter_path(drama_id_text_path)
    tutorial_text = _ffmpeg_filter_path(tutorial_text_path)
    return ",".join(
        [
            _base_video_filter(),
            "drawbox=x=42:y=60:w=996:h=170:color=black@0.78:t=fill",
            "drawbox=x=42:y=60:w=14:h=170:color=0xFF2E88@1.0:t=fill",
            (
                "drawtext=fontfile='%s':textfile='%s':"
                "fontcolor=white:fontsize=64:x=80:y=88"
            )
            % (font, drama_text),
            (
                "drawtext=fontfile='%s':textfile='%s':"
                "fontcolor=white:fontsize=30:x=(w-text_w)/2:y=h-142:"
                "box=1:boxcolor=black@0.72:boxborderw=18"
            )
            % (font, tutorial_text),
        ]
    )


def _encoder_arguments(config):
    if config.video_encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-profile:v",
            "high",
            "-rc",
            "vbr",
            "-cq",
            "20",
            "-b:v",
            "8M",
            "-maxrate",
            "10M",
            "-bufsize",
            "16M",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "19",
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
    ]


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
                    "[0:v]%s[base];[%s:v]scale=198:198:flags=lanczos[logo];"
                    "[base][logo]overlay=72:108:format=auto[v]"
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
            "160k",
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
):
    """Overlap the last source frame into the playing outro with a phone match."""

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
    video_graph = (
        "[0:v]trim=start=0:end=%(start)s,setpts=PTS-STARTPTS[pre];"
        "[0:v]trim=start=%(start)s:end=%(source_end)s,setpts=PTS-STARTPTS,"
        "scale=w='trunc((1080-320*%(progress)s)/2)*2':"
        "h='trunc((1920-568*%(progress)s)/2)*2':"
        "eval=frame,format=rgba,"
        "fade=t=out:st=%(fade_start)s:d=0.250000:alpha=1[foreground];"
        "[1:v]trim=start=0:end=%(transition)s,setpts=PTS-STARTPTS[background];"
        "[background][foreground]overlay=x=(W-w)/2:y=(H-h)/2:"
        "shortest=1:format=auto[bridge];"
        "[1:v]trim=start=%(transition)s:end=%(outro_end)s,"
        "setpts=PTS-STARTPTS[post];"
        "[pre][bridge][post]concat=n=3:v=1:a=0[outv];"
        "[0:a]afade=t=out:st=%(start)s:d=%(transition)s[sa];"
        "[1:a]adelay=%(delay)d|%(delay)d[oa];"
        "[sa][oa]amix=inputs=2:duration=longest:normalize=0,"
        "alimiter=limit=0.95[outa]"
    ) % {
        "delay": int(round(transition_start * 1000)),
        "fade_start": fade_start_text,
        "outro_end": outro_duration_text,
        "progress": scale_progress,
        "source_end": source_duration_text,
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
            "160k",
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
            Timeout=max(60, self.config.transcode_timeout),
            KeepAlive=False,
        )
        return CosS3Client(cos_config)

    def head(self, key):
        try:
            return self.client.head_object(
                Bucket=self.config.cos_bucket,
                Key=key,
            )
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

    def upload(self, key, local_path, sha256_value, size):
        existing = self.head(key)
        if existing is not None:
            if self._head_matches(existing, size, sha256_value):
                return True
            raise TTGPUError(
                "cos_object_conflict",
                "existing prepared object failed integrity verification",
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
        except Exception:
            raise TTGPUError("cos_upload_failed", "COS upload failed", 502) from None
        if not self._head_matches(self.head(key), size, sha256_value):
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
            try:
                raw = exc.read(MAX_RESPONSE_BYTES)
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {}
            finally:
                exc.close()
            code = _upstream_error_code(data)
            log_id = _upstream_log_id(data)
            raise TTGPUError(
                "tt_upstream_rejected",
                "TikTok API rejected the request (%s)" % code,
                502,
                {"log_id": log_id} if log_id else None,
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
                safe_code = code if SAFE_UPSTREAM_CODE_RE.fullmatch(code) else "rejected"
                raise TTGPUError(
                    "tt_upstream_rejected",
                    "TikTok API rejected the request (%s)" % safe_code,
                    502,
                    {"log_id": log_id} if log_id else None,
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
    result = {
        "content_id": content_id,
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


def _prepare_response(manifest, reused):
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
    return {
        "brand_overlay_review_required": bool(
            result.get("brand_overlay_review_required", True)
        ),
        "content_id": result["content_id"],
        "direct_post_eligible": result.get("direct_post_eligible") is True,
        "job_id": result["job_id"],
        "output_sha256": result["output_sha256"],
        "output_size": result["output_size"],
        "output_url": result["output_url"],
        "probe": result["probe"],
        "profile": result["profile"],
        "reused": bool(reused),
        "status": "ready",
    }


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
    ):
        self.config = config
        self.runner = runner or subprocess.run
        self.downloader = downloader or download_source
        self._object_store_instance = object_store
        self.tiktok_api = tiktok_api or TikTokContentPostingAPI()
        self._gpu_slot = threading.Lock()
        self.manifest_root = _ensure_private_directory(config.work_root / "manifests")
        self.publish_root = _ensure_private_directory(config.work_root / "publishes")
        self.lock_root = _ensure_private_directory(config.work_root / "locks")
        self.jobs_root = _ensure_private_directory(config.work_root / "jobs")

    def _object_store(self):
        if self._object_store_instance is None:
            self._object_store_instance = CosObjectStore(self.config)
        return self._object_store_instance

    def _prepare_manifest_path(self, job_id):
        return self.manifest_root / ("%s.json" % job_id)

    def _publish_ledger_path(self, job_id):
        return self.publish_root / ("%s.json" % job_id)

    def _lock_path(self, job_id):
        return self.lock_root / ("%s.lock" % job_id)

    def _cos_key(self, output_sha):
        return "%s/%s/%s.mp4" % (
            self.config.cos_prefix,
            output_sha[:2],
            output_sha,
        )

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
        with _job_lock(self._lock_path(job_id)):
            return self._prepare_locked(request)

    def _prepare_locked(self, request):
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
        existing = _read_json(manifest_path)
        if existing is not None:
            stored_request = existing.get("request")
            if (
                existing.get("status") == "ready"
                and isinstance(stored_request, dict)
                and all(
                    stored_request.get(key) == value
                    for key, value in reuse_contract.items()
                )
            ):
                return _prepare_response(existing, True)
            raise TTGPUError(
                "prepare_idempotency_conflict",
                "job_id already belongs to a different prepared artifact",
                409,
            )
        outro_sha, outro_size = _file_sha256(self.config.fixed_outro_path)
        logo_sha, logo_size = _file_sha256(self.config.logo_path)
        job_dir = Path(
            tempfile.mkdtemp(prefix=job_id + ".", dir=str(self.jobs_root))
        )
        os.chmod(job_dir, 0o700)
        source_path = job_dir / "source.mp4"
        source_normalized = job_dir / "source-normalized.mp4"
        outro_normalized = job_dir / "outro-normalized.mp4"
        output_path = job_dir / "prepared.mp4"
        drama_text_path = job_dir / "drama-id.txt"
        tutorial_text_path = job_dir / "tutorial-label.txt"
        try:
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
            )
            if not isinstance(source_actual, dict):
                source_sha, source_size = _file_sha256(source_path)
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
                "logo_sha256": logo_sha,
                "logo_size": logo_size,
                "outro_sha256": outro_sha,
                "outro_size": outro_size,
                "source_sha256": source_sha,
                "source_size": source_size,
                "transition": "phone-match-0.9s",
            }
            source_probe = probe_media(self.config, source_path, self.runner)
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
            outro_probe = probe_media(
                self.config,
                self.config.fixed_outro_path,
                self.runner,
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
                    "source plus fixed outro exceeds the configured duration",
                    400,
                )
            with self._gpu_slot:
                _run_command(
                    self.runner,
                    build_normalize_command(
                        self.config,
                        source_path,
                        source_normalized,
                        source_info,
                        _base_video_filter(),
                        logo_path=self.config.logo_path,
                        output_duration=effective_source_duration,
                    ),
                    self.config.transcode_timeout,
                    "source_transcode_failed",
                )
                _run_command(
                    self.runner,
                    build_normalize_command(
                        self.config,
                        self.config.fixed_outro_path,
                        outro_normalized,
                        outro_info,
                        build_outro_filter(
                            self.config,
                            drama_text_path,
                            tutorial_text_path,
                        ),
                    ),
                    self.config.transcode_timeout,
                    "outro_transcode_failed",
                )
                _run_command(
                    self.runner,
                    build_phone_match_command(
                        self.config,
                        source_normalized,
                        outro_normalized,
                        output_path,
                        effective_source_duration,
                        outro_info["duration"],
                    ),
                    self.config.transcode_timeout,
                    "phone_match_transition_failed",
                )
            output_sha, output_size = _file_sha256(output_path)
            if output_size <= 0 or output_size > int(self.config.max_output_bytes):
                raise TTGPUError(
                    "prepared_media_invalid",
                    "prepared output size is outside the contract",
                    500,
                )
            output_probe = probe_media(self.config, output_path, self.runner)
            safe_probe = validate_prepared_output(
                output_probe,
                output_path,
                self.config.max_output_bytes,
                expected_duration,
            )
            cos_key = self._cos_key(output_sha)
            reused = self._object_store().upload(
                cos_key,
                output_path,
                output_sha,
                output_size,
            )
            output_url = self._object_store().url(cos_key)
            if not output_url.startswith(self.config.cos_domain.rstrip("/") + "/"):
                raise TTGPUError(
                    "cos_verification_failed",
                    "prepared URL is outside the configured pull origin",
                    500,
                )
            result = {
                "brand_overlay_review_required": True,
                "content_id": request["content_id"],
                "direct_post_eligible": False,
                "job_id": job_id,
                "output_sha256": output_sha,
                "output_size": output_size,
                "output_url": output_url,
                "probe": safe_probe,
                "profile": self.config.profile,
            }
            manifest = {
                "completed_at": _utc_now(),
                "cos_key": cos_key,
                "object_reused": bool(reused),
                "request": request_fingerprint,
                "result": result,
                "status": "ready",
                "version": 1,
            }
            _atomic_write_json(manifest_path, manifest)
            return _prepare_response(manifest, False)
        finally:
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
            return self._publish_locked(request)

    def _publish_locked(self, request):
        job_id = request["job_id"]
        prepare_manifest = _read_json(self._prepare_manifest_path(job_id))
        if not prepare_manifest or prepare_manifest.get("status") != "ready":
            raise TTGPUError(
                "prepared_artifact_not_found",
                "prepare must complete before publish",
                409,
            )
        prepared = _prepare_response(prepare_manifest, True)
        if not prepared["direct_post_eligible"]:
            raise TTGPUError(
                "tt_media_profile_not_direct_post_eligible",
                "prepared media profile is not eligible for TikTok Direct Post",
                403,
                {"profile": prepared["profile"]},
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
            "updated_at": _utc_now(),
            "version": 1,
        }
        try:
            with open_access_token(
                request["credential_envelope"],
                self.config.credential_seal_key,
                job_id=job_id,
                source_account_id=request["source_account_id"],
                operation="publish",
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
            return {
                "job_id": job_id,
                "publish_id": publish_id,
                "state": state,
                "status": upstream,
            }


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
                "brand_overlay_review_required": True,
                "direct_post_eligible": False,
                "gates": self.server.processor.config.gate_state(),
                "profile": self.server.processor.config.profile,
                "status": "ok",
                "transition": "phone-match-0.9s",
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


def serve():
    config = WorkerConfig.from_env()
    processor = TTPostGPUProcessor(config)
    server = TTPostGPUHTTPServer(
        (config.host, config.port),
        processor,
        config.internal_token,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
