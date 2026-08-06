#!/usr/bin/env python3
"""Offline integration tests for the isolated TT auto code-route broker."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_auto_posts.code_broker import (  # noqa: E402
    AUTO_QUEUE_ID_NAMESPACE,
    AutoCodeBrokerError,
    AutoCodeBrokerHTTPServer,
    LegacyCodeRouteStore,
    synthetic_queue_id,
)
from features.tt_auto_posts.code_broker_client import AutoCodeBrokerClient  # noqa: E402
from features.tt_auto_posts.links import build_auto_w2a_url  # noqa: E402
from features.tt_posts.code_routes import TTCodeRouteResolver  # noqa: E402
from features.tt_posts.core import TTPostStore  # noqa: E402


class TTAutoCodeBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "tt-post.sqlite3"
        TTPostStore(self.db_path)
        self.store = LegacyCodeRouteStore(self.db_path)
        self.store.validate_schema()

    @staticmethod
    def route(task_id=17, content_id="CONTENT00017"):
        return build_auto_w2a_url(
            link_id=task_id,
            username="account640",
            timestamp=1_754_300_000,
            language="en",
            drama_name="Drama Seventeen",
            tag="hook",
            page_name="Account 640",
            page_id="640",
            material_name="clip-17.mp4",
            material_id="M17",
            content_id=content_id,
        )

    def test_freeze_is_globally_routable_idempotent_and_does_not_create_queue(self):
        first = self.store.freeze(
            17,
            content_id="CONTENT00017",
            long_url=self.route(),
            created_at="2026-08-06T08:00:00Z",
        )
        replay = self.store.freeze(
            17,
            content_id="CONTENT00017",
            long_url=self.route(),
            created_at="2026-08-06T08:00:00Z",
        )
        self.assertEqual(first, replay)
        self.assertRegex(first["code"], r"^[A-Z0-9]{4}$")
        self.assertEqual(synthetic_queue_id(17), AUTO_QUEUE_ID_NAMESPACE + 17)

        resolver = TTCodeRouteResolver(self.db_path)
        resolved = resolver.resolve(first["code"], "Search")
        self.assertEqual(resolved["item"]["content_id"], "CONTENT00017")
        self.assertEqual(resolved["item"]["route_mode"], "code_exact")

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tt_post_queue").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT queue_id FROM tt_post_code_route WHERE code=?",
                    (first["code"],),
                ).fetchone()[0],
                AUTO_QUEUE_ID_NAMESPACE + 17,
            )

    def test_idempotent_freeze_supports_non_ascii_attribution_fields(self):
        long_url = build_auto_w2a_url(
            link_id=21,
            username="account640",
            timestamp=1_754_300_000,
            language="en",
            drama_name="Never Cross the Dragon Heiress 龙女",
            tag="Fantasy 奇幻",
            page_name="Dramawave 热门短剧",
            page_id="640",
            material_name="精剪素材-21.mp4",
            material_id="M21",
            content_id="CONTENT00021",
        )
        first = self.store.freeze(
            21,
            content_id="CONTENT00021",
            long_url=long_url,
            created_at="2026-08-06T08:04:00Z",
        )
        replay = self.store.freeze(
            21,
            content_id="CONTENT00021",
            long_url=long_url,
            created_at="2026-08-06T08:04:00Z",
        )
        self.assertEqual(first, replay)

    def test_published_state_enables_latest_drama_clone_and_cannot_downgrade(self):
        route = self.store.freeze(
            18,
            content_id="CONTENT00018",
            long_url=self.route(18, "CONTENT00018"),
            created_at="2026-08-06T08:01:00Z",
        )
        published = self.store.set_state(
            18,
            state="published",
            updated_at="2026-08-06T08:10:00Z",
        )
        self.assertEqual(published["code"], route["code"])
        self.assertEqual(published["published_at"], "2026-08-06T08:10:00Z")
        resolved = TTCodeRouteResolver(self.db_path).resolve(
            "CONTENT00018", "Featured"
        )
        self.assertEqual(resolved["item"]["route_mode"], "published_clone")
        self.assertEqual(resolved["item"]["code"], route["code"])
        with self.assertRaises(AutoCodeBrokerError) as caught:
            self.store.set_state(
                18,
                state="failed",
                updated_at="2026-08-06T08:11:00Z",
            )
        self.assertEqual(caught.exception.code, "tt_auto_code_state_conflict")

    def test_same_task_cannot_change_frozen_route(self):
        self.store.freeze(
            19,
            content_id="CONTENT00019",
            long_url=self.route(19, "CONTENT00019"),
            created_at="2026-08-06T08:02:00Z",
        )
        with self.assertRaises(AutoCodeBrokerError) as caught:
            self.store.freeze(
                19,
                content_id="CONTENT99999",
                long_url=self.route(19, "CONTENT99999"),
                created_at="2026-08-06T08:02:00Z",
            )
        self.assertEqual(caught.exception.code, "tt_auto_code_route_conflict")

    def test_loopback_client_freezes_through_authenticated_broker(self):
        token = "a" * 64
        server = AutoCodeBrokerHTTPServer(
            ("127.0.0.1", 18832),
            self.store,
            token,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop_server)
        client = AutoCodeBrokerClient("http://127.0.0.1:18832", token)
        code = client.freeze_route(
            20,
            content_id="CONTENT00020",
            long_url=self.route(20, "CONTENT00020"),
            created_at="2026-08-06T08:03:00Z",
        )
        self.assertRegex(code, r"^[A-Z0-9]{4}$")
        state = client.set_state(
            20,
            state="published",
            updated_at="2026-08-06T08:12:00Z",
        )
        self.assertEqual(state["code"], code)
        self.assertEqual(state["state"], "published")


if __name__ == "__main__":
    unittest.main()
