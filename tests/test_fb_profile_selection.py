"""Page portrait admission, deterministic two-level weights and read isolation."""
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from features.fb_auto_posts.profile_selection import (
    Decision, HIERARCHY_VERSION, TAXONOMY_VERSION,
    load_classifications, load_profiles,
)


VERSION = "20261112T010203123456Z"
NOW = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)


def node(key, income, children=None, **values):
    return dict(key=key, revenue_cents=income, share_bp=10000, matchable=True,
                children=children or [], **values)


def snapshot(page_id="123", **meta):
    portrait = dict(
        page_id=page_id, status="ready", revenue_cents=10000,
        hierarchy_version=HIERARCHY_VERSION,
        audiences=[
            node("female", 7000, [node("sweet_romance", 5600), node("ceo_wealth", 1400)]),
            node("male", 3000, [node("revenge", 3000)]),
        ],
    )
    metadata = dict(version=VERSION, generated_at="2026-11-12T01:02:03+00:00",
                    start="2026-11-05", end="2026-11-11", taxonomy_version=TAXONOMY_VERSION,
                    hierarchy_version=HIERARCHY_VERSION, **meta)
    return {"pages": {page_id: portrait}, "errors": {}}, metadata


def classification(audience="female", themes=None):
    return dict(audience=audience, themes=themes or ["sweet_romance"], raw_labels=["Romance"])


