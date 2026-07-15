import base64
import hashlib
import html as html_lib
import json
import lzma
import mimetypes
import os
import posixpath
import re
from urllib.parse import unquote


META_CTA_HOOK = "FbPlayableAd.onCTAClick"
DEFAULT_META_ASSET_LIMIT_BYTES = 4_800_000
RESOURCE_ENCODING = "lzma+base94"
BASE94_ALPHABET = "".join(
    chr(code)
    for code in range(32, 127)
    if chr(code) != "<"
)
BASE94_RADIX = len(BASE94_ALPHABET)
BASE94_LOW_BITS = 13
BASE94_LOW_MASK = (1 << BASE94_LOW_BITS) - 1
BASE94_THRESHOLD = BASE94_RADIX * BASE94_RADIX - (1 << BASE94_LOW_BITS) - 1
LZMA_DECODER_PATH = os.path.join(os.path.dirname(__file__), "vendor", "lzma-d-min.js")
HTML_ATTRIBUTE_TEXT = r'(?:[^>"\']|"[^"]*"|\'[^\']*\')*'
PLAYABLE_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline' 'wasm-unsafe-eval'; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; media-src data: blob:; font-src data:; "
    "connect-src 'none'; worker-src 'none'; object-src 'none'; "
    "frame-src 'self' data: blob:; base-uri 'none'; form-action 'none'"
)
CSS_URL_PATTERN = re.compile(
    r"url\(\s*(?:\"(?P<double>(?:\\.|[^\"\\])*)\"|'(?P<single>(?:\\.|[^'\\])*)'|(?P<bare>(?:\\.|[^)\s\\])+))\s*\)",
    re.I | re.S,
)

if BASE94_RADIX != 94:
    raise RuntimeError("invalid Base94 alphabet")


class PlayableCompatibilityError(ValueError):
    pass


