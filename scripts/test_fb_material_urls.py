"""Regress the production Filipino-material legacy COS URL failure."""
import unittest

from features.fb_auto_posts.repositories import _https


class LegacyCOSURLTests(unittest.TestCase):
    host = "advertising-1306474899.cos.ap-hongkong.myqcloud.com"

    def test_owned_legacy_object_uses_same_path_and_query_over_tls(self):
        path = "/2025/clip%20one.mp4?versionId=abc%2F123"
        self.assertEqual(_https("http://" + self.host + path), "https://" + self.host + path)

    def test_other_http_hosts_and_ambiguous_authorities_stay_rejected(self):
        for authority in ("example.com", self.host + ".example.com", "user@" + self.host,
                          "user:password@" + self.host, self.host + ":80", self.host + ":invalid"):
            with self.subTest(authority=authority):
                self.assertEqual(_https("http://" + authority + "/clip.mp4"), "")

    def test_fragment_stays_rejected(self):
        self.assertEqual(_https("http://" + self.host + "/clip.mp4#part"), "")

    def test_existing_tls_contract_remains_unchanged(self):
        url = "https://media.example.com/clip.mp4?version=2"
        self.assertEqual(_https(url), url)
        self.assertEqual(_https("https://user:password@media.example.com/clip.mp4"), "")


if __name__ == "__main__":
    unittest.main()
