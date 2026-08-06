#!/usr/bin/env python3
"""Offline contract tests for the independent TT auto-post short links."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_auto_posts.links import (  # noqa: E402
    AutoPostLinkError,
    build_auto_short_url,
    build_auto_w2a_url,
    render_auto_caption,
    validate_auto_short_url,
    write_auto_short_redirect,
)


class TTAutoPostLinksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "s2l"

    def test_short_url_uses_independent_namespace_and_strict_identity(self):
        url = build_auto_short_url(123)
        self.assertEqual(url, "https://gy.g2flow.com/s2l/tt-auto/123.html")
        self.assertEqual(validate_auto_short_url(url), url)
        for invalid in (
            "https://gy.g2flow.com/s2l/123.html",
            "http://gy.g2flow.com/s2l/tt-auto/123.html",
            "https://gy.g2flow.com/s2l/tt-auto/123.html?q=1",
            "https://evil.example/s2l/tt-auto/123.html",
            "https://gy.g2flow.com/s2l/tt-auto/0.html",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(AutoPostLinkError):
                validate_auto_short_url(invalid)

    def test_redirect_is_atomic_idempotent_and_immutable(self):
        first_target = build_auto_w2a_url(
            link_id=41,
            username="user41",
            timestamp=1_754_300_000,
            language="en",
            drama_name="Drama One",
            tag="hook",
            page_name="Account 41",
            page_id="640",
            material_name="clip-1.mp4",
            material_id="M1",
            content_id="C1",
        )
        path = write_auto_short_redirect(self.root, 41, first_target)
        self.assertEqual(path, self.root / "tt-auto" / "41.html")
        contents = path.read_text(encoding="utf-8")
        self.assertIn(first_target.replace("&", "&amp;"), contents)
        self.assertEqual(write_auto_short_redirect(self.root, 41, first_target), path)

        with self.assertRaises(AutoPostLinkError) as caught:
            write_auto_short_redirect(
                self.root,
                41,
                build_auto_w2a_url(
                    link_id=41,
                    username="user41",
                    timestamp=1_754_300_000,
                    language="en",
                    drama_name="Drama Two",
                    tag="hook",
                    page_name="Account 41",
                    page_id="640",
                    material_name="clip-2.mp4",
                    material_id="M2",
                    content_id="C2",
                ),
            )
        self.assertEqual(caught.exception.code, "tt_auto_short_link_conflict")
        self.assertEqual(path.read_text(encoding="utf-8"), contents)

    def test_w2a_url_is_tt_scoped_and_keeps_task_identity(self):
        url = build_auto_w2a_url(
            link_id=88,
            username="user88",
            timestamp=1_754_300_000,
            language="en",
            drama_name="Drama Eight",
            tag="hook",
            page_name="Account Eight",
            page_id="640",
            material_name="clip.mp4",
            material_id="M88",
            content_id="C88",
        )
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(query["af_c_id"], ["88"])
        self.assertEqual(query["af_ad_id"], ["M88"])
        self.assertEqual(query["af_dp"], ["C88"])
        self.assertEqual(query["af_channel"], ["TT"])
        self.assertIn("*88", query["c"][0])

    def test_caption_renders_supported_macros_including_code(self):
        short_url = build_auto_short_url(9)
        rendered = render_auto_caption(
            "Drama {{content_id}}\n{desc}\n{url}",
            "C9",
            short_url=short_url,
            description="A short description",
        )
        self.assertIn("C9", rendered)
        self.assertIn("A short description", rendered)
        self.assertIn(short_url, rendered)
        code_rendered = render_auto_caption(
            "Code: {code}\n{url}",
            "C9",
            short_url=short_url,
            description="desc",
            code="AB12",
        )
        self.assertEqual(code_rendered, "Code: AB12\n%s" % short_url)
        with self.assertRaises(AutoPostLinkError) as caught:
            render_auto_caption(
                "{code} {url}",
                "C9",
                short_url=short_url,
                description="desc",
            )
        self.assertEqual(caught.exception.code, "caption_code_required")

    def test_redirect_rejects_relative_root_and_non_w2a_target(self):
        valid = build_auto_w2a_url(
            link_id=1,
            username="user1",
            timestamp=1_754_300_000,
            language="en",
            drama_name="Drama One",
            tag="hook",
            page_name="Account One",
            page_id="640",
            material_name="clip.mp4",
            material_id="M1",
            content_id="C1",
        )
        with self.assertRaises(AutoPostLinkError):
            write_auto_short_redirect(
                "relative/path",
                1,
                valid,
            )
        with self.assertRaises(Exception):
            write_auto_short_redirect(
                self.root,
                1,
                "https://evil.example/steal",
            )


if __name__ == "__main__":
    unittest.main()