class DecisionTests(unittest.TestCase):
    def test_page_and_identity_replay_is_stable(self):
        first = Decision("123", ["template", 8, "slot", 2], snapshot())
        second = Decision("123", ["template", 8, "slot", 2], snapshot())
        self.assertEqual(first.orders, second.orders)
        self.assertEqual(first.audit(classification()), second.audit(classification()))
        self.assertEqual(first.audit(classification())["page_id"], "123")
        self.assertNotIn("x_user_id", first.audit(classification()))

    def test_income_distribution_and_conditional_theme_weights(self):
        female = romance = 0
        for i in range(4000):
            path = Decision("123", ["slot", i], snapshot()).preferred()
            if path[0] == "female":
                female += 1
                romance += path[1] == "sweet_romance"
        self.assertLess(abs(female / 4000 - .7), .035)
        self.assertLess(abs(romance / female - .8), .04)

    def test_rank_keeps_audience_above_a_matching_theme_elsewhere(self):
        decision = Decision("123", "slot", snapshot())
        first, second = decision.audience_order
        first_theme = decision.orders[(first,)][0]
        second_theme = decision.orders[(second,)][0]
        self.assertLess(decision.rank(classification(first, ["__unknown__"])),
                        decision.rank(classification(second, [second_theme])))
        self.assertLess(decision.rank(classification(first, [first_theme])),
                        decision.rank(classification(first, ["__unknown__"])))

    def test_multiple_theme_matches_use_best_conditional_rank(self):
        decision = Decision("123", "slot", snapshot())
        themes = decision.orders[("female",)]
        self.assertEqual(decision.rank(classification("female", themes)),
                         decision.rank(classification("female", [themes[0]])))

    def test_missing_theme_income_retains_audience_preference(self):
        data, meta = snapshot()
        data["pages"]["123"]["audiences"][0]["children"] = []
        decision = Decision("123", "slot", (data, meta))
        self.assertFalse(decision.reason)
        self.assertEqual(decision.rank(classification("female"))[1], 0)
        self.assertEqual(decision.rank(classification("female", ["revenge"]))[1], 0)

    def test_signed_adjustment_cannot_drive_positive_preferences(self):
        data, meta = snapshot()
        data["pages"]["123"]["status"] = "signed_adjustment"
        decision = Decision("123", "slot", (data, meta))
        self.assertEqual(decision.reason, "signed_income_adjustment")
        self.assertEqual(decision.rank(classification()), (0, 0))
        self.assertEqual(decision.preferred(), [])

    def test_unavailable_empty_and_zero_income_are_distinct(self):
        cases = [(({"pages": {}}, {"unavailable": True}), "portrait_unavailable"),
                 (({"pages": {}}, {}), "no_revenue_history")]
        data, meta = snapshot()
        data["pages"]["123"].update(revenue_cents=0, status="no_income")
        cases.append(((data, meta), "no_positive_revenue"))
        for value, reason in cases:
            with self.subTest(reason=reason):
                decision = Decision("123", "slot", value)
                self.assertEqual(decision.reason, reason)
                self.assertEqual(decision.rank(classification()), (0, 0))
                self.assertEqual(decision.audit(classification())["fallback_reason"], reason)

    def test_version_mismatch_and_incomplete_data_degrade_explicitly(self):
        for field, value, reason in [
            ("taxonomy_version", "old", "portrait_mapping_outdated"),
            ("hierarchy_version", "old", "portrait_hierarchy_outdated"),
            ("missing_dates", ["2026-11-07"], "portrait_incomplete"),
        ]:
            data, meta = snapshot()
            meta[field] = value
            self.assertEqual(Decision("123", "slot", (data, meta)).reason, reason)

    def test_unknown_negative_and_unreliable_shares_are_not_preferences(self):
        for audiences in [
            [node("__unknown__", 10000)],
            [node("female", -1)],
            [dict(node("female", 10000), share_bp=None)],
        ]:
            data, meta = snapshot()
            data["pages"]["123"]["audiences"] = audiences
            decision = Decision("123", "slot", (data, meta))
            self.assertEqual(decision.reason, "no_valid_audience_profile")
            self.assertEqual(decision.preferred(), [])

    def test_bad_page_payload_is_local_and_cannot_break_selection(self):
        data, meta = snapshot()
        data["pages"]["123"]["revenue_cents"] = "not-cents"
        self.assertEqual(Decision("123", "slot", (data, meta)).reason, "portrait_invalid")
        self.assertEqual(Decision("123", "slot", None).reason, "portrait_invalid")

    def test_audit_records_version_window_stale_and_material_fallback(self):
        decision = Decision("123", "slot", snapshot(stale=True))
        audit = decision.audit(classification(), fallback="candidate_metadata_unavailable")
        self.assertEqual(audit["profile_version"], VERSION)
        self.assertEqual(audit["source_window"], {"start": "2026-11-05", "end": "2026-11-11"})
        self.assertTrue(audit["profile_stale"])
        self.assertEqual(audit["fallback_reason"], "candidate_metadata_unavailable")
        self.assertTrue(audit["weights"])
        self.assertTrue(audit["conditional_weights"])
        self.assertEqual(json.loads(json.dumps(audit)), audit)

    def test_audit_distinguishes_theme_and_audience_shortages(self):
        decision = Decision("123", "slot", snapshot())
        audience, theme = decision.preferred()
        other = next(key for key in decision.audience_order if key != audience)
        self.assertEqual(decision.audit(classification(audience, [theme]))["fallback_reason"], "")
        self.assertEqual(decision.audit(classification(audience, ["__unknown__"]))["fallback_level"], "theme")
        self.assertEqual(decision.audit(classification(other))["fallback_level"], "audience")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def publish(self, pages=None, meta=None):
        data, default_meta = snapshot()
        pages = data["pages"] if pages is None else pages
        meta = default_meta if meta is None else meta
        path = self.root / (VERSION + ".sqlite3")
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("CREATE TABLE profiles(page_id TEXT PRIMARY KEY,summary TEXT NOT NULL,tree TEXT NOT NULL);"
                             "CREATE TABLE metadata(payload TEXT);")
            for page_id, portrait in pages.items():
                payload = portrait if isinstance(portrait, str) else json.dumps(portrait)
                db.execute("INSERT INTO profiles VALUES(?,?,?)", (page_id, "{}", payload))
            db.execute("INSERT INTO metadata VALUES(?)", (json.dumps(meta),))
        (self.root / "current.json").write_text(json.dumps(dict(version=VERSION, database=path.name)), encoding="utf-8")
        return path

    def test_reads_tree_only_for_requested_pages_without_modification(self):
        path = self.publish()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        data, meta = load_profiles(["123", "999", "123"], root=self.root, now=NOW)
        self.assertEqual(set(data["pages"]), {"123"})
        self.assertEqual(meta["version"], VERSION)
        self.assertFalse(meta["stale"])
        self.assertTrue(data["pages"]["123"]["audiences"][0]["children"])
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual({p.name for p in self.root.iterdir()}, {"current.json", path.name})

    def test_missing_root_does_not_create_any_file_or_directory(self):
        root = self.root / "absent"
        data, meta = load_profiles(["123"], root=root)
        self.assertEqual(data["pages"], {})
        self.assertTrue(meta["unavailable"])
        self.assertFalse(root.exists())

    def test_missing_database_is_never_created(self):
        path = self.publish()
        path.unlink()
        self.assertTrue(load_profiles(["123"], root=self.root)[1]["unavailable"])
        self.assertFalse(path.exists())

    def test_bounded_batches_handle_more_than_sqlite_legacy_variable_limit(self):
        pages = {str(i): snapshot(str(i))[0]["pages"][str(i)] for i in range(1, 1102)}
        self.publish(pages)
        data, meta = load_profiles(pages, root=self.root, now=NOW)
        self.assertEqual(len(data["pages"]), 1101)
        self.assertEqual(meta["version"], VERSION)

    def test_malformed_page_does_not_hide_valid_siblings(self):
        data, _ = snapshot()
        pages = {**data["pages"], "456": "{broken"}
        self.publish(pages)
        loaded = load_profiles(["123", "456"], root=self.root, now=NOW)
        self.assertFalse(Decision("123", "slot", loaded).reason)
        self.assertEqual(Decision("456", "slot", loaded).reason, "portrait_invalid")

    def test_stale_good_snapshot_remains_usable_and_marked(self):
        self.publish()
        loaded = load_profiles(["123"], root=self.root, now=NOW.replace(hour=9))
        self.assertTrue(loaded[1]["stale"])
        self.assertFalse(Decision("123", "slot", loaded).reason)

    def test_manifest_cannot_select_another_path_or_mismatched_generation(self):
        self.publish()
        current = self.root / "current.json"
        current.write_text(json.dumps(dict(version=VERSION, database="../other.sqlite3")), encoding="utf-8")
        self.assertTrue(load_profiles(["123"], root=self.root)[1]["unavailable"])
        current.write_text(json.dumps(dict(version="../other", database="../other.sqlite3")), encoding="utf-8")
        self.assertTrue(load_profiles(["123"], root=self.root)[1]["unavailable"])

    def test_metadata_must_match_pinned_manifest_generation(self):
        _, meta = snapshot()
        meta["version"] = "20261111T010203123456Z"
        self.publish(meta=meta)
        self.assertTrue(load_profiles(["123"], root=self.root)[1]["unavailable"])


