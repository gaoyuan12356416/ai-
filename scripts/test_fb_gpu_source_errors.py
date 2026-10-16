import json
import unittest

from features.fb_auto_posts.gpu import GPUPrepareClient, GPUPrepareError


class SourceErrorTests(unittest.TestCase):
    def request_error(self, message):
        class Response:
            status = 400

            def read(self, limit):
                return json.dumps({"code": "invalid_request", "message": message}).encode()

        class Connection:
            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        client = GPUPrepareClient("k" * 48, connection_factory=lambda *args: Connection())
        with self.assertRaises(GPUPrepareError) as caught:
            client.prepare(job_id="fb-page-" + "a" * 48, content_id="drama", source_url="https://source.example/video.mp4", video_template="random_overlay")
        return caught.exception

    def test_source_host_rejection_has_stable_specific_code(self):
        error = self.request_error("source URL host invalid")
        self.assertEqual(error.code, "fb_auto_source_url_not_allowed")
        self.assertIn("域名", str(error))

    def test_other_invalid_requests_do_not_masquerade_as_host_errors_or_leak_messages(self):
        error = self.request_error("secret=do-not-echo")
        self.assertEqual(error.code, "invalid_request")
        self.assertIn("参数", str(error))
        self.assertNotIn("do-not-echo", str(error))
