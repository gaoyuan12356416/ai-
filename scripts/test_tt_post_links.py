#!/usr/bin/env python3
"""Offline contracts for TikTok organic-post W2A short links."""

from __future__ import annotations

import html
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from features.tt_posts.links import (  # noqa: E402
    TTPostLinkError,
    TT_SHORT_LINK_NAMESPACE,
    TT_W2A_BASE_URL,
    build_short_url,
    build_w2a_url,
    short_link_id,
    validate_short_url,
    validate_w2a_url,
    write_short_redirect,
)


def tracking_params(**overrides):
    values = {
        "username": "creator_101",
        "timestamp": 1784736000,
        "material_language": "en",
        "drama_name": "The Contract Bride",
        "tag": "romance",
        "link_id": TT_SHORT_LINK_NAMESPACE + 9,
        "page_name": "DramaWave popular reels",
        "page_id": "101",
        "material_name": "The Prodigy Sage Is Back",
        "material_id": "9001",
        "queue_id": 27,
        "content_id": "ABCD1234",
    }
    values.update(overrides)
    return values


class TTPostLinkTests(unittest.TestCase):
    def test_reserved_id_and_public_url_are_stable(self):
        link_id = short_link_id(9)
        self.assertEqual(TT_SHORT_LINK_NAMESPACE + 9, link_id)
        self.assertEqual(
            "https://gy.g2flow.com/s2l/8000000000000000009.html",
            build_short_url(link_id),
        )
        self.assertEqual(
            build_short_url(link_id),
            validate_short_url(build_short_url(link_id)),
        )
        with self.assertRaises(TTPostLinkError):
            build_short_url(9)
        with self.assertRaises(TTPostLinkError):
            validate_short_url(
                "https://ai.yingliangads.com/s2l/"
                "8000000000000000009.html"
            )

    def test_w2a_url_matches_x_field_order_and_attribution_contract(self):
        target = build_w2a_url(tracking_params())
        self.assertTrue(target.startswith(TT_W2A_BASE_URL + "?"))
        pairs = urllib.parse.parse_qsl(
            urllib.parse.urlsplit(target).query,
            keep_blank_values=True,
        )
        self.assertEqual(
            [key for key, _value in pairs],
            [
                "c",
                "af_adset",
                "af_adset_id",
                "af_ad",
                "af_ad_id",
                "af_channel",
                "af_c_id",
                "af_dp",
            ],
        )
        values = dict(pairs)
        self.assertEqual(
            values["c"],
            (
                "yingliang_post_CLV_VL_creator_101*"
                "1784736000noneen*The Contract Bride*romance*"
                "8000000000000000009"
            ),
        )
        self.assertEqual(values["af_adset"], "DramaWave popular reels")
        self.assertEqual(values["af_adset_id"], "101")
        self.assertEqual(
            values["af_ad"],
            "The Prodigy Sage Is Back_contentid[ABCD1234]",
        )
        self.assertEqual(values["af_ad_id"], "9001")
        self.assertEqual(values["af_channel"], "AIpost")
        self.assertEqual(values["af_c_id"], "27")
        self.assertEqual(values["af_dp"], "ABCD1234")
        self.assertEqual(validate_w2a_url(target), target)

    def test_w2a_url_rejects_missing_unknown_and_reserved_delimiters(self):
        missing = tracking_params()
        missing.pop("tag")
        with self.assertRaises(TTPostLinkError):
            build_w2a_url(missing)
        with self.assertRaises(TTPostLinkError):
            build_w2a_url({**tracking_params(), "unknown": "value"})
        with self.assertRaises(TTPostLinkError):
            build_w2a_url(tracking_params(drama_name="bad*delimiter"))

    def test_wrapper_is_atomic_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "s2l"
            link_id = TT_SHORT_LINK_NAMESPACE + 9
            first_target = build_w2a_url(tracking_params())
            destination = write_short_redirect(root, link_id, first_target)
            self.assertEqual(
                destination,
                root / "8000000000000000009.html",
            )
            payload = destination.read_text(encoding="utf-8")
            self.assertIn(
                html.escape(first_target, quote=True),
                payload,
            )
            self.assertEqual(
                destination,
                write_short_redirect(root, link_id, first_target),
            )
            second_target = build_w2a_url(
                tracking_params(queue_id=28)
            )
            with self.assertRaises(TTPostLinkError) as caught:
                write_short_redirect(root, link_id, second_target)
            self.assertEqual("tt_short_link_conflict", caught.exception.code)
            self.assertEqual(
                payload,
                destination.read_text(encoding="utf-8"),
            )

    def test_concurrent_different_targets_cannot_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "s2l"
            link_id = TT_SHORT_LINK_NAMESPACE + 9
            targets = [
                build_w2a_url(tracking_params(queue_id=27)),
                build_w2a_url(tracking_params(queue_id=28)),
            ]
            barrier = threading.Barrier(2)
            outcomes = []
            outcome_lock = threading.Lock()

            def publish(target):
                barrier.wait()
                try:
                    write_short_redirect(root, link_id, target)
                    result = ("created", target)
                except TTPostLinkError as exc:
                    result = (exc.code, target)
                with outcome_lock:
                    outcomes.append(result)

            workers = [
                threading.Thread(target=publish, args=(target,))
                for target in targets
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive())

            self.assertEqual(
                sorted(item[0] for item in outcomes),
                ["created", "tt_short_link_conflict"],
            )
            winner = next(
                target
                for status, target in outcomes
                if status == "created"
            )
            destination = root / "8000000000000000009.html"
            payload = destination.read_text(encoding="utf-8")
            self.assertIn(html.escape(winner, quote=True), payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
