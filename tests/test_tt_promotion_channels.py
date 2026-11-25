import json
from collections import defaultdict
from pathlib import Path
import sqlite3
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

    def test_profile_failure_keeps_account_policy_progress_and_pending_legacy_job(self):
        stats = defaultdict(channels._stats)
        stats["10"].update(today_published=2, slots={"a", "b", "c"})
        stats["10"]["inflight"].append({"id": 9, "source": "direct_test", "status": "unknown"})
        with patch("features.tt_promotion.profiles.profiles_for_accounts", side_effect=ValueError("invalid snapshot")), patch.object(
            channels, "handoff", return_value=stats
        ):
            data = channels.channel_data(self.actor, detail="10", path=self.path, facts=self.source)
        item = data["channel"]
        self.assertEqual(set(data["meta"]["module_errors"]), {"profile"})
        self.assertEqual((item["eligible"], item["enabled"], item["today_published"], item["today_expected"]), (True, True, 2, 3))
        self.assertEqual((item["legacy_scheduled"], item["legacy_conflict"], item["legacy_inflight_count"]), (False, True, 1))
        self.assertIsNone(item["has_profile"])
        self.assertIsNone(item["revenue_28d"])
        self.assertTrue(data["handoff"]["available"])

    def test_unavailable_handoff_is_unknown_including_when_income_is_available(self):
        profile = {"accounts": {"10": {"status": "available", "windows": {"28d": {"revenue_cents": 512}}}}, "meta": {"status": "complete"}}
        with patch("features.tt_promotion.profiles.profiles_for_accounts", return_value=profile), patch.object(
            channels, "handoff", side_effect=sqlite3.OperationalError("read failed")
        ):
            data = channels.channel_data(self.actor, detail="10", path=self.path, facts=self.source)
        item = data["channel"]
        self.assertEqual(set(data["meta"]["module_errors"]), {"handoff"})
        self.assertFalse(data["handoff"]["available"])
        self.assertEqual(item["revenue_28d"], 5.12)
        for key in ["legacy_conflict", "legacy_scheduled", "today_published", "today_expected"]:
            self.assertIsNone(item[key])

    def test_module_retry_reads_only_requested_source(self):
        profile = {"accounts": {}, "meta": {}}
        with patch("features.tt_promotion.profiles.profiles_for_accounts", return_value=profile) as portraits, patch.object(
            channels, "handoff", return_value=defaultdict(channels._stats)
        ) as plans:
            for section in ["profile", "handoff"]:
                portraits.reset_mock()
                plans.reset_mock()
                data = channels.channel_data(self.actor, path=self.path, facts=self.source, section=section)
                self.assertEqual(data["meta"]["section"], section)
                self.assertEqual(portraits.call_count, int(section == "profile"))
                self.assertEqual(plans.call_count, int(section == "handoff"))
            with self.assertRaises(channels.ChannelError):
                channels.channel_data(self.actor, path=self.path, facts=self.source, section="other")
