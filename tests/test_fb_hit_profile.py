"""Strict reverse portrait matching for FB's explicit hit-drama action."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.fb_auto_posts.hit_profile import (
    MATCH_POLICY, filter_pages, freeze_match, match_reason, profileaudit,
)
from features.fb_auto_posts.profile_selection import (
    Decision, HIERARCHY_VERSION, TAXONOMY_VERSION,
)


def page(pid, lang="en", eligible=1):
    return SimpleNamespace(page_id=str(pid), language=lang, eligible_token_count=eligible)


def portrait(pid, audience="female", themes=None, income=1000, status="ready"):
    themes = {"sweet_romance": income} if themes is None else themes
    return dict(page_id=str(pid), status=status, revenue_cents=income, hierarchy_version=HIERARCHY_VERSION,
                audiences=[dict(key=audience, revenue_cents=income, share_bp=10000, matchable=True,
                                children=[dict(key=key, revenue_cents=value, share_bp=10000, matchable=True)
                                          for key, value in themes.items()])])


def snapshot(*profiles, **meta):
    return ({"pages": {p["page_id"]: p for p in profiles}}, dict(
        version="20261112T040000000000Z", generated_at="2026-11-12T04:00:00+00:00",
        start="2026-11-05", end="2026-11-11", taxonomy_version=TAXONOMY_VERSION,
        hierarchy_version=HIERARCHY_VERSION, **meta))


def classification(audience="female", themes=None, **values):
    return dict(audience=audience, themes=themes or ["sweet_romance"], raw_labels=["female", "sweet"], **values)


def target(**by_language):
    ids = sorted({cid for values in by_language.values() for cid in values})
    return dict(version=2, content_id=ids[0] if len(ids) == 1 else "", content_ids=ids,
                languages=sorted(by_language), by_language=by_language,
                request={"operation_id": "test-0001", "id_type": "resource_id", "drama_id": "123"})


class HitProfileTests(unittest.TestCase):
    def freeze(self, pages, raw_target, profile_snapshot, catalog, **kwargs):
        with patch("features.fb_auto_posts.hit_profile.load_profiles", return_value=profile_snapshot) as reader, \
                patch("features.fb_auto_posts.hit_profile.load_classifications", return_value=catalog) as labels:
            frozen = freeze_match(pages, raw_target, object(), ["template", 1, "operation"], **kwargs)
        return frozen, reader, labels

    def test_only_language_audience_and_positive_theme_match(self):
        pages = [page(1), page(2), page(3), page(4, eligible=0), page(5, "id")]
        profiles = snapshot(portrait(1), portrait(2, themes={"revenge": 1000}),
                            portrait(3, "male", {"revenge": 1000}), portrait(4), portrait(5))
        original = target(en=["A"])
        frozen, reader, labels = self.freeze(pages, original, profiles, {("A", "en"): classification()})
        self.assertEqual(frozen["page_ids"], ["1"])
        self.assertEqual(frozen["page_content_ids"], {"1": ["A"]})
        self.assertEqual(frozen["page_languages"], {"1": "en"})
        self.assertEqual({row["page_id"]: row["reason"] for row in frozen["skipped_pages"]},
                         {"2": "theme_not_matched", "3": "audience_not_matched",
                          "4": "page_not_publishable", "5": "language_not_matched"})
        reader.assert_called_once_with(["1", "2", "3", "4", "5"])
        labels.assert_called_once()
        self.assertEqual(labels.call_args.args[1], ["A"])
        self.assertNotIn("page_ids", original)
        self.assertEqual(frozen["request"], original["request"])

    def test_multilingual_resource_freezes_exact_matching_versions_per_page(self):
        raw_target = target(en=["enA"], id=["idB", "idWrong"])
        catalog = {("enA", "en"): classification(),
                   ("idB", "id"): classification("male", ["revenge"]),
                   ("idWrong", "id"): classification("female", ["sweet_romance"])}
        profiles = snapshot(portrait(1), portrait(2, "male", {"revenge": 1000}))
        frozen, _, _ = self.freeze([page(1), page(2, "id")], raw_target, profiles, catalog)
        self.assertEqual(frozen["page_content_ids"], {"1": ["enA"], "2": ["idB"]})
        self.assertEqual(set(frozen["profiles"]["2"]), {"idB"})
        self.assertEqual(profileaudit("2", "idB", frozen)["classification"]["language"], "id")
        self.assertFalse(profileaudit("2", "idWrong", frozen)["matched"])

    def test_portuguese_alias_does_not_include_portugal_variant(self):
        raw_target = target(pt=["ptA"])
        profiles = snapshot(portrait(1), portrait(2), portrait(3))
        frozen, _, _ = self.freeze([page(1, "pt-br"), page(2, "pt"), page(3, "pt-pt")],
                                  raw_target, profiles, {("ptA", "pt-br"): classification()})
        self.assertEqual(frozen["page_ids"], ["1", "2"])
        self.assertEqual(frozen["page_languages"], {"1": "pt-br", "2": "pt-br"})
        self.assertEqual([p.page_id for p in filter_pages([page(1, "pt"), page(2, "pt_BR")], frozen)], ["1", "2"])
        self.assertEqual(frozen["skipped_pages"][0]["reason"], "language_not_matched")

    def test_japanese_alias_and_legacy_single_target(self):
        raw_target = dict(content_id="jaA", languages=["jp"])
        frozen, _, _ = self.freeze([page(1, "ja")], raw_target, snapshot(portrait(1)),
                                  {("jaA", "ja"): classification()})
        self.assertEqual(frozen["page_content_ids"], {"1": ["jaA"]})

    def test_missing_zero_and_signed_income_skip_without_ordinary_fallback(self):
        profiles = snapshot(portrait(2, income=0, status="no_income"),
                            portrait(3, status="signed_adjustment"))
        frozen, _, _ = self.freeze([page(1), page(2), page(3)], target(en=["A"]), profiles,
                                  {("A", "en"): classification()})
        self.assertEqual(frozen["page_ids"], [])
        self.assertEqual([p["reason"] for p in frozen["skipped_pages"]],
                         ["no_revenue_history", "no_positive_revenue", "signed_income_adjustment"])

    def test_unavailable_profile_never_matches_all_pages(self):
        frozen, _, _ = self.freeze([page(1)], target(en=["A"]), ({"pages": {}}, {"unavailable": True}),
                                  {("A", "en"): classification()})
        self.assertEqual(frozen["page_ids"], [])
        self.assertEqual(frozen["skipped_pages"][0]["reason"], "portrait_unavailable")

    def test_classification_outage_has_explicit_skipped_reason(self):
        with patch("features.fb_auto_posts.hit_profile.load_profiles", return_value=snapshot(portrait(1))), \
                patch("features.fb_auto_posts.hit_profile.load_classifications", side_effect=RuntimeError("unavailable")):
            frozen = freeze_match([page(1)], target(en=["A"]), object(), "operation")
        self.assertEqual(frozen["page_ids"], [])
        self.assertEqual(frozen["skipped_pages"][0]["reason"], "candidate_metadata_unavailable")

    def test_other_product_does_not_consume_dramawave_profiles(self):
        frozen, reader, labels = self.freeze([page(1)], target(en=["A"]), snapshot(portrait(1)), {}, app_id="other")
        reader.assert_not_called()
        labels.assert_not_called()
        self.assertEqual(frozen["page_ids"], [])
        self.assertEqual(frozen["skipped_pages"][0]["reason"], "unsupported_profile_product")

    def test_positive_minor_theme_is_match_without_random_first_preference(self):
        profiles = snapshot(portrait(1, themes={"sweet_romance": 990, "ceo_wealth": 10}))
        metadata = classification(themes=["ceo_wealth"])
        decision = Decision("1", ["hit-drama", "op"], profiles)
        self.assertFalse(match_reason(decision, metadata))
        frozen, _, _ = self.freeze([page(1)], target(en=["A"]), profiles, {("A", "en"): metadata})
        audit = profileaudit("1", "A", frozen)
        self.assertTrue(audit["matched"])
        self.assertFalse(audit["fallback"])
        self.assertEqual(audit["fallback_reason"], "")
        self.assertNotIn("preferred_path", audit)
        self.assertNotIn("conditional_weights", audit)
        self.assertNotIn("weights", audit)
        self.assertEqual(audit["matched_themes"], [{"key": "ceo_wealth", "name": "豪门总裁", "revenue_cents": 10}])

    def test_zero_theme_and_cross_audience_theme_are_not_matches(self):
        p = portrait(1, themes={"sweet_romance": 0, "ceo_wealth": 500}, income=1000)
        p["audiences"].append(dict(key="male", revenue_cents=500, share_bp=5000, matchable=True,
                                    children=[dict(key="revenge", revenue_cents=500, share_bp=10000, matchable=True)]))
        decision = Decision("1", "slot", snapshot(p))
        self.assertEqual(match_reason(decision, classification(themes=["sweet_romance"])), "theme_not_matched")
        self.assertEqual(match_reason(decision, classification(themes=["revenge"])), "theme_not_matched")
        self.assertEqual(match_reason(decision, classification("__unknown__", ["ceo_wealth"])), "audience_not_matched")

    def test_each_selected_content_uses_its_own_compact_audit(self):
        profiles = snapshot(portrait(1, themes={"sweet_romance": 700, "ceo_wealth": 300}), stale=True)
        catalog = {("A", "en"): classification(themes=["sweet_romance"]),
                   ("B", "en"): classification(themes=["ceo_wealth"])}
        frozen, _, _ = self.freeze([page(1)], target(en=["A", "B"]), profiles, catalog)
        audit = profileaudit("1", "B", frozen)
        self.assertEqual(audit["content_id"], "B")
        self.assertEqual(audit["classification"]["content_id"], "B")
        self.assertEqual(audit["selected_path"], ["female", "ceo_wealth"])
        self.assertEqual(audit["source_window"], {"start": "2026-11-05", "end": "2026-11-11"})
        self.assertTrue(audit["profile_stale"])
        self.assertEqual(audit["match_policy"], MATCH_POLICY)
        audit["matched_themes"][0]["revenue_cents"] = -1
        self.assertEqual(frozen["profiles"]["1"]["B"]["matched_themes"][0]["revenue_cents"], 300)
        self.assertFalse(profileaudit("2", "B", frozen)["matched"])

    def test_recheck_excludes_new_pages_and_changed_language(self):
        frozen, _, _ = self.freeze([page(1), page(2, "id")], target(en=["A"], id=["B"]),
                                  snapshot(portrait(1), portrait(2)),
                                  {("A", "en"): classification(), ("B", "id"): classification()})
        current = [page(1), page(2, "en"), page(3)]
        self.assertEqual([p.page_id for p in filter_pages(current, frozen)], ["1"])
        self.assertEqual(frozen["page_ids"], ["1", "2"])

    def test_recheck_rejects_missing_or_mismatched_per_content_evidence(self):
        frozen, _, _ = self.freeze([page(1)], target(en=["A"]), snapshot(portrait(1)),
                                  {("A", "en"): classification()})
        for changed in ("missing", "wrong-language", "wrong-content"):
            copy = deepcopy(frozen)
            if changed == "missing":
                copy["profiles"]["1"] = {}
            elif changed == "wrong-language":
                copy["profiles"]["1"]["A"]["classification"]["language"] = "id"
            else:
                copy["profiles"]["1"]["A"]["content_id"] = "B"
            self.assertEqual(filter_pages([page(1)], copy), [])
        self.assertEqual(filter_pages([page(1)], target(en=["A"])), [])

    def test_missing_classification_skips_without_raising(self):
        frozen, _, _ = self.freeze([page(1)], target(en=["A"]), snapshot(portrait(1)), {})
        self.assertEqual(frozen["page_ids"], [])
        self.assertEqual(frozen["skipped_pages"][0]["reason"], "candidate_metadata_missing")


if __name__ == "__main__":
    unittest.main()
