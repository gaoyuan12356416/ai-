#!/usr/bin/env python3
import argparse
import ast
from email import policy
from email.parser import BytesParser
import html as html_lib
import io
import json
import lzma
import os
import re
import secrets
import shutil
import sys
import tempfile
import zipfile


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fb_playable_generator import (
    DEFAULT_META_ASSET_LIMIT_BYTES,
    PlayableCompatibilityError,
    RESOURCE_ENCODING,
    BASE94_ALPHABET,
    _base94_decode,
    _base94_encode,
    _attribute_value,
    _defer_game_scripts,
    _encode_script_raw_text,
    _inject_head,
    _javascript_code_view,
    _javascript_sources_from_markup,
    _loader_shim,
    _meta_playable_documents,
    _patch_javascript,
    _reject_external_markup,
    _replace_attribute_value,
    _rewrite_css,
    _safe_json_for_script,
    _strip_source_csp,
    build_meta_playable_html,
    validate_meta_playable_html,
)


TRANSLATIONS = {
    "en": {
        "headline": "Install to Play More",
        "subtitle": "Your playable preview has ended.",
        "cta": "Install to Play More",
        "plays": "Plays",
    }
}


def assert_multipart_trial_seconds_contract():
    app_path = os.path.join(ROOT, "app.py")
    with open(app_path, "r", encoding="utf-8") as handle:
        app_source = handle.read()
    tree = ast.parse(app_source, filename=app_path)
    parser_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "parse_playable_preview_multipart"
    )

    boundary = "playable-fixture-boundary"

    def multipart_body(fields, files):
        chunks = []
        for key, value in fields:
            chunks.extend([
                "--%s\r\n" % boundary,
                'Content-Disposition: form-data; name="%s"\r\n\r\n' % key,
                "%s\r\n" % value,
            ])
        for key, filename, data in files:
            chunks.extend([
                "--%s\r\n" % boundary,
                'Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (key, filename),
                "Content-Type: text/html\r\n\r\n",
            ])
            chunks.append(data)
            chunks.append(b"\r\n")
        chunks.append("--%s--\r\n" % boundary)
        return b"".join(
            item if isinstance(item, bytes) else item.encode("utf-8")
            for item in chunks
        )

    class FakeHandler:
        def __init__(self, body):
            self.headers = {
                "Content-Type": "multipart/form-data; boundary=%s" % boundary
            }
            self.rfile = io.BytesIO(body)

    namespace = {"BytesParser": BytesParser, "policy": policy}
    module = ast.Module(body=[parser_node], type_ignores=[])
    exec(compile(module, app_path, "exec"), namespace)
    body = multipart_body(
        [
            ("store_url", "https://play.google.com/store/apps/details?id=fixture"),
            ("trial_seconds", "7"),
        ],
        [("static_page", "index.html", b"<!doctype html><html></html>")],
    )
    payload = namespace["parse_playable_preview_multipart"](
        FakeHandler(body), len(body)
    )
    if payload.get("trial_seconds") != "7":
        raise AssertionError("multipart trial_seconds was not preserved")
    closing = ("--%s--\r\n" % boundary).encode("ascii")
    truncated_body = body[:-len(closing)]
    try:
        namespace["parse_playable_preview_multipart"](
            FakeHandler(truncated_body), len(truncated_body)
        )
    except ValueError as error:
        if "invalid multipart request body" not in str(error):
            raise
    else:
        raise AssertionError("multipart body without a closing boundary was accepted")
    unknown_charset_body = body.replace(
        b'name="trial_seconds"\r\n\r\n',
        b'name="trial_seconds"\r\nContent-Type: text/plain; charset=x-unknown\r\n\r\n',
        1,
    )
    try:
        namespace["parse_playable_preview_multipart"](
            FakeHandler(unknown_charset_body), len(unknown_charset_body)
        )
    except ValueError as error:
        if "invalid multipart field encoding" not in str(error):
            raise
    else:
        raise AssertionError("unknown multipart charset was accepted")
    duplicate_body = multipart_body(
        [("trial_seconds", "7"), ("trial_seconds", "8")],
        [("static_page", "index.html", b"<!doctype html><html></html>")],
    )
    try:
        namespace["parse_playable_preview_multipart"](
            FakeHandler(duplicate_body), len(duplicate_body)
        )
    except ValueError as error:
        if "duplicate multipart field" not in str(error):
            raise
    else:
        raise AssertionError("duplicate multipart scalar was accepted")
    multi_upload_body = multipart_body(
        [("store_url", "https://example.test/store")],
        [
            ("static_page", "index.html", b"<!doctype html><html></html>"),
            ("file", "second.html", b"<!doctype html><html></html>"),
        ],
    )
    try:
        namespace["parse_playable_preview_multipart"](
            FakeHandler(multi_upload_body), len(multi_upload_body)
        )
    except ValueError as error:
        if "multiple upload files" not in str(error):
            raise
    else:
        raise AssertionError("multiple multipart uploads were accepted")
    return True