class CatalogTests(unittest.TestCase):
    def test_same_shared_product_language_and_labels_are_used(self):
        class MySQL:
            schema = "catalog"

            def __init__(self):
                self.calls = []

            def select(self, sql, params):
                self.calls.append((sql, params))
                base = dict(content_id="drama", app_id=1479, language="jp", series_code="s")
                return [dict(base, labels="female,Romance,CEO")] if "ads_drama_resource" in sql else [dict(base, resource_type=2)]

        mysql = MySQL()
        result = load_classifications(mysql, ["drama", "drama"])
        self.assertEqual(set(result), {("drama", "ja")})
        self.assertEqual(result[("drama", "ja")]["audience"], "female")
        self.assertEqual(len(mysql.calls), 2)
        self.assertTrue(all(params == [1479, "drama"] for _, params in mysql.calls))
        self.assertTrue(all("SELECT" in sql and "FORCE INDEX" in sql for sql, _ in mysql.calls))

    def test_catalog_failure_remains_visible_to_caller(self):
        class BrokenMySQL:
            schema = "catalog"

            def select(self, *args):
                raise RuntimeError("source unavailable")

        with self.assertRaises(RuntimeError):
            load_classifications(BrokenMySQL(), ["drama"])


if __name__ == "__main__":
    unittest.main()
