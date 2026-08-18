import unittest

from features.fb_auto_posts.validation import ValidationError, normalize_template_payload


def payload():
    rule = {"spend_min": None, "spend_max": None, "roas_min": None, "roas_max": None, "sort_by": "roas", "sort_direction": "desc"}
    return {"name": "English FB", "group_ids": ["6"], "language": "english", "message_template": "{{drama_name}} · {{material_name}} · {{content_id}}", "video_template": "random_overlay", "material_data_source": 6, "metric_window_days": 7, "drama_launch_window_days": 0, "cooldown_days": 30, "drama_rule": {**rule, "resource_type_v2": []}, "material_rule": {**rule, "duration_min_seconds": 1, "duration_max_seconds": 600}, "schedule": {"mode": "fixed", "times": ["10:30"]}}


class ValidationTests(unittest.TestCase):
    def test_video_only_and_fb_default_source_are_frozen(self):
        result = normalize_template_payload(payload())
        self.assertEqual(result["material_type"], "video")
        self.assertEqual(result["material_data_source"], 6)
        self.assertEqual(result["video_template"], "random_overlay")
        self.assertEqual(result["language"], "en")

    def test_bcp47_language_code_is_preserved(self):
        raw=payload(); raw["language"]="zh-tw"
        self.assertEqual(normalize_template_payload(raw)["language"],"zh-tw")

    def test_video_template_is_strict_and_required(self):
        for value in (None, "", "legacy"):
            raw = payload()
            if value is None: raw.pop("video_template")
            else: raw["video_template"] = value
            with self.assertRaises(ValidationError) as caught: normalize_template_payload(raw)
            self.assertEqual(caught.exception.code, "fb_auto_video_template_required")
            self.assertEqual(caught.exception.status, 409)

    def test_unknown_or_incomplete_macro_is_rejected(self):
        for text in ("{{url}}", "{{drama_name}"):
            value = payload(); value["message_template"] = text
            with self.assertRaises(ValidationError): normalize_template_payload(value)

    def test_random_window_must_support_hour_spacing(self):
        value = payload(); value["schedule"] = {"mode": "random", "daily_count": 3, "start": "08:00", "end": "09:00"}
        with self.assertRaises(ValidationError): normalize_template_payload(value)

    def test_random_window_accounts_for_whole_hour_exclusion(self):
        value = payload(); value["schedule"] = {"mode": "random", "daily_count": 2, "start": "00:00", "end": "01:00"}
        with self.assertRaises(ValidationError): normalize_template_payload(value)


if __name__ == "__main__": unittest.main()
