from email.message import Message
import unittest

from features.tt_drama_resources import (
    ResourceContentMismatchError,
    ResourceParseError,
    ResourceSourceError,
    W2AHTMLClient,
    parse_w2a_resource_html,
)


CONTENT_ID = "Ag0rfr5F0F"
COVER_URL = (
    "https://cdn.usrgrow.com/storage/icons/"
    "app_657_16f5e3142eb3121ea55506cbdeb5ec9e_banner.jpg"
)


def resource_html(
    content_id=CONTENT_ID,
    *,
    title="Her Beast",
    description="A story &amp; its description.",
    primary_cover=COVER_URL,
    fallback_cover=COVER_URL,
    include_description=True,
):
    description_node = (
        '<div class="desc ">%s</div>' % description
        if include_description
        else ""
    )
    return """
    <!doctype html>
    <html>
      <head>
        <script>
          let link=[{"sub3":"private-value","dl":
          "dramawave://dramawave.app?redirect=%%2Fdetail%%3Fid%%3D%s",
          "episode_1":"https://video.example/private.mp4"}]
        </script>
      </head>
      <body>
        <img id="image" class="bg-img" src="%s" alt="bg">
        <div class="content-box">
          <div class="cover-container">
            <img id="topReading" class="cover img-loading"
                 data-src="%s" alt="material">
          </div>
          <div class="info">
            <h1 class="title"><span>%s</span></h1>
            %s
          </div>
        </div>
      </body>
    </html>
    """ % (
        content_id,
        fallback_cover,
        primary_cover,
        title,
        description_node,
    )


class _FakeResponse:
    def __init__(
        self,
        body,
        *,
        url,
        status=200,
        content_type="text/html; charset=utf-8",
    ):
        self.body = body
        self.url = url
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.closed = False

    def read(self, limit):
        return self.body[:limit]

    def geturl(self):
        return self.url

    def close(self):
        self.closed = True


class _FakeOpener:
    def __init__(self, factory):
        self.factory = factory
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.factory(request)


class W2AResourceParserTests(unittest.TestCase):
    def test_extracts_only_static_public_fields(self):
        item = parse_w2a_resource_html(
            resource_html(),
            CONTENT_ID,
            landing_id=2049,
        )
        self.assertEqual(item["content_id"], CONTENT_ID)
        self.assertEqual(item["resolved_content_id"], CONTENT_ID)
        self.assertEqual(item["title"], "Her Beast")
        self.assertEqual(item["description"], "A story & its description.")
        self.assertEqual(item["cover_url"], COVER_URL)
        self.assertEqual(len(item["content_hash"]), 64)
        serialized = repr(item)
        self.assertNotIn("episode_1", serialized)
        self.assertNotIn("private-value", serialized)
        self.assertNotIn("let link", serialized)
        self.assertNotIn("source_url", item)

    def test_description_may_be_empty_but_field_must_exist(self):
        item = parse_w2a_resource_html(
            resource_html(description=""),
            CONTENT_ID,
        )
        self.assertEqual(item["description"], "")
        with self.assertRaisesRegex(ResourceParseError, "description field"):
            parse_w2a_resource_html(
                resource_html(include_description=False),
                CONTENT_ID,
            )

    def test_uses_allowlisted_fallback_cover(self):
        item = parse_w2a_resource_html(
            resource_html(
                primary_cover="https://evil.example/cover.jpg",
                fallback_cover=COVER_URL,
            ),
            CONTENT_ID,
        )
        self.assertEqual(item["cover_url"], COVER_URL)

    def test_rejects_unsafe_cover_and_missing_title(self):
        with self.assertRaisesRegex(ResourceParseError, "cover URL"):
            parse_w2a_resource_html(
                resource_html(
                    primary_cover="http://cdn.usrgrow.com/cover.jpg",
                    fallback_cover="https://evil.example/cover.jpg",
                ),
                CONTENT_ID,
            )
        with self.assertRaisesRegex(ResourceParseError, "title"):
            parse_w2a_resource_html(
                resource_html(title="   "),
                CONTENT_ID,
            )

    def test_rejects_resolved_content_id_mismatch(self):
        with self.assertRaises(ResourceContentMismatchError) as raised:
            parse_w2a_resource_html(
                resource_html(content_id="Different1"),
                CONTENT_ID,
            )
        self.assertEqual(raised.exception.requested_content_id, CONTENT_ID)
        self.assertEqual(raised.exception.resolved_content_id, "Different1")

    def test_requires_link_payload_in_static_source(self):
        html = resource_html().replace("let link=", "window.other=")
        with self.assertRaisesRegex(ResourceParseError, "link payload"):
            parse_w2a_resource_html(html, CONTENT_ID)


class W2AHTMLClientTests(unittest.TestCase):
    def test_gets_only_fixed_source_document_and_parses_it(self):
        expected_url = (
            "https://www.dramawavew2a.com/ads/0/2049/view"
            "?af_dp=Ag0rfr5F0F"
        )
        response_holder = {}

        def factory(request):
            response = _FakeResponse(
                resource_html().encode("utf-8"),
                url=request.full_url,
            )
            response_holder["value"] = response
            return response

        opener = _FakeOpener(factory)
        client = W2AHTMLClient(opener=opener)
        item = client.fetch(CONTENT_ID)
        self.assertEqual(item["title"], "Her Beast")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, expected_url)
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertEqual(timeout, 5.0)
        self.assertTrue(response_holder["value"].closed)

    def test_rejects_redirect_non_html_and_oversized_body(self):
        source_url = (
            "https://www.dramawavew2a.com/ads/0/2049/view"
            "?af_dp=Ag0rfr5F0F"
        )
        redirected = W2AHTMLClient(
            opener=_FakeOpener(
                lambda _request: _FakeResponse(
                    resource_html().encode(),
                    url="https://evil.example/",
                )
            )
        )
        with self.assertRaisesRegex(ResourceSourceError, "redirected"):
            redirected.fetch(CONTENT_ID)

        non_html = W2AHTMLClient(
            opener=_FakeOpener(
                lambda _request: _FakeResponse(
                    b"{}",
                    url=source_url,
                    content_type="application/json",
                )
            )
        )
        with self.assertRaisesRegex(ResourceSourceError, "return HTML"):
            non_html.fetch(CONTENT_ID)

        oversized = W2AHTMLClient(
            max_html_bytes=1024,
            opener=_FakeOpener(
                lambda _request: _FakeResponse(
                    b"x" * 1025,
                    url=source_url,
                )
            ),
        )
        with self.assertRaisesRegex(ResourceSourceError, "byte limit"):
            oversized.fetch(CONTENT_ID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
