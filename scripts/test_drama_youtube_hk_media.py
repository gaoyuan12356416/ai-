import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from features.drama_synthesis.youtube import YouTubeHTTPError, YouTubeRemoteMediaExecutor
from features.drama_synthesis.youtube_media import YouTubeMediaExecutorService


class RemoteExecutorTests(unittest.TestCase):
    def test_client_requires_loopback_tunnel_and_token(self):
        with self.assertRaises(ValueError):
            YouTubeRemoteMediaExecutor("http://43.154.250.89:8787", "token")
        with self.assertRaises(ValueError):
            YouTubeRemoteMediaExecutor("http://127.0.0.1:18788", "")

    def test_upload_session_host_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            service = YouTubeMediaExecutorService(temp, ["cos.example.test"])
            source = Path(temp) / "task-1" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"video-bytes")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaises(YouTubeHTTPError) as caught:
                service.upload({
                    "task_id": 1, "session_uri": "https://evil.example/upload", "offset": 0,
                    "size": source.stat().st_size, "sha256": digest,
                })
            self.assertEqual("youtube_upload_session_denied", caught.exception.code)

    def test_upload_binds_remote_file_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            service = YouTubeMediaExecutorService(temp, ["cos.example.test"])
            source = Path(temp) / "task-9" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"fixed-video")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            service.client.upload = mock.Mock(return_value={"state": "resume", "next_byte": 4})
            result = service.upload({
                "task_id": 9,
                "session_uri": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x",
                "offset": 0, "size": source.stat().st_size, "sha256": digest,
            })
            self.assertEqual({"ok": True, "state": "resume", "next_byte": 4}, result)
            source.write_bytes(b"changed-video")
            with self.assertRaises(YouTubeHTTPError) as caught:
                service.upload({
                    "task_id": 9,
                    "session_uri": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x",
                    "offset": 0, "size": len(b"fixed-video"), "sha256": digest,
                })
            self.assertTrue(caught.exception.unknown)

    def test_cleanup_is_scoped_to_one_task(self):
        with tempfile.TemporaryDirectory() as temp:
            service = YouTubeMediaExecutorService(temp, ["cos.example.test"])
            first = Path(temp) / "task-1"
            second = Path(temp) / "task-2"
            first.mkdir(); second.mkdir()
            service.cleanup({"task_id": 1})
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main()
