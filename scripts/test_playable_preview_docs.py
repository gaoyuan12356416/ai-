#!/usr/bin/env python3
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import publish_playable_preview_docs as publisher
import render_playable_preview_docs as renderer


SOURCE = ROOT / "doc" / "deployment" / "playable-preview-api.md"
HTML_SOURCE = ROOT / "doc" / "deployment" / "playable-preview-api.html"


class StructureAudit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.counts = {}
        self.forbidden_tags = []
        self.event_attributes = []
        self.csp = ""
        self.source_hash = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag in {"script", "iframe", "object", "embed", "form", "base"}:
            self.forbidden_tags.append(tag)
        attr_map = {str(key).lower(): str(value or "") for key, value in attrs}
        for key in attr_map:
            if key.startswith("on"):
                self.event_attributes.append("%s.%s" % (tag, key))
        if tag == "meta" and attr_map.get("http-equiv", "").lower() == "content-security-policy":
            self.csp = attr_map.get("content", "")
        if tag == "meta" and attr_map.get("name", "").lower() == "playable-doc-source-sha256":
            self.source_hash = attr_map.get("content", "")


class FakeCosError(Exception):
    def __init__(self, status_code, error_code=""):
        Exception.__init__(self, "%s %s" % (status_code, error_code))
        self.status_code = status_code
        self.error_code = error_code

    def get_status_code(self):
        return self.status_code

    def get_error_code(self):
        return self.error_code


class FakeBody:
    def __init__(self, payload):
        self.payload = payload

    def get_raw_stream(self):
        return io.BytesIO(self.payload)

    def close(self):
        return None


class FakeCosClient:
    def __init__(self, initial=None, fail_once_key=""):
        self.objects = dict(initial or {})
        self.fail_once_key = fail_once_key
        self.failed = False
        self.put_log = []

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        self.put_log.append(key)
        if key == self.fail_once_key and not self.failed:
            self.failed = True
            raise RuntimeError("injected COS failure for %s" % key)
        payload = kwargs["Body"]
        if hasattr(payload, "read"):
            payload = payload.read()
        payload = bytes(payload)
        etag = hashlib.md5(payload).hexdigest()
        self.objects[key] = {
            "payload": payload,
            "content_type": kwargs["ContentType"],
            "cache_control": kwargs["CacheControl"],
            "etag": etag,
        }
        return {"ETag": '"%s"' % etag}

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key not in self.objects:
            raise FakeCosError(404, "NoSuchKey")
        item = self.objects[key]
        return {
            "Body": FakeBody(item["payload"]),
            "Content-Type": item["content_type"],
            "Cache-Control": item["cache_control"],
            "ETag": '"%s"' % item["etag"],
        }

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"], None)
        return {}


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def test_cos_transaction(markdown_payload, html_payload):
    items = [
        {
            "key": publisher.DOC_OBJECT_KEY,
            "payload": markdown_payload,
            "content_type": publisher.MARKDOWN_CONTENT_TYPE,
        },
        {
            "key": publisher.HTML_DOC_OBJECT_KEY,
            "payload": html_payload,
            "content_type": publisher.HTML_CONTENT_TYPE,
        },
    ]
    old_markdown = b"# previous public document\n"
    initial = {
        publisher.DOC_OBJECT_KEY: {
            "payload": old_markdown,
            "content_type": publisher.MARKDOWN_CONTENT_TYPE,
            "cache_control": publisher.CACHE_CONTROL,
            "etag": hashlib.md5(old_markdown).hexdigest(),
        }
    }
    failing = FakeCosClient(initial=initial, fail_once_key=publisher.HTML_DOC_OBJECT_KEY)
    try:
        publisher.publish_cos_bundle(failing, "test-bucket", items)
        raise AssertionError("injected final HTML failure must abort publication")
    except RuntimeError as exc:
        assert_true("injected COS failure" in str(exc), "the original publication failure must propagate")
    assert_true(
        failing.objects[publisher.DOC_OBJECT_KEY]["payload"] == old_markdown,
        "Markdown must roll back when the final HTML commit fails",
    )
    assert_true(
        publisher.HTML_DOC_OBJECT_KEY not in failing.objects,
        "previously missing HTML must remain absent after rollback",
    )
    assert_true(
        not any(".playable-preview-stage/" in key for key in failing.objects),
        "staged COS objects must always be removed",
    )

    successful = FakeCosClient(initial=initial)
    result = publisher.publish_cos_bundle(successful, "test-bucket", items)
    assert_true(
        successful.objects[publisher.DOC_OBJECT_KEY]["payload"] == markdown_payload,
        "successful publication must replace Markdown",
    )
    assert_true(
        successful.objects[publisher.HTML_DOC_OBJECT_KEY]["payload"] == html_payload,
        "successful publication must create HTML",
    )
    fixed_puts = [
        key
        for key in successful.put_log
        if key in {publisher.DOC_OBJECT_KEY, publisher.HTML_DOC_OBJECT_KEY}
    ]
    assert_true(
        fixed_puts[-2:] == [publisher.DOC_OBJECT_KEY, publisher.HTML_DOC_OBJECT_KEY],
        "HTML must be the final fixed-key commit object",
    )
    assert_true(
        result["verification"][publisher.HTML_DOC_OBJECT_KEY]["sha256"]
        == hashlib.sha256(html_payload).hexdigest(),
        "successful COS readback must match the HTML SHA-256",
    )


