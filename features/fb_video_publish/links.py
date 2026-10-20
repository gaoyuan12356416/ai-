"""Facebook-only frozen attribution and immutable short-link file publication."""
from __future__ import annotations

import hashlib
import html
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from .templates import TemplateError

BASE_URL = "https://www.dramawavew2a.com/ads/101/2310/view"
SHORT_BASE_URL = "https://gy.g2flow.com/s2l/facebook"
ATTRIBUTION_VERSION = "facebook-video-attribution-v1"
PARAMETERS = ("c", "af_adset", "af_adset_id", "af_ad", "af_ad_id", "af_channel", "af_c_id", "af_dp")


def _wrapper(long_url: str) -> bytes:
    escaped = html.escape(long_url, quote=True)
    return (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        f'<meta http-equiv="refresh" content="0;url={escaped}">'
        f'<link rel="canonical" href="{escaped}"><title>DramaWave</title>'
        "<script>(function(){var u=new URL(document.querySelector('link[rel=canonical]').href);"
        "var p=new URLSearchParams(location.search),f=p.get('fbclid');"
        "if(f&&f.length<=512){u.searchParams.set('fbclid',f)}location.replace(u.toString())})()</script>"
        "</head><body></body></html>\n"
    ).encode("utf-8")


def _publish_file(root, ledger_id: int, body: bytes, owner_user: str) -> dict:
    """Same no-overwrite contract as YouTube, with no dependency on its runtime."""
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise TemplateError("short_link_path_invalid", "短链根目录无效", 503)
    owner = None
    if owner_user:
        if os.name == "nt":
            raise TemplateError("short_link_owner_invalid", "生产短链属主校验需要Linux", 503)
        import pwd
        try:
            owner = pwd.getpwnam(owner_user)
        except KeyError:
            raise TemplateError("short_link_owner_invalid", "短链属主不存在", 503) from None
    else:
        root.mkdir(mode=0o750, parents=True, exist_ok=True)

    def check(path, directory=False):
        if path.is_symlink():
            raise TemplateError("short_link_path_invalid", "短链路径不得为符号链接", 503)
        metadata = path.stat()
        valid_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
        if not valid_type or (owner and (metadata.st_uid != owner.pw_uid or metadata.st_gid != owner.pw_gid
                                        or stat.S_IMODE(metadata.st_mode) != (0o750 if directory else 0o640))):
            raise TemplateError("short_link_owner_invalid", "短链目录或文件属主权限无效", 503)

    check(root, True)
    target = root / (str(_ledger_id(ledger_id)) + ".html")
    if target.is_symlink():
        raise TemplateError("short_link_path_invalid", "短链路径不得为符号链接", 503)
    if target.exists():
        check(target)
        if target.read_bytes() != body:
            raise TemplateError("short_link_immutable_conflict", "短链ID已存在且目标不一致", 409)
        return {"reused": True, "sha256": hashlib.sha256(body).hexdigest()}
    temporary, created = None, False
    try:
        descriptor, filename = tempfile.mkstemp(prefix=target.name + ".tmp.", dir=str(root))
        temporary = Path(filename)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            if owner:
                os.fchmod(handle.fileno(), 0o640)
                os.fchown(handle.fileno(), owner.pw_uid, owner.pw_gid)
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
            created = True
            check(target)
        except FileExistsError:
            check(target)
            if target.read_bytes() != body:
                raise TemplateError("short_link_immutable_conflict", "短链ID已存在且目标不一致", 409)
        if created and hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    check(target)
    if target.read_bytes() != body:
        raise TemplateError("short_link_readback_failed", "短链文件读回不一致", 503)
    return {"reused": not created, "sha256": hashlib.sha256(body).hexdigest()}


def _positive(value: Any, field: str, *, max_digits: int = 40) -> str:
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0," + str(max_digits - 1) + "}", result):
        raise TemplateError("attribution_invalid", field + "无效", 409)
    return result


def _ledger_id(value: Any) -> int:
    result = int(_positive(value, "发布记录ID", max_digits=19))
    if result > 9223372036854775807:
        raise TemplateError("attribution_invalid", "发布记录ID无效", 409)
    return result


def short_url(ledger_id: int) -> str:
    return SHORT_BASE_URL + "/" + str(_ledger_id(ledger_id)) + ".html"


