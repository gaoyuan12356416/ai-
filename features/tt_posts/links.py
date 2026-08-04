"""TikTok organic-post W2A tracking and immutable short-link wrappers."""

from __future__ import annotations

import html
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Mapping


TT_W2A_BASE_URL = "https://www.dramawavew2a.com/ads/101/2250/view"
TT_SHORT_BASE_URL = "https://gy.g2flow.com/s2l"
TT_SHORT_LINK_NAMESPACE = 8_000_000_000_000_000_000
TT_SHORT_LINK_MAX_LOCAL_ID = 9_223_372_036_854_775_807
TT_DIRECT_TEST_SHORT_LINK_NAMESPACE = 8_500_000_000_000_000_000
TT_DIRECT_TEST_SHORT_LINK_SLOTS = 499_999_999_999_999_999
TT_SHORT_LINK_ID_RE = re.compile(r"8[0-9]{18}")
TT_SHORT_LINK_FILENAME_RE = re.compile(r"8[0-9]{18}[.]html")
TT_AUTO_SHORT_LINK_ID_RE = re.compile(r"[1-9][0-9]{0,18}")
TT_AUTO_SHORT_LINK_FILENAME_RE = re.compile(r"[1-9][0-9]{0,18}[.]html")
TT_W2A_QUERY_FIELDS = (
    "c",
    "af_adset",
    "af_adset_id",
    "af_ad",
    "af_ad_id",
    "af_channel",
    "af_c_id",
    "af_dp",
)


