#!/usr/bin/env python3
"""Focused public-input validation tests for X automatic templates."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_auto_posts.validation import (  # noqa: E402
    ValidationError,
    config_hash,
    normalize_template_payload,
    valid_internal_bearer,
)


def metric_rule(**extra):
    value = {
        "spend_min": None,
        "spend_max": None,
        "roas_min": None,
        "roas_max": None,
        "sort_by": "spend",
        "sort_direction": "desc",
    }
    value.update(extra)
    return value


def valid_payload(**overrides):
    value = {
        "name": "English automatic template",
        "account_ids": ["101", "102"],
        "language": "EN",
        "body_template": "🎬 {{drama_name}}\n{{desc}}\n{{url}}",
        "metric_window_days": 7,
        "drama_launch_window_days": 30,
        "cooldown_days": 7,
        "platform": 0,
        "drama_rule": metric_rule(resource_type_v2=["1", "5"]),
        "material_rule": metric_rule(
            duration_min_seconds=1,
            duration_max_seconds=600,
        ),
        "schedule": {"mode": "fixed", "times": ["08:05", "20:35"]},
    }
    value.update(overrides)
    return value


class XAutoPostValidationTests(unittest.TestCase):
    def test_valid_payload_freezes_required_language_and_body(self):
        normalized = normalize_template_payload(valid_payload())
        self.assertEqual(normalized["language"], "en")
        self.assertEqual(normalized["platform"], 0)
        self.assertEqual(
            normalized["body_template"],
            "🎬 {{drama_name}}\n{{desc}}\n{{url}}",
        )
        self.assertEqual(normalized["material_rule"]["duration_max_seconds"], 600)
        self.assertEqual(len(config_hash(normalized)), 64)

    def test_language_is_required_and_canonical(self):
        for language in (None, "", "zh_CN", "../../en"):
            with self.subTest(language=language):
                payload = valid_payload(language=language)
                with self.assertRaises(ValidationError):
                    normalize_template_payload(payload)

    def test_platform_is_fixed_to_zero(self):
        with self.assertRaises(ValidationError):
            normalize_template_payload(valid_payload(platform=1))

    def test_body_requires_drama_name_and_desc_exactly_once(self):
        invalid = (
            "{{desc}}",
            "{{drama_name}}",
            "{{drama_name}} {{desc}} {{desc}}",
            "{{drama_name}} {{desc}} {{episode_number}}",
            "{{drama_name}} {{desc}} {{unknown}}",
            "{{drama_name}} {{desc}",
        )
        for template in invalid:
            with self.subTest(template=template):
                with self.assertRaises(ValidationError) as caught:
                    normalize_template_payload(
                        valid_payload(body_template=template)
                    )
                self.assertEqual(
                    caught.exception.code,
                    "x_auto_body_template_invalid",
                )

    def test_url_macro_is_optional_but_may_not_repeat(self):
        without_url = normalize_template_payload(
            valid_payload(body_template="{{drama_name}}\n{{desc}}")
        )
        self.assertNotIn("{{url}}", without_url["body_template"])
        with self.assertRaises(ValidationError):
            normalize_template_payload(
                valid_payload(
                    body_template=(
                        "{{drama_name}} {{desc}} {{url}} {{url}}"
                    )
                )
            )

    def test_automatic_duration_hard_ceiling_is_600_seconds(self):
        normalized = normalize_template_payload(valid_payload())
        self.assertEqual(normalized["material_rule"]["duration_max_seconds"], 600)
        with self.assertRaises(ValidationError):
            normalize_template_payload(
                valid_payload(
                    material_rule=metric_rule(
                        duration_min_seconds=1,
                        duration_max_seconds=601,
                    )
                )
            )

    def test_schedule_is_stable_and_rejects_duplicate_times(self):
        random_payload = normalize_template_payload(
            valid_payload(schedule={"mode": "random", "daily_count": 3})
        )
        self.assertEqual(
            random_payload["schedule"],
            {"mode": "random", "daily_count": 3},
        )
        with self.assertRaises(ValidationError):
            normalize_template_payload(
                valid_payload(
                    schedule={"mode": "fixed", "times": ["08:05", "08:05"]}
                )
            )

    def test_internal_bearer_rejects_documented_placeholders(self):
        self.assertTrue(valid_internal_bearer("a" * 32))
        self.assertFalse(valid_internal_bearer("replace-with-unique-random-token"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
