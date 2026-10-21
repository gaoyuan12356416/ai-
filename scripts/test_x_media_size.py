"""Offline regression for videos above the removed 512 MiB application cap."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from features.x_posts import service, media_repair
from features.x_posts.media_limits import MAX_POST_VIDEO_BYTES, video_byte_limit
from scripts.test_x_posts import ScriptedHttpClient, valid_probe_payload


class XMediaSizeTests(unittest.TestCase):
    def test_large_video_reaches_probe_and_keeps_duration(self):
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(valid_probe_payload(duration="1565")))
        runner = mock.Mock(return_value=completed)
        with mock.patch.object(Path, 'stat', return_value=SimpleNamespace(st_size=1209559729)):
            result = service.probe_media('source.mp4', runner=runner,
                                         max_duration_seconds=service.PREMIUM_MAX_DURATION_SECONDS)
        self.assertEqual(result['size'], 1209559729)
        self.assertEqual(result['duration'], 1565)
        runner.assert_called_once()

    def test_account_byte_boundaries(self):
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(valid_probe_payload()))
        for premium, maximum in [(False, 8 * 1024**3), (True, 16 * 1024**3)]:
            for extra in (0, 1):
                with self.subTest(premium=premium, extra=extra), mock.patch.object(
                        Path, 'stat', return_value=SimpleNamespace(st_size=maximum+extra)):
                    runner = mock.Mock(return_value=completed)
                    if extra:
                        with self.assertRaises(service.XPostError) as caught:
                            service.probe_media('source.mp4', max_bytes=video_byte_limit(premium), runner=runner)
                        self.assertEqual(caught.exception.code, 'media_too_large')
                        runner.assert_not_called()
                    else:
                        self.assertEqual(service.probe_media('source.mp4', max_bytes=video_byte_limit(premium),
                                                            runner=runner)['size'], maximum)

    def test_download_large_header_is_not_rejected_by_legacy_cap(self):
        class ReachedStream(Exception):
            pass
        class HeaderResponse(service.HttpResponse):
            def iter_bytes(self):
                raise ReachedStream()
        client = ScriptedHttpClient([HeaderResponse(200, {'content-type':'video/mp4',
                                                          'content-length':'1209559729'})])
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ReachedStream):
            service.download_media('https://media.example.com/source.mp4', Path(temp)/'video',
                                   ['media.example.com'], http_client=client)

    def test_download_over_platform_cap_stops_before_streaming(self):
        client = ScriptedHttpClient([service.HttpResponse(200, {'content-type':'video/mp4',
                                                               'content-length':str(MAX_POST_VIDEO_BYTES+1)})])
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(service.XPostError) as caught:
            service.download_media('https://media.example.com/source.mp4', Path(temp)/'video',
                                   ['media.example.com'], http_client=client)
        self.assertEqual(caught.exception.code, 'media_too_large')

    def test_16gib_upload_passes_old_1000_segment_ceiling(self):
        client = service.XApiClient()
        requests = []
        def request(method, path, token, body=None, content_type=None, **kwargs):
            requests.append((path, body))
            return {'data':{'id':'123'}} if path.endswith('/initialize') else {'data':{}}
        client._request = request
        handle = mock.MagicMock()
        handle.__enter__.return_value.read.return_value = b'x'
        with mock.patch.object(Path, 'stat', return_value=SimpleNamespace(st_size=MAX_POST_VIDEO_BYTES)), \
             mock.patch.object(Path, 'open', return_value=handle):
            self.assertEqual(client.upload_media('fake-token', 'source.mp4')['media_id'], '123')
        self.assertEqual(json.loads(requests[0][1])['total_bytes'], MAX_POST_VIDEO_BYTES)
        appends = [p for p, body in requests if p.endswith('/append')]
        self.assertEqual(len(appends), 4096)
        self.assertTrue(requests[-1][0].endswith('/finalize'))

    def test_gpu_default_preserves_long_video_bitrate(self):
        self.assertEqual(media_repair.DEFAULT_OUTPUT_MAX_BYTES, MAX_POST_VIDEO_BYTES)
        rates = media_repair.output_rate_control(media_repair.DEFAULT_OUTPUT_MAX_BYTES, 1565)
        self.assertEqual(rates['video_bitrate_kbps'], 5000)


if __name__ == '__main__':
    unittest.main()
