import json
import unittest
from unittest.mock import patch

from features.social_analytics.adapters import normalized
from features.social_analytics.backend_scope import SCOPE, facebook_source, restrict
from features.social_analytics.report import ReportError, parameters, summarize
from features.social_daily_report.collector import present
from features.social_daily_report.report import build_cards, require_complete

DAY = "2026-10-20"


def link(platform="facebook", cid="6", owner="owner", published=True, date="2026-10-19", manual=False):
    site = dict(facebook="2049", youtube="2284", x="2116")[platform]
    row = normalized(platform, dict(
        key=(cid, "unrelated display name"), link_id=cid, site_id=site, owner=owner, owner_label=owner,
        tenant="tenant", channel="page", channel_label="Page", drama="drama", drama_label="Drama",
        language="en", date=date, excluded=False, link_type="manual" if manual else "auto",
        publications=[dict(id=platform + ":post:" + cid, date=date, kind="post")] if published else []))
    row["effect_source"] = dict(adset_id="page", ad_id="material")
    return row


def metric(platform="facebook", cid="6", cents=100, **updates):
    row = dict(platform=platform, site_id=dict(facebook="2049", youtube="2284", x="2116")[platform],
               date=DAY, campaign_id=cid, campaign="renamed campaign", adset_id="page", ad_id="material",
               revenue_cents=cents, refund_cents=0, installs=1, clicks=2, views=3, conversions=1)
    row.update(updates)
    return row


class BackendScopeTests(unittest.TestCase):
    def test_reused_legacy_id_does_not_enter_backend_person_totals(self):
        catalog = [link()]
        values = [metric(cents=123), metric(cents=78000, adset_id="legacy-page", ad_id="legacy-material")]
        _, kept, _, audit = restrict(catalog, values, DAY)
        self.assertEqual(sum(r["revenue_cents"] for r in kept), 123)
        self.assertEqual(audit["outside"][0]["revenue_cents"], 78000)
        self.assertEqual(audit["outside"][0]["reason"], "outside_backend_frozen_source_ids")

    def test_manual_shortlink_kept_without_publication_but_failed_post_excluded(self):
        catalog = [link("youtube", "yt", published=False, manual=True), link(cid="failed", published=False)]
        _, kept, _, audit = restrict(catalog, [metric("youtube", "yt", cents=400), metric(cid="failed", cents=999)], DAY)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["campaign_id"], "yt")
        self.assertEqual(audit["included_totals"]["revenue_cents"], 400)

    def test_manual_facebook_and_x_are_posts_not_standalone_shortlinks(self):
        for platform in ("facebook", "x"):
            with self.subTest(platform=platform):
                c, m, _, _ = restrict([link(platform, manual=True, published=False)], [metric(platform)], DAY)
                self.assertEqual(c, [])
                self.assertEqual(m, [])

    def test_future_publication_does_not_authorize_previous_day_effects(self):
        c, m, _, _ = restrict([link(date="2026-10-21")], [metric()], DAY)
        self.assertEqual((c, m), ([], []))

    def test_duplicate_identity_uses_confirmed_fact_without_double_counting(self):
        pending, published = link(published=False), link()
        c, m, _, _ = restrict([pending, published], [metric()], DAY)
        self.assertEqual(len(c), 1)
        self.assertEqual(m[0]["revenue_cents"], 100)

    def test_global_owner_conflict_is_not_resolved_by_scope_filter(self):
        with self.assertRaisesRegex(ReportError, "campaign ID"):
            restrict([link(owner="one"), link(owner="two", published=False)], [metric()], DAY)

    def test_source_proof_is_not_guessed_from_campaign_name(self):
        row = link()
        row.pop("effect_source")
        with self.assertRaises(ReportError):
            restrict([row], [metric()], DAY)
        _, kept, _, _ = restrict([link()], [metric(campaign="completely renamed")], DAY)
        self.assertEqual(kept[0]["revenue_cents"], 100)

    def test_scoped_zero_is_known_only_when_raw_source_date_was_present(self):
        c, m, coverage, audit = restrict([link()], [metric(cid="outside", cents=459351)], DAY)
        params = parameters({"start": [DAY], "end": [DAY], "platform": ["facebook"], "group_by": ["owner,platform"]})
        # Facebook has two independent sites; the second site also has a real source row.
        coverage["facebook:2310"] = {DAY}
        report = summarize(c, m, {"_system_report": True}, params, "snapshot", metric_coverage=coverage)
        report["meta"]["content_scope"] = SCOPE
        daily = present(report, DAY)
        require_complete(daily)
        self.assertEqual(daily["totals"]["revenue_cents"], 0)
        self.assertEqual(daily["totals"]["published_count"], 0)
        text = json.dumps(build_cards(daily, "chat"), ensure_ascii=False)
        self.assertNotIn("4,593.51", text)
        self.assertNotIn("未匹配发布记录", text)
        self.assertIn("手动短链", text)
        missing = summarize(c, [], {"_system_report": True}, params, "snapshot")
        with self.assertRaises(ValueError):
            require_complete(present(missing, DAY))

    def test_raw_facebook_query_preserves_source_id_dimensions(self):
        with patch("features.social_analytics.routes.query_source_rows", return_value=[metric()]) as query:
            rows, _ = facebook_source({}, DAY, DAY, "2049")
        self.assertIn("BINARY adset_id,BINARY ad_id", query.call_args.args[1])
        self.assertEqual(rows[0]["adset_id"], "page")


if __name__ == "__main__":
    unittest.main()
