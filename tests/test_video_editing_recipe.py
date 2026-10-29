import copy
import unittest

from features.drama_synthesis.core import DramaSynthesisError
from features.video_editing.recipe import (LEGACY_PROFILE, PROFILE, validate_recipe, recipe_fingerprint,
    payload_fingerprint, output_frames, ms_to_frame, preview_geometry)


def fixture():
    return {"version": 1, "profile": PROFILE, "sources": [{"source_id": "a", "kind": "asset",
        "url": "https://bucket.cos.ap-hongkong.myqcloud.com/video-editing/a.mp4?q-signature=secret",
        "identity": {"bucket": "bucket", "key": "video-editing/a.mp4", "size_bytes": 100, "etag": "123", "sha256": "a" * 64},
        "duration_ms": 10000, "width": 1920, "height": 1080, "has_audio": True}],
        "clips": [{"id": "c1", "source_id": "a", "start_ms": 1000, "end_ms": 2000,
                   "fit": "contain", "mute": False, "transition": "cut"}]}


class RecipeTests(unittest.TestCase):
    def test_legacy_fingerprints_remain_exact(self):
        value = fixture(); value["profile"] = LEGACY_PROFILE
        self.assertEqual(recipe_fingerprint(value), "26ce1f5a98d1ff3cc2ea672950b9b9bfce9d327def43c03d46aefb026d31cef3")
        payload = {"job_id": "ve_" + "a" * 32, "task_type": "video_editing_v1", "operation": "render", "recipe": value}
        self.assertEqual(payload_fingerprint(payload), "d9690eb2c5c03a0948e20ecbb962965f5ab41e840c79eba15a42acd336490767")
        self.assertNotIn("mirror", validate_recipe(value)["clips"][0])
        self.assertNotIn("subtitle_band", validate_recipe(value)["clips"][0])

    def test_live_compact_fingerprints_and_both_profile_version_combinations(self):
        value = fixture()
        self.assertEqual(recipe_fingerprint(value), "910e935fb51bd9a054acb2a19bf36dd02d9ad4c005853e2b362af4eb705c8979")
        payload = {"job_id": "ve_" + "a" * 32, "task_type": "video_editing_v1", "operation": "render", "recipe": value}
        self.assertEqual(payload_fingerprint(payload), "ecb47995714e57a01cdbd92bdb6837b5d1928d032f74e60e6ed0a65b2b8baad9")
        hashes = set()
        for profile in (LEGACY_PROFILE, PROFILE):
            for version in (1, 2):
                with self.subTest(profile=profile, version=version):
                    recipe = fixture(); recipe.update(profile=profile, version=version)
                    if version == 2: recipe["clips"][0]["mirror"] = True
                    normalized = validate_recipe(recipe)
                    self.assertEqual((normalized["profile"], normalized["version"]), (profile, version))
                    self.assertEqual(validate_recipe(normalized), normalized)
                    hashes.add(recipe_fingerprint(recipe))
            probe = fixture(); probe.update(profile=profile, clips=[])
            self.assertEqual(validate_recipe(probe, "probe")["profile"], profile)
        self.assertEqual(len(hashes), 4)
        for profile in ("video-editing-720-v3", None, []):
            invalid = fixture(); invalid["profile"] = profile
            with self.assertRaises(DramaSynthesisError): validate_recipe(invalid)

    def test_v2_mirror_defaults_band_and_preserves_explicit_off(self):
        value = fixture(); value["version"] = 2
        value["clips"][0]["mirror"] = True
        normalized = validate_recipe(value)
        self.assertEqual(normalized["clips"][0]["subtitle_band"], {"top": 960, "bottom": 1280})
        self.assertEqual(validate_recipe(normalized), normalized)
        original_hash = recipe_fingerprint(value)
        value["clips"][0]["subtitle_band"] = None
        self.assertIsNone(validate_recipe(value)["clips"][0]["subtitle_band"])
        self.assertNotEqual(recipe_fingerprint(value), original_hash)
        value["clips"][0].update(mirror=False, subtitle_band={"top": 2, "bottom": 300})
        self.assertEqual(validate_recipe(value)["clips"][0]["subtitle_band"], {"top": 2, "bottom": 300})
        self.assertNotEqual(recipe_fingerprint(value), original_hash)
        payload = {"job_id": "ve_" + "a" * 32, "task_type": "video_editing_v1", "operation": "render", "recipe": value}
        self.assertEqual(payload_fingerprint(payload), payload_fingerprint({**payload, "recipe": validate_recipe(value)}))

    def test_v2_rejects_invalid_effects_and_keeps_probes_v1(self):
        for patch in ({"mirror": 1}, {"mirror": "true"}, {"subtitle_band": {}},
                      {"subtitle_band": {"top": 960, "bottom": 1280, "x": 1}},
                      *({"subtitle_band": {"top": t, "bottom": b}} for t, b in
                        ((-2, 1280), (961, 1280), (960, 1279), (960, 1282), (960, 960), (True, 1280), (0, 0)))):
            with self.subTest(patch=patch):
                value = fixture(); value["version"] = 2; value["clips"][0].update(patch)
                with self.assertRaises(DramaSynthesisError): validate_recipe(value)
        value = fixture(); value.update(version=2, clips=[])
        with self.assertRaises(DramaSynthesisError): validate_recipe(value, "probe")

    def test_preview_geometry_recovers_proxy_content_and_square_pixel_shape(self):
        legacy = {"width": 1920, "height": 1080, "sample_aspect_ratio": "1:1"}
        self.assertEqual(preview_geometry(legacy, proxy=True)["content_rect"],
                         {"x": 0, "y": 436, "width": 720, "height": 406})
        rotated = {"width": 480, "height": 640, "sample_aspect_ratio": "4:3", "rotation": 90}
        self.assertEqual(preview_geometry(rotated)["display_width"], 360)
        self.assertEqual(preview_geometry(rotated, proxy=True)["content_rect"],
                         {"x": 0, "y": 0, "width": 720, "height": 1280})
        self.assertEqual(preview_geometry({"width": 720, "height": 480, "sample_aspect_ratio": "4:3"})["display_width"], 960)
        self.assertIsNone(preview_geometry({}))

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
