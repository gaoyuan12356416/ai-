"""Single-pass Facebook caption macros. Never allocate a task or a link here."""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

CAPTION_MAX_BYTES = 5000
TEMPLATE_MAX_BYTES = 16000
MACROS = {"name": "drama_name", "desc": "drama_description", "url": "short_url"}
TOKEN_RE = re.compile(r"\{(name|desc|url)\}")
PREVIEW_LINK = "[提交后生成独立短链]"


class TemplateError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(message)


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise TemplateError("invalid_template", "发布文案必须为文本")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in value):
        raise TemplateError("invalid_template", "发布文案包含无效控制字符")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise TemplateError("invalid_template", "发布文案字符编码无效") from None
    return value.strip()


def validate_template(template: str, *, manual=False) -> str:
    value = _text(template)
    if manual:
        return value
    if not value:
        raise TemplateError("required_text", "请填写发布文案")
    if len(value.encode("utf-8")) > TEMPLATE_MAX_BYTES:
        raise TemplateError("template_too_long", "发布文案模板超过16000字节")
    residual = TOKEN_RE.sub("", value)
    if "{" in residual or "}" in residual:
        raise TemplateError("unknown_macro", "仅支持 {name}、{desc}、{url}，请检查宏参数和花括号")
    return value


def render_template(template: str, material: Mapping[str, Any], short_url: str = "", *, manual=False) -> str:
    return _render(template, material, short_url, manual=manual)


def _render(template: str, material: Mapping[str, Any], short_url: str, *, preview: bool = False, manual=False) -> str:
    value = validate_template(template, manual=manual)
    values = {
        "name": str(material.get("drama_name") or "").strip(),
        "desc": str(material.get("drama_description") or "").strip(),
        "url": str(short_url or "").strip(),
    }

    def replace(match):
        key = match.group(1)
        replacement = values[key]
        if not replacement:
            if manual and key != "url":
                return ""
            raise TemplateError("macro_source_missing", "素材缺少 " + match.group(0) + " 的有效值", 409)
        if key == "url" and not preview:
            parsed = urlsplit(replacement)
            if (parsed.scheme != "https" or parsed.netloc != "gy.g2flow.com"
                    or not re.fullmatch(r"/s2l/facebook/[1-9][0-9]{0,18}\.html", parsed.path)
                    or parsed.query or parsed.fragment):
                raise TemplateError("macro_source_missing", "{url}必须为本次发布的Facebook短链", 409)
        # Do not recursively expand braces that occur in source metadata.
        return _text(replacement)

    rendered = TOKEN_RE.sub(replace, value).strip()
    if not manual and len(rendered.encode("utf-8")) > CAPTION_MAX_BYTES:
        raise TemplateError("text_too_long", "替换后的发布文案超过5000 UTF-8字节", 409)
    return rendered


def render_preview(template: str, material: Mapping[str, Any], placeholder: str = PREVIEW_LINK, *, manual=False) -> str:
    """Read-only preview: budget the maximum real ID, never allocate a link."""
    _render(template, material, "https://gy.g2flow.com/s2l/facebook/9223372036854775807.html", manual=manual)
    return _render(template, material, placeholder, preview=True, manual=manual)


def manual_text(pair, config):
    """Resolve each field separately, before preview and immutable task freezing."""
    caption = validate_template(pair.get("caption_template", ""), manual=True)
    title = _text(pair.get("title", ""))
    return (caption or validate_template(config.get("caption_template", ""), manual=True),
            title or _text(config.get("title", "")))
