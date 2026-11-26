import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.social_material_effects import data
from features.tt_promotion.publishing import PublicationError, selection_rule
from features.tt_promotion.social_effects import SocialRules


class SocialEffectsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, SOCIAL_MATERIAL_EFFECT_ROOT=directory.name)
        env.start()
        self.addCleanup(env.stop)

        def post(key, cid, mid, views, revenue):
            return dict(
                post_id=key,
                content_id=cid,
                material_id=mid,
                views=views,
                revenue_cents=revenue,
                namespace="ads_custom_source",
                catalog="dramawave",
                product="DramaWave",
                language="en",
                account_id="1",
            )

        data.publish(
            directory.name,
            "x",
            [post("1", "C1", "12", 8, 24), post("2", "C1", "10", 12, 100), post("3", "C2", "15", 0, None)],
            dict(version="x1", generated_at="v1"),
        )
        data.publish(
            directory.name,
            "facebook",
            [post("1", "C1", "12", None, 1)],
            dict(version="f1", generated_at="v1"),
        )

    def rule(self, **values):
        return selection_rule(values, {"sort_by": "roas", "sort_direction": "desc"})

    def test_bounds_and_sort_unknown_zero_and_evidence(self):
        cfg = dict(
            drama_rule=self.rule(effect_channel="all", x_views_min=20),
            material_rule=self.rule(sort_by="x_views", effect_channel="all"),
        )
        s = SocialRules(cfg, "en")
        candidates = [SimpleNamespace(content_id=i) for i in ("C0", "C1", "C2")]
        rejected = {}
        self.assertEqual(s.apply("drama", candidates, rejected), candidates[1:2])
        self.assertEqual(rejected, {"drama_social_views": 2})
        mats = [SimpleNamespace(source=SimpleNamespace(material_id=i)) for i in ("11", "12", "10")]
        self.assertEqual(s.apply("material", mats, {}, "C1"), mats[::-1])
        audit = s.audit("C1", "12")["material"]
        self.assertEqual((audit["known"]["views"], audit["coverage"]["views"], audit["posts"]), (8, 1, 2))
        self.assertIsNone(audit["metrics"]["views"])
        self.assertEqual(audit["metrics"]["revenue"], 0.25)
        self.assertEqual(audit["versions"], {"x": "x1", "facebook": "f1"})
        s = SocialRules(dict(drama_rule=self.rule(effect_channel="all", x_views_max=20)), "en")
        self.assertEqual(s.apply("drama", candidates, {}), candidates[2:])
        for direction in ("asc", "desc"):
            s = SocialRules(dict(drama_rule=self.rule(sort_by="x_views", sort_direction=direction)), "en")
            self.assertEqual(s.apply("drama", candidates, {})[-1], candidates[0])

    def test_tiktok_income_and_validation(self):
        for bad in (
            {"x_views_min": 0.1},
            {"x_likes_min": 1, "x_likes_max": 0},
            {"effect_channel": "unknown"},
            {"effect_channel": "tiktok", "sort_by": "x_comments"},
            {"x_revenue_min": "NaN"},
        ):
            with self.assertRaises(PublicationError):
                self.rule(**bad)
        cfg = dict(
            creator={"tenant_key": "a", "user_id": "me"},
            drama_rule=self.rule(effect_channel="tiktok", x_revenue_min=".25", x_revenue_max=".25"),
        )
        with patch(
            "features.tt_promotion.profiles.social_metrics",
            return_value=dict(
                version="one", end="yesterday", status="complete", dramas={"A": 25, "B": 0}, materials={}
            ),
        ) as read:
            s = SocialRules(cfg, "en")
        read.assert_called_once_with(cfg["creator"], "en")
        rows = [SimpleNamespace(content_id=i) for i in "ABC"]
        self.assertEqual(s.apply("drama", rows, {}), rows[:1])
        self.assertEqual(s.audit("B", "1")["drama"]["metrics"]["revenue"], 0)
        self.assertIsNone(s.audit("C", "1")["drama"]["metrics"]["revenue"])
