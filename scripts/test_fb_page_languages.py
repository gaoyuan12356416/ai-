"""Page-language routing contracts; all planning uses a temporary local ledger."""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, StoreError
from features.fb_auto_posts.languages import candidate_snapshots_for_pages, page_language
from features.fb_auto_posts.repositories import CandidateSnapshot, MaterialCandidate, PageTarget
from features.fb_auto_posts.validation import ValidationError, config_hash, normalize_template_payload
from scripts.test_fb_auto_validation import payload
from scripts.test_fb_auto_v2 import Clock


def page(page_id, language, token_count=1):
    return PageTarget("6", ("6",), str(page_id), "248", "UTC", language, token_count)


def material(material_id, language):
    return MaterialCandidate(
        str(material_id), "drama-" + language,
        f"https://cdn.example/{language}/{material_id}.mp4",
        "Material " + language, "Drama " + language, language,
        Decimal("30"), Decimal("1"), Decimal("100"),
        Decimal("1"), Decimal("100"), "1", "Description " + language,
    )


class Pages:
    def __init__(self, rows):
        self.rows = rows

    def legacy_conflicts(self, _groups):
        return []

    def list_pages(self, *_args, **_kwargs):
        return list(self.rows)


class Materials:
    def __init__(self, catalog=None):
        self.catalog = catalog if catalog is not None else {
            "en": (material("101", "en"),),
            "es": (material("201", "es"),),
        }
        self.calls = []
        self.choose_calls = []

    def candidate_snapshot(self, config):
        self.calls.append(copy.deepcopy(config))
        language = config["language"]
        generation = {"en": 11, "es": 12}.get(language, 13)
        return CandidateSnapshot(tuple(self.catalog.get(language, ())), (generation,), ("2026-08-17",))

    def choose_from(self, items, excluded):
        self.choose_calls.append((tuple(item.material_id for item in items), set(excluded)))
        return next((item for item in items if item.material_id not in excluded), None)


