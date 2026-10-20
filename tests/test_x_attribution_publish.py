"""Exercise real publishing boundary with a wholly in-memory HTTP fake."""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from features.x_posts import attribution, service
from scripts.test_x_posts import ScriptedHttpClient, candidate, response, valid_probe_payload


class PublishAttributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "x.sqlite3"
        self.store = service.XPostStore(self.db)
        self.queue = self.store.enqueue(candidate())
        self.log = self.store.reserve_log(self.queue["id"])
        self.context = dict(
            version=attribution.VERSION, af_c_id=str(self.queue["id"]),
            frozen_at=self.log["created_at"], tenant="tenant", owner="user", owner_name="User",
            sub_user_id="248", channel_id="2076951197916037120", channel_name="Frozen Account",
            material_id="88001", material_name="best_video", content_id="32001",
            drama_name="The Contract Bride", language="en", tag="romance",
            link_type="system", excluded=False,
        )
        self.kwargs = dict(
            db_path=self.db, queue_id=self.queue["id"], account={"id": 2, "username": "ShortsDramhx"},
            access_token="isolated-test-token", public_root=self.root / "s2l",
            short_base_url="https://gy.g2flow.com/s2l", allowed_media_hosts=["media.example.com"],
            sleeper=lambda _: None, timeout=2,
        )
        patcher = mock.patch.dict(os.environ, X_POST_ATTRIBUTION_V2_ENABLED="1")
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self):
        return ScriptedHttpClient([
            service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video"),
            response(200, {"data": {"id": "media1"}}), response(200, {"data": {"expires_at": 1}}),
            response(200, {"data": {"id": "media1"}}), response(201, {"data": {"id": "190002", "text": "ok"}}),
        ])

    def test_generated_url_uses_frozen_identity_and_does_not_republish(self):
        client = self.client()
        with mock.patch.object(attribution, "resolve_via_loopback", return_value=self.context) as resolver, \
                mock.patch.object(service, "probe_media", return_value=valid_probe_payload()):
            result = service.publish_canary(**self.kwargs, http_client=client)
            count = len(client.requests)
            again = service.publish_canary(**self.kwargs, http_client=client)
            self.assertEqual(result["post_id"], again["post_id"])
            self.assertEqual(len(client.requests), count)
            resolver.assert_called_once()
        with closing(sqlite3.connect(self.db)) as c:
            url = c.execute("SELECT long_url FROM x_post_publish_log").fetchone()[0]
            frozen = json.loads(c.execute("SELECT context_json FROM x_post_link_attribution").fetchone()[0])
        values = parse_qs(urlsplit(url).query)
        self.assertEqual(len(values), 8)
        self.assertEqual(values["af_channel"], ["248"])
        self.assertEqual(values["af_c_id"], [str(self.queue["id"])])
        self.assertIn("*%snoneen*" % attribution.epoch(self.log["created_at"]), values["c"][0])
        self.assertEqual(frozen["channel_name"], "Frozen Account")

    def test_mapping_failure_is_explicit_and_no_x_request_occurs(self):
        client = ScriptedHttpClient([])
        with mock.patch.object(attribution, "resolve_via_loopback",
                               side_effect=attribution.AttributionError("x_attribution_user_ambiguous")):
            with self.assertRaises(service.XPostError) as raised:
                service.publish_canary(**self.kwargs, http_client=client)
        self.assertEqual(raised.exception.code, "x_attribution_user_ambiguous")
        self.assertEqual(client.requests, [])
        self.assertIsNone(attribution.load_frozen(self.db, self.log["id"]))

    def test_preexisting_frozen_identity_is_reused_without_remote_resolution(self):
        attribution.freeze(self.db, self.log, self.context)
        with mock.patch.object(attribution, "resolve_via_loopback", side_effect=AssertionError("must reuse")), \
                mock.patch.object(service, "probe_media", return_value=valid_probe_payload()):
            result = service.publish_canary(**self.kwargs, http_client=self.client())
        self.assertEqual(result["status"], "published")


if __name__ == "__main__":
    unittest.main()
