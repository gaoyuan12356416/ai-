"""Preparation receipts cross the real channel SQLite boundary in isolated fixtures."""
import json
import unittest
from unittest.mock import patch

from features.pipeline_dispatch.cloud_admin import session_actor
from features.pipeline_dispatch.core import DispatchError


class CosmosNativeTests(unittest.TestCase):
    def test_x_cloud_preparation_enters_original_publisher(self):
        from scripts.test_pipeline_x_native import NativeTests
        fixture = NativeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        queue = fixture.queue(auto=True)
        job = dict(fixture.job(queue,"prepare",backend="cos"),node_id="cos-cloud")
        desc = fixture.bridge.reserve(job,job)
        self.assertIn("backend:cos",desc["required_capabilities"])
        artifact = {"url":"https://media.example.test/ci-fixture.mp4","sha256":"b"*64,"size":3333,
                    "mime":"video/mp4","duration_seconds":30,"width":720,"height":1280,"profile":"source"}
        ack = {"source_url":desc["request"]["source_url"],"artifact":artifact}
        fixture.bridge.checkpoint(job,job,"x_preflight",ack)
        fixture.bridge.complete(job,{"artifact":artifact},job)
        pub = fixture.job(queue,"publish")
        self.assertEqual(fixture.bridge.reserve(pub,pub)["artifact"],artifact)

    def test_tt_cloud_receipt_is_idempotent_and_publishable(self):
        from scripts.test_pipeline_tt_bridge import RoutedTTBridgeTests
        fixture = RoutedTTBridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.job.update(node_id="cos-cloud", executor_backend="cos")
        fixture.claim["node_id"]="cos-cloud"
        fixture.test_frozen_route_intercepts_reserve_completion_publish_and_repeat()

    def test_only_authenticated_admin_can_change_capacity(self):
        for user, code in [({"user_id":"member","is_admin":False},403),({},401),({"user_id":"admin","is_admin":True},200)]:
            with self.subTest(expected=code), patch("features.pipeline_dispatch.cloud_admin.http.client.HTTPConnection") as factory:
                resp=factory.return_value.getresponse.return_value
                resp.status=200
                resp.read.return_value=json.dumps({"authenticated":bool(user),"user":user}).encode()
                if code==200:self.assertTrue(session_actor("session=fixture")["is_admin"])
                else:
                    with self.assertRaises(DispatchError) as cm:session_actor("session=fixture")
                    self.assertEqual(cm.exception.status,code)


if __name__=="__main__":
    unittest.main()
