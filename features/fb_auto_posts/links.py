"""Immutable FB auto-post short links and W2A attribution URLs."""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Mapping


FB_W2A_BASE_URL = "https://www.dramawavew2a.com/ads/0/2049/view"
FB_SHORT_BASE_URL = "https://gy.g2flow.com/s2l/fb"
FB_W2A_QUERY_FIELDS = (
    "c",
    "af_adset",
    "af_adset_id",
    "af_ad",
    "af_ad_id",
    "af_channel",
    "af_c_id",
    "af_dp",
)
_LINK_ID_RE = re.compile(r"[1-9][0-9]{0,18}")


class FBPostLinkError(RuntimeError):
    """Known failure before a Graph write is attempted."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code or "fb_auto_link_error")[:96]
        self.status = int(status)
        super().__init__(str(message or "FB自动发布短链失败")[:500])


def _positive_id(value: Any, label: str = "FB发布任务ID") -> int:
    if isinstance(value, bool):
        raise FBPostLinkError("fb_auto_short_link_id_invalid", f"{label}无效")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = 0
    if result <= 0 or result > 9_223_372_036_854_775_807:
        raise FBPostLinkError("fb_auto_short_link_id_invalid", f"{label}无效")
    return result


def _clean_text(value: Any, label: str, limit: int, *, forbidden: str = "") -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > int(limit)
        or any(ord(char) < 32 for char in text)
        or any(char in text for char in forbidden)
    ):
        raise FBPostLinkError("fb_auto_link_metadata_invalid", f"{label}无效", 409)
    return text


def _clean_token(value: Any, label: str, limit: int) -> str:
    text = _clean_text(value, label, limit)
    if not re.fullmatch(r"[A-Za-z0-9._~:-]+", text):
        raise FBPostLinkError("fb_auto_link_metadata_invalid", f"{label}无效", 409)
    return text


def build_short_url(task_id: Any) -> str:
    normalized = _positive_id(task_id)
    return f"{FB_SHORT_BASE_URL}/{normalized}.html"


def validate_short_url(value: Any) -> str:
    text = str(value or "").strip()
    parsed = urllib.parse.urlsplit(text)
    base = urllib.parse.urlsplit(FB_SHORT_BASE_URL)
    if (
        parsed.scheme != "https"
        or parsed.hostname != base.hostname
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not re.fullmatch(re.escape(base.path) + r"/[1-9][0-9]{0,18}[.]html", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise FBPostLinkError("fb_auto_short_url_invalid", "FB自动发布短链地址无效")
    return text


def build_w2a_url(params: Mapping[str, Any]) -> str:
    """Build the frozen TT-compatible attribution query for one FB task."""

    required = {
        "username",
        "timestamp",
        "material_language",
        "drama_name",
        "tag",
        "task_id",
        "page_name",
        "page_id",
        "material_name",
        "material_id",
        "content_id",
    }
    if not isinstance(params, Mapping) or set(params) != required:
        raise FBPostLinkError("fb_auto_link_metadata_invalid", "FB短链归因字段不完整或包含未知字段", 409)

    username = _clean_text(str(params["username"] or "").lstrip("@"), "Page标识", 50, forbidden="*[]")
    if not re.fullmatch(r"[A-Za-z0-9._]+", username):
        raise FBPostLinkError("fb_auto_link_metadata_invalid", "Page标识无效", 409)
    timestamp = _positive_id(params["timestamp"], "时间戳")
    task_id = _positive_id(params["task_id"])
    language = _clean_text(params["material_language"], "素材语言", 32, forbidden="*[]")
    drama_name = _clean_text(params["drama_name"], "剧名", 255, forbidden="*[]")
    tag = _clean_text(params["tag"], "素材标签", 255, forbidden="*[]")
    page_name = _clean_text(params["page_name"], "Page名称", 255)
    page_id = _clean_token(params["page_id"], "Page ID", 128)
    material_name = _clean_text(params["material_name"], "素材名称", 255)
    material_id = _clean_token(params["material_id"], "素材ID", 128)
    content_id = _clean_token(params["content_id"], "content_id", 128)

    campaign = "yingliang_post_CLV_VL_%s*%snone%s*%s*%s*%s" % (
        username,
        timestamp,
        language,
        drama_name,
        tag,
        task_id,
    )
    query = urllib.parse.urlencode(
        (
            ("c", campaign),
            ("af_adset", page_name),
            ("af_adset_id", page_id),
            ("af_ad", "%s_contentid[%s]" % (material_name, content_id)),
            ("af_ad_id", material_id),
            ("af_channel", "AIpost"),
            ("af_c_id", str(task_id)),
            ("af_dp", content_id),
        ),
        quote_via=urllib.parse.quote,
        safe="*",
    )
    return validate_w2a_url(FB_W2A_BASE_URL + "?" + query)


def validate_w2a_url(value: Any) -> str:
    text = str(value or "")
    parsed = urllib.parse.urlsplit(text)
    base = urllib.parse.urlsplit(FB_W2A_BASE_URL)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.hostname != base.hostname
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != base.path
        or parsed.fragment
        or tuple(key for key, _value in pairs) != FB_W2A_QUERY_FIELDS
        or any(not item for _key, item in pairs)
        or dict(pairs).get("af_channel") != "AIpost"
    ):
        raise FBPostLinkError("fb_auto_short_link_target_invalid", "FB短链目标或归因参数无效", 409)
    return text


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_short_redirect(public_root: Any, task_id: Any, long_url: Any) -> Path:
    """Atomically create one immutable redirect before the Graph POST."""

    target = validate_w2a_url(long_url)
    normalized_id = _positive_id(task_id)
    configured_root = Path(str(public_root or "").strip()).expanduser()
    if not configured_root.is_absolute():
        raise FBPostLinkError("fb_auto_short_link_root_invalid", "FB短链目录必须是绝对路径", 500)
    if configured_root.exists() and configured_root.is_symlink():
        raise FBPostLinkError("fb_auto_short_link_root_invalid", "FB短链目录不能是符号链接", 500)
    try:
        configured_root.mkdir(mode=0o755, parents=False, exist_ok=True)
        root = configured_root.resolve(strict=True)
        if not root.is_dir():
            raise OSError("not a directory")
        os.chmod(root, 0o755)
    except OSError as exc:
        raise FBPostLinkError(
            "fb_auto_short_link_write_failed",
            "FB短链目录不可用: %s" % type(exc).__name__,
            500,
        ) from None

    destination = root / ("%d.html" % normalized_id)
    escaped = html.escape(target, quote=True)
    js_target = (
        json.dumps(target, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    payload = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        '<meta http-equiv="Cache-Control" content="no-store">'
        '<meta http-equiv="refresh" content="0;url=%s">'
        '<link rel="canonical" href="%s"><title>Redirecting</title>'
        '<script>location.replace(%s);</script></head>'
        '<body><a rel="noreferrer" href="%s">Continue</a></body></html>\n'
        % (escaped, escaped, js_target, escaped)
    ).encode("utf-8")
    if destination.is_symlink():
        raise FBPostLinkError("fb_auto_short_link_conflict", "FB短链ID已绑定到不同目标", 409)
    if destination.exists():
        try:
            if destination.is_file() and destination.read_bytes() == payload:
                os.chmod(destination, 0o644)
                return destination
        except OSError:
            pass
        raise FBPostLinkError("fb_auto_short_link_conflict", "FB短链ID已绑定到不同目标", 409)

    temporary_path = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=".%d." % normalized_id, suffix=".tmp", dir=str(root))
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
        os.link(str(temporary_path), str(destination))
        try:
            temporary_path.unlink()
        except OSError:
            pass
        temporary_path = None
        _fsync_directory(root)
        return destination
    except FileExistsError:
        if destination.exists() and not destination.is_symlink() and destination.read_bytes() == payload:
            return destination
        raise FBPostLinkError("fb_auto_short_link_conflict", "FB短链ID已绑定到不同目标", 409) from None
    except OSError as exc:
        raise FBPostLinkError(
            "fb_auto_short_link_write_failed",
            "FB短链页面写入失败: %s" % type(exc).__name__,
            500,
        ) from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


__all__ = [
    "FBPostLinkError",
    "FB_SHORT_BASE_URL",
    "FB_W2A_BASE_URL",
    "FB_W2A_QUERY_FIELDS",
    "build_short_url",
    "build_w2a_url",
    "validate_short_url",
    "validate_w2a_url",
    "write_short_redirect",
]
