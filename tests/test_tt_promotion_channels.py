import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.tt_promotion import channels, http_server


class ChannelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "new.sqlite3"
        channels.initialize(self.path)
        self.actor = {"user_id": "a", "tenant_key": "A", "role": "user"}
        self.policy = dict(
            privacy_level="PUBLIC_TO_EVERYONE",
            allow_comment=True,
            allow_duet=False,
            allow_stitch=False,
            brand_content_toggle=False,
            brand_organic_toggle=True,
            is_aigc=False,
        )
        self.source = {
            "items": [
                {
                    "account_id": "10",
                    "publish_eligible": True,
                    "account_settings": dict(self.policy, configured=True),
                }
            ]
        }

    def test_first_edit_seeds_independent_policy_and_unknown_bulk_is_atomic(self):
        channels.save_settings(
            self.actor, {"account_ids": ["10"], "settings": {"enabled": False}}, self.source, self.path
        )
        channels.save_settings(
            self.actor,
            {"account_ids": ["10"], "settings": {"drama_language": "pt_BR"}},
            {
                "items": [
                    {
                        "account_id": "10",
                        "account_settings": dict(self.policy, configured=True, allow_comment=False),
                    }
                ]
            },
            self.path,
        )
        with self.assertRaises(channels.ChannelError):
            channels.save_settings(
                self.actor,
                {"account_ids": ["10", "11"], "settings": {"enabled": True}},
                self.source,
                self.path,
            )
        row = channels._setting_rows(self.path, "A")["10"]
        self.assertEqual((row["enabled"], row["language"], row["version"]), (0, "pt-br", 2))
        self.assertTrue(json.loads(row["policy_json"])["allow_comment"])
        self.assertEqual(channels._setting_rows(self.path, "B"), {})

    def test_new_account_can_configure_without_legacy_row(self):
        facts = {"items": [{"account_id": "11", "publish_eligible": True}]}
        live = dict(
            privacy_level_options=["PUBLIC_TO_EVERYONE"],
            comment_disabled=False,
            duet_disabled=False,
            stitch_disabled=False,
        )
        with patch.object(channels, "creator_info", return_value=live):
            channels.save_settings(
                self.actor,
                {"account_ids": ["11"], "settings": {"publish_policy": self.policy}},
                facts,
                self.path,
            )
            with self.assertRaises(Exception) as exc:
                channels.save_settings(
                    self.actor,
                    {
                        "account_ids": ["11"],
                        "settings": {"publish_policy": dict(self.policy, privacy_level="SELF_ONLY")},
                    },
                    facts,
                    self.path,
                )
            self.assertEqual(exc.exception.status, 409)
        self.assertEqual(
            json.loads(channels._setting_rows(self.path, "A")["11"]["policy_json"]), self.policy
        )

    def test_two_independent_permissions_and_old_item_containment(self):
        nav = json.loads(
            (Path(__file__).resolve().parents[1] / "static" / "navigation.json").read_text(encoding="utf8")
        )
        for key, allow, deny in [
            ("tt_promotion_channels", "ttPromotionChannels", "ttPromotionAuto"),
            ("tt_promotion_auto", "ttPromotionAuto", "ttAccountSettings"),
            ("tt_posts", "ttAccountSettings", "ttPromotionAuto"),
        ]:
            actor = dict(self.actor, permissions={key: True})
            self.assertTrue(http_server.permitted(actor, allow, nav))
            self.assertFalse(http_server.permitted(actor, deny, nav))
