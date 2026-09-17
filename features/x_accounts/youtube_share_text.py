"""Bounded text rendering for YouTube link posts; no credentials or I/O.

Uses X's v3 character weights, NFC and 23-character URL accounting. Unusual
emoji/URL forms are counted conservatively; the X API remains authoritative.
"""
import re
import unicodedata
from urllib.parse import urlsplit

DEFAULT_TEMPLATE = '{title}\n\n{youtube_url}'
LIMIT = 280
MACROS = (
    ('title', '视频标题'), ('youtube_url', 'YouTube 视频链接'),
    ('short_url', '推广短链'),
    ('channel_name', '频道名称'), ('channel_url', '频道链接'),
    ('name', '剧名'), ('desc', '剧情简介'),
)
_MACRO = re.compile(r'\{([^{}\n]{0,80})\}')
_URL = re.compile(r'https?://[^\s<>"\u3000-\u303f\uff00-\uffef]+', re.I)
_INVALID = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufeff\ufffe\uffff\ud800-\udfff]')


def canonical_url(video_id):
    if not isinstance(video_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('YouTube 视频 ID 无效')
    return 'https://www.youtube.com/watch?v=' + video_id


def _url_parts(text):
    for match in _URL.finditer(text):
        url = match.group().rstrip('.,!?:;')
        while url and url[-1] in ')]}' and url.count(url[-1]) > url.count({')': '(', ']': '[', '}': '{'}[url[-1]]):
            url = url[:-1]
        try:
            host = urlsplit(url).hostname or ''
            # Avoid undercounting ordinary prose that merely begins with http.
            valid = bool(re.fullmatch(r'[A-Za-z0-9.-]+', host) and '.' in host and len(host.rsplit('.', 1)[-1]) >= 2)
        except ValueError:
            valid = False
        if valid:
            yield match.start(), match.start() + len(url), url


def _char_weight(c):
    n = ord(c)
    return 1 if n <= 0x10ff or 0x2000 <= n <= 0x200d or 0x2010 <= n <= 0x201f or 0x2032 <= n <= 0x2037 else 2


def contains_source_url(text, video_id):
    """Require the exact canonical source URL, rather than a matching prefix."""
    expected = canonical_url(video_id)
    return any(found == expected for _, _, found in _url_parts(str(text)))


def _plain_weight(text):
    # Recognize common emoji modifiers, regional flags, keycaps and ZWJ
    # sequences. Do not collapse arbitrary Unicode joined by ZWJ.
    def emoji(c):
        n = ord(c)
        return 0x1f300 <= n <= 0x1faff or 0x2600 <= n <= 0x27bf or n in (0x00a9, 0x00ae, 0x203c, 0x2049, 0x2122, 0x2139, 0x3030, 0x303d, 0x3297, 0x3299)
    families = ('👨‍👩‍👧‍👦', '👨‍👩‍👦‍👦', '👨‍👩‍👧‍👧', '👨‍👩‍👦', '👨‍👩‍👧',
                '👩‍👩‍👧‍👦', '👨‍👨‍👧‍👦', '👩‍👩‍👦', '👨‍👨‍👦', '👩‍👩‍👧', '👨‍👨‍👧',
                '👨‍🎤', '👩‍🎤', '👨‍💻', '👩‍💻', '🏳️‍🌈', '🏴‍☠️', '❤️‍🔥')
    modifier_bases = frozenset('👋🤚🖐✋🖖👌🤏✌🤞🤟🤘🤙👈👉👆🖕👇☝👍👎✊👊🤛🤜👏🙌👐🤲🙏✍💅🤳💪👂👃👶👦👧👨👩👴👵🙋🙅🙆🙇🤦🤷')
    total = i = 0
    while i < len(text):
        c = text[i]
        combined = next((seq for seq in families if text.startswith(seq, i)), None)
        if combined:
            total += 2; i += len(combined); continue
        if 0x1f1e6 <= ord(c) <= 0x1f1ff and i + 1 < len(text) and 0x1f1e6 <= ord(text[i + 1]) <= 0x1f1ff:
            total += 2; i += 2; continue
        j = i + 1
        if c in '#*0123456789':
            if j < len(text) and text[j] == '\ufe0f': j += 1
            if j < len(text) and text[j] == '\u20e3':
                total += 2; i = j + 1; continue
        if emoji(c):
            j = i + 1
            if j < len(text) and text[j] == '\ufe0f': j += 1
            if c in modifier_bases and j < len(text) and 0x1f3fb <= ord(text[j]) <= 0x1f3ff: j += 1
            total += 2; i = j; continue
        total += _char_weight(c); i += 1
    return total


def weighted_length(text):
    text = unicodedata.normalize('NFC', str(text))
    total = offset = 0
    for start, end, _ in _url_parts(text):
        total += _plain_weight(text[offset:start]) + 23
        offset = end
    return total + _plain_weight(text[offset:])


def preview(template, values):
    errors = []
    if not isinstance(template, str) or len(template) > 5000:
        return {'text': '', 'valid': False, 'errors': ['描述模板最多 5000 个字符'], 'weighted_length': 0, 'limit': LIMIT, 'appended_url': False}
    allowed = dict(MACROS)
    def replace(match):
        key = match.group(1)
        if key not in allowed:
            errors.append('不支持的宏参数：{' + key + '}')
            return match.group()
        value = str(values.get(key) or '')
        if not value.strip():
            errors.append(allowed[key] + '为空，请修改描述')
        return value
    text = _MACRO.sub(replace, template)
    # A typo must not silently turn into a literal macro in a public post.
    remaining = _MACRO.sub('', template)
    if '{' in remaining or '}' in remaining or '{{' in template or '}}' in template:
        errors.append('宏参数格式无效，请使用单层花括号')
    text = unicodedata.normalize('NFC', text.strip())
    url = str(values.get('youtube_url') or '')
    urls = list(_url_parts(text))
    appended = bool(url and not any(found == url for _, _, found in urls))
    if appended:
        # Prefer the video as the card source when a promotional link is used.
        text = url + '\n\n' + text if urls else text + ('\n\n' if text else '') + url
    elif urls and urls[0][2] != url:
        errors.append('请将 {youtube_url} 放在其他链接之前，以 YouTube 视频作为卡片来源')
    if _INVALID.search(text): errors.append('描述含有不支持的控制字符')
    weight = weighted_length(text)
    if not text: errors.append('请填写描述')
    if weight > LIMIT: errors.append('展开后的描述超出 X 的 280 字符限制，请缩短文案')
    return {'text': text, 'valid': not errors, 'errors': list(dict.fromkeys(errors)),
            'weighted_length': weight, 'limit': LIMIT, 'appended_url': appended}