def _text(value: Any, field: str, *, campaign: bool = False, maximum: int = 512) -> str:
    value = str(value or "").strip()
    if (not value or len(value) > maximum or any(ord(char) < 32 for char in value)
            or (campaign and "*" in value)):
        raise TemplateError("attribution_invalid", "长链归因字段无效：" + field, 409)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise TemplateError("attribution_invalid", "长链归因字段编码无效：" + field, 409) from None
    return value


def build_attribution_url(context: Mapping[str, Any], ledger_id: int, created_at_utc: str) -> str:
    record = str(_ledger_id(ledger_id))
    try:
        created = datetime.fromisoformat(str(created_at_utc).replace("Z", "+00:00"))
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("timezone required")
        stamp = int(created.astimezone(timezone.utc).timestamp())
        if stamp <= 0:
            raise ValueError("invalid timestamp")
    except (TypeError, ValueError, OverflowError):
        raise TemplateError("attribution_invalid", "发布记录创建时间无效", 409) from None
    page_id = _positive(context.get("page_id", ""), "Page ID")
    material_id = _positive(context.get("material_id", ""), "素材ID", max_digits=19)
    user_id = _positive(context.get("sub_user_id", ""), "发布人sub_user_id")
    task_id = str(context.get("task_id") or "")
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise TemplateError("attribution_invalid", "发布任务ID无效", 409)
    language = _text(context.get("language"), "language", campaign=True, maximum=32)
    drama = _text(context.get("drama_name"), "drama_name", campaign=True)
    tag = _text(context.get("tag") or context.get("material_tag") or "none", "tag", campaign=True)
    content_id = _text(context.get("content_id"), "content_id", maximum=256)
    page_name = _text(context.get("page_name"), "page_name")
    material_name = _text(context.get("material_name"), "material_name")
    campaign = f"yingliang_post_CLV_VL_facebook_{page_id}*{stamp}none{language}*{drama}*{tag}*{record}"
    return BASE_URL + "?" + urlencode([
        ("c", campaign), ("af_adset", page_name), ("af_adset_id", page_id),
        ("af_ad", material_name + "_contentid[" + content_id + "]"),
        ("af_ad_id", material_id), ("af_channel", user_id),
        ("af_c_id", task_id), ("af_dp", content_id),
    ], quote_via=quote, safe="*")


def validate_long_url(long_url: str, ledger_id: int) -> None:
    try:
        parsed = urlsplit(long_url)
        values = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        data = dict(values)
        campaign = data.get("c", "").split("*")
        if (long_url.split("?", 1)[0] != BASE_URL or parsed.fragment or parsed.username or parsed.password
                or tuple(key for key, _ in values) != PARAMETERS or not all(value for _, value in values)
                or not re.fullmatch(r"[a-f0-9]{32}", data["af_c_id"])
                or len(campaign) != 5
                or campaign[0] != "yingliang_post_CLV_VL_facebook_" + data["af_adset_id"]
                or not re.fullmatch(r"[1-9][0-9]*none[a-z0-9-]+", campaign[1])
                or campaign[-1] != str(_ledger_id(ledger_id))
                or not data["af_ad"].endswith("_contentid[" + data["af_dp"] + "]")
                or any(ord(char) < 32 for _, value in values for char in value)):
            raise ValueError("invalid frozen attribution")
        _positive(data["af_adset_id"], "Page ID")
        _positive(data["af_ad_id"], "素材ID", max_digits=19)
        _positive(data["af_channel"], "发布人sub_user_id")
    except (TypeError, ValueError, KeyError):
        raise TemplateError("attribution_invalid", "Facebook归因长链与发布记录不一致", 409) from None


def publish_link(root, ledger_id: int, long_url: str, owner_user: str = "") -> dict:
    """Only a worker may call this after the database has frozen URL and ID."""
    validate_long_url(long_url, ledger_id)
    body = _wrapper(long_url)
    expected = hashlib.sha256(body).hexdigest()
    result = _publish_file(root, ledger_id, body, owner_user)
    if result.get("sha256") != expected:
        raise TemplateError("short_link_readback_failed", "短链文件读回不一致", 503)
    return {"short_url": short_url(ledger_id), "long_url": long_url, "sha256": expected,
            "reused": bool(result.get("reused"))}