def app_ast_nodes(*names):
    app_path = os.path.join(ROOT, "app.py")
    with open(app_path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=app_path)
    wanted = set(names)
    nodes = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            nodes[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    nodes[target.id] = node
    missing = wanted.difference(nodes)
    if missing:
        raise AssertionError("app.py AST nodes missing: %s" % sorted(missing))
    return app_path, [nodes[name] for name in names]


def assert_translation_isolation_contract():
    app_path, nodes = app_ast_nodes(
        "PLAYABLE_PREVIEW_TRANSLATIONS", "playable_preview_translations"
    )
    namespace = {"json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), app_path, "exec"), namespace)
    defaults = namespace["PLAYABLE_PREVIEW_TRANSLATIONS"]
    original = defaults["en"]["headline"]
    first = namespace["playable_preview_translations"]({"headline_text": "REQUEST-A"})
    second = namespace["playable_preview_translations"]({})
    if first["en"]["headline"] != "REQUEST-A":
        raise AssertionError("request translation override was not applied")
    if second["en"]["headline"] != original or defaults["en"]["headline"] != original:
        raise AssertionError("translation override leaked into a later request")
    return True


def assert_auth_mode_contract():
    names = (
        "resolve_playable_preview_auth_mode",
        "evaluate_playable_preview_auth",
        "log_playable_preview_auth",
    )
    app_path, nodes = app_ast_nodes(*names)

    class CaptureLogging:
        messages = []

        @classmethod
        def info(cls, template, *values):
            cls.messages.append(template % values)

    namespace = {"logging": CaptureLogging, "secrets": secrets}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), app_path, "exec"), namespace)
    resolve = namespace[names[0]]
    evaluate = namespace[names[1]]
    log_auth = namespace[names[2]]
    if resolve("", "") != "observe" or resolve("", "secret") != "enforce":
        raise AssertionError("default auth mode compatibility changed")
    for mode in ("off", "observe", "enforce"):
        if resolve(mode.upper(), "") != mode:
            raise AssertionError("explicit auth mode was not normalized")
    try:
        resolve("typo", "")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid auth mode was accepted")
    expected = "sentinel-secret-never-log"
    cases = [
        ({}, "observe", (False, False, True)),
        ({"Authorization": "Bearer wrong"}, "observe", (True, False, True)),
        ({"Authorization": "Bearer " + expected}, "enforce", (True, True, True)),
        ({"X-API-Token": expected}, "enforce", (True, True, True)),
        ({"Authorization": "Bearer 错误令牌"}, "enforce", (True, False, False)),
        ({}, "enforce", (False, False, False)),
    ]
    for headers, mode, wanted in cases:
        if evaluate(headers, expected, mode) != wanted:
            raise AssertionError("auth evaluation mismatch for %s" % mode)
    present, valid, allowed = evaluate({}, expected, "observe")
    log_auth("/api/fb-playable/preview", "observe", present, valid, allowed)
    rendered = "\n".join(CaptureLogging.messages)
    if expected in rendered or "Authorization" in rendered or "X-API-Token" in rendered:
        raise AssertionError("auth log leaked credential material")
    return True


def assert_json_request_contract():
    app_path, nodes = app_ast_nodes("parse_playable_preview_request")

    class FakeHandler:
        def __init__(self, body):
            self.headers = {
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
            }
            self.rfile = io.BytesIO(body)

    namespace = {
        "PLAYABLE_PREVIEW_MAX_UPLOAD_BYTES": 1024,
        "base64": __import__("base64"),
        "json": json,
        "parse_playable_preview_multipart": None,
        "re": re,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), app_path, "exec"), namespace)
    parse_request = namespace["parse_playable_preview_request"]
    for invalid_body in (b"[]", b"null", b'"text"'):
        try:
            parse_request(FakeHandler(invalid_body))
        except ValueError as error:
            if "must be an object" not in str(error):
                raise
        else:
            raise AssertionError("non-object JSON request body was accepted")
    return True