class PageLanguageHelperTests(unittest.TestCase):
    def test_aliases_case_whitespace_and_region_separators(self):
        examples = {
            " English ": "en", "SPANISH": "es", "Filipino": "tl",
            "Portuguese": "pt", " EN_us ": "en-us", "zh_Hant_TW": "zh-hant-tw",
            "pt-br": "pt-br", "ar": "ar",
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(page_language(value), expected)

    def test_empty_or_malformed_languages_have_no_default(self):
        for value in (None, "", " \t ", "en US", "en!", "zh--cn", "a", "abcdefghi", "en-us-x-y-z"):
            with self.subTest(value=value):
                self.assertEqual(page_language(value), "")

    def test_unique_page_languages_query_once_and_preserve_selection_rules(self):
        config = {"language": "fr", "material_data_source": 6, "drama_rule": {"sort_by": "roas"}}
        before = copy.deepcopy(config)
        materials = Materials()
        snapshots = candidate_snapshots_for_pages(
            config, [page(1, "en"), page(2, "English"), page(3, "ES"), page(4, "")], materials,
        )
        self.assertEqual(set(snapshots), {"en", "es"})
        self.assertCountEqual([call["language"] for call in materials.calls], ["en", "es"])
        self.assertTrue(all(call["drama_rule"] == config["drama_rule"] for call in materials.calls))
        self.assertEqual(config, before)
        self.assertEqual(snapshots["en"].metric_generation_ids, (11,))
        self.assertEqual(snapshots["es"].metric_dates, ("2026-08-17",))

    def test_empty_page_languages_never_query_legacy_template_language(self):
        materials = Materials()
        result = candidate_snapshots_for_pages({"language": "en"}, [page(1, ""), page(2, "bad language")], materials)
        self.assertEqual(result, {})
        self.assertEqual(materials.calls, [])

    def test_wrong_language_adapter_results_are_discarded(self):
        materials = Materials({"en": (material("101", "english"), material("201", "es"))})
        result = candidate_snapshots_for_pages({}, [page(1, "en")], materials)
        self.assertEqual([item.material_id for item in result["en"].candidates], ["101"])

    def test_snapshot_failure_propagates_without_template_fallback(self):
        class UnavailableMaterials(Materials):
            def candidate_snapshot(self, config):
                self.calls.append(config["language"])
                raise RuntimeError("catalog unavailable")

        materials = UnavailableMaterials()
        with self.assertRaisesRegex(RuntimeError, "catalog unavailable"):
            candidate_snapshots_for_pages({"language": "en"}, [page(1, "es")], materials)
        self.assertEqual(materials.calls, ["es"])


class TemplateLanguageCompatibilityTests(unittest.TestCase):
    def test_new_payload_needs_no_language_and_does_not_save_one(self):
        raw = payload()
        raw.pop("language", None)
        normalized = normalize_template_payload(raw)
        self.assertNotIn("language", normalized)
        self.assertEqual(normalized["material_data_source"], 6)
        self.assertEqual(normalized["video_template"], "random_overlay")

    def test_legacy_payload_language_is_accepted_and_ignored(self):
        raw = payload()
        raw["language"] = "Spanish"
        self.assertNotIn("language", normalize_template_payload(raw))

    def test_public_template_cannot_override_planner_candidate_limit(self):
        raw = payload()
        raw["_candidate_limit"] = 1
        with self.assertRaises(ValidationError) as caught:
            normalize_template_payload(raw)
        self.assertEqual(caught.exception.code, "invalid_request")


class PageLanguagePlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc))
        self.store = FBAutoPostStore(Path(self.tmp.name) / "state.sqlite3", now_fn=self.clock)
        self.actor = ActorScope("u", "QA", False, "248")
        raw = payload()
        raw["message_template"] = "{{drama_name}} | {{desc}} | {{url}}"
        self.template = self.store.create_template(raw, self.actor, {"app_id": "1479", "product": "Dramawave"})

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, pages, materials, slot="manual:language", hours_ahead=1):
        return self.store.create_run(
            self.template["id"], slot, "manual", self.actor, pages, materials,
            planned_publish_at_utc=(self.clock() + timedelta(hours=hours_ahead)).isoformat(timespec="seconds"),
        )

    def tasks(self, run_id):
        with self.store.connect() as conn:
            return {row["page_id"]: dict(row) for row in conn.execute("SELECT * FROM fb_auto_task WHERE run_id=? ORDER BY page_id", (run_id,))}

    def snapshots(self, run_id):
        with self.store.connect() as conn:
            return {row["page_id"]: dict(row) for row in conn.execute("SELECT * FROM fb_auto_run_page WHERE run_id=? ORDER BY page_id", (run_id,))}

    def test_mixed_pages_select_matching_drama_media_description_and_freeze_language(self):
        materials = Materials()
        result = self.plan(Pages([page(10001, "English"), page(10002, "es"), page(10003, "EN")]), materials)
        tasks, snapshots = self.tasks(result["run_id"]), self.snapshots(result["run_id"])
        self.assertEqual(result["summary"]["queued_tasks"], 3)
        self.assertCountEqual([call["language"] for call in materials.calls], ["en", "es"])
        for page_id, language, material_id in (("10001", "en", "101"), ("10002", "es", "201"), ("10003", "en", "101")):
            with self.subTest(page_id=page_id):
                task = tasks[page_id]
                self.assertEqual((task["material_id"], task["content_id"], task["status"]), (material_id, "drama-" + language, "planned"))
                self.assertIn(f"/{language}/{material_id}.mp4", task["source_media_url"])
                self.assertEqual(task["message_text"], f"Drama {language} | Description {language} | {task['short_url']}")
                self.assertEqual(snapshots[page_id]["language"], language)
        with self.store.connect() as conn:
            row = conn.execute("SELECT metric_generation_ids_json FROM fb_auto_run WHERE id=?", (result["run_id"],)).fetchone()
        self.assertEqual(json.loads(row[0]), [11, 12])

    def test_legacy_stored_template_language_cannot_override_page_language(self):
        legacy = {**self.template["config"], "language": "fr"}
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_template_version SET config_json=?,config_sha256=? WHERE template_id=? AND version=?", (json.dumps(legacy), config_hash(legacy), self.template["id"], self.template["version"]))
        materials = Materials()
        result = self.plan(Pages([page(10001, "es")]), materials)
        self.assertEqual([call["language"] for call in materials.calls], ["es"])
        self.assertEqual(self.tasks(result["run_id"])["10001"]["content_id"], "drama-es")
        self.assertEqual(self.store.get_template(self.template["id"], self.actor)["config"]["language"], "fr")

    def test_blank_and_invalid_language_skip_only_affected_pages(self):
        materials = Materials()
        result = self.plan(Pages([page(10001, "en"), page(10002, ""), page(10003, "bad language")]), materials)
        tasks, snapshots = self.tasks(result["run_id"]), self.snapshots(result["run_id"])
        self.assertEqual((result["summary"]["queued_tasks"], result["summary"]["skipped_tasks"]), (1, 2))
        for page_id in ("10002", "10003"):
            self.assertEqual((tasks[page_id]["status"], tasks[page_id]["skip_reason"]), ("skipped", "fb_auto_page_language_missing"))
            self.assertEqual((tasks[page_id]["material_id"], tasks[page_id]["source_media_url"]), ("", ""))
            self.assertEqual(snapshots[page_id]["skip_reason"], "fb_auto_page_language_missing")
        self.assertEqual(len(materials.choose_calls), 1)

    def test_all_missing_languages_complete_as_skipped_without_catalog_reads(self):
        materials = Materials()
        result = self.plan(Pages([page(10001, ""), page(10002, "bad language")]), materials)
        self.assertEqual((result["summary"]["queued_tasks"], result["summary"]["skipped_tasks"]), (0, 2))
        self.assertEqual(materials.calls, [])
        self.assertEqual(self.store.get_run(result["run_id"], self.actor)["run"]["status"], "completed")

    def test_no_media_in_page_language_never_borrows_other_language(self):
        materials = Materials({"en": (material("101", "en"),), "es": ()})
        result = self.plan(Pages([page(10001, "en"), page(10002, "es")]), materials)
        tasks = self.tasks(result["run_id"])
        self.assertEqual(tasks["10001"]["status"], "planned")
        self.assertEqual((tasks["10002"]["status"], tasks["10002"]["skip_reason"], tasks["10002"]["material_id"]), ("skipped", "fb_auto_no_eligible_video", ""))

    def test_missing_token_still_skips_page_with_valid_language(self):
        materials = Materials()
        result = self.plan(Pages([page(10001, "es", 0)]), materials)
        self.assertEqual(self.tasks(result["run_id"])["10001"]["skip_reason"], "fb_page_missing_eligible_token")
        self.assertEqual(materials.choose_calls, [])

    def test_new_language_during_catalog_read_defers_without_partial_ledger(self):
        class ChangedPages(Pages):
            def __init__(self):
                self.reads = 0

            def list_pages(self, *_args, **_kwargs):
                self.reads += 1
                return [page(10001, "en" if self.reads == 1 else "es")]

        with self.assertRaises(StoreError) as caught:
            self.plan(ChangedPages(), Materials())
        self.assertEqual(caught.exception.code, "fb_auto_page_language_changed")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_run").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_task").fetchone()[0], 0)

    def test_page_language_becoming_blank_is_skipped_without_stale_choice(self):
        class ClearedPages(Pages):
            def __init__(self):
                self.reads = 0

            def list_pages(self, *_args, **_kwargs):
                self.reads += 1
                return [page(10001, "en" if self.reads == 1 else "")]

        materials = Materials()
        result = self.plan(ClearedPages(), materials)
        self.assertEqual(self.tasks(result["run_id"])["10001"]["skip_reason"], "fb_auto_page_language_missing")
        self.assertEqual(materials.choose_calls, [])

    def test_future_run_uses_current_language_and_preserves_earlier_frozen_rows(self):
        pages, materials = Pages([page(10001, "en")]), Materials()
        first = self.plan(pages, materials, "manual:first", hours_ahead=1)
        first_tasks, first_snapshots = self.tasks(first["run_id"]), self.snapshots(first["run_id"])
        pages.rows = [page(10001, "es")]
        second = self.plan(pages, materials, "manual:second", hours_ahead=2)
        self.assertEqual(self.tasks(second["run_id"])["10001"]["content_id"], "drama-es")
        self.assertEqual(self.tasks(first["run_id"]), first_tasks)
        self.assertEqual(self.snapshots(first["run_id"]), first_snapshots)

    def test_repeated_slot_keeps_frozen_choice_and_does_not_query_catalog_again(self):
        pages, materials = Pages([page(10001, "en")]), Materials()
        first = self.plan(pages, materials)
        before = self.tasks(first["run_id"])
        pages.rows = [page(10001, "es")]
        repeated = self.plan(pages, materials)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["run_id"], first["run_id"])
        self.assertEqual(len(materials.calls), 1)
        self.assertEqual(self.tasks(first["run_id"]), before)

    def test_cooldown_exhaustion_does_not_fall_back_to_another_language(self):
        pages, materials = Pages([page(10001, "en"), page(10002, "es")]), Materials()
        self.plan(pages, materials, "manual:first", hours_ahead=1)
        second = self.plan(pages, materials, "manual:second", hours_ahead=2)
        tasks = self.tasks(second["run_id"])
        self.assertEqual({task["skip_reason"] for task in tasks.values()}, {"fb_auto_no_eligible_video"})
        self.assertTrue(all(task["material_id"] == "" for task in tasks.values()))


if __name__ == "__main__":
    unittest.main()