class TTPostLinkError(ValueError):
    """Known pre-publish short-link failure."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)


def _positive_int(
    value: Any,
    label: str,
    maximum: int = 9_223_372_036_854_775_807,
) -> int:
    if isinstance(value, bool):
        raise TTPostLinkError("invalid_request", "%s无效" % label)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise TTPostLinkError("invalid_request", "%s无效" % label) from None
    if parsed <= 0 or parsed > maximum:
        raise TTPostLinkError("invalid_request", "%s无效" % label)
    return parsed


def _clean_text(
    value: Any,
    label: str,
    limit: int,
    *,
    forbidden: str = "",
) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > int(limit)
        or any(ord(char) < 32 for char in text)
        or any(char in text for char in forbidden)
    ):
        raise TTPostLinkError("invalid_request", "%s无效" % label)
    return text


def _clean_token(value: Any, label: str, limit: int) -> str:
    text = _clean_text(value, label, limit)
    if not re.fullmatch(r"[A-Za-z0-9._~:-]+", text):
        raise TTPostLinkError("invalid_request", "%s无效" % label)
    return text


def short_link_id(queue_id: Any) -> int:
    """Use the immutable auto-increment queue identity as the short-link ID."""

    local_id = _positive_int(
        queue_id,
        "TikTok发布任务ID",
        TT_SHORT_LINK_MAX_LOCAL_ID,
    )
    return local_id


def direct_test_short_link_id(identity: Any) -> int:
    """Map one explicit test attempt into a separate TT link namespace."""

    normalized = _clean_text(
        identity,
        "TikTok test publish identity",
        512,
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    local_id = (
        int.from_bytes(digest[:16], "big")
        % TT_DIRECT_TEST_SHORT_LINK_SLOTS
    ) + 1
    return TT_DIRECT_TEST_SHORT_LINK_NAMESPACE + local_id


def build_short_url(link_id: Any) -> str:
    normalized = _positive_int(link_id, "TikTok短链ID")
    text = str(normalized)
    if TT_SHORT_LINK_ID_RE.fullmatch(text):
        # Historical automatic links and direct-test links keep their exact
        # immutable 19-digit URL and filesystem location.
        return "%s/%s.html" % (TT_SHORT_BASE_URL, normalized)
    if not TT_AUTO_SHORT_LINK_ID_RE.fullmatch(text):
        raise TTPostLinkError("tt_short_link_id_invalid", "TikTok短链ID无效")
    return "%s/tt/%s.html" % (TT_SHORT_BASE_URL, normalized)


def validate_short_url(value: Any) -> str:
    url = str(value or "")
    parsed = urllib.parse.urlsplit(url)
    expected_base = urllib.parse.urlsplit(TT_SHORT_BASE_URL)
    filename = parsed.path.rsplit("/", 1)[-1]
    directory = parsed.path.rsplit("/", 1)[0]
    legacy = bool(
        directory == expected_base.path
        and TT_SHORT_LINK_FILENAME_RE.fullmatch(filename)
    )
    automatic = bool(
        directory == expected_base.path + "/tt"
        and TT_AUTO_SHORT_LINK_FILENAME_RE.fullmatch(filename)
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_base.hostname
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not (legacy or automatic)
        or parsed.query
        or parsed.fragment
    ):
        raise TTPostLinkError("tt_short_url_invalid", "TikTok短链地址无效")
    return url


def build_w2a_url(params: Mapping[str, Any]) -> str:
    """Build one immutable TT W2A attribution URL."""

    if not isinstance(params, Mapping):
        raise TTPostLinkError("invalid_request", "W2A参数必须是对象")
    required = {
        "username",
        "timestamp",
        "material_language",
        "drama_name",
        "tag",
        "link_id",
        "page_name",
        "page_id",
        "material_name",
        "material_id",
        "queue_id",
        "content_id",
    }
    allowed = required | {"channel"}
    if not required.issubset(params) or not set(params).issubset(allowed):
        raise TTPostLinkError(
            "invalid_request",
            "W2A参数字段不完整或包含未知字段",
        )
    username = _clean_text(
        str(params["username"] or "").lstrip("@"),
        "TikTok用户名",
        50,
        forbidden="*[]",
    )
    if not re.fullmatch(r"[A-Za-z0-9._]+", username):
        raise TTPostLinkError("invalid_request", "TikTok用户名无效")
    timestamp = _positive_int(params["timestamp"], "时间戳")
    link_id = _positive_int(params["link_id"], "TikTok短链ID")
    language = _clean_text(
        params["material_language"],
        "素材语言",
        32,
        forbidden="*[]",
    )
    drama_name = _clean_text(
        params["drama_name"],
        "剧名",
        255,
        forbidden="*[]",
    )
    tag = _clean_text(params["tag"], "标签", 255, forbidden="*[]")
    page_name = _clean_text(params["page_name"], "TikTok账号名", 255)
    page_id = _clean_token(params["page_id"], "TikTok账号ID", 128)
    material_name = _clean_text(params["material_name"], "素材名", 255)
    material_id = _clean_token(params["material_id"], "素材ID", 128)
    queue_id = _positive_int(params["queue_id"], "队列ID")
    content_id = _clean_token(params["content_id"], "content_id", 128)
    channel = str(params.get("channel") or "TT").strip()
    if channel not in {"TT", "Search", "Featured", "AIpost"}:
        raise TTPostLinkError("invalid_request", "TikTok归因渠道无效")

    campaign = "yingliang_post_CLV_VL_%s*%snone%s*%s*%s*%s" % (
        username,
        timestamp,
        language,
        drama_name,
        tag,
        link_id,
    )
    query = urllib.parse.urlencode(
        (
            ("c", campaign),
            ("af_adset", page_name),
            ("af_adset_id", page_id),
            ("af_ad", "%s_contentid[%s]" % (material_name, content_id)),
            ("af_ad_id", material_id),
            ("af_channel", channel),
            ("af_c_id", str(queue_id)),
            ("af_dp", content_id),
        ),
        quote_via=urllib.parse.quote,
        safe="*",
    )
    return TT_W2A_BASE_URL + "?" + query


def build_w2a_url_from_fields(
    fields: Mapping[str, Any],
    *,
    channel: Any,
) -> str:
    """Rebuild frozen attribution while changing only ``af_channel``."""

    required = {
        "c",
        "af_adset",
        "af_adset_id",
        "af_ad",
        "af_ad_id",
        "af_c_id",
        "af_dp",
    }
    if not isinstance(fields, Mapping) or set(fields) != required:
        raise TTPostLinkError("invalid_request", "TikTok W2A frozen fields are invalid")
    normalized_channel = str(channel or "").strip()
    if normalized_channel not in {"TT", "Search", "Featured", "AIpost"}:
        raise TTPostLinkError("invalid_request", "TikTok attribution channel is invalid")
    normalized = {
        name: _clean_text(fields[name], name, 2048)
        for name in required
    }
    query = urllib.parse.urlencode(
        (
            ("c", normalized["c"]),
            ("af_adset", normalized["af_adset"]),
            ("af_adset_id", normalized["af_adset_id"]),
            ("af_ad", normalized["af_ad"]),
            ("af_ad_id", normalized["af_ad_id"]),
            ("af_channel", normalized_channel),
            ("af_c_id", normalized["af_c_id"]),
            ("af_dp", normalized["af_dp"]),
        ),
        quote_via=urllib.parse.quote,
        safe="*",
    )
    return validate_w2a_url(TT_W2A_BASE_URL + "?" + query)


def build_generic_w2a_url(content_id: Any, channel: Any) -> str:
    """Build the fixed no-history search fallback from trusted values only."""

    normalized_content_id = _clean_token(content_id, "content_id", 128)
    normalized_channel = str(channel or "").strip()
    if normalized_channel not in {"Search", "Featured"}:
        raise TTPostLinkError("invalid_request", "TikTok search source is invalid")
    query = urllib.parse.urlencode(
        (
            ("af_dp", normalized_content_id),
            ("c", "TTpost"),
            ("af_c_id", "0001"),
            ("af_channel", normalized_channel),
        ),
        quote_via=urllib.parse.quote,
    )
    target = TT_W2A_BASE_URL + "?" + query
    parsed = urllib.parse.urlsplit(target)
    base = urllib.parse.urlsplit(TT_W2A_BASE_URL)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.hostname != base.hostname
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != base.path
        or parsed.fragment
        or tuple(key for key, _value in pairs)
        != ("af_dp", "c", "af_c_id", "af_channel")
        or any(not value for _key, value in pairs)
    ):
        raise TTPostLinkError(
            "tt_short_link_target_invalid",
            "TikTok W2A fallback target is invalid",
        )
    return target


def validate_w2a_url(value: Any) -> str:
    url = str(value or "")
    parsed = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(TT_W2A_BASE_URL)
    if (
        parsed.scheme != "https"
        or parsed.hostname != base.hostname
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != base.path
        or parsed.fragment
    ):
        raise TTPostLinkError(
            "tt_short_link_target_invalid",
            "TikTok短链目标不是允许的W2A地址",
        )
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if (
        tuple(key for key, _value in pairs) != TT_W2A_QUERY_FIELDS
        or any(not value for _key, value in pairs)
        or dict(pairs).get("af_channel")
        not in {"AIpost", "TT", "Search", "Featured"}
    ):
        raise TTPostLinkError(
            "tt_short_link_target_invalid",
            "TikTok W2A参数不完整",
        )
    return url


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        str(path),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_short_redirect(public_root: Any, link_id: Any, long_url: Any) -> Path:
    """Atomically create an immutable TT wrapper before Direct Post init."""

    target = validate_w2a_url(long_url)
    normalized_id = _positive_int(link_id, "TikTok短链ID")
    normalized_text = str(normalized_id)
    legacy = bool(TT_SHORT_LINK_ID_RE.fullmatch(normalized_text))
    automatic = bool(TT_AUTO_SHORT_LINK_ID_RE.fullmatch(normalized_text))
    if not (legacy or automatic):
        raise TTPostLinkError(
            "tt_short_link_id_invalid",
            "TikTok短链ID无效",
        )
    configured_root = Path(str(public_root or "").strip()).expanduser()
    if not configured_root.is_absolute():
        raise TTPostLinkError(
            "tt_short_link_root_invalid",
            "TikTok短链目录必须是绝对路径",
            500,
        )
    if configured_root.exists() and configured_root.is_symlink():
        raise TTPostLinkError(
            "tt_short_link_root_invalid",
            "TikTok短链目录不能是符号链接",
            500,
        )
    try:
        configured_root.mkdir(mode=0o755, parents=False, exist_ok=True)
        root = configured_root.resolve(strict=True)
        if not root.is_dir():
            raise OSError("not a directory")
        os.chmod(root, 0o755)
    except OSError as exc:
        raise TTPostLinkError(
            "tt_short_link_write_failed",
            "TikTok短链目录不可用: %s" % type(exc).__name__,
            500,
        ) from None

    destination_root = root
    if automatic and not legacy:
        destination_root = root / "tt"
        if destination_root.exists() and destination_root.is_symlink():
            raise TTPostLinkError(
                "tt_short_link_root_invalid",
                "TikTok自动短链目录不能是符号链接",
                500,
            )
        try:
            destination_root.mkdir(mode=0o755, parents=False, exist_ok=True)
            destination_root = destination_root.resolve(strict=True)
            if destination_root.parent != root or not destination_root.is_dir():
                raise OSError("invalid automatic short-link directory")
            os.chmod(destination_root, 0o755)
        except OSError as exc:
            raise TTPostLinkError(
                "tt_short_link_write_failed",
                "TikTok自动短链目录不可用: %s" % type(exc).__name__,
                500,
            ) from None
    destination = destination_root / ("%s.html" % normalized_id)
    escaped = html.escape(target, quote=True)
    js_target = (
        json.dumps(target, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    payload = (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        "<meta http-equiv=\"Cache-Control\" content=\"no-store\">"
        "<meta http-equiv=\"refresh\" content=\"0;url=%s\">"
        "<link rel=\"canonical\" href=\"%s\"><title>Redirecting</title>"
        "<script>location.replace(%s);</script></head>"
        "<body><a rel=\"noreferrer\" href=\"%s\">Continue</a></body></html>\n"
        % (escaped, escaped, js_target, escaped)
    ).encode("utf-8")
    if destination.exists():
        try:
            if (
                not destination.is_symlink()
                and destination.is_file()
                and destination.read_bytes() == payload
            ):
                os.chmod(destination, 0o644)
                return destination
        except OSError:
            pass
        raise TTPostLinkError(
            "tt_short_link_conflict",
            "TikTok短链ID已存在且目标不同",
            409,
        )

    temporary_path = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".%s." % normalized_id,
            suffix=".tmp",
            dir=str(destination_root),
        )
        temporary_path = Path(raw_path)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        # A hard-link publish is atomic and, unlike ``os.replace``, can never
        # overwrite an immutable wrapper created by a concurrent worker.
        os.link(str(temporary_path), str(destination))
        try:
            temporary_path.unlink()
        except OSError:
            # The immutable destination is already durable. A hidden temporary
            # hard link is harmless and must not turn a valid redirect into a
            # failed publish boundary.
            pass
        temporary_path = None
        _fsync_directory(destination_root)
        return destination
    except FileExistsError:
        if destination.exists() and destination.read_bytes() == payload:
            return destination
        raise TTPostLinkError(
            "tt_short_link_conflict",
            "TikTok短链ID已存在且目标不同",
            409,
        ) from None
    except OSError as exc:
        raise TTPostLinkError(
            "tt_short_link_write_failed",
            "TikTok短链页面写入失败: %s" % type(exc).__name__,
            500,
        ) from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