def assert_cleanup_wrapper_contract():
    app_path, nodes = app_ast_nodes(
        "cleanup_playable_preview_artifacts",
        "create_playable_preview",
    )
    cleaned = []

    class FixedUUID:
        hex = "fixed-preview-id"

    class UUIDModule:
        @staticmethod
        def uuid4():
            return FixedUUID()

    def fail_create(_payload, _preview_id):
        raise RuntimeError("fault injection")

    class QuietLogging:
        @staticmethod
        def warning(*_args):
            return None

    with tempfile.TemporaryDirectory() as root:
        output_dir = os.path.join(root, "fixed-preview-id")
        os.makedirs(output_dir)
        with open(os.path.join(output_dir, "partial"), "wb") as handle:
            handle.write(b"partial")

        def fail_client():
            raise RuntimeError("COS init failed")

        namespace = {
            "COS_BUCKET": "fixture-bucket",
            "build_cos_object_key": lambda value: value,
            "cos_enabled": lambda: True,
            "get_cos_client": fail_client,
            "logging": QuietLogging,
            "os": os,
            "playable_preview_root": lambda: root,
            "shutil": shutil,
            "uuid": UUIDModule,
            "_create_playable_preview": fail_create,
            "cleanup_playable_preview_artifacts": None,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), app_path, "exec"), namespace)
        namespace["cleanup_playable_preview_artifacts"]("fixed-preview-id")
        if os.path.exists(output_dir):
            raise AssertionError("COS cleanup failure prevented local cleanup")

    namespace.update({
        "uuid": UUIDModule,
        "_create_playable_preview": fail_create,
        "cleanup_playable_preview_artifacts": cleaned.append,
    })
    try:
        namespace["create_playable_preview"]({})
    except RuntimeError as error:
        if str(error) != "fault injection":
            raise
    else:
        raise AssertionError("fault injection did not propagate")
    if cleaned != ["fixed-preview-id"]:
        raise AssertionError("failed playable preview did not run cleanup")
    return True


