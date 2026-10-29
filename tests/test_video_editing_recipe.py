import copy
import unittest

from features.drama_synthesis.core import DramaSynthesisError
from features.video_editing.recipe import (LEGACY_PROFILE, PROFILE, validate_recipe, recipe_fingerprint,
    payload_fingerprint, output_frames, ms_to_frame)


def fixture():
    return {"version": 1, "profile": PROFILE, "sources": [{"source_id": "a", "kind": "asset",
        "url": "https://bucket.cos.ap-hongkong.myqcloud.com/video-editing/a.mp4?q-signature=secret",
        "identity": {"bucket": "bucket", "key": "video-editing/a.mp4", "size_bytes": 100, "etag": "123", "sha256": "a" * 64},
        "duration_ms": 10000, "width": 1920, "height": 1080, "has_audio": True}],
        "clips": [{"id": "c1", "source_id": "a", "start_ms": 1000, "end_ms": 2000,
                   "fit": "contain", "mute": False, "transition": "cut"}]}


class RecipeTests(unittest.TestCase):
    def test_legacy_profile_keeps_existing_hash_and_compact_profile_is_distinct(self):
        legacy = fixture(); legacy["profile"] = LEGACY_PROFILE
        compact = copy.deepcopy(legacy); compact["profile"] = PROFILE
        self.assertEqual(validate_recipe(legacy)["profile"], LEGACY_PROFILE)
        self.assertEqual(validate_recipe(compact)["profile"], PROFILE)
        self.assertEqual(recipe_fingerprint(legacy), "26ce1f5a98d1ff3cc2ea672950b9b9bfce9d327def43c03d46aefb026d31cef3")
        payload = {"job_id": "ve_" + "a" * 32, "task_type": "video_editing_v1", "operation": "render", "recipe": legacy}
        self.assertEqual(payload_fingerprint(payload), "d9690eb2c5c03a0948e20ecbb962965f5ab41e840c79eba15a42acd336490767")
        self.assertNotEqual(recipe_fingerprint(legacy), recipe_fingerprint(compact))
        for profile in (LEGACY_PROFILE, PROFILE):
            probe = fixture(); probe.update(profile=profile, clips=[])
            self.assertEqual(validate_recipe(probe, "probe")["profile"], profile)
        for profile in ("video-editing-720-v3", None, []):
            invalid = fixture(); invalid["profile"] = profile
            with self.assertRaises(DramaSynthesisError): validate_recipe(invalid)

    def test_cutting_and_frame_rounding_are_deterministic(self):
        value = validate_recipe(fixture())
        self.assertEqual((value["clips"][0]["start_frame"], output_frames(value)), (30, 30))
        self.assertEqual(ms_to_frame(50), 2)
        self.assertEqual(value, validate_recipe(value))

    def test_signed_url_rotation_does_not_change_identity(self):
        value = fixture(); other = copy.deepcopy(value)
        other["sources"][0]["url"] = other["sources"][0]["url"].replace("secret", "rotated")
        self.assertEqual(recipe_fingerprint(value), recipe_fingerprint(other))
        other["sources"][0]["identity"]["etag"] = "changed"
        self.assertNotEqual(recipe_fingerprint(value), recipe_fingerprint(other))

    def test_episode_locator_and_business_query_stay_bound(self):
        value = fixture(); source = value["sources"][0]
        source["kind"] = "episode"; source["identity"].pop("bucket"); source["identity"].pop("key")
        original = recipe_fingerprint(value)
        source["url"] += "&episode=2"
        self.assertNotEqual(original, recipe_fingerprint(value))
        original = recipe_fingerprint(value); source["url"] = source["url"].replace("a.mp4", "b.mp4")
        self.assertNotEqual(original, recipe_fingerprint(value))

    def test_every_render_choice_changes_hash(self):
        original = fixture()
        for field, changed in (("mute", True), ("fit", "cover"), ("start_ms", 1200), ("end_ms", 2300)):
            value = copy.deepcopy(original); value["clips"][0][field] = changed
            self.assertNotEqual(recipe_fingerprint(original), recipe_fingerprint(value))

    def test_fixed_fade_overlap_and_short_clip_rejection(self):
        value = fixture(); value["clips"][0]["transition"] = "fade"
        value["clips"].append({**value["clips"][0], "id": "c2", "transition": "cut"})
        self.assertEqual(output_frames(validate_recipe(value)), 51)
        value["clips"][0]["end_ms"] = 1200
        with self.assertRaises(DramaSynthesisError) as caught:
            validate_recipe(value)
        self.assertEqual(caught.exception.code, "video_editing_transition_too_long")

    def test_two_sided_fade_budget_and_last_clip(self):
        value = fixture(); value["clips"] *= 0
        for n in range(3):
            value["clips"].append({"id": str(n), "source_id": "a", "start_ms": 0, "end_ms": 500, "transition": "fade" if n < 2 else "cut"})
        with self.assertRaises(DramaSynthesisError): validate_recipe(value)
        value["clips"][1]["end_ms"] = 600
        validate_recipe(value)
        value["clips"][-1]["transition"] = "fade"
        with self.assertRaises(DramaSynthesisError): validate_recipe(value)

    def test_mutable_source_without_identity_is_rejected(self):
        value = fixture(); value["sources"][0]["identity"] = {"bucket": "bucket", "key": "file", "size_bytes": 100}
        with self.assertRaises(DramaSynthesisError): validate_recipe(value)

    def test_probe_accepts_pre_metadata_but_render_does_not(self):
        value = fixture(); value["clips"] = []
        for key in ("duration_ms", "width", "height", "has_audio"):
            value["sources"][0].pop(key)
        validate_recipe(value, "probe")
        with self.assertRaises(DramaSynthesisError): validate_recipe(value, "render")

    def test_invalid_ranges_booleans_and_url_protocols(self):
        for key, bad in (("start_ms", True), ("end_ms", 10001), ("end_ms", 999), ("start_ms", float("nan"))):
            value = fixture(); value["clips"][0][key] = bad
            with self.assertRaises(DramaSynthesisError): validate_recipe(value)
        for url in ("file:///etc/passwd", "http://127.0.0.1/a", "https://user:password@source/a", "https://source:444/a"):
            value = fixture(); value["sources"][0]["url"] = url
            with self.assertRaises(DramaSynthesisError): validate_recipe(value)

    def test_more_than_four_hours_is_not_a_publishing_limit(self):
        value = fixture(); value["sources"][0]["duration_ms"] = 20_000_000
        value["clips"][0]["end_ms"] = 18_000_000
        validate_recipe(value)


if __name__ == "__main__": unittest.main()
