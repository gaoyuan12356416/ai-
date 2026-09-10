"""Offline platform error diagnostics; no Google requests."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.youtube_auto_publish.engine import ReviewedYouTubeHTTPClient
from features.drama_synthesis.youtube import YouTubeHTTPError


class PlatformErrorCase(unittest.TestCase):
    def client(self,response):
        session=Mock();session.get.return_value=response;session.post.return_value=response
        return ReviewedYouTubeHTTPClient(session_factory=lambda:session)

    def test_empty_owner_response_explicit_and_unknown(self):
        response=Mock(status_code=200);response.json.return_value={'items':[]}
        with self.assertRaises(YouTubeHTTPError) as ctx:
            self.client(response).read_reviewed_video_state('OFFLINE','c68YK2Xm0Yk',expected_channel_id='UCtest')
        self.assertTrue(ctx.exception.unknown)
        self.assertIn('未返回此视频',str(ctx.exception))

    def test_thumbnail_error_keeps_safe_reason_and_status_only(self):
        response=Mock(status_code=403)
        response.json.return_value={'error':{'message':'TOKEN-DO-NOT-PERSIST','errors':[{'reason':'forbidden'}]}}
        with self.assertRaises(YouTubeHTTPError) as ctx:
            self.client(response).set_thumbnail('OFFLINE',video_id='c68YK2Xm0Yk',content=b'offline',mime_type='image/jpeg')
        self.assertIn('403',str(ctx.exception));self.assertIn('forbidden',str(ctx.exception))
        self.assertNotIn('TOKEN',str(ctx.exception))


if __name__=='__main__':unittest.main()
