"""Personal X copy contract; never changes legacy global-template semantics."""
from __future__ import annotations

import re
import urllib.parse

DEFAULT_COPY = "{name}\n{desc}\n{url}"
COPY_CONTRACT_VERSION = 2
_MACRO = re.compile(r"\{(name|desc|url)\}")
_URL = re.compile(r"https?://[^\s<>]+", re.I)


def normalize_caption(value):
    from features.x_posts.service import XPostError
    if not isinstance(value, str):
        raise XPostError("x_video_caption_invalid", "发布文案必须是文本", 400)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value or len(value) > 4000 or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise XPostError("x_video_caption_invalid", "发布文案不能为空、含控制字符或超过4000字符", 400)
    remainder = _MACRO.sub("", value)
    if "{" in remainder or "}" in remainder:
        raise XPostError("x_video_caption_macro_invalid", "文案仅支持 {name}、{desc}、{url}，请检查宏格式", 400)
    return value


def weighted_length(value):
    from features.x_posts.service import _tweet_char_weight
    total, end = 0, 0
    for match in _URL.finditer(value):
        total += sum(_tweet_char_weight(c) for c in value[end:match.start()]) + 23
        end = match.end()
    return total + sum(_tweet_char_weight(c) for c in value[end:])


def render_caption(caption, name, description, short_url):
    from features.x_posts.service import XPostError
    template = normalize_caption(caption)
    parsed = urllib.parse.urlsplit(str(short_url))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise XPostError("x_video_short_url_invalid", "发布短链无效", 400)
    values = {"name": str(name or "").strip(), "desc": str(description or "").strip(), "url": str(short_url)}
    for key in _MACRO.findall(template):
        if not values[key]:
            raise XPostError("x_video_caption_source_missing", "文案宏所需的剧名或描述缺失", 400)
    rendered = _MACRO.sub(lambda match: values[match.group(1)], template).strip()
    if not rendered or weighted_length(rendered) > 280:
        raise XPostError("x_post_copy_too_long", "展开后的发布文案超过 X 的 280 加权字符限制，请缩减文案", 400)
    return rendered
