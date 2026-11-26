import unittest
from collections import defaultdict
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.social_analytics.adapters import normalized
from features.social_analytics.tiktok import campaign_key
from features.tt_promotion import channels, profiles


DAY = "2026-11-25"
ACTOR = dict(tenant_key="T", user_id="u", role="user")


def catalog_row(key, drama, material, *, owner="u", tenant="T", lang="en"):
    row = normalized(
        "tiktok",
        dict(
            key=[key, ""],
            site_id="2250",
            link_id=key,
            tenant=tenant,
            owner=owner,
            drama=drama,
            language=lang,
            channel="123",
            link_type="auto_template",
            excluded=False,
            publications=[dict(id=key, date=DAY, kind="video")],
        ),
    )
    row["tt_frozen_route"] = dict(key=row["key"], date=DAY)
    row["tt_identity"] = [owner, "1", row["channel"], drama, material, key]
    return row


class OrganicMetricsTest(unittest.TestCase):
    def test_scoped_performance_uses_verified_route_and_material_evidence(self):
        keys = [campaign_key(str(i), "Case") for i in range(10)]
        rows = [
            catalog_row(keys[0], "a", "101"),
            catalog_row(keys[1], "a", "102"),
            catalog_row(keys[2], "b", "201", owner="other"),
            catalog_row(keys[3], "a", "301", tenant="T2"),
            catalog_row(keys[4], "c", "401", lang="fr"),
            catalog_row(keys[5], "a", "501"),
            catalog_row(keys[5], "a", "502"),
            catalog_row(keys[6], "x", "601"),
            catalog_row(keys[6], "y", "602"),
            catalog_row(keys[7], "a", "701"),
            catalog_row(keys[8], "z", "800"),
        ]
        effects = [
            dict(platform="tiktok", site_id="2250", date=DAY, campaign_id=campaign, revenue_cents=amount)
            for campaign, amount in zip(keys, (500, 70, 91, 35, 24, 19, 45, 900, 0, 15))
        ]
        effects[7]["tiktok_unassigned"] = True
        manifest = dict(
            version="v",
            end=DAY,
            generated_at=DAY + "T23:00:00+00:00",
            coverage={"tiktok:2250": [DAY]},
            iaa_coverage={"tiktok:2250": [DAY]},
        )
        snapshot = profiles.build_snapshot(rows, effects, {}, manifest, date.fromisoformat(DAY))
        metric = profiles.social_metrics(ACTOR, "en", product="DramaWave", snapshot=snapshot)
        self.assertEqual(metric["status"], "complete")
        self.assertEqual(metric["dramas"], {"a": 589, "z": 0})
        self.assertEqual(metric["materials"], {"101": 500, "102": 70, "800": 0})
        self.assertEqual(
            profiles.social_metrics(dict(ACTOR, role="admin"), "en", snapshot=snapshot)["dramas"],
            {"a": 589, "z": 0, "b": 91},
        )
        self.assertEqual(profiles.social_metrics(ACTOR, "fr", snapshot=snapshot)["materials"], {"401": 24})
        self.assertEqual(
            profiles.social_metrics(ACTOR, "en", product="HotDrama", snapshot=snapshot)["status"],
            "unavailable",
        )
        self.assertEqual(
            profiles.social_metrics({"_system_report": True}, "en", snapshot=snapshot)["dramas"], {}
        )
        snapshot["meta"]["status"] = "partial"
        self.assertNotIn("800", profiles.social_metrics(ACTOR, "en", snapshot=snapshot)["materials"])

    def test_old_snapshot_and_missing_metrics_remain_unknown(self):
        with TemporaryDirectory() as location:
            root = Path(location)
            profiles._write_snapshot(root, "current.json", {"meta": {"version": "old"}, "facts": []})
            metric = profiles.social_metrics(ACTOR, "en", root=root)
            self.assertEqual(
                (metric["status"], metric["dramas"], metric["materials"]), ("unavailable", {}, {})
            )
            profiles._write_snapshot(
                root, "followers.json", {"1": {"count": 0, "seen_at": "2026-11-25 13:05:00"}}
            )
            self.assertEqual(profiles.account_followers(root)["1"]["count"], 0)

    def test_channel_total_and_source_proven_zero_fans(self):
        updated = "2026-11-25 13:05:00"
        counts = {"1": {"count": 0, "seen_at": updated}, "3": {"count": 0, "seen_at": "2026-11-24 00:00:00"}}
        accounts = [
            (aid, {"account_id": aid, "last_seen_at": updated, "fan_count": fans, **extra})
            for aid, fans, extra in [
                ("1", 0, {}),
                ("2", 0, {}),
                ("3", 0, {}),
                ("4", 37, {}),
                ("5", 0, {"has_metric_snapshot": 1}),
                ("6", 2, {"has_metric_snapshot": 0}),
            ]
        ]
        sample = dict(
            accounts={
                aid: dict(
                    status="available",
                    windows={"28d": {"revenue_cents": 10}, "all": {"revenue_cents": 12345}},
                )
                for aid, _ in accounts
            },
            meta={},
        )
        facts = {"items": [row for _, row in accounts]}
        with (
            patch.object(profiles, "account_followers", return_value=counts),
            patch.object(profiles, "profiles_for_accounts", return_value=sample),
            patch.object(channels, "handoff", return_value=defaultdict(channels._stats)),
            patch.object(channels, "_setting_rows", return_value={}),
        ):
            result = channels.channel_data(ACTOR, facts=facts)
        self.assertEqual([r["followers"] for r in result["items"]], [0, None, None, 37, 0, None])
        self.assertTrue(
            all(r["revenue_total"] == 123.45 and r["revenue_28d"] == 0.1 for r in result["items"])
        )