def _read_text(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _mime_type(path):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.lower().endswith(".wasm"):
        return "application/wasm"
    if path.lower().endswith(".json"):
        return "application/json"
    return mime


def _is_external_url(value):
    text = str(value or "").strip()
    return bool(re.match(r"^(?:https?:)?//", text, re.I))


def _is_passthrough_url(value):
    text = str(value or "").strip()
    lowered = text.lower()
    return not text or text.startswith("#") or lowered.startswith(
        ("data:", "blob:", "about:")
    )


def _resource_key(value, base_dir=""):
    text = html_lib.unescape(str(value or "").strip())
    if _is_external_url(text):
        raise PlayableCompatibilityError("external resource is not Meta-compatible: %s" % text)
    if _is_passthrough_url(text):
        return ""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text):
        raise PlayableCompatibilityError("unsupported resource URL: %s" % text)
    text = unquote(text.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
    text = posixpath.normpath(posixpath.join(base_dir, text)).lstrip("/")
    if text == ".." or text.startswith("../"):
        raise PlayableCompatibilityError("resource escapes playable root: %s" % value)
    return text


def _collect_resources(game_dir, entry_path):
    entry_path = os.path.abspath(entry_path)
    entry_dir = os.path.dirname(entry_path)
    resources = {}
    for current_root, _, filenames in os.walk(game_dir):
        for filename in filenames:
            path = os.path.abspath(os.path.join(current_root, filename))
            if path == entry_path:
                continue
            key = os.path.relpath(path, entry_dir).replace(os.sep, "/")
            if key == ".." or key.startswith("../"):
                raise PlayableCompatibilityError("playable resource is outside entry directory: %s" % key)
            with open(path, "rb") as handle:
                content = handle.read()
            if path.lower().endswith(('.js', '.mjs')):
                content = _patch_javascript(_read_text(path), key).encode('utf-8')
            resources[key] = {
                "path": path,
                "mime": _mime_type(path),
                "content": content,
            }
    return resources


def _data_uri(resource):
    encoded = base64.b64encode(resource["content"]).decode("ascii")
    return "data:%s;base64,%s" % (resource["mime"], encoded)


def _base94_encode(data):
    buffer_value = 0
    bit_count = 0
    output = []
    for byte in data:
        buffer_value |= byte << bit_count
        bit_count += 8
        if bit_count > BASE94_LOW_BITS:
            value = buffer_value & BASE94_LOW_MASK
            if value > BASE94_THRESHOLD:
                buffer_value >>= BASE94_LOW_BITS
                bit_count -= BASE94_LOW_BITS
            else:
                value = buffer_value & ((1 << (BASE94_LOW_BITS + 1)) - 1)
                buffer_value >>= BASE94_LOW_BITS + 1
                bit_count -= BASE94_LOW_BITS + 1
            output.append(BASE94_ALPHABET[value % BASE94_RADIX])
            output.append(BASE94_ALPHABET[value // BASE94_RADIX])
    if bit_count:
        output.append(BASE94_ALPHABET[buffer_value % BASE94_RADIX])
        if bit_count > 7 or buffer_value >= BASE94_RADIX:
            output.append(BASE94_ALPHABET[buffer_value // BASE94_RADIX])
    return "".join(output)


def _base94_decode(value):
    lookup = {character: index for index, character in enumerate(BASE94_ALPHABET)}
    buffer_value = 0
    bit_count = 0
    pending = -1
    output = bytearray()
    for character in value:
        decoded = lookup.get(character)
        if decoded is None:
            continue
        if pending < 0:
            pending = decoded
            continue
        pending += decoded * BASE94_RADIX
        buffer_value |= pending << bit_count
        if (pending & BASE94_LOW_MASK) > BASE94_THRESHOLD:
            bit_count += BASE94_LOW_BITS
        else:
            bit_count += BASE94_LOW_BITS + 1
        while bit_count > 7:
            output.append(buffer_value & 255)
            buffer_value >>= 8
            bit_count -= 8
        pending = -1
    if pending >= 0:
        output.append((buffer_value | (pending << bit_count)) & 255)
    return bytes(output)


def _javascript_previous_nonspace(code_view, index):
    cursor = index - 1
    while cursor >= 0 and code_view[cursor].isspace():
        cursor -= 1
    return cursor


def _javascript_matching_open(code_view, close_index, opening, closing):
    depth = 0
    for cursor in range(close_index, -1, -1):
        character = code_view[cursor]
        if character == closing:
            depth += 1
        elif character == opening:
            depth -= 1
            if depth == 0:
                return cursor
    return -1


def _javascript_word_before(code_view, index):
    cursor = _javascript_previous_nonspace(code_view, index)
    end = cursor + 1
    while cursor >= 0 and (
        code_view[cursor].isalnum() or code_view[cursor] in "_$"
    ):
        cursor -= 1
    return "".join(code_view[cursor + 1:end])


def _javascript_control_paren(code_view, close_index):
    opening = _javascript_matching_open(code_view, close_index, "(", ")")
    return opening >= 0 and _javascript_word_before(code_view, opening) in {
        "catch", "for", "if", "switch", "while", "with",
    }


def _javascript_control_block(code_view, close_index):
    opening = _javascript_matching_open(code_view, close_index, "{", "}")
    if opening < 0:
        return False
    previous = _javascript_previous_nonspace(code_view, opening)
    if previous >= 0 and code_view[previous] == ")":
        return _javascript_control_paren(code_view, previous)
    return _javascript_word_before(code_view, opening) in {
        "do", "else", "finally", "try",
    }


def _javascript_regex_starts(code_view, index):
    cursor = index - 1
    while cursor >= 0 and code_view[cursor].isspace():
        cursor -= 1
    if cursor < 0:
        return True
    previous = code_view[cursor]
    if (
        previous in "+-"
        and cursor > 0
        and code_view[cursor - 1] == previous
    ):
        return False
    if previous == ")":
        return _javascript_control_paren(code_view, cursor)
    if previous == "}":
        return _javascript_control_block(code_view, cursor)
    if previous in "([{,:;=!?&|+-*%^~<>":
        return True
    if previous.isalnum() or previous in "_$":
        end = cursor + 1
        while cursor >= 0 and (code_view[cursor].isalnum() or code_view[cursor] in "_$"):
            cursor -= 1
        return "".join(code_view[cursor + 1:end]) in {
            "await", "case", "delete", "do", "else", "in", "instanceof",
            "new", "of", "return", "throw", "typeof", "void", "yield",
        }
    return False


def _javascript_code_view(source):
    """Return executable JavaScript with comments and literals blanked in place."""
    text = str(source or "")
    output = [" "] * len(text)
    state = "code"
    template_expression_depths = []
    index = 0
    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "line_comment":
            if character in "\r\n":
                output[index] = character
                state = "code"
            index += 1
            continue
        if state == "block_comment":
            if character in "\r\n":
                output[index] = character
            if character == "*" and following == "/":
                index += 2
                state = "code"
            else:
                index += 1
            continue
        if state in ("single_quote", "double_quote"):
            quote = "'" if state == "single_quote" else '"'
            if character == "\\":
                index += 2
                continue
            if character == quote:
                state = "code"
            elif character in "\r\n":
                output[index] = character
            index += 1
            continue
        if state == "template":
            if character == "\\":
                index += 2
                continue
            if character == "`":
                state = "code"
                index += 1
                continue
            if character == "$" and following == "{":
                template_expression_depths.append(1)
                state = "code"
                index += 2
                continue
            if character in "\r\n":
                output[index] = character
            index += 1
            continue
        if state == "regex":
            in_character_class = False
            while index < len(text):
                character = text[index]
                if character == "\\":
                    index += 2
                    continue
                if character == "[":
                    in_character_class = True
                elif character == "]":
                    in_character_class = False
                elif character == "/" and not in_character_class:
                    index += 1
                    while index < len(text) and text[index].isalpha():
                        index += 1
                    state = "code"
                    break
                elif character in "\r\n":
                    output[index] = character
                    state = "code"
                    index += 1
                    break
                index += 1
            continue

        if character == "/" and following == "/":
            state = "line_comment"
            index += 2
            continue
        if character == "/" and following == "*":
            state = "block_comment"
            index += 2
            continue
        if character == "'":
            state = "single_quote"
            index += 1
            continue
        if character == '"':
            state = "double_quote"
            index += 1
            continue
        if character == "`":
            state = "template"
            index += 1
            continue
        if character == "/" and _javascript_regex_starts(output, index):
            state = "regex"
            index += 1
            continue
        if template_expression_depths and character == "{":
            template_expression_depths[-1] += 1
        elif template_expression_depths and character == "}":
            template_expression_depths[-1] -= 1
            if template_expression_depths[-1] == 0:
                template_expression_depths.pop()
                state = "template"
                index += 1
                continue
        output[index] = character
        index += 1
    return "".join(output)


def _replace_javascript_identifiers(source, replacements):
    code_view = _javascript_code_view(source)
    matches = []
    for identifier, replacement in replacements.items():
        for match in re.finditer(
            r"(?<![\w$])%s(?![\w$])" % re.escape(identifier),
            code_view,
        ):
            matches.append((match.start(), match.end(), replacement))
    result = source
    for start, end, replacement in sorted(matches, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _javascript_executable_member_strings(
    source,
    names,
    objects=("window", "globalThis", "self", "this", "parent", "top"),
):
    wanted = tuple(sorted((str(name) for name in names), key=len, reverse=True))
    owners = tuple(sorted((str(name) for name in objects), key=len, reverse=True))
    if not wanted or not owners:
        return []
    gap = r"(?:\s|/\*.*?\*/|//[^\r\n]*(?:\r?\n|$))*"
    pattern = re.compile(
        r"\b(?:%s)%s\[%s(?P<quote>['\"`])(?P<name>%s)(?P=quote)%s\]"
        % (
            "|".join(re.escape(name) for name in owners),
            gap,
            gap,
            "|".join(re.escape(name) for name in wanted),
            gap,
        ),
        re.S,
    )
    code_view = _javascript_code_view(source)
    matches = []
    for match in pattern.finditer(source):
        structural_view = code_view[match.start():match.start("quote")]
        if any(not character.isspace() for character in structural_view):
            matches.append(match)
    return matches


def _javascript_executable_reflect_members(
    source,
    names,
    objects=("window", "globalThis", "self", "this", "parent", "top"),
):
    wanted = tuple(sorted((str(name) for name in names), key=len, reverse=True))
    owners = tuple(sorted((str(name) for name in objects), key=len, reverse=True))
    if not wanted or not owners:
        return []
    gap = r"(?:\s|/\*.*?\*/|//[^\r\n]*(?:\r?\n|$))*"
    pattern = re.compile(
        r"\bReflect%s\.%sget%s\(%s(?:%s)%s,%s(?P<quote>['\"`])(?P<name>%s)(?P=quote)%s\)"
        % (
            gap,
            gap,
            gap,
            gap,
            "|".join(re.escape(name) for name in owners),
            gap,
            gap,
            "|".join(re.escape(name) for name in wanted),
            gap,
        ),
        re.S,
    )
    code_view = _javascript_code_view(source)
    return [
        match
        for match in pattern.finditer(source)
        if not code_view[match.start()].isspace()
    ]


def _javascript_uses_string_timer(source):
    gap = r"(?:\s|/\*.*?\*/|//[^\r\n]*(?:\r?\n|$))*"
    pattern = re.compile(
        r"\b(?:setTimeout|setInterval)%s\(%s['\"`]" % (gap, gap),
        re.S,
    )
    code_view = _javascript_code_view(source)
    return any(
        not code_view[match.start()].isspace()
        for match in pattern.finditer(source)
    )


def _javascript_has_unsafe_eval_reference(source):
    code_view = _javascript_code_view(source)
    if re.search(r"(?<![\w$])eval(?![\w$])", code_view):
        return True
    for match in re.finditer(r"(?<![\w$])Function(?![\w$])", code_view):
        # Reading Function.prototype is used by common compatibility wrappers
        # (for example, binding an AudioContext constructor) and does not
        # compile source text. Any other Function reference can be aliased to
        # the dynamic constructor and is rejected.
        if re.match(r"\s*\.\s*prototype\b", code_view[match.end():]):
            continue
        return True
    return False


def _javascript_has_unsafe_constructor_reference(source):
    code_view = _javascript_code_view(source)
    for match in re.finditer(r"\.\s*constructor\b", code_view):
        if re.match(r"\s*=(?!=|>)", code_view[match.end():]):
            continue
        return True
    computed_pattern = re.compile(
        r"\[\s*(?P<quote>['\"`])constructor(?P=quote)\s*\]",
        re.S,
    )
    for match in computed_pattern.finditer(source):
        if code_view[match.start()].isspace():
            continue
        if re.match(r"\s*=(?!=|>)", code_view[match.end():]):
            continue
        return True
    reflect_pattern = re.compile(
        r"\bReflect\s*\.\s*get\s*\(\s*[^,\r\n]+,\s*(?P<quote>['\"`])constructor(?P=quote)\s*\)",
        re.S,
    )
    return any(
        not code_view[match.start()].isspace()
        for match in reflect_pattern.finditer(source)
    )


def _replace_javascript_member_strings(source, replacements):
    matches = _javascript_executable_member_strings(source, replacements)
    result = source
    for match in reversed(matches):
        replacement = replacements[match.group("name")]
        result = result[:match.start()] + replacement + result[match.end():]
    return result


def _replace_javascript_reflect_members(source, replacements):
    matches = _javascript_executable_reflect_members(source, replacements)
    result = source
    for match in reversed(matches):
        replacement = replacements[match.group("name")]
        result = result[:match.start()] + replacement + result[match.end():]
    return result


def _javascript_redirect_patterns():
    owners = r"(?:window|globalThis|self|document|top|parent|this)"
    assignment = r"(?:\+=|-=|\*=|/=|%=|&&=|\|\|=|\?\?=|=(?!=|>))"
    return {
        "window_open": (
            r"(?<![\w$.])\bopen\s*\(|\b%s\s*\.\s*open\s*\(" % owners
        ),
        "direct_location": (
            r"(?<![\w$.])\blocation(?:\s*\.\s*href)?\s*%s"
            r"|\b%s\s*\.\s*location(?:\s*\.\s*href)?\s*%s"
            r"|(?<![\w$.])\blocation\s*\.\s*(?:assign|replace)\s*\("
            r"|\b%s\s*\.\s*location\s*\.\s*(?:assign|replace)\s*\("
            % (assignment, owners, assignment, owners)
        ),
    }


def _javascript_unsafe_eval_pattern():
    owners = r"(?:window|globalThis|self|document|top|parent|this)"
    return (
        r"\(\s*0\s*,\s*eval\s*\)\s*(?:\?\.)?\s*\("
        r"|\(\s*eval\s*\)\s*(?:\?\.)?\s*\("
        r"|(?<![\w$.])\beval\s*(?:\?\.)?\s*\("
        r"|(?<![\w$.])\bFunction\s*(?:\?\.)?\s*\("
        r"|\b%s\s*\.\s*(?:eval|Function)\s*(?:\?\.)?\s*\("
        r"|\b(?:eval|Function|setTimeout|setInterval)\s*\.\s*(?:call|apply|bind)\s*\("
        r"|\b%s\s*\.\s*(?:eval|Function|setTimeout|setInterval)\s*\.\s*(?:call|apply|bind)\s*\("
        % (owners, owners)
    )


def _patch_javascript(source, source_name):
    patched = source
    defold_pattern = re.compile(
        r"function\s+_dmSysOpenURL\s*\(e,r\)\s*\{.*?\}(?=\s*function\s+_emscripten_)",
        re.S,
    )
    defold_match = defold_pattern.search(_javascript_code_view(patched))
    if defold_match:
        patched = (
            patched[:defold_match.start()]
            + 'function _dmSysOpenURL(e,r){window.parent.postMessage({type:"meta-playable-cta"},"*");return!0}'
            + patched[defold_match.end():]
        )
    # Location cannot be safely monkey-patched in browsers. Redirect every
    # direct executable reference to a frozen facade installed by the loader.
    # Literal/computed reflection variants are still rejected by validation.
    patched = _replace_javascript_identifiers(
        patched,
        {"location": "__playableLocation"},
    )
    code_view = _javascript_code_view(patched)
    for pattern in _javascript_redirect_patterns().values():
        if re.search(pattern, code_view, re.S):
            raise PlayableCompatibilityError(
                "direct JavaScript redirect remains in %s" % source_name
            )
    computed_redirect_members = {
        match.group("name")
        for match in _javascript_executable_member_strings(
            patched,
            ("assign", "href", "location", "open", "replace"),
            objects=(
                "window", "globalThis", "self", "document",
                "this", "top", "parent", "location",
            ),
        )
    }
    computed_redirect_members.update(
        match.group("name")
        for match in _javascript_executable_reflect_members(
            patched,
            ("assign", "href", "location", "open", "replace"),
            objects=(
                "window", "globalThis", "self", "document",
                "this", "top", "parent", "location",
            ),
        )
    )
    if computed_redirect_members:
        raise PlayableCompatibilityError(
            "computed JavaScript redirect remains in %s" % source_name
        )
    computed_unsafe_members = {
        match.group("name")
        for match in _javascript_executable_member_strings(
            patched,
            ("Function", "eval", "setInterval", "setTimeout"),
        )
    }
    computed_unsafe_members.update(
        match.group("name")
        for match in _javascript_executable_reflect_members(
            patched,
            ("Function", "eval", "setInterval", "setTimeout"),
        )
    )
    if (
        re.search(_javascript_unsafe_eval_pattern(), code_view, re.S)
        or computed_unsafe_members
        or _javascript_uses_string_timer(patched)
        or _javascript_has_unsafe_eval_reference(patched)
        or _javascript_has_unsafe_constructor_reference(patched)
    ):
        raise PlayableCompatibilityError(
            "unsafe JavaScript evaluation remains in %s" % source_name
        )
    if re.search(
        r"\b(?:Worker|SharedWorker|WebSocket|EventSource)\s*\(|\bnavigator\s*\.\s*sendBeacon\s*\(|(?<![\w$.])\bimport\b(?!\s*\.)|(?<![\w$.])\bexport\b[^;]*\bfrom\b",
        code_view,
        re.S,
    ):
        raise PlayableCompatibilityError(
            "unsupported browser capability remains in %s" % source_name
        )
    return patched


def _css_url_value(match):
    return next(
        (
            match.group(group)
            for group in ("double", "single", "bare")
            if match.group(group) is not None
        ),
        "",
    ).strip()


def _validate_rewritten_css(source, source_name):
    commentless = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    if re.search(r"@import\b", commentless, re.I):
        raise PlayableCompatibilityError(
            "CSS @import is not supported in %s" % source_name
        )
    if re.search(r"(?:-webkit-)?image-set\s*\(", commentless, re.I):
        raise PlayableCompatibilityError(
            "CSS image-set is not supported in %s" % source_name
        )
    for match in CSS_URL_PATTERN.finditer(commentless):
        value = _css_url_value(match)
        if value and not _is_passthrough_url(value):
            raise PlayableCompatibilityError(
                "external CSS resource remains in %s: %s"
                % (source_name, value)
            )
    residue = CSS_URL_PATTERN.sub("", commentless)
    if re.search(r"url\s*\(", residue, re.I):
        raise PlayableCompatibilityError(
            "malformed CSS url remains in %s" % source_name
        )
    if re.search(r"https?://|['\"]\s*//", residue, re.I):
        raise PlayableCompatibilityError(
            "external CSS resource remains in %s" % source_name
        )


def _rewrite_css(source, resources, base_dir, source_name, consumed):
    if re.search(r"@import\b", re.sub(r"/\*.*?\*/", "", source, flags=re.S), re.I):
        raise PlayableCompatibilityError(
            "CSS @import is not supported in %s" % source_name
        )

    def replace(match):
        raw = _css_url_value(match)
        if _is_passthrough_url(raw):
            return match.group(0)
        key = _resource_key(raw, base_dir)
        resource = resources.get(key)
        if not resource:
            raise PlayableCompatibilityError(
                "missing CSS resource %s referenced by %s" % (key, source_name)
            )
        consumed.add(key)
        return 'url("%s")' % _data_uri(resource)

    rewritten = CSS_URL_PATTERN.sub(replace, source)
    _validate_rewritten_css(rewritten, source_name)
    return rewritten


def _attribute_value(attributes, name):
    match = re.search(
        r"(?<![A-Za-z0-9_:.-])%s\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s\"'=<>`]+))"
        % re.escape(name),
        attributes,
        re.I | re.S,
    )
    if not match:
        return ""
    value = next(
        (match.group(group) for group in ("double", "single", "bare") if match.group(group) is not None),
        "",
    )
    return html_lib.unescape(value)


def _attribute_items(attributes):
    pattern = re.compile(
        r"(?P<name>[^\s=/>]+)\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s\"'=<>`]+))",
        re.I | re.S,
    )
    items = []
    for match in pattern.finditer(attributes):
        value = next(
            (
                match.group(group)
                for group in ("double", "single", "bare")
                if match.group(group) is not None
            ),
            "",
        )
        items.append((match.group("name"), html_lib.unescape(value)))
    return items


def _is_javascript_script_type(value):
    script_type = _normalized_script_type(value)
    return (
        not script_type
        or script_type == "module"
        or "javascript" in script_type
        or script_type in {
            "application/ecmascript",
            "application/x-ecmascript",
            "text/ecmascript",
            "text/jscript",
            "text/livescript",
        }
    )


def _normalized_script_type(value):
    return str(value or "").split(";", 1)[0].strip().lower()


def _remove_attribute(attributes, name):
    return re.sub(
        r"\s+(?<![A-Za-z0-9_:.-])%s\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+)"
        % re.escape(name),
        "",
        attributes,
        flags=re.I | re.S,
    )


def _replace_attribute_value(attributes, name, value):
    pattern = re.compile(
        r"((?<![A-Za-z0-9_:.-])%s\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+)"
        % re.escape(name),
        re.I | re.S,
    )
    escaped = html_lib.escape(str(value or ""), quote=True)
    return pattern.sub(
        lambda match: match.group(1) + '"' + escaped + '"',
        attributes,
        count=1,
    )


def _strip_source_csp(document):
    pattern = re.compile(
        r"<meta(?P<attrs>%s)>" % HTML_ATTRIBUTE_TEXT,
        re.I | re.S,
    )

    def replace(match):
        http_equiv = _attribute_value(match.group("attrs"), "http-equiv")
        if http_equiv.strip().lower().startswith("content-security-policy"):
            return ""
        return match.group(0)

    return pattern.sub(replace, document)


def _inline_scripts(document, resources, consumed):
    script_src_pattern = re.compile(
        r"<script(?P<attrs>%s)>(?P<body>.*?)</script>" % HTML_ATTRIBUTE_TEXT,
        re.I | re.S,
    )

    def replace_external_script(match):
        src = _attribute_value(match.group("attrs"), "src")
        if not src:
            return match.group(0)
        key = _resource_key(src)
        resource = resources.get(key)
        if not resource:
            raise PlayableCompatibilityError("missing script resource: %s" % key)
        consumed.add(key)
        code = resource["content"].decode("utf-8", errors="replace")
        code = re.sub(r"</script", r"<\\/script", code, flags=re.I)
        attrs = _remove_attribute(match.group("attrs"), "src")
        attrs = _remove_attribute(attrs, "integrity")
        attrs = _remove_attribute(attrs, "crossorigin")
        return "<script%s>%s</script>" % (attrs, code)

    document = script_src_pattern.sub(replace_external_script, document)
    inline_pattern = re.compile(
        r"<script(?P<attrs>%s)>(?P<body>.*?)</script>" % HTML_ATTRIBUTE_TEXT,
        re.I | re.S,
    )

    def patch_inline_script(match):
        attrs = match.group("attrs")
        script_type = _attribute_value(attrs, "type")
        if not _is_javascript_script_type(script_type):
            return match.group(0)
        code = _patch_javascript(match.group("body"), "inline script")
        return "<script%s>%s</script>" % (attrs, code)

    return inline_pattern.sub(patch_inline_script, document)


def _inline_links(document, resources, consumed):
    link_pattern = re.compile(
        r"<link(?P<attrs>%s)>" % HTML_ATTRIBUTE_TEXT,
        re.I | re.S,
    )

    def replace_link(match):
        attrs = match.group("attrs")
        href = _attribute_value(attrs, "href")
        rel = _attribute_value(attrs, "rel").lower()
        if not href:
            return match.group(0)
        if _is_passthrough_url(href):
            return match.group(0)
        key = _resource_key(href)
        resource = resources.get(key)
        if not resource:
            raise PlayableCompatibilityError("missing linked resource: %s" % key)
        if "stylesheet" in rel:
            consumed.add(key)
            css = _rewrite_css(
                _read_text(resource["path"]),
                resources,
                posixpath.dirname(key),
                key,
                consumed,
            )
            return "<style>%s</style>" % css
        if "manifest" in rel:
            consumed.add(key)
            return ""
        if any(item in rel for item in ("preload", "prefetch", "modulepreload")):
            return ""
        consumed.add(key)
        attrs = _replace_attribute_value(attrs, "href", _data_uri(resource))
        return "<link%s>" % attrs

    return link_pattern.sub(replace_link, document)


def _inline_style_blocks(document, resources, consumed):
    pattern = re.compile(
        r"<style(?P<attrs>%s)>(?P<body>.*?)</style>" % HTML_ATTRIBUTE_TEXT,
        re.I | re.S,
    )

    def replace(match):
        body = _rewrite_css(
            match.group("body"), resources, "", "inline style", consumed
        )
        return "<style%s>%s</style>" % (match.group("attrs"), body)

    return pattern.sub(replace, document)


def _inline_style_attributes(document, resources, consumed):
    pattern = re.compile(
        r"<(?P<tag>[A-Za-z][^\s/>]*)(?P<attrs>%s)>" % HTML_ATTRIBUTE_TEXT,
        re.S,
    )

    def replace(match):
        attrs = match.group("attrs")
        style = _attribute_value(attrs, "style")
        if not style:
            return match.group(0)
        rewritten = _rewrite_css(
            style,
            resources,
            "",
            "inline style attribute",
            consumed,
        )
        attrs = _replace_attribute_value(attrs, "style", rewritten)
        return "<%s%s>" % (match.group("tag"), attrs)

    return pattern.sub(replace, document)


def _inline_media_attributes(document, resources, consumed):
    tag_pattern = re.compile(
        r"<(?P<tag>img|audio|video|source|track)\b(?P<attrs>%s)>"
        % HTML_ATTRIBUTE_TEXT,
        re.I | re.S,
    )

    def replace_tag(match):
        attrs = match.group("attrs")
        for attr_name in ("src", "poster"):
            value = _attribute_value(attrs, attr_name)
            if not value or _is_passthrough_url(value):
                continue
            key = _resource_key(value)
            resource = resources.get(key)
            if not resource:
                raise PlayableCompatibilityError("missing media resource: %s" % key)
            consumed.add(key)
            attrs = _replace_attribute_value(
                attrs, attr_name, _data_uri(resource)
            )
        return "<%s%s>" % (match.group("tag"), attrs)

    return tag_pattern.sub(replace_tag, document)


def _defer_game_scripts(document):
    pattern = re.compile(
        r"<script(?P<attrs>%s)>(?P<body>.*?)</script>" % HTML_ATTRIBUTE_TEXT,
        re.I | re.S,
    )

    def replace(match):
        attrs = match.group("attrs")
        script_type = _attribute_value(attrs, "type")
        if not _is_javascript_script_type(script_type):
            return match.group(0)
        code = re.sub(r"</script", r"<\/script", match.group("body"), flags=re.I)
        inert = '<script type="application/x-playable-code">%s</script>' % code
        module_type = (
            "s.type='module';"
            if _normalized_script_type(script_type) == "module"
            else ""
        )
        runner = (
            "(function(r){window.__playableReady.then(function(){var s=document.createElement('script');"
            "%ss.textContent=r.previousElementSibling.textContent;r.parentNode.insertBefore(s,r);});})(document.currentScript);"
            % module_type
        )
        return "%s<script>%s</script>" % (inert, runner)

    return pattern.sub(replace, document)


def _is_raster_data_url(value):
    return bool(
        re.match(
            r"^data:image/(?:png|jpe?g|gif|webp|avif|bmp|x-icon|vnd\.microsoft\.icon)(?:;|,)",
            str(value or "").strip(),
            re.I,
        )
    )


def _reject_external_markup(document):
    for style_match in re.finditer(
        r"<style(?P<attrs>%s)>(?P<body>.*?)</style>" % HTML_ATTRIBUTE_TEXT,
        document,
        re.I | re.S,
    ):
        _validate_rewritten_css(style_match.group("body"), "style block")
    for tag_match in re.finditer(
        r"<[A-Za-z][^\s/>]*(?P<attrs>%s)>" % HTML_ATTRIBUTE_TEXT,
        document,
        re.S,
    ):
        style = _attribute_value(tag_match.group("attrs"), "style")
        if style:
            _validate_rewritten_css(style, "inline style attribute")
    markup_only = re.sub(
        r"(<(?:script|style)\b%s>).*?(</(?:script|style)\s*>)"
        % HTML_ATTRIBUTE_TEXT,
        r"\1\2",
        document,
        flags=re.I | re.S,
    )
    resource_attributes = {
        "a": ("href", "xlink:href"),
        "area": ("href", "xlink:href"),
        "base": ("href",),
        "body": ("background",),
        "button": ("formaction",),
        "embed": ("src",),
        "form": ("action",),
        "iframe": ("src", "srcdoc"),
        "image": ("href", "xlink:href"),
        "img": ("src", "srcset"),
        "input": ("src", "formaction"),
        "link": ("href",),
        "meta": (),
        "object": ("data",),
        "script": ("src", "href", "xlink:href"),
        "audio": ("src",),
        "video": ("src", "poster"),
        "source": ("src", "srcset"),
        "track": ("src",),
        "use": ("href", "xlink:href"),
        "feimage": ("href", "xlink:href"),
    }
    tag_pattern = re.compile(
        r"<(?:[A-Za-z][\w.-]*:)?(?P<tag>%s)\b(?P<attrs>%s)>"
        % ("|".join(resource_attributes), HTML_ATTRIBUTE_TEXT),
        re.I | re.S,
    )
    for match in tag_pattern.finditer(markup_only):
        tag = match.group("tag").lower()
        attrs = match.group("attrs")
        if tag == "iframe" and _attribute_value(attrs, "srcdoc"):
            raise PlayableCompatibilityError("external markup URL remains: iframe srcdoc")
        for name in resource_attributes[tag]:
            if name == "srcdoc":
                continue
            value = _attribute_value(attrs, name)
            if name == "srcset" and value:
                raise PlayableCompatibilityError(
                    "external markup URL remains: srcset"
                )
            if (
                value
                and name in ("href", "action", "formaction")
                and tag in ("a", "area", "base", "form", "button", "input")
                and not value.startswith("#")
            ):
                raise PlayableCompatibilityError(
                    "external markup URL remains: %s" % value
                )
            lowered = value.strip().lower()
            if value and tag == "iframe" and name == "src" and lowered != "about:blank":
                raise PlayableCompatibilityError(
                    "external markup URL remains: active iframe source"
                )
            if value and tag in ("object", "embed", "script"):
                raise PlayableCompatibilityError(
                    "external markup URL remains: active %s source" % tag
                )
            if value and tag == "link":
                rel = _attribute_value(attrs, "rel").lower().split()
                if "icon" not in rel or not _is_raster_data_url(value):
                    raise PlayableCompatibilityError(
                        "external markup URL remains: active link source"
                    )
            if value and tag in ("image", "feimage") and not _is_raster_data_url(value):
                raise PlayableCompatibilityError(
                    "external markup URL remains: active SVG image source"
                )
            if value and tag == "use" and not value.startswith("#"):
                raise PlayableCompatibilityError(
                    "external markup URL remains: active SVG use source"
                )
            if (
                value
                and tag in ("img", "body")
                and lowered.startswith("data:")
                and not _is_raster_data_url(value)
            ):
                raise PlayableCompatibilityError(
                    "external markup URL remains: non-raster image data"
                )
            if value and not _is_passthrough_url(value):
                raise PlayableCompatibilityError(
                    "external markup URL remains: %s" % value
                )
        if tag == "meta" and _attribute_value(attrs, "http-equiv").lower() == "refresh":
            raise PlayableCompatibilityError("external markup URL remains: meta refresh")


def _load_lzma_decoder():
    if not os.path.isfile(LZMA_DECODER_PATH):
        raise PlayableCompatibilityError("LZMA browser decoder is missing")
    decoder = _read_text(LZMA_DECODER_PATH)
    if "this.LZMA" not in decoder:
        raise PlayableCompatibilityError("invalid LZMA browser decoder")
    return re.sub(r"</script", r"<\\/script", decoder, flags=re.I)


def _pack_resources(resources):
    metadata = {}
    payload = bytearray()
    for key, item in sorted(resources.items()):
        content = item["content"]
        offset = len(payload)
        payload.extend(content)
        metadata[key] = [item["mime"], offset, len(content)]
    compressed = lzma.compress(
        bytes(payload),
        format=lzma.FORMAT_ALONE,
        filters=[{
            "id": lzma.FILTER_LZMA1,
            "dict_size": 1 << 26,
            "lc": 4,
            "lp": 0,
            "pb": 0,
            "mode": lzma.MODE_NORMAL,
            "nice_len": 273,
            "mf": lzma.MF_BT4,
            "depth": 0,
        }],
    )
    encoded = _base94_encode(compressed)
    if "<" in encoded or _base94_decode(encoded) != compressed:
        raise PlayableCompatibilityError("Base94 package round-trip failed")
    return {
        "metadata": metadata,
        "encoded": encoded,
        "raw_size": len(payload),
        "compressed_size": len(compressed),
        "encoded_size": len(encoded),
        "file_count": len(metadata),
    }


def _safe_json_for_script(value, ensure_ascii=False):
    encoded = json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        separators=(",", ":"),
    )
    return (
        encoded
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _loader_shim(resources):
    package = _pack_resources(resources)
    metadata_json = _safe_json_for_script(
        package["metadata"], ensure_ascii=True
    )
    alphabet_json = json.dumps(BASE94_ALPHABET, ensure_ascii=True)
    decoder = _load_lzma_decoder()
    shim = """<script type="application/x-playable-data">%s</script><script>
%s
(function(){
  var __playablePackageMeta=%s;
  var __playablePackageData=document.currentScript.previousElementSibling.textContent;
  var __playablePackageRawSize=%d;
  var __playablePackageBytes=null;
  var __playableAlphabet=%s;
  var __playableBase=%d,__playableMask=%d,__playableThreshold=%d;
  function __playableDecodeBase94(input){
    var table=new Int16Array(128),i;
    for(i=0;i<table.length;i+=1){table[i]=-1;}
    for(i=0;i<__playableAlphabet.length;i+=1){table[__playableAlphabet.charCodeAt(i)]=i;}
    var bufferValue=0,bitCount=0,pending=-1,output=[];
    for(i=0;i<input.length;i+=1){
      var code=input.charCodeAt(i),decoded=code<table.length?table[code]:-1;
      if(decoded<0){continue;}
      if(pending<0){pending=decoded;continue;}
      pending+=decoded*__playableBase;
      bufferValue|=pending<<bitCount;
      bitCount+=(pending&__playableMask)>__playableThreshold?13:14;
      while(bitCount>7){output.push(bufferValue&255);bufferValue>>>=8;bitCount-=8;}
      pending=-1;
    }
    if(pending>=0){output.push((bufferValue|(pending<<bitCount))&255);}
    return new Uint8Array(output);
  }
  function __playableInitialize(){
    var packed=__playableDecodeBase94(__playablePackageData);
    var unpacked=window.LZMA.decompress(packed);
    var bytes=unpacked instanceof Uint8Array?unpacked:new Uint8Array(unpacked||[]);
    if(bytes.length!==__playablePackageRawSize){throw new Error('embedded package size mismatch: '+bytes.length+' != '+__playablePackageRawSize);}
    __playablePackageBytes=bytes;
    window.__playableFiles=__playablePackageMeta;
    window.__playableCompression='lzma+base94';
    return true;
  }
  function __playableKey(value){
    var text=String(value||'').split(String.fromCharCode(92)).join('/').split('#')[0].split('?')[0];
    if(/^[A-Za-z][A-Za-z0-9+.-]*:/.test(text)||text.indexOf('//')===0){throw new Error('external resource blocked: '+text);}
    while(text.indexOf('./')===0){text=text.slice(2);}
    while(text.indexOf('/')===0){text=text.slice(1);}
    var parts=[];
    text.split('/').forEach(function(part){
      if(!part||part==='.'){return;}
      if(part==='..'){if(!parts.length){throw new Error('resource path escapes root');}parts.pop();return;}
      parts.push(part);
    });
    try{return decodeURIComponent(parts.join('/'));}catch(err){return parts.join('/');}
  }
  function __playableFile(value){
    var key=__playableKey(value);
    var meta=__playablePackageMeta[key];
    if(!meta){throw new Error('embedded resource not found: '+key);}
    if(!__playablePackageBytes){throw new Error('embedded package is not ready');}
    var bytes=__playablePackageBytes.subarray(meta[1],meta[1]+meta[2]);
    return {key:key,mime:meta[0],bytes:bytes};
  }
  function __playableText(bytes){
    if(typeof TextDecoder==='function'){return new TextDecoder('utf-8').decode(bytes);}
    var binary='';
    for(var i=0;i<bytes.length;i+=1){binary+=String.fromCharCode(bytes[i]);}
    try{return decodeURIComponent(escape(binary));}catch(err){return binary;}
  }
  function __PlayableXHR(){
    this.method='GET';this.url='';this.async=true;this.readyState=0;this.status=0;
    this.responseType='';this.response=null;this.responseText='';this.responseURL='';
    this.onload=null;this.onerror=null;this.onprogress=null;this.onreadystatechange=null;
    this._listeners={};this._responseHeaders={};this.upload={};
  }
  __PlayableXHR.prototype.open=function(method,url,async){this.method=String(method||'GET').toUpperCase();this.url=url;this.async=async!==false;this.readyState=1;};
  __PlayableXHR.prototype.setRequestHeader=function(){};
  __PlayableXHR.prototype.overrideMimeType=function(){};
  __PlayableXHR.prototype.abort=function(){};
  __PlayableXHR.prototype.addEventListener=function(type,handler){(this._listeners[type]||(this._listeners[type]=[])).push(handler);};
  __PlayableXHR.prototype.removeEventListener=function(type,handler){var list=this._listeners[type]||[];this._listeners[type]=list.filter(function(item){return item!==handler;});};
  __PlayableXHR.prototype._emit=function(type,event){
    var handler=this['on'+type];if(typeof handler==='function'){handler.call(this,event);}
    (this._listeners[type]||[]).slice().forEach(function(item){item.call(this,event);},this);
  };
  __PlayableXHR.prototype.getResponseHeader=function(name){return this._responseHeaders[String(name||'').toLowerCase()]||null;};
  __PlayableXHR.prototype.getAllResponseHeaders=function(){var self=this;return Object.keys(this._responseHeaders).map(function(key){return key+': '+self._responseHeaders[key];}).join(String.fromCharCode(13,10));};
  __PlayableXHR.prototype.send=function(){
    var self=this;
    function complete(){
      try{
        var file=__playableFile(self.url),bytes=file.bytes;
        self.status=200;self.readyState=4;self.responseURL='embedded://'+file.key;
        self._responseHeaders={'content-length':String(bytes.length),'content-type':file.mime};
        if(self.method==='HEAD'){self.response='';self.responseText='';}
        else if(self.responseType==='arraybuffer'){self.response=bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength);}
        else if(self.responseType==='blob'&&typeof Blob==='function'){self.response=new Blob([bytes],{type:file.mime});}
        else if(self.responseType==='json'){self.response=JSON.parse(__playableText(bytes));}
        else{self.responseText=__playableText(bytes);self.response=self.responseText;}
        self._emit('progress',{lengthComputable:true,loaded:bytes.length,total:bytes.length,target:self});
        if(typeof self.onreadystatechange==='function'){self.onreadystatechange();}
        self._emit('load',{target:self});self._emit('loadend',{target:self});
      }catch(err){
        self.status=404;self.readyState=4;
        if(typeof self.onreadystatechange==='function'){self.onreadystatechange();}
        self._emit('error',{target:self,error:err});self._emit('loadend',{target:self});
      }
    }
    if(this.async){setTimeout(complete,0);}else{complete();}
  };
  function __playableRead(value){
    try{
      var file=__playableFile(value);
      if(typeof Response==='function'){
        return Promise.resolve(new Response(file.bytes,{status:200,headers:{'Content-Type':file.mime,'Content-Length':String(file.bytes.length)}}));
      }
      return Promise.resolve({ok:true,status:200,headers:{get:function(name){return String(name).toLowerCase()==='content-length'?String(file.bytes.length):file.mime;}},arrayBuffer:function(){return Promise.resolve(file.bytes.buffer.slice(file.bytes.byteOffset,file.bytes.byteOffset+file.bytes.byteLength));},text:function(){return Promise.resolve(__playableText(file.bytes));},json:function(){return Promise.resolve(JSON.parse(__playableText(file.bytes)));}});
    }catch(err){return Promise.reject(err);}
  }
  function __playableRequestInstall(){
    window.parent.postMessage({type:'meta-playable-cta'},'*');
  }
  var __playableLocationFacade={};
  Object.defineProperties(__playableLocationFacade,{
    href:{enumerable:true,get:function(){return 'about:blank';},set:__playableRequestInstall},
    origin:{enumerable:true,value:'null'},protocol:{enumerable:true,value:'about:'},
    host:{enumerable:true,value:''},hostname:{enumerable:true,value:''},port:{enumerable:true,value:''},
    pathname:{enumerable:true,value:'blank'},search:{enumerable:true,value:''},hash:{enumerable:true,value:''},
    assign:{value:__playableRequestInstall},replace:{value:__playableRequestInstall},reload:{value:function(){}},
    toString:{value:function(){return 'about:blank';}}
  });
  Object.freeze(__playableLocationFacade);
  function __playableLock(target,name,descriptor){
    try{Object.defineProperty(target,name,descriptor);return true;}catch(error){return false;}
  }
  var __playableLocationDescriptor={configurable:false,enumerable:false,get:function(){return __playableLocationFacade;},set:__playableRequestInstall};
  if(!__playableLock(window,'__playableLocation',__playableLocationDescriptor)||!__playableLock(document,'__playableLocation',__playableLocationDescriptor)){
    throw new Error('playable location guard is unavailable');
  }
  function __playableOpen(){__playableRequestInstall();return null;}
  if(!__playableLock(window,'open',{configurable:false,writable:false,value:__playableOpen})){
    throw new Error('playable popup guard is unavailable');
  }
  var __playableNativeSetTimeout=window.setTimeout;
  var __playableNativeSetInterval=window.setInterval;
  function __playableSafeTimeout(handler){
    if(typeof handler!=='function'){console.warn('String timer blocked by playable sandbox');return 0;}
    return __playableNativeSetTimeout.apply(window,arguments);
  }
  function __playableSafeInterval(handler){
    if(typeof handler!=='function'){console.warn('String timer blocked by playable sandbox');return 0;}
    return __playableNativeSetInterval.apply(window,arguments);
  }
  if(!__playableLock(window,'setTimeout',{configurable:false,writable:false,value:__playableSafeTimeout})||!__playableLock(window,'setInterval',{configurable:false,writable:false,value:__playableSafeInterval})){
    throw new Error('playable timer guard is unavailable');
  }
  function __playableBlockNavigation(event){
    var node=event&&event.target;
    while(node&&node.nodeType===1){
      var tag=String(node.tagName||'').toLowerCase();
      if(tag==='a'||tag==='area'){
        var href=node.getAttribute('href')||node.getAttribute('xlink:href')||'';
        if(href&&href.charAt(0)!=='#'){
          event.preventDefault();event.stopImmediatePropagation();__playableRequestInstall();
        }
        return;
      }
      node=node.parentElement;
    }
  }
  window.addEventListener('click',__playableBlockNavigation,true);
  window.addEventListener('auxclick',__playableBlockNavigation,true);
  document.addEventListener('click',__playableBlockNavigation,true);
  document.addEventListener('auxclick',__playableBlockNavigation,true);
  window.__playableNavigationGuard=true;
  window.__PlayableXHR=__PlayableXHR;
  window.__playableRead=__playableRead;
  Object.defineProperty(window,'XMLHttpRequest',{configurable:true,writable:true,value:__PlayableXHR});
  Object.defineProperty(window,'fetch',{configurable:true,writable:true,value:__playableRead});
  window.__playableReady=Promise.resolve().then(__playableInitialize);
  window.__playableReady.then(function(){window.parent.postMessage({type:'meta-playable-ready'},'*');}).catch(function(error){
    console.error('Playable package initialization failed',error);
    window.parent.postMessage({type:'meta-playable-error',message:String(error&&error.message||error)},'*');
  });
})();
</script>""" % (
        package["encoded"],
        decoder,
        metadata_json,
        package["raw_size"],
        alphabet_json,
        BASE94_RADIX,
        BASE94_LOW_MASK,
        BASE94_THRESHOLD,
    )
    metrics = {
        "resource_encoding": RESOURCE_ENCODING,
        "packed_file_count": package["file_count"],
        "resource_raw_bytes": package["raw_size"],
        "resource_compressed_bytes": package["compressed_size"],
        "resource_encoded_bytes": package["encoded_size"],
    }
    return shim, metrics

def _inject_head(document, content):
    head_pattern = r"<head\b%s>" % HTML_ATTRIBUTE_TEXT
    if re.search(head_pattern, document, re.I):
        return re.sub(
            "(" + head_pattern + ")",
            lambda match: match.group(1) + content,
            document,
            count=1,
            flags=re.I,
        )
    html_pattern = r"<html\b%s>" % HTML_ATTRIBUTE_TEXT
    if re.search(html_pattern, document, re.I):
        return re.sub(
            "(" + html_pattern + ")",
            lambda match: match.group(1) + "<head>" + content + "</head>",
            document,
            count=1,
            flags=re.I,
        )
    return "<head>" + content + "</head>" + document


def _inner_document(game_dir, entry_relative):
    game_dir = os.path.abspath(game_dir)
    entry_path = os.path.abspath(os.path.join(game_dir, entry_relative.replace("/", os.sep)))
    if not entry_path.startswith(game_dir + os.sep) or not os.path.isfile(entry_path):
        raise PlayableCompatibilityError("playable entry is missing or unsafe: %s" % entry_relative)
    resources = _collect_resources(game_dir, entry_path)
    consumed = set()
    document = _strip_source_csp(_read_text(entry_path))
    document = _inline_links(document, resources, consumed)
    document = _inline_style_blocks(document, resources, consumed)
    document = _inline_style_attributes(document, resources, consumed)
    document = _inline_media_attributes(document, resources, consumed)
    document = _inline_scripts(document, resources, consumed)
    _reject_external_markup(document)
    document = _defer_game_scripts(document)
    packed_resources = {
        key: item for key, item in resources.items() if key not in consumed
    }
    loader, package_metrics = _loader_shim(packed_resources)
    csp_meta = '<meta http-equiv="Content-Security-Policy" content="%s">' % (
        html_lib.escape(PLAYABLE_CSP, quote=True)
    )
    document = _inject_head(document, csp_meta + loader)
    package_metrics["embedded_file_count"] = len(resources)
    package_metrics["inlined_file_count"] = len(consumed)
    return document, resources, package_metrics


def _encode_script_raw_text(document):
    seed = hashlib.sha256(document.encode("utf-8")).digest()
    for marker_index in range(16):
        marker = "~p%s~" % hashlib.sha256(
            seed + marker_index.to_bytes(2, "big")
        ).hexdigest()[:16]
        if marker not in document:
            return document.replace("<", marker), marker
    raise PlayableCompatibilityError("unable to reserve playable raw-text marker")


def _outer_document(inner_document, title, play_count, trial_seconds, translations):
    protected_inner, less_than_marker = _encode_script_raw_text(inner_document)
    safe_title = html_lib.escape(str(title or "Playable Preview"))
    config_json = _safe_json_for_script({
        "playCount": max(1, int(play_count or 1)),
        "trialSeconds": max(1, min(120, int(trial_seconds or 20))),
        "translations": translations or {},
    })
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
  <meta http-equiv="Content-Security-Policy" content="%s">
  <title>%s</title>
  <style>
    html,body{margin:0;width:100%%;height:100%%;overflow:hidden;background:#05070a;font-family:Arial,Helvetica,sans-serif;color:#fff}
    #game-frame{position:fixed;inset:0;width:100%%;height:100%%;border:0;background:#000}
    #timer{position:fixed;top:12px;right:12px;z-index:5;padding:7px 10px;border-radius:999px;background:rgba(0,0,0,.55);font-size:13px}
    #plays{position:fixed;top:12px;left:12px;z-index:5;padding:7px 10px;border-radius:999px;background:rgba(0,0,0,.55);font-size:13px}
    #overlay{position:fixed;inset:0;z-index:10;display:none;align-items:center;justify-content:center;padding:24px;background:rgba(5,7,10,.82)}
    #overlay.show{display:flex}.panel{width:min(380px,100%%);text-align:center}.headline{margin:0 0 8px;font-size:25px}.subcopy{margin:0 0 18px;color:rgba(255,255,255,.78)}
    .actions{display:flex;flex-direction:column;gap:10px}button{border:0;border-radius:10px;padding:15px 18px;font-size:17px;font-weight:800;cursor:pointer}
    #install-button{color:#050505;background:#fff}#replay-button{color:#fff;background:rgba(255,255,255,.16)}
  </style>
</head>
<body>
  <script type="application/x-playable-html" id="game-source" data-lt-marker="%s">%s</script>
  <iframe id="game-frame" title="Playable game" sandbox="allow-scripts allow-pointer-lock" allow="autoplay; fullscreen; gamepad; accelerometer; gyroscope"></iframe>
  <div id="plays">Plays: 1</div><div id="timer">20s</div>
  <div id="overlay" aria-live="polite"><div class="panel">
    <h1 class="headline" data-i18n="headline">Install to Play More</h1>
    <p class="subcopy" data-i18n="subtitle">Your playable preview has ended.</p>
    <div class="actions"><button id="install-button" type="button">Install to Play More</button><button id="replay-button" type="button">Play Again</button></div>
  </div></div>
  <script>
  (function(){
    var cfg=%s,frame=document.getElementById('game-frame'),timer=document.getElementById('timer'),plays=document.getElementById('plays'),overlay=document.getElementById('overlay'),replay=document.getElementById('replay-button');
    var source=document.getElementById('game-source'),innerSource=source.textContent.split(source.getAttribute('data-lt-marker')).join('<');
    var attempts=0,timeoutId=0,tickId=0,trialStarted=false;
    function langCopy(){var table=cfg.translations||{},langs=(navigator.languages&&navigator.languages.length?navigator.languages:[navigator.language||'en']);for(var i=0;i<langs.length;i+=1){var key=String(langs[i]||'').toLowerCase().replace(/_/g,'-');if(table[key]){return table[key];}var base=key.split('-')[0];if(table[base]){return table[base];}}return table.en||{};}
    var copy=langCopy();document.querySelector('[data-i18n="headline"]').textContent=copy.headline||'Install to Play More';document.querySelector('[data-i18n="subtitle"]').textContent=copy.subtitle||'Your playable preview has ended.';document.getElementById('install-button').textContent=copy.cta||'Install to Play More';
    function stopTimers(){clearTimeout(timeoutId);clearInterval(tickId);}
    function endTrial(){stopTimers();trialStarted=false;timer.textContent='0s';replay.style.display=attempts<cfg.playCount?'block':'none';overlay.classList.add('show');frame.style.pointerEvents='none';}
    function beginCountdown(){if(trialStarted){return;}trialStarted=true;var remaining=cfg.trialSeconds;timer.textContent=remaining+'s';tickId=setInterval(function(){remaining=Math.max(0,remaining-1);timer.textContent=remaining+'s';},1000);timeoutId=setTimeout(endTrial,cfg.trialSeconds*1000);}
    function startTrial(){stopTimers();trialStarted=false;attempts+=1;plays.textContent=(copy.plays||'Plays')+': '+attempts+'/'+cfg.playCount;overlay.classList.remove('show');frame.style.pointerEvents='auto';timer.textContent='...';frame.srcdoc=innerSource;}
    function install(){if(window.FbPlayableAd&&typeof window.FbPlayableAd.onCTAClick==='function'){window.FbPlayableAd.onCTAClick();}}
    window.addEventListener('message',function(event){if(!event||!event.data||event.source!==frame.contentWindow){return;}if(event.data.type==='meta-playable-ready'){beginCountdown();}else if(event.data.type==='meta-playable-cta'){install();}else if(event.data.type==='meta-playable-game-over'){endTrial();}else if(event.data.type==='meta-playable-error'){console.error('Playable failed',event.data.message||'unknown error');endTrial();}});
    document.getElementById('install-button').addEventListener('click',install);replay.addEventListener('click',startTrial);startTrial();
  })();
  </script>
</body>
</html>
""" % (
    html_lib.escape(PLAYABLE_CSP, quote=True),
    safe_title,
    less_than_marker,
    protected_inner,
    config_json,
)


def _javascript_sources_from_markup(document):
    sources = []
    for match in re.finditer(
        r"<script(?P<attrs>%s)>(?P<body>.*?)</script>" % HTML_ATTRIBUTE_TEXT,
        document,
        re.I | re.S,
    ):
        script_type = _attribute_value(match.group("attrs"), "type")
        if (
            script_type.lower() != "application/x-playable-code"
            and not _is_javascript_script_type(script_type)
        ):
            continue
        sources.append(match.group("body"))
    for tag in re.finditer(
        r"<[A-Za-z][^\s/>]*(?P<attrs>%s)>" % HTML_ATTRIBUTE_TEXT,
        document,
        re.S,
    ):
        for name, value in _attribute_items(tag.group("attrs")):
            if name.lower().startswith("on"):
                sources.append(value)
    return sources


def _meta_playable_documents(document):
    raw_match = next(
        (
            match
            for match in re.finditer(
                r"<script(?P<attrs>%s)>(?P<body>.*?)</script>"
                % HTML_ATTRIBUTE_TEXT,
                document,
                re.I | re.S,
            )
            if _attribute_value(match.group("attrs"), "id") == "game-source"
        ),
        None,
    )
    if raw_match:
        marker = _attribute_value(raw_match.group("attrs"), "data-lt-marker")
        if not marker:
            raise PlayableCompatibilityError("playable inner document marker is missing")
        protected_inner = raw_match.group("body")
        if "<" in protected_inner:
            raise PlayableCompatibilityError("playable inner document raw-text protection is invalid")
        inner_document = protected_inner.replace(marker, "<")
        outer_document = document[:raw_match.start()] + document[raw_match.end():]
        return outer_document, inner_document
    template_match = next(
        (
            match
            for match in re.finditer(
                r"<template(?P<attrs>%s)>(?P<body>.*?)</template>"
                % HTML_ATTRIBUTE_TEXT,
                document,
                re.I | re.S,
            )
            if _attribute_value(match.group("attrs"), "id") == "game-source"
        ),
        None,
    )
    inner_document = html_lib.unescape(template_match.group("body")) if template_match else ""
    outer_document = (
        document[:template_match.start()] + document[template_match.end():]
        if template_match
        else document
    )
    return outer_document, inner_document


def validate_meta_playable_html(document):
    outer_document, inner_document = _meta_playable_documents(document)
    javascript_sources = _javascript_sources_from_markup(outer_document)
    if inner_document:
        javascript_sources.extend(_javascript_sources_from_markup(inner_document))
    code_view = "\n".join(
        _javascript_code_view(source) for source in javascript_sources
    )
    computed_unsafe_members = {
        match.group("name")
        for source in javascript_sources
        for match in _javascript_executable_member_strings(
            source, ("Function", "eval", "setInterval", "setTimeout")
        )
    }
    computed_unsafe_members.update(
        match.group("name")
        for source in javascript_sources
        for match in _javascript_executable_reflect_members(
            source, ("Function", "eval", "setInterval", "setTimeout")
        )
    )
    uses_string_timer = any(
        _javascript_uses_string_timer(source)
        for source in javascript_sources
    )
    has_unsafe_eval_reference = any(
        _javascript_has_unsafe_eval_reference(source)
        for source in javascript_sources
    )
    has_unsafe_constructor_reference = any(
        _javascript_has_unsafe_constructor_reference(source)
        for source in javascript_sources
    )
    computed_redirect_members = {
        match.group("name")
        for source in javascript_sources
        for match in _javascript_executable_member_strings(
            source,
            ("assign", "href", "location", "open", "replace"),
            objects=(
                "window", "globalThis", "self", "document",
                "this", "top", "parent", "location",
            ),
        )
    }
    computed_redirect_members.update(
        match.group("name")
        for source in javascript_sources
        for match in _javascript_executable_reflect_members(
            source,
            ("assign", "href", "location", "open", "replace"),
            objects=(
                "window", "globalThis", "self", "document",
                "this", "top", "parent", "location",
            ),
        )
    )
    redirect_checks = _javascript_redirect_patterns()
    checks = {
        "window_open": redirect_checks["window_open"],
        "direct_location": redirect_checks["direct_location"],
        "unsafe_eval_bootstrap": _javascript_unsafe_eval_pattern(),
        "unguarded_location_reference": r"(?<![\w$])location(?![\w$])",
        "unsupported_network_api": r"\b(?:Worker|SharedWorker|WebSocket|EventSource)\s*\(|\bnavigator\s*\.\s*sendBeacon\s*\(|(?<![\w$.])\bimport\b(?!\s*\.)|(?<![\w$.])\bexport\b[^;]*\bfrom\b",
    }
    failures = [
        name for name, pattern in checks.items()
        if re.search(pattern, code_view, re.S)
    ]
    if computed_unsafe_members:
        failures.append("unsafe_eval_bootstrap")
    if uses_string_timer:
        failures.append("unsafe_eval_bootstrap")
    if has_unsafe_eval_reference or has_unsafe_constructor_reference:
        failures.append("unsafe_eval_bootstrap")
    if "open" in computed_redirect_members:
        failures.append("window_open")
    if computed_redirect_members.difference(("open",)):
        failures.append("direct_location")
    failures = list(dict.fromkeys(failures))
    try:
        _reject_external_markup(outer_document)
        if inner_document:
            _reject_external_markup(inner_document)
    except PlayableCompatibilityError:
        failures.append("external_markup")
    if not re.search(r"\bFbPlayableAd\s*\.\s*onCTAClick\b", code_view):
        failures.append("missing_meta_cta_hook")
    if failures:
        raise PlayableCompatibilityError("Meta compatibility validation failed: %s" % ", ".join(failures))
    return {
        "single_file": True,
        "native_network_requests": 0,
        "direct_redirects": 0,
        "unsafe_eval_calls": 0,
        "csp_safe_script_bootstrap": True,
        "navigation_guard": (
            "window.__playableNavigationGuard=true" in inner_document
            and "__playableBlockNavigation" in inner_document
            and "__playableLocationFacade" in inner_document
        ),
        "safe_timer_wrappers": (
            "__playableSafeTimeout" in inner_document
            and "__playableSafeInterval" in inner_document
        ),
        "embedded_csp": (
            "Content-Security-Policy" in outer_document
            and bool(inner_document)
            and "Content-Security-Policy" in inner_document
        ),
        "opaque_origin_sandbox": bool(
            re.search(
                r"<iframe\b(?=[^>]*\bsandbox=)(?![^>]*allow-same-origin)",
                outer_document,
                re.I | re.S,
            )
        ),
        "cta_hook": META_CTA_HOOK,
    }


def build_meta_playable_html(
    game_dir,
    entry_relative,
    title,
    play_count,
    trial_seconds,
    translations,
    max_asset_bytes=DEFAULT_META_ASSET_LIMIT_BYTES,
):
    inner, resources, package_metrics = _inner_document(game_dir, entry_relative)
    document = _outer_document(inner, title, play_count, trial_seconds, translations)
    compatibility = validate_meta_playable_html(document)
    html_size = len(document.encode("utf-8"))
    limit = max(1, int(max_asset_bytes or DEFAULT_META_ASSET_LIMIT_BYTES))
    if html_size > limit:
        raise PlayableCompatibilityError(
            "generated Meta playable HTML exceeds safety limit: %s > %s"
            % (html_size, limit)
        )
    compatibility.update(package_metrics)
    compatibility["embedded_file_count"] = len(resources)
    compatibility["source_entry"] = entry_relative.replace("\\", "/")
    compatibility["html_size"] = html_size
    compatibility["meta_size_limit_bytes"] = limit
    compatibility["html_size_headroom_bytes"] = limit - html_size
    return document, compatibility