def main():
    markdown_payload = SOURCE.read_bytes()
    markdown_text = markdown_payload.decode("utf-8")
    normalized_text = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_sha256 = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    html_payload = HTML_SOURCE.read_bytes()
    html_text = html_payload.decode("utf-8")
    test_cos_transaction(markdown_payload, html_payload)

    rendered, render_details = renderer.render_file(str(SOURCE), str(HTML_SOURCE))
    assert_true(rendered == html_payload, "tracked HTML must exactly match the Markdown renderer output")

    with tempfile.TemporaryDirectory(prefix="playable-docs-") as temp_dir:
        crlf_source = Path(temp_dir) / "playable-preview-api.md"
        crlf_source.write_bytes(normalized_text.replace("\n", "\r\n").encode("utf-8"))
        crlf_rendered, _ = renderer.render_file(str(crlf_source), str(Path(temp_dir) / "output.html"))
        assert_true(crlf_rendered == html_payload, "HTML rendering must be independent of source line endings")

    markdown_details = publisher.document_details(str(SOURCE))[1]
    html_details = publisher.html_document_details(str(HTML_SOURCE), markdown_details["normalized_sha256"])[1]
    assert_true(markdown_details["normalized_sha256"] == normalized_sha256, "normalized source hash mismatch")

    audit = StructureAudit()
    audit.feed(html_text)
    audit.close()
    assert_true(html_text.lower().count("<!doctype html>") == 1, "HTML must contain one doctype")
    assert_true('<html lang="zh-CN">' in html_text, "HTML language must be zh-CN")
    assert_true('<meta charset="utf-8">' in html_text, "HTML must declare UTF-8")
    assert_true('name="viewport"' in html_text, "HTML must include a viewport meta tag")
    assert_true(audit.counts.get("h1") == 1, "HTML must contain one page title")
    assert_true(audit.counts.get("h2") == 11, "all 11 level-two sections must render")
    assert_true(audit.counts.get("h3") == 2, "both level-three sections must render")
    assert_true(audit.counts.get("pre") == 14, "all 14 fenced code blocks must render")
    assert_true(audit.counts.get("table") == 5, "all 5 Markdown tables must render")
    assert_true(not audit.forbidden_tags, "rendered document must not contain active embedded tags")
    assert_true(not audit.event_attributes, "rendered document must not contain event-handler attributes")
    assert_true("default-src 'none'" in audit.csp, "HTML document CSP must default-deny resources")
    assert_true("style-src 'unsafe-inline'" in audit.csp, "HTML document CSP must allow only its inline style")
    assert_true(audit.source_hash == normalized_sha256, "HTML source hash meta must match normalized Markdown")
    assert_true("&lt;script&gt;" in html_text, "script examples must be escaped as text")
    assert_true("javascript:" not in html_text.lower(), "HTML document must not contain javascript URLs")
    assert_true("<script" not in html_text.lower(), "HTML document must not contain scripts")

    for content in (markdown_text, html_text):
        assert_true(renderer.LEGACY_ENDPOINT not in content, "legacy endpoint must be absent")
        api_urls = sorted(set(re.findall(r"https://ai\.yingliangads\.com/api/[^\s'\"`<]+", content)))
        assert_true(api_urls == [renderer.CANONICAL_ENDPOINT], "only the canonical API URL may appear")

    result = {
        "ok": True,
        "source": str(SOURCE),
        "source_size": len(markdown_payload),
        "normalized_source_sha256": normalized_sha256,
        "html": str(HTML_SOURCE),
        "html_size": len(html_payload),
        "html_sha256": hashlib.sha256(html_payload).hexdigest(),
        "publisher_html_sha256": html_details["html_sha256"],
        "counts": audit.counts,
        "canonical_api_url": renderer.CANONICAL_ENDPOINT,
        "legacy_endpoint_count": html_text.count(renderer.LEGACY_ENDPOINT),
        "cos_transaction_rollback": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
