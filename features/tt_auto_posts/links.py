"""Independent immutable short links and caption rendering for TT auto posts."""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from features.tt_posts.core import (
    TTPostError,
    render_caption_template,
)
from features.tt_posts.links import build_w2a_url, validate_w2a_url


AUTO_SHORT_BASE_URL = "https://gy.g2flow.com/s2l/tt-auto"
MAX_LINK_ID = 9_223_372_036_854_775_807


class AutoPostLinkError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code or "tt_auto_link_error")[:80]
        self.status = int(status)
        super().__init__(str(message or "TT auto short link failed")[:500])


def _positive_id(value: Any) -> int:
    if isinstance(value, bool):
        raise AutoPostLinkError("tt_auto_link_id_invalid", "短链ID无效")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = 0
    if result <= 0 or result > MAX_LINK_ID:
        raise AutoPostLinkError("tt_auto_link_id_invalid", "短链ID无效")
    return result


def build_auto_short_url(link_id: Any) -> str:
    return "%s/%d.html" % (AUTO_SHORT_BASE_URL, _positive_id(link_id))


def validate_auto_short_url(value: Any) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    base = urlsplit(AUTO_SHORT_BASE_URL)
    if (
        parsed.scheme != "https"
        or parsed.hostname != base.hostname
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not re.fullmatch(
            re.escape(base.path) + r"/[1-9][0-9]{0,18}[.]html",
            parsed.path,
        )
        or parsed.query
        or parsed.fragment
    ):
        raise AutoPostLinkError("tt_auto_short_url_invalid", "短链地址无效")
    return text


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_auto_short_redirect(public_root: Any, link_id: Any, long_url: Any) -> Path:
    target = validate_w2a_url(long_url)
    normalized_id = _positive_id(link_id)
    configured_root = Path(str(public_root or "").strip()).expanduser()
    if not configured_root.is_absolute():
        raise AutoPostLinkError(
            "tt_auto_short_link_root_invalid", "短链目录必须是绝对路径", 500
        )
    if configured_root.exists() and configured_root.is_symlink():
        raise AutoPostLinkError(
            "tt_auto_short_link_root_invalid", "短链目录不能是符号链接", 500
        )
    try:
        configured_root.mkdir(mode=0o755, parents=True, exist_ok=True)
        root = configured_root.resolve(strict=True)
        if not root.is_dir():
            raise OSError("not a directory")
        destination_root = root / "tt-auto"
        destination_root.mkdir(mode=0o755, exist_ok=True)
        destination_root = destination_root.resolve(strict=True)
        if destination_root.parent != root or destination_root.is_symlink():
            raise OSError("invalid short-link directory")
    except OSError as exc:
        raise AutoPostLinkError(
            "tt_auto_short_link_write_failed",
            "短链目录不可用: %s" % type(exc).__name__,
            500,
        ) from None

    destination = destination_root / ("%d.html" % normalized_id)
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
    if destination.exists():
        try:
            if not destination.is_symlink() and destination.read_bytes() == payload:
                return destination
        except OSError:
            pass
        raise AutoPostLinkError(
            "tt_auto_short_link_conflict", "短链ID已绑定到不同目标", 409
        )

    temporary: Optional[Path] = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".%d." % normalized_id,
            suffix=".tmp",
            dir=str(destination_root),
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.link(str(temporary), str(destination))
        temporary.unlink()
        temporary = None
        _fsync_directory(destination_root)
        return destination
    except FileExistsError:
        if destination.exists() and destination.read_bytes() == payload:
            return destination
        raise AutoPostLinkError(
            "tt_auto_short_link_conflict", "短链ID已绑定到不同目标", 409
        ) from None
    except OSError as exc:
        raise AutoPostLinkError(
            "tt_auto_short_link_write_failed",
            "短链写入失败: %s" % type(exc).__name__,
            500,
        ) from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def build_auto_w2a_url(
    *,
    link_id: Any,
    username: Any,
    timestamp: Any,
    language: Any,
    drama_name: Any,
    tag: Any,
    page_name: Any,
    page_id: Any,
    material_name: Any,
    material_id: Any,
    content_id: Any,
) -> str:
    normalized_id = _positive_id(link_id)
    # The read-only TikTok account snapshot exposes an external account
    # identity rather than a guaranteed public handle.  Keep the automatic
    # flow aligned with the legacy TT pool by normalizing that identity into
    # the conservative character set accepted by the W2A campaign contract.
    # Page ID remains the stable fallback and is also carried separately as
    # af_adset_id, so attribution does not depend on a guessed public handle.
    tracking_username = re.sub(
        r"[^A-Za-z0-9._]",
        "_",
        str(username or "").lstrip("@"),
    ).strip("_")
    if not tracking_username or len(tracking_username) > 50:
        tracking_username = re.sub(
            r"[^A-Za-z0-9._]",
            "_",
            str(page_id or ""),
        ).strip("_")
    return build_w2a_url(
        {
            "username": tracking_username,
            "timestamp": timestamp,
            "material_language": language,
            "drama_name": drama_name,
            "tag": tag or "TTauto",
            "link_id": normalized_id,
            "page_name": page_name,
            "page_id": page_id,
            "material_name": material_name,
            "material_id": material_id,
            "queue_id": normalized_id,
            "content_id": content_id,
            "channel": "TT",
            "af_dp_first": True,
        }
    )


def render_auto_caption(
    template: Any,
    content_id: Any,
    *,
    short_url: Any,
    description: Any,
    code: Any = None,
) -> str:
    normalized_url = validate_auto_short_url(short_url)
    try:
        rendered = render_caption_template(
            template,
            content_id,
            description=description,
            code=code,
            defer_url=True,
        )
    except TTPostError as exc:
        raise AutoPostLinkError(
            str(getattr(exc, "code", "tt_auto_caption_invalid")),
            str(exc),
            int(getattr(exc, "status", 400) or 400),
        ) from None
    rendered = rendered.replace("{url}", normalized_url).strip()
    if "{url}" in rendered:
        raise AutoPostLinkError("tt_auto_caption_invalid", "文案URL变量无效")
    try:
        units = len(rendered.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        units = 2201
    if not rendered or units > 2200:
        raise AutoPostLinkError("tt_auto_caption_length_invalid", "发布文案长度无效")
    return rendered


__all__ = [
    "AutoPostLinkError",
    "build_auto_short_url",
    "build_auto_w2a_url",
    "render_auto_caption",
    "validate_auto_short_url",
    "write_auto_short_redirect",
]