def assert_zip_extraction_limits_contract():
    with open(os.path.join(ROOT, "app.py"), "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    if not any(
        isinstance(node, ast.Import)
        and any(alias.name == "posixpath" for alias in node.names)
        for node in tree.body
    ):
        raise AssertionError("app.py does not import posixpath")
    app_path, nodes = app_ast_nodes("safe_extract_zip")
    namespace = {"os": os, "posixpath": __import__("posixpath"), "re": re, "zipfile": zipfile}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), app_path, "exec"), namespace)
    safe_extract_zip = namespace["safe_extract_zip"]
    with tempfile.TemporaryDirectory() as root:
        bomb_path = os.path.join(root, "bomb.zip")
        with zipfile.ZipFile(bomb_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large.bin", b"0" * 8192)
        target = os.path.join(root, "bomb")
        try:
            safe_extract_zip(bomb_path, target, 1024, 10)
        except ValueError as error:
            if "extracted size exceeds limit" not in str(error):
                raise
        else:
            raise AssertionError("compressed ZIP bomb was accepted")
        traversal_path = os.path.join(root, "traversal.zip")
        with zipfile.ZipFile(traversal_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("../escape.txt", b"escape")
        try:
            safe_extract_zip(traversal_path, os.path.join(root, "traversal"), 1024, 10)
        except ValueError as error:
            if "unsafe zip entry" not in str(error):
                raise
        else:
            raise AssertionError("ZIP traversal was accepted")
        directories_path = os.path.join(root, "directories.zip")
        with zipfile.ZipFile(directories_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for index in range(5):
                archive.writestr("dir-%s/" % index, b"")
            archive.writestr("file.txt", b"ok")
        try:
            safe_extract_zip(
                directories_path,
                os.path.join(root, "directories"),
                1024,
                2,
            )
        except ValueError as error:
            if "too many files" not in str(error):
                raise
        else:
            raise AssertionError("ZIP directory entry count bypassed the file-count limit")
        unsupported_path = os.path.join(root, "unsupported.zip")
        with zipfile.ZipFile(unsupported_path, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("file.txt", b"ok")
        with open(unsupported_path, "rb") as handle:
            payload = bytearray(handle.read())
        local_header = payload.find(b"PK\x03\x04")
        central_header = payload.find(b"PK\x01\x02")
        payload[local_header + 8:local_header + 10] = (99).to_bytes(2, "little")
        payload[central_header + 10:central_header + 12] = (99).to_bytes(2, "little")
        with open(unsupported_path, "wb") as handle:
            handle.write(payload)
        try:
            safe_extract_zip(
                unsupported_path,
                os.path.join(root, "unsupported"),
                1024,
                10,
            )
        except ValueError as error:
            if "unsupported zip compression method" not in str(error):
                raise
        else:
            raise AssertionError("unsupported ZIP compression was accepted")
        for index, names in enumerate(
            (("node", "node/child.txt"), ("node/child.txt", "node"))
        ):
            conflict_path = os.path.join(root, "conflict-%s.zip" % index)
            with zipfile.ZipFile(conflict_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for name in names:
                    archive.writestr(name, b"conflict")
            try:
                safe_extract_zip(
                    conflict_path,
                    os.path.join(root, "conflict-%s" % index),
                    1024,
                    10,
                )
            except ValueError as error:
                if "file and directory paths conflict" not in str(error):
                    raise
            else:
                raise AssertionError("ZIP file/directory ancestor conflict was accepted")
    return True


def assert_lexical_validation_contract():
    harmless = (
        'var a="fetch(";var b="XMLHttpRequest";var c="window.open(";'
        "// window.open('comment')\n"
        "/* location.assign('comment') */\n"
        r"var pattern=/window\.open\(/;"
    )
    if _patch_javascript(harmless, "harmless.js") != harmless:
        raise AssertionError("JavaScript strings/comments/regex literals were modified")
    for harmless_regex in (
        r"if(ok) /fetch\(/.test(value)",
        r"while(ok) /XMLHttpRequest/.test(value)",
        r"if(ok){} /fetch\(/.test(value)",
    ):
        if _patch_javascript(harmless_regex, "regex.js") != harmless_regex:
            raise AssertionError("control-statement regex literal was modified")
    patched = _patch_javascript(
        "function load(){return fetch('data.json')}var xhr=new XMLHttpRequest();",
        "network.js",
    )
    if patched != "function load(){return fetch('data.json')}var xhr=new XMLHttpRequest();":
        raise AssertionError("global network source was rewritten instead of runtime-bound")
    computed = _patch_javascript(
        "var f=window['fetch'];var X=window[\"XMLHttpRequest\"];new X();f('data.json');",
        "computed.js",
    )
    if computed != "var f=window['fetch'];var X=window[\"XMLHttpRequest\"];new X();f('data.json');":
        raise AssertionError("computed network source was rewritten")
    division = _patch_javascript(
        "FbPlayableAd.onCTAClick; x++ / fetch('data.json');",
        "division.js",
    )
    if division != "FbPlayableAd.onCTAClick; x++ / fetch('data.json');":
        raise AssertionError("division source was rewritten")
    for computed_source in (
        "this['fetch']('data.json')",
        "window /* gap */ ['fetch']('data.json')",
        "window[`fetch`]('data.json')",
        "parent['fetch']('data.json')",
        "top[`fetch`]('data.json')",
        'Reflect.get(window,"fetch")("data.json")',
    ):
        if _patch_javascript(computed_source, "computed-gap.js") != computed_source:
            raise AssertionError("computed native fetch variant was rewritten")
    custom_api = "var api={fetch:function(){return 1}};api.fetch();api['fetch']();"
    if _patch_javascript(custom_api, "custom-api.js") != custom_api:
        raise AssertionError("custom fetch API was rewritten")
    location_source = (
        "const go=location;go.href='https://example.test';"
        "const {location:other}=window;other.assign('https://example.test');"
        "self.location += '?x';document.location='https://example.test';"
    )
    patched_location = _patch_javascript(location_source, "location-alias.js")
    if (
        re.search(r"(?<![\w$])location(?![\w$])", _javascript_code_view(patched_location))
        or patched_location.count("__playableLocation") < 4
    ):
        raise AssertionError("location aliases were not redirected to the safe facade")
    dollar_identifier = "var $location={href:'local'};$location.href;var my$location=1;"
    if _patch_javascript(dollar_identifier, "dollar-identifier.js") != dollar_identifier:
        raise AssertionError("location rewrite modified a dollar-prefixed identifier")
    for unsafe in (
        "window.open('https://example.test')",
        "open('https://example.test')",
        "parent.open('https://example.test')",
    ):
        try:
            _patch_javascript(unsafe, "unsafe.js")
        except PlayableCompatibilityError:
            pass
        else:
            raise AssertionError("executable redirect was accepted")
    for unsafe_code in (
        "Function('return 1')();FbPlayableAd.onCTAClick();",
        "window['Function']('return 1')();FbPlayableAd.onCTAClick();",
        "window['open']('https://example.test');FbPlayableAd.onCTAClick();",
        "setTimeout('fetch(\\\"https://example.test\\\")',0);FbPlayableAd.onCTAClick();",
        "new Worker('worker.js');FbPlayableAd.onCTAClick();",
        "import('module.js');FbPlayableAd.onCTAClick();",
        "export * from './remote.js';FbPlayableAd.onCTAClick();",
        "eval?.('1');FbPlayableAd.onCTAClick();",
        "(eval)('1');FbPlayableAd.onCTAClick();",
        "window.eval?.('1');FbPlayableAd.onCTAClick();",
        "Function.call(null,'return 1')();FbPlayableAd.onCTAClick();",
        "setTimeout.call(window,'code',1);FbPlayableAd.onCTAClick();",
        "const run=eval;run('1');FbPlayableAd.onCTAClick();",
        "const Factory=Function;new Factory('return 1')();FbPlayableAd.onCTAClick();",
        "const Factory=Function.prototype.constructor;new Factory('return 1')();FbPlayableAd.onCTAClick();",
        "const Factory=(function(){})['constructor'];new Factory('return 1')();FbPlayableAd.onCTAClick();",
        "const Factory=Reflect.get(function(){},'constructor');new Factory('return 1')();FbPlayableAd.onCTAClick();",
        "const go=location;go.href='https://example.test';FbPlayableAd.onCTAClick();",
        "const {location:go}=window;go.assign('https://example.test');FbPlayableAd.onCTAClick();",
        "window['location'].assign('https://example.test');FbPlayableAd.onCTAClick();",
    ):
        try:
            validate_meta_playable_html("<script>%s</script>" % unsafe_code)
        except PlayableCompatibilityError:
            pass
        else:
            raise AssertionError("computed or direct unsafe JavaScript was accepted")
    for safe_module_code in (
        "var value=import.meta.url;FbPlayableAd.onCTAClick();",
        "api.import(callback);FbPlayableAd.onCTAClick();",
        "export const value=1;FbPlayableAd.onCTAClick();",
    ):
        validate_meta_playable_html("<script type='module'>%s</script>" % safe_module_code)
    validate_meta_playable_html(
        "<script>var bind=Function.prototype.bind;FbPlayableAd.onCTAClick();</script>"
    )
    for legacy_mime in ("text/ecmascript", "application/ecmascript"):
        try:
            validate_meta_playable_html(
                "<script type='%s'>fetch('https://example.test')</script>"
                "<script type='%s'>window.open('https://example.test')</script>"
                "<script>FbPlayableAd.onCTAClick();</script>" % (legacy_mime, legacy_mime)
            )
        except PlayableCompatibilityError:
            pass
        else:
            raise AssertionError("legacy executable ECMAScript MIME was treated as inert")
    for handler_markup in (
        "<body onclick=open(location.href)><script>FbPlayableAd.onCTAClick();</script>",
        "<body onload=window.open(location.href)><script>FbPlayableAd.onCTAClick();</script>",
    ):
        try:
            validate_meta_playable_html(handler_markup)
        except PlayableCompatibilityError:
            pass
        else:
            raise AssertionError("unquoted executable event handler was accepted")
    if _attribute_value(" src=https://example.test/game.js", "src") != "https://example.test/game.js":
        raise AssertionError("unquoted HTML attribute was not parsed")
    suffix_attributes = ' data-src="lazy.png" src="real.png" xlink:href="sprite.svg" href="page.html"'
    if _attribute_value(suffix_attributes, "src") != "real.png":
        raise AssertionError("src matched the suffix of data-src")
    replaced_attributes = _replace_attribute_value(suffix_attributes, "src", "embedded.png")
    if 'data-src="lazy.png"' not in replaced_attributes or 'src="embedded.png"' not in replaced_attributes:
        raise AssertionError("src replacement changed a similarly named attribute")
    if _attribute_value(suffix_attributes, "href") != "page.html":
        raise AssertionError("href matched the suffix of xlink:href")
    if "s.type='module';" not in _defer_game_scripts(
        "<script type='MODULE; charset=UTF-8'>export const value=1;</script>"
    ):
        raise AssertionError("module script type normalization was not preserved")
    source_csp = (
        "<head><meta http-equiv='Content-Security-Policy' content=\"script-src 'none'\">"
        "<meta http-equiv='Content-Security-Policy-Report-Only' content=\"default-src 'none'\"></head>"
    )
    if "Content-Security-Policy" in _strip_source_csp(source_csp):
        raise AssertionError("source CSP meta was not removed before controlled CSP injection")
    for markup in (
        "<area href=https://example.test/install>",
        "<svg><a xlink:href=https://example.test/install><text>Install</text></a></svg>",
        "<img src=https://example.test/pixel.png>",
        "<script src=https://example.test/game.js></script>",
        "<iframe srcdoc='&lt;img src=https://example.test/x&gt;'></iframe>",
        '<img alt=">" src=https://example.test/quoted-gap.png>',
        "<svg><image href=https://example.test/svg.png></image></svg>",
        "<svg><use xlink:href=https://example.test/sprite.svg#icon></use></svg>",
        '<link rel=stylesheet href="data:text/css,@import%20url(https://example.test/x.css)">',
        '<iframe src="data:text/html,%3Cscript%3Efetch(https://example.test)%3C/script%3E"></iframe>',
        '<div title=">" style="background:url(https://example.test/x.png)"></div>',
    ):
        try:
            _reject_external_markup(markup)
        except PlayableCompatibilityError:
            pass
        else:
            raise AssertionError("unsafe unquoted or nested markup was accepted")
    try:
        _rewrite_css('@import "https://example.test/x.css";', {}, "", "fixture.css", set())
    except PlayableCompatibilityError:
        pass
    else:
        raise AssertionError("CSS @import was accepted")
    for unsafe_css in (
        'background:url("https://example.test/x(1).png")',
        'background:image-set("https://example.test/a.png" 1x)',
    ):
        try:
            _rewrite_css(unsafe_css, {}, "", "fixture.css", set())
        except PlayableCompatibilityError:
            pass
        else:
            raise AssertionError("unsupported external CSS image was accepted")
    local_resource = {
        "path": "",
        "mime": "image/png",
        "content": b"fixture",
    }
    consumed = set()
    rewritten_css = _rewrite_css(
        'background:url("icon(1).png")',
        {"icon(1).png": local_resource},
        "",
        "fixture.css",
        consumed,
    )
    if "data:image/png;base64," not in rewritten_css or "icon(1).png" not in consumed:
        raise AssertionError("quoted CSS URL containing parentheses was not inlined")
    return True


def assert_base94_raw_text_contract():
    if len(BASE94_ALPHABET) != 94 or "<" in BASE94_ALPHABET:
        raise AssertionError("Base94 alphabet is not script raw-text safe")
    samples = [
        b"",
        b"\x00",
        b"\x00\xff",
        bytes(range(256)),
        bytes(range(256)) * 17,
    ]
    for sample in samples:
        encoded = _base94_encode(sample)
        if "<" in encoded or _base94_decode(encoded) != sample:
            raise AssertionError("Base94 byte round-trip failed")
    original = (
        "prefix~p00000000~<script>one</ScRiPt>"
        r"<\/script><div data-value='literal'>ok</div>suffix"
    )
    protected, marker = _encode_script_raw_text(original)
    if (
        marker in original
        or "<" in protected
        or protected.replace(marker, "<") != original
        or _encode_script_raw_text(original) != (protected, marker)
    ):
        raise AssertionError("script raw-text marker round-trip failed")
    try:
        _meta_playable_documents(
            '<script type="application/x-playable-html" id="game-source" '
            'data-lt-marker="~marker~"><div></script>'
        )
    except PlayableCompatibilityError:
        pass
    else:
        raise AssertionError("invalid raw-text protection was accepted")
    injected = _inject_head(
        "<!doctype html><html><head></head><body></body></html>",
        r"<script>var value='\6';</script>",
    )
    if r"var value='\6'" not in injected:
        raise AssertionError("head injection treated payload backslashes as group references")
    safe_json = _safe_json_for_script(
        {"value": "</ScRiPt><script>window.PWN=1</script>\u2028"}
    )
    if "<" in safe_json or "\u2028" in safe_json:
        raise AssertionError("script JSON escaping is incomplete")
    malicious_key = "foo</script><script>window.PWN=1</script>/x.bin"
    loader, _ = _loader_shim({
        malicious_key: {
            "content": b"fixture",
            "mime": "application/octet-stream",
            "path": "",
        }
    })
    if "</script><script>window.PWN=1" in loader:
        raise AssertionError("resource metadata escaped its executable script")
    return True


def write_fixture(root):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as handle:
        handle.write(
            """<!doctype html><html><head>
<meta http-equiv="Content-Security-Policy" content="script-src 'none'">
<link rel="icon" href="icon&amp;v.png"><style>.hero{background:url('icon.png')}</style>
</head><body><img src="icon&amp;v.png"><canvas id="game"></canvas><script src=loader.js></script>
<script>fetch('config.json').then(function(r){return r.json();}).then(function(v){window.fixture=v;});</script>
</body></html>"""
        )
    with open(os.path.join(root, "loader.js"), "w", encoding="utf-8") as handle:
        handle.write(
            """function load(){var req=new XMLHttpRequest();req.open('GET','game.wasm',true);req.send();}
function _dmSysOpenURL(e,r){var t='https://store.example/app';window.location=t;return true}
function _emscripten_fixture(){return fetch('game.wasm')}load();"""
        )
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as handle:
        json.dump({"ok": True}, handle)
    with open(os.path.join(root, "game.wasm"), "wb") as handle:
        handle.write(b"\x00asm\x01\x00\x00\x00")
    with open(os.path.join(root, "icon.png"), "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\nfixture")
    with open(os.path.join(root, "icon&v.png"), "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\nentity-fixture")


def decode_inner(document):
    _, inner = _meta_playable_documents(document)
    if not inner:
        raise AssertionError("embedded game source not found")
    return inner


def decode_embedded_scripts(inner):
    metadata_match = re.search(
        r"var __playablePackageMeta=(\{.*?\});",
        inner,
        re.S,
    )
    data_match = re.search(
        r'<script type="application/x-playable-data">(.*?)</script>',
        inner,
        re.S,
    )
    if not metadata_match or not data_match:
        raise AssertionError("embedded compact resource package not found")
    resources = json.loads(metadata_match.group(1))
    compressed = _base94_decode(data_match.group(1))
    payload = lzma.decompress(compressed, format=lzma.FORMAT_ALONE)
    scripts = []
    for key, (_, offset, length) in resources.items():
        if key.lower().endswith((".js", ".mjs")):
            scripts.append(payload[offset:offset + length].decode("utf-8"))
    return "\n".join(scripts)


def run_fixture(
    game_dir,
    entry="index.html",
    output_html="",
    translations=None,
    title="Fixture Playable",
):
    document, compatibility = build_meta_playable_html(
        game_dir,
        entry,
        title,
        1,
        20,
        translations or TRANSLATIONS,
    )
    validate_meta_playable_html(document)
    inner = decode_inner(document)
    embedded_scripts = decode_embedded_scripts(inner)
    runtime_source = inner + "\n" + embedded_scripts
    runtime_code = "\n".join(
        [_javascript_code_view(source) for source in _javascript_sources_from_markup(inner)]
        + [_javascript_code_view(embedded_scripts)]
    )
    assertions = {
        "single_file": compatibility.get("single_file") is True,
        "compact_resource_package": compatibility.get("resource_encoding") == RESOURCE_ENCODING,
        "under_html_size_limit": compatibility.get("html_size", DEFAULT_META_ASSET_LIMIT_BYTES + 1) <= DEFAULT_META_ASSET_LIMIT_BYTES,
        "meta_cta": "FbPlayableAd.onCTAClick" in document,
        "no_native_xhr": (
            "Object.defineProperty(window,'XMLHttpRequest'" in inner
            and compatibility.get("native_network_requests") == 0
        ),
        "no_native_fetch": (
            "Object.defineProperty(window,'fetch'" in inner
            and compatibility.get("native_network_requests") == 0
        ),
        "embedded_loader": "__PlayableXHR" in inner and "__playableRead" in inner,
        "no_unsafe_eval_bootstrap": re.search(
            r"\(\s*0\s*,\s*eval\s*\)\s*\(|\beval\s*\(|\bnew\s+Function\s*\(",
            runtime_code,
        ) is None,
        "csp_safe_script_bootstrap": (
            "document.createElement('script')" in inner
            and "r.parentNode.insertBefore(s,r)" in inner
            and compatibility.get("unsafe_eval_calls") == 0
            and compatibility.get("csp_safe_script_bootstrap") is True
        ),
        "embedded_csp": compatibility.get("embedded_csp") is True,
        "navigation_guard": (
            compatibility.get("navigation_guard") is True
            and "__playableBlockNavigation" in inner
            and "__playableLocationFacade" in inner
        ),
        "safe_timer_wrappers": compatibility.get("safe_timer_wrappers") is True,
        "source_csp_replaced": (
            document.count('http-equiv="Content-Security-Policy"') == 2
            and "script-src 'none'" not in document
        ),
        "opaque_origin_sandbox": compatibility.get("opaque_origin_sandbox") is True,
        "defold_cta_bridge": 'meta-playable-cta' in runtime_source,
        "no_direct_redirect": re.search(
            r"\bwindow\s*\.\s*open\s*\(|\b(?:window|document|top|parent)\s*\.\s*location\s*=|\blocation\s*\.\s*(?:href\s*=|assign\s*\(|replace\s*\()",
            runtime_code,
            re.I | re.S,
        ) is None,
        "no_store_href": re.search(r"href=[\"']https?://", document, re.I) is None,
    }
    with tempfile.TemporaryDirectory() as package_dir:
        index_path = os.path.join(package_dir, "index.html")
        zip_path = os.path.join(package_dir, "playable.zip")
        with open(index_path, "wb") as handle:
            handle.write(document.encode("utf-8"))
        with zipfile.ZipFile(
            zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=8
        ) as archive:
            archive.write(index_path, "index.html")
        with zipfile.ZipFile(zip_path) as archive:
            if archive.namelist() != ["index.html"]:
                raise AssertionError("Meta package must contain only top-level index.html")
        zip_size = os.path.getsize(zip_path)
    assertions["under_zip_size_limit"] = zip_size <= DEFAULT_META_ASSET_LIMIT_BYTES
    failed = [name for name, passed in assertions.items() if not passed]
    if failed:
        raise AssertionError("compatibility assertions failed: %s" % ", ".join(failed))

    if output_html:
        os.makedirs(os.path.dirname(os.path.abspath(output_html)), exist_ok=True)
        with open(output_html, "wb") as handle:
            handle.write(document.encode("utf-8"))
    return {
        "ok": True,
        "html_size": len(document.encode("utf-8")),
        "zip_size": zip_size,
        "compatibility": compatibility,
        "assertions": assertions,
        "output_html": os.path.abspath(output_html) if output_html else "",
    }


def safe_extract(zip_path, target):
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            destination = os.path.abspath(os.path.join(target, info.filename))
            if not destination.startswith(os.path.abspath(target) + os.sep):
                raise ValueError("unsafe zip entry: %s" % info.filename)
        archive.extractall(target)


def find_entry(root):
    candidates = []
    for current_root, _, filenames in os.walk(root):
        for filename in filenames:
            if filename.lower() == "index.html":
                candidates.append(os.path.relpath(os.path.join(current_root, filename), root).replace(os.sep, "/"))
    if not candidates:
        raise ValueError("fixture zip has no index.html")
    candidates.sort(key=lambda item: (item.count("/"), len(item), item))
    return candidates[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-zip", default="")
    parser.add_argument("--output-html", default="")
    parser.add_argument("--title", default="Boxrob")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as root:
        if args.source_zip:
            safe_extract(os.path.abspath(args.source_zip), root)
            entry = find_entry(root)
            entry_dir = os.path.dirname(os.path.join(root, entry))
            entry_name = os.path.basename(entry)
            app_path, nodes = app_ast_nodes("PLAYABLE_PREVIEW_TRANSLATIONS")
            namespace = {}
            exec(
                compile(ast.Module(body=nodes, type_ignores=[]), app_path, "exec"),
                namespace,
            )
            result = run_fixture(
                entry_dir,
                entry_name,
                args.output_html,
                translations=namespace["PLAYABLE_PREVIEW_TRANSLATIONS"],
                title=args.title,
            )
        else:
            write_fixture(root)
            result = run_fixture(root, "index.html", args.output_html)
            try:
                build_meta_playable_html(
                    root,
                    "index.html",
                    "Fixture Playable",
                    1,
                    20,
                    TRANSLATIONS,
                    max_asset_bytes=1024,
                )
            except PlayableCompatibilityError as error:
                if "exceeds safety limit" not in str(error):
                    raise
                result["assertions"]["oversize_guard"] = True
            else:
                raise AssertionError("HTML size guard did not reject an oversized asset")
            result["assertions"]["multipart_trial_seconds"] = assert_multipart_trial_seconds_contract()
            result["assertions"]["translation_isolation"] = assert_translation_isolation_contract()
            result["assertions"]["auth_modes"] = assert_auth_mode_contract()
            result["assertions"]["json_request_shape"] = assert_json_request_contract()
            result["assertions"]["failure_cleanup"] = assert_cleanup_wrapper_contract()
            result["assertions"]["zip_extraction_limits"] = assert_zip_extraction_limits_contract()
            result["assertions"]["lexical_validation"] = assert_lexical_validation_contract()
            result["assertions"]["base94_raw_text"] = assert_base94_raw_text_contract()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
