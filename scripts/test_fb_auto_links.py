import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from features.fb_auto_posts.links import (
    FBPostLinkError,
    FB_W2A_QUERY_FIELDS,
    build_short_url,
    build_w2a_url,
    validate_short_url,
    validate_w2a_url,
    write_short_redirect,
)


def fields(task_id=7):
    return {
        "username":"10001",
        "timestamp":1787191200,
        "material_language":"en",
        "drama_name":"My Drama",
        "tag":"hook",
        "task_id":task_id,
        "page_name":"Free Reels",
        "page_id":"10001",
        "material_name":"Opening",
        "material_id":"501",
        "content_id":"AcWE9aQz8q",
    }


class LinkTests(unittest.TestCase):
    def test_short_and_long_urls_follow_exact_fb_contract(self):
        self.assertEqual(build_short_url(7),"https://gy.g2flow.com/s2l/fb/7.html")
        self.assertEqual(validate_short_url(build_short_url(7)),build_short_url(7))
        long_url=build_w2a_url(fields())
        parsed=urllib.parse.urlsplit(long_url); pairs=urllib.parse.parse_qsl(parsed.query,keep_blank_values=True)
        self.assertEqual(parsed.scheme+"://"+parsed.netloc+parsed.path,"https://www.dramawavew2a.com/ads/0/2049/view")
        self.assertEqual(tuple(key for key,_value in pairs),FB_W2A_QUERY_FIELDS)
        values=dict(pairs)
        self.assertEqual(values["af_channel"],"AIpost"); self.assertEqual(values["af_c_id"],"7")
        self.assertEqual(values["af_dp"],"AcWE9aQz8q"); self.assertEqual(values["af_ad_id"],"501")
        self.assertEqual(values["c"],"yingliang_post_CLV_VL_10001*1787191200noneen*My Drama*hook*7")
        self.assertEqual(validate_w2a_url(long_url),long_url)

    def test_writer_is_atomic_idempotent_and_conflict_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"fb"; target=build_w2a_url(fields())
            destination=write_short_redirect(root,7,target)
            self.assertEqual(destination,root/"7.html"); self.assertIn("AIpost",destination.read_text(encoding="utf-8"))
            self.assertEqual(write_short_redirect(root,7,target),destination)
            with self.assertRaises(FBPostLinkError) as caught: write_short_redirect(root,7,build_w2a_url(fields(8)))
            self.assertEqual(caught.exception.code,"fb_auto_short_link_conflict")

    def test_writer_rejects_relative_and_symlink_root(self):
        target=build_w2a_url(fields())
        with self.assertRaises(FBPostLinkError): write_short_redirect("relative/fb",7,target)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"fb"; root.mkdir()
            original=Path.is_symlink
            with patch.object(Path,"is_symlink",autospec=True,side_effect=lambda value: value == root or original(value)):
                with self.assertRaises(FBPostLinkError) as caught: write_short_redirect(root,7,target)
            self.assertEqual(caught.exception.code,"fb_auto_short_link_root_invalid")


if __name__ == "__main__": unittest.main()
