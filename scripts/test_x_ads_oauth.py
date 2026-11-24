import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import x_ads_oauth as ads

APP = {"consumer_key": "sample-app", "consumer_secret": "sample-client-secret"}
ACTOR = {"tenant_key": "org", "user_id": "operator"}
CHANNEL = {"username": "shorts", "x_user_id": "1234"}
BROWSER = "test-browser"
CALLBACK = "https://example.com/registered"
REQUEST = {"oauth_token": "request-value", "oauth_token_secret": "request-secret", "oauth_callback_confirmed": "true"}
ACCESS = {"oauth_token": "access-value", "oauth_token_secret": "access-secret", "user_id": "1234"}
RETURN = {"oauth_token": ["request-value"], "oauth_verifier": ["sample-verifier"]}


class AdsGrantChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="x-ads-sandbox-")
        self.addCleanup(self.temp.cleanup)
        self.directory = self.temp.name

    def start(self):
        with patch.object(ads, "token_request", return_value=REQUEST):
            url = ads.start(self.directory, APP, CALLBACK, ACTOR, BROWSER, CHANNEL)
        self.assertEqual(urlsplit(url).netloc, "api.x.com")
        self.assertEqual(parse_qs(urlsplit(url).query)["screen_name"], ["shorts"])
        self.assertNotIn("secret", url)

    def finish(self, actor=ACTOR, browser=BROWSER, fields=ACCESS, query=RETURN):
        with patch.object(ads, "token_request", return_value=fields):
            return ads.finish(self.directory, APP, actor, browser, query)

    def test_actor_browser_and_subject_binding_and_replay(self):
        self.start()
        for actor, browser in [({"user_id": "other"}, BROWSER), (ACTOR, "different")]:
            with self.assertRaisesRegex(ads.AdsError, "浏览器"):
                self.finish(actor, browser)
        self.assertEqual(self.finish(), ("1234", "shorts"))
        self.assertEqual(ads.status(self.directory, APP, "1234"), {"authorized": True, "report": None})
        with self.assertRaises(ads.AdsError):
            self.finish()
        self.start()
        with self.assertRaisesRegex(ads.AdsError, "频道不同"):
            self.finish(fields=dict(ACCESS, user_id="9876", oauth_token="wrong"))
        with ads.database(self.directory) as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT token,x_id FROM grants")], [("access-value", "1234")])

    def test_denial_and_old_request_are_never_exchanged(self):
        self.start()
        with patch.object(ads, "token_request") as exchange:
            with self.assertRaisesRegex(ads.AdsError, "取消"):
                ads.finish(self.directory, APP, ACTOR, BROWSER, {"denied": ["request-value"]})
            exchange.assert_not_called()
        self.start()
        with ads.database(self.directory) as db:
            db.execute("UPDATE pending SET started=0")
        with self.assertRaises(ads.AdsError):
            self.finish()
        self.assertFalse(ads.status(self.directory, APP, "1234")["authorized"])

    def test_ads_denial_preserves_oauth_and_scoped_card_read_follows_pagination(self):
        self.start()
        self.finish()
        with patch.object(ads, "ads_read", return_value=({"http": 403, "error": "UNAUTHORIZED_CLIENT_APPLICATION"}, [], None)):
            self.assertEqual(ads.probe(self.directory, APP, "1234")["accounts_api"]["http"], 403)
        self.assertTrue(ads.status(self.directory, APP, "1234")["authorized"])
        ok = {"http": 200}
        replies = [(ok, [], "page2"), (ok, [{"id": "abcd", "name": "Ads test"}], None),
                   (ok, [{"user_id": "1234", "promotable_user_type": "FULL"}], None), (ok, [], None)]
        with patch.object(ads, "ads_read", side_effect=replies) as read:
            report = ads.probe(self.directory, APP, "1234")
        self.assertEqual(read.call_args_list[1].args[2]["cursor"], "page2")
        self.assertEqual(read.call_args_list[-1].args[1:], ("/accounts/abcd/cards", {"count": 1}))
        self.assertEqual(report["accounts"][0]["cards_api"], {"http": 200})
        self.assertNotIn("secret", str(ads.status(self.directory, APP, "1234")))


if __name__ == "__main__":
    unittest.main()
