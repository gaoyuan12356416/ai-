#!/usr/bin/env python3
"""Secret-safe HTTP client contracts for the X auto execution bridge."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_auto_posts.x_sidecar import (  # noqa: E402
    XPostAutoBridgeClient,
    XPostBridgeError,
)


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.content = json.dumps(payload or {}).encode("utf-8")


class XPostAutoBridgeClientTests(unittest.TestCase):
    def client(self, session=None):
        return XPostAutoBridgeClient(
            "http://127.0.0.1:8810",
            "b" * 48,
            timeout=30,
            session=session,
        )

    def test_only_exact_loopback_and_non_placeholder_token_are_allowed(self):
        for url, token in (
            ("http://localhost:8810", "b" * 48),
            ("http://127.0.0.1:18833", "b" * 48),
            (
                "http://127.0.0.1:8810",
                "replace-with-unique-random-token-at-least-32-characters",
            ),
        ):
            with self.subTest(url=url), self.assertRaises(XPostBridgeError):
                XPostAutoBridgeClient(url, token)

    def test_long_x_publish_timeout_matches_existing_runner_budget(self):
        client = XPostAutoBridgeClient(
            "http://127.0.0.1:8810",
            "b" * 48,
            timeout=120,
            publish_timeout=9000,
        )
        self.assertEqual(client.timeout, 120)
        self.assertEqual(client.publish_timeout, 9000)
        with self.assertRaises(XPostBridgeError):
            XPostAutoBridgeClient(
                "http://127.0.0.1:8810",
                "b" * 48,
                publish_timeout=10201,
            )

    def test_only_publish_route_uses_the_long_transport_budget(self):
        session = mock.Mock()
        session.post.return_value = FakeResponse(payload={"item": {"status": "queued"}})
        client = XPostAutoBridgeClient(
            "http://127.0.0.1:8810",
            "b" * 48,
            timeout=120,
            publish_timeout=9000,
            session=session,
        )
        client.query_run(7)
        self.assertEqual(session.post.call_args.kwargs["timeout"], 120)
        client.publish_queue(8)
        self.assertEqual(session.post.call_args.kwargs["timeout"], 9000)

    def test_create_run_uses_only_auto_template_route_and_header_token(self):
        session = mock.Mock()
        session.post.return_value = FakeResponse(
            payload={"item": {"id": 7, "trigger_source": "auto_template"}}
        )
        payload = {
            "external_task_key": "x-auto-task-1",
            "template_ref": "x-auto-template-2",
            "template_version": 3,
            "account_id": 4,
            "material_id": "5",
            "body_template": "{{drama_name}} {{desc}}",
            "actor": "x_auto_post_service",
        }
        item = self.client(session).create_run(payload)
        self.assertEqual(item["id"], 7)
        args, kwargs = session.post.call_args
        self.assertEqual(
            args[0],
            "http://127.0.0.1:8810/internal/posts/auto-template/runs/create",
        )
        self.assertEqual(kwargs["json"], payload)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + "b" * 48)
        self.assertNotIn("Authorization", kwargs["json"])

    def test_material_unavailability_preserves_request_order(self):
        session = mock.Mock()
        session.post.return_value = FakeResponse(
            payload={"item": {"material_keys": ["3", "1"]}}
        )
        result = self.client(session).unavailable_material_ids(["1", "2", "3"])
        self.assertEqual(result, ["1", "3"])

    def test_plan_transport_loss_is_marked_unknown(self):
        session = mock.Mock()
        session.post.side_effect = requests.ConnectionError("lost")
        with self.assertRaises(XPostBridgeError) as caught:
            self.client(session).create_plan(7, {"material_id": "9"})
        self.assertTrue(caught.exception.unknown_outcome)

    def test_exact_recovery_uses_run_scoped_route_and_safe_dto(self):
        session = mock.Mock()
        session.post.return_value = FakeResponse(
            payload={
                "item": {
                    "busy": False,
                    "recovered": True,
                    "run": {"id": 17, "trigger_source": "auto_template"},
                }
            }
        )
        result = self.client(session).recover_run(17)
        self.assertTrue(result["recovered"])
        self.assertEqual(
            session.post.call_args.args[0],
            "http://127.0.0.1:8810/internal/posts/auto-template/runs/17/recover",
        )
        session.post.return_value = FakeResponse(
            payload={"item": {"busy": "false", "recovered": False, "run": {}}}
        )
        with self.assertRaises(XPostBridgeError):
            self.client(session).recover_run(17)

    def test_non_object_item_and_non_auto_route_fail_closed(self):
        session = mock.Mock()
        session.post.return_value = FakeResponse(payload={"item": []})
        with self.assertRaises(XPostBridgeError):
            self.client(session).query_run(7)
        with self.assertRaises(XPostBridgeError):
            self.client(session)._post("/internal/posts/manual-plan")


if __name__ == "__main__":
    unittest.main()
