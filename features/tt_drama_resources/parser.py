"""Static HTML parser for server-rendered W2A resource fields."""

from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qs, urlsplit

from .models import (
    DEFAULT_COVER_HOSTS,
    ResourceContentMismatchError,
    ResourceParseError,
    compact_text,
    normalize_content_id,
    normalize_landing_id,
    public_content_hash,
    sanitize_cover_url,
)


_LINK_ASSIGNMENT_PATTERN = re.compile(r"\blet\s+link\s*=\s*\[", re.IGNORECASE)
_VOID_ELEMENTS = frozenset(
    (
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    )
)


class _W2AFieldParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.title_parts = []
        self.description_parts = []
        self.title_seen = False
        self.description_seen = False
        self.primary_cover = ""
        self.fallback_cover = ""
        self._capture = None
        self._script_depth = 0
        self.script_parts = []
        self._script_bytes = 0

    @staticmethod
    def _attribute_map(attrs):
        return {
            str(key or "").lower(): str(value or "")
            for key, value in attrs
            if key
        }

    @staticmethod
    def _classes(attributes):
        return frozenset(attributes.get("class", "").split())

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        attributes = self._attribute_map(attrs)
        classes = self._classes(attributes)

        if tag == "img":
            element_id = attributes.get("id", "")
            if element_id == "topReading" and not self.primary_cover:
                self.primary_cover = attributes.get("data-src", "").strip()
            elif element_id == "image" and "bg-img" in classes:
                if not self.fallback_cover:
                    self.fallback_cover = attributes.get("src", "").strip()

        inside_info = any("info" in item[1] for item in self.stack)
        if tag == "h1" and "title" in classes and not self.title_seen:
            self.title_seen = True
            self._capture = ("title", len(self.stack))
        elif (
            tag == "div"
            and "desc" in classes
            and inside_info
            and not self.description_seen
        ):
            self.description_seen = True
            self._capture = ("description", len(self.stack))

        if tag == "script":
            self._script_depth += 1
        if tag not in _VOID_ELEMENTS:
            self.stack.append((tag, classes))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        normalized = str(tag or "").lower()
        if normalized not in _VOID_ELEMENTS:
            self.handle_endtag(normalized)

    def handle_endtag(self, tag):
        tag = str(tag or "").lower()
        if tag == "script" and self._script_depth > 0:
            self._script_depth -= 1

        pop_index = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                pop_index = index
                break
        if pop_index is None:
            return
        if self._capture is not None and self._capture[1] >= pop_index:
            self._capture = None
        del self.stack[pop_index:]

    def handle_data(self, data):
        if self._capture is not None:
            if self._capture[0] == "title":
                self.title_parts.append(data)
            else:
                self.description_parts.append(data)
        if self._script_depth and self._script_bytes < 256 * 1024:
            remaining = (256 * 1024) - self._script_bytes
            piece = str(data or "")[:remaining]
            self.script_parts.append(piece)
            self._script_bytes += len(piece)


def _extract_link_array(text, opening_index):
    depth = 0
    in_string = False
    escaped = False
    for index in range(opening_index, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return text[opening_index : index + 1]
    raise ResourceParseError("W2A link payload is incomplete")


def _first_query_value(query, key):
    values = query.get(key) or []
    return str(values[0] or "") if values else ""


def _extract_content_id_from_deep_link(deep_link):
    try:
        parsed = urlsplit(str(deep_link or ""))
    except ValueError:
        raise ResourceParseError("W2A deep link is invalid") from None
    query = parse_qs(parsed.query, keep_blank_values=True)
    direct = _first_query_value(query, "content_id") or _first_query_value(
        query, "id"
    )
    if direct:
        try:
            return normalize_content_id(direct)
        except ValueError:
            raise ResourceParseError(
                "W2A deep link contains an invalid content_id"
            ) from None

    redirect = _first_query_value(query, "redirect")
    if not redirect:
        raise ResourceParseError("W2A deep link has no content_id")
    try:
        nested_query = parse_qs(
            urlsplit(redirect).query,
            keep_blank_values=True,
        )
    except ValueError:
        raise ResourceParseError("W2A redirect deep link is invalid") from None
    nested = _first_query_value(
        nested_query, "content_id"
    ) or _first_query_value(nested_query, "id")
    try:
        return normalize_content_id(nested)
    except ValueError:
        raise ResourceParseError(
            "W2A redirect deep link contains an invalid content_id"
        ) from None


def extract_resolved_content_id(script_text):
    text = str(script_text or "")
    assignment = _LINK_ASSIGNMENT_PATTERN.search(text)
    if assignment is None:
        raise ResourceParseError("W2A link payload is missing")
    raw_array = _extract_link_array(text, assignment.end() - 1)
    try:
        payload = json.loads(raw_array)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ResourceParseError("W2A link payload is not valid JSON") from None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ResourceParseError("W2A link payload has no first item")
    deep_link = payload[0].get("dl")
    if not isinstance(deep_link, str) or not deep_link:
        raise ResourceParseError("W2A link payload has no deep link")
    return _extract_content_id_from_deep_link(deep_link)


def parse_w2a_resource_html(
    html,
    requested_content_id,
    *,
    landing_id=2049,
    allowed_cover_hosts=None,
):
    """Extract public resource fields without executing page JavaScript."""
    requested = normalize_content_id(requested_content_id)
    landing = normalize_landing_id(landing_id)
    if not isinstance(html, str):
        raise ResourceParseError("W2A response is not decoded HTML")

    parser = _W2AFieldParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        raise ResourceParseError("W2A HTML parsing failed") from None

    resolved = extract_resolved_content_id("\n".join(parser.script_parts))
    if resolved != requested:
        raise ResourceContentMismatchError(requested, resolved)

    title = compact_text("".join(parser.title_parts), 240)
    description = compact_text("".join(parser.description_parts), 2000)
    if not parser.title_seen or not title:
        raise ResourceParseError("W2A title is missing")
    if not parser.description_seen:
        raise ResourceParseError("W2A description field is missing")

    hosts = allowed_cover_hosts or DEFAULT_COVER_HOSTS
    cover_url = sanitize_cover_url(parser.primary_cover, hosts)
    if not cover_url:
        cover_url = sanitize_cover_url(parser.fallback_cover, hosts)
    if not cover_url:
        raise ResourceParseError("W2A cover URL is missing or not allowlisted")

    item = {
        "landing_id": landing,
        "content_id": requested,
        "resolved_content_id": resolved,
        "title": title,
        "description": description,
        "cover_url": cover_url,
        "country": "",
        "language": "",
        "episode_count": 0,
    }
    item["content_hash"] = public_content_hash(item)
    return item
