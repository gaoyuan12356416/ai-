"""Regression checks for frozen provider actions and conditional same-bucket copies."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from features.pipeline_dispatch.cloud_fb_input import cloud_input
from features.pipeline_dispatch.cloud_jobs import CloudLedger, fingerprint
from features.pipeline_dispatch.cloud_objects import ObjectStore


class HongKongFlowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = CloudLedger(Path(tmp.name) / "actions.db")
        self.ex = Mock()
        self.ex.jobs = SimpleNamespace(ledger=self.ledger, bucket="test", region="ap-hongkong",
                                       prefix="worker", client=Mock())
        self.ex.config = {"fb_cloud_input": True, "ffprobe": "ffprobe"}
        self.identity = ["fb:auto:123", "fb_prepare", "7"]
        self.request = {"source_url": "https://test.cos.ap-hongkong.myqcloud.com/custom/source/a.mp4"}
        self.head = {"etag": '"abcd"', "content-length": "128", "accept-ranges": "bytes", "x-cos-hash-crc64ecma": "123"}
        self.probe = SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "audio"}, {"codec_type":"video"}],
                                                       "format": {"duration": "7.5"}}).encode())

    def test_retry_keeps_the_snapshot_after_original_changes(self):
        self.ex.origin.head.side_effect = [self.head, None, self.head]
        with patch("subprocess.run", return_value=self.probe):
            first = cloud_input(self.ex, self.request, self.identity)
        self.assertEqual(first[1], 7.5)
        self.assertEqual(self.ex.jobs.client.copy_object.call_args.kwargs["CopySourceIfMatch"], '"abcd"')
        self.ex.config["fb_cloud_input"] = False
        self.ex.origin.head.side_effect = AssertionError("must not revisit original")
        self.assertEqual(first, cloud_input(self.ex, self.request, self.identity))

    def test_existing_paid_action_uses_original_body(self):
        self.ledger.intent(fingerprint([self.identity, "render"]), "old", "old.mp4", "Transcode")
        self.assertIsNone(cloud_input(self.ex, self.request, self.identity))
        self.ex.origin.head.assert_not_called()

    def test_no_audio_and_hash_bound_source_use_local_validation(self):
        self.request["source_sha256"] = "a" * 64
        self.assertIsNone(cloud_input(self.ex, self.request, self.identity))
        self.request.pop("source_sha256")
        self.ex.origin.head.return_value = self.head
        with patch("subprocess.run", return_value=SimpleNamespace(stdout=b'{"streams":[{"codec_type":"video"}],"format":{"duration":"2"}}')):
            self.assertIsNone(cloud_input(self.ex, self.request, self.identity))
        self.ex.jobs.client.copy_object.assert_not_called()

    def test_delivery_verified_after_unknown_copy_ack(self):
        client = Mock()
        store = ObjectStore(client, "dest", "outputs", "https://dest.example")
        client.copy_object.side_effect = TimeoutError()
        with patch.object(store, "head", side_effect=[None, {"content-length":"4", "x-cos-meta-sha256":"abcd"}]):
            store.upload("out", Path("must-not-open"), "abcd", 4, copy_from=({"Bucket":"src"}, '"etag"'))
        client.put_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
