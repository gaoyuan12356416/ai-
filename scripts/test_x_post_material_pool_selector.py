#!/usr/bin/env python3
"""Offline regression tests for manual X-post material-pool hydration."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.selector import (  # noqa: E402
    CandidateQueryError,
    normalize_material_url,
    select_pool_candidates,
)


def material_row(material_id, **overrides):
    row = {
        "material_id": str(material_id),
        "product": "Dramawave",
        "material_url": "https://media.example.test/%s.mp4" % material_id,
        "material_name": "material-%s.mp4" % material_id,
        "material_language": "en",
        "content_id": "C%s" % material_id,
        "source_tag_name": "high_quality",
        "video_duration": 30,
    }
    row.update(overrides)
    return row


def drama_row(material_id, **overrides):
    row = {
        "content_id": "C%s" % material_id,
        "series_code": "S%s" % material_id,
        "language": "en",
        "drama_name": "Drama %s" % material_id,
        "drama_labels": "Fantasy,Counterattack",
        "drama_description": "A complete and safe drama description.",
    }
    row.update(overrides)
    return row


def deploy_row(material_id, deploy_time=0, **overrides):
    row = {
        "content_id": "C%s" % material_id,
        "app_id": 1479,
        "app": "com.dramawave.app",
        "language": "en",
        "deploy_time": deploy_time,
    }
    row.update(overrides)
    return row


def pool_item(pool_item_id, material_id, created_at):
    return {
        "id": pool_item_id,
        "material_id": str(material_id),
        "created_at": created_at,
    }


class PoolCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params):
        params = tuple(params)
        self.connection.calls.append((sql, params))
        if "ads_custom_source cs" in sql:
            material_id = str(params[0])
            if material_id in self.connection.query_error_material_ids:
                raise RuntimeError("simulated read-only connection loss")
            row = self.connection.materials.get(material_id)
            self.rows = [] if row is None else [row]
        elif "ads_facebook_violations" in sql:
            material_id = str(params[0])
            if material_id in self.connection.query_error_material_ids:
                raise RuntimeError("simulated read-only connection loss")
            self.rows = [
                {
                    "facebook_count": self.connection.violations.get(material_id, 0),
                    "tiktok_count": 0,
                    "twitter_count": 0,
                    "resource_audit_count": 0,
                }
            ]
        elif "resource_tags" in sql:
            self.rows = [
                {"tag_name": value}
                for value in self.connection.material_tags.get(str(params[0]), [])
            ]
        elif "ads_drama_info i" in sql:
            content_id = str(params[0])
            material_id = content_id.lstrip("C")
            if material_id in self.connection.query_error_material_ids:
                raise RuntimeError("simulated read-only connection loss")
            self.rows = self.connection.deploy_rows.get(
                material_id,
                [deploy_row(material_id)],
            )
        elif "ads_drama_resource" in sql:
            content_id = str(params[0])
            material_id = content_id.lstrip("C")
            self.rows = self.connection.drama_rows.get(
                material_id,
                [drama_row(material_id)],
            )
        else:
            raise AssertionError("unexpected SQL: %s" % sql)

    def fetchall(self):
        return list(self.rows)

    def close(self):
        return None


class PoolConnection:
    def __init__(self, material_ids):
        self.materials = {
            str(material_id): material_row(material_id)
            for material_id in material_ids
        }
        self.violations = {}
        self.material_tags = {}
        self.drama_rows = {}
        self.deploy_rows = {}
        self.query_error_material_ids = set()
        self.calls = []

    def cursor(self):
        return PoolCursor(self)


class ManualPoolSelectorTests(unittest.TestCase):
    def test_pool_order_is_created_at_then_id_and_does_not_use_insight(self):
        connection = PoolConnection([10, 20, 30])
        selected, rejections = select_pool_candidates(
            connection,
            [
                pool_item(3, 30, "2026-07-23T02:00:00Z"),
                pool_item(2, 20, "2026-07-23T01:00:00Z"),
                pool_item(1, 10, "2026-07-23T01:00:00Z"),
            ],
            "2026-07-22",
            limit=2,
        )

        self.assertEqual(rejections, [])
        self.assertEqual([item["material_id"] for item in selected], ["30", "20"])
        self.assertEqual([item["pool_item_id"] for item in selected], [3, 2])
        self.assertEqual([item["spend"] for item in selected], [0.0, 0.0])
        statements = [sql for sql, _params in connection.calls]
        self.assertTrue(all("ads_custom_source_insight" not in sql for sql in statements))
        material_calls = [
            (sql, params)
            for sql, params in connection.calls
            if "ads_custom_source cs" in sql
        ]
        self.assertEqual([params[0] for _sql, params in material_calls], ["30", "20"])
        material_sql, material_params = material_calls[0]
        self.assertIn("cs.id = %s", material_sql)
        self.assertIn("cs.type = %s", material_sql)
        self.assertIn("cs.product = %s", material_sql)
        self.assertEqual(material_params, ("30", "Dramawave", 2, 0, 1, 600))
        drama_calls = [
            (sql, params)
            for sql, params in connection.calls
            if "ads_drama_resource" in sql
        ]
        self.assertEqual(drama_calls[0][1], ("C30", "en"))
        deploy_calls = [
            (sql, params)
            for sql, params in connection.calls
            if "ads_drama_info i" in sql
        ]
        self.assertEqual(deploy_calls[0][1], ("C30", 1479, "en"))

    def test_future_deploy_time_is_skipped_until_the_boundary_passes(self):
        shanghai = timezone(timedelta(hours=8))
        now = datetime(2026, 7, 27, 10, 0, 0, tzinfo=shanghai)
        deploy_time = int((now + timedelta(hours=2)).timestamp())
        connection = PoolConnection([1, 2])
        connection.deploy_rows["1"] = [
            deploy_row(1, deploy_time, app="com.dramawave.app"),
            deploy_row(1, deploy_time, app="6670430706"),
        ]
        connection.deploy_rows["2"] = [
            deploy_row(2, int(now.timestamp())),
        ]

        selected, rejections = select_pool_candidates(
            connection,
            [
                pool_item(1, 1, "2026-07-23T00:00:01Z"),
                pool_item(2, 2, "2026-07-23T00:00:00Z"),
            ],
            "2026-07-22",
            limit=1,
            now=now,
        )

        self.assertEqual([item["material_id"] for item in selected], ["2"])
        self.assertEqual(
            [item["error_code"] for item in rejections],
            ["drama_not_yet_deliverable"],
        )
        self.assertIn("2026-07-27 12:00:00", rejections[0]["error_message"])

        selected, rejections = select_pool_candidates(
            connection,
            [pool_item(1, 1, "2026-07-23T00:00:00Z")],
            "2026-07-22",
            limit=1,
            now=now + timedelta(hours=2),
        )
        self.assertEqual(rejections, [])
        self.assertEqual([item["material_id"] for item in selected], ["1"])
        self.assertEqual(selected[0]["drama_deploy_time"], deploy_time)

    def test_latest_dramawave_platform_time_controls_eligibility(self):
        shanghai = timezone(timedelta(hours=8))
        now = datetime(2026, 7, 27, 10, 0, 0, tzinfo=shanghai)
        connection = PoolConnection([1])
        connection.deploy_rows["1"] = [
            deploy_row(1, int((now - timedelta(hours=1)).timestamp())),
            deploy_row(
                1,
                int((now + timedelta(hours=1)).timestamp()),
                app="6670430706",
            ),
        ]

        selected, rejections = select_pool_candidates(
            connection,
            [pool_item(1, 1, "2026-07-23T00:00:00Z")],
            "2026-07-22",
            limit=1,
            now=now,
        )

        self.assertEqual(selected, [])
        self.assertEqual(rejections[0]["error_code"], "drama_not_yet_deliverable")

    def test_missing_or_invalid_dramawave_deploy_time_fails_closed(self):
        shanghai = timezone(timedelta(hours=8))
        now = datetime(2026, 7, 27, 10, 0, 0, tzinfo=shanghai)
        connection = PoolConnection([1, 2, 3])
        connection.deploy_rows["1"] = []
        connection.deploy_rows["2"] = [deploy_row(2, -1)]
        connection.deploy_rows["3"] = [deploy_row(3, 0)]

        selected, rejections = select_pool_candidates(
            connection,
            [
                pool_item(1, 1, "2026-07-23T00:00:02Z"),
                pool_item(2, 2, "2026-07-23T00:00:01Z"),
                pool_item(3, 3, "2026-07-23T00:00:00Z"),
            ],
            "2026-07-22",
            limit=1,
            now=now,
        )

        self.assertEqual([item["material_id"] for item in selected], ["3"])
        self.assertEqual(
            [item["error_code"] for item in rejections],
            ["drama_deploy_time_missing", "drama_deploy_time_invalid"],
        )

    def test_item_level_safety_rejections_are_reported_and_scanning_continues(self):
        connection = PoolConnection(range(1, 7))
        connection.violations["1"] = 1
        connection.materials["2"]["source_tag_name"] = "sexual_content"
        connection.material_tags["3"] = ["blood_gore"]
        connection.drama_rows["4"] = [
            drama_row(4),
            drama_row(4, series_code="ANOTHER_SERIES"),
        ]
        connection.materials["5"]["material_url"] = "ftp://media.example.test/5.mp4"

        selected, rejections = select_pool_candidates(
            connection,
            [
                pool_item(material_id, material_id, "2026-07-23T00:00:0%sZ" % (6 - material_id))
                for material_id in range(1, 7)
            ],
            "2026-07-22",
            limit=1,
        )

        self.assertEqual([item["material_id"] for item in selected], ["6"])
        self.assertEqual(
            [item["error_code"] for item in rejections],
            [
                "material_has_violation",
                "material_source_tag_unsafe",
                "material_tag_unsafe",
                "drama_mapping_ambiguous",
                "material_url_not_https",
            ],
        )
        self.assertTrue(
            all(
                set(item)
                == {"pool_item_id", "material_id", "error_code", "error_message"}
                for item in rejections
            )
        )

    def test_sexual_or_violent_drama_labels_are_allowed(self):
        connection = PoolConnection([8])
        connection.drama_rows["8"] = [
            drama_row(8, drama_labels="Sexual Content,Graphic Violence"),
        ]

        selected, rejections = select_pool_candidates(
            connection,
            [pool_item(1, 8, "2026-07-23T00:00:00Z")],
            "2026-07-22",
            limit=1,
        )

        self.assertEqual(rejections, [])
        self.assertEqual([item["material_id"] for item in selected], ["8"])
        self.assertEqual(selected[0]["tag"], "Sexual Content")

    def test_http_material_url_is_upgraded_to_https_before_selection(self):
        connection = PoolConnection([6])
        connection.materials["6"]["material_url"] = (
            "http://media.example.test/custom/source/6.mp4"
        )

        selected, rejections = select_pool_candidates(
            connection,
            [pool_item(1, 6, "2026-07-23T00:00:00Z")],
            "2026-07-22",
            limit=1,
        )

        self.assertEqual(rejections, [])
        self.assertEqual(
            selected[0]["material_url"],
            "https://media.example.test/custom/source/6.mp4",
        )
        self.assertEqual(
            normalize_material_url("HTTP://media.example.test/a.mp4"),
            "https://media.example.test/a.mp4",
        )

    def test_duplicate_normalized_drama_rows_are_accepted(self):
        connection = PoolConnection([8])
        connection.drama_rows["8"] = [
            drama_row(8, drama_labels="Fantasy, Counterattack"),
            drama_row(
                8,
                language="EN",
                drama_labels="fantasy,counterattack",
            ),
        ]

        selected, rejections = select_pool_candidates(
            connection,
            [pool_item(1, 8, "2026-07-23T00:00:00Z")],
            "2026-07-22",
            limit=1,
        )

        self.assertEqual(rejections, [])
        self.assertEqual([item["material_id"] for item in selected], ["8"])

    def test_missing_material_and_bad_pool_input_are_safe_rejections(self):
        connection = PoolConnection([])
        selected, rejections = select_pool_candidates(
            connection,
            [
                {"id": 1, "material_id": "not-a-number", "created_at": "bad-date"},
                pool_item(2, 99, "2026-07-23T00:00:00Z"),
            ],
            "2026-07-22",
            limit=1,
        )

        self.assertEqual(selected, [])
        self.assertEqual(
            [item["error_code"] for item in rejections],
            ["pool_item_invalid", "material_not_found_or_ineligible"],
        )

    def test_non_dramawave_material_is_rejected_fail_closed(self):
        connection = PoolConnection([11, 12])
        connection.materials["11"]["product"] = "OtherProduct"

        selected, rejections = select_pool_candidates(
            connection,
            [
                pool_item(1, 11, "2026-07-23T00:00:01Z"),
                pool_item(2, 12, "2026-07-23T00:00:00Z"),
            ],
            "2026-07-22",
            limit=1,
        )

        self.assertEqual([item["material_id"] for item in selected], ["12"])
        self.assertEqual(
            [item["error_code"] for item in rejections],
            ["material_product_mismatch"],
        )

    def test_mysql_query_failure_aborts_instead_of_becoming_item_rejection(self):
        connection = PoolConnection([9, 10])
        connection.query_error_material_ids.add("9")

        with self.assertRaises(CandidateQueryError):
            select_pool_candidates(
                connection,
                [
                    pool_item(1, 9, "2026-07-23T00:00:01Z"),
                    pool_item(2, 10, "2026-07-23T00:00:00Z"),
                ],
                "2026-07-22",
                limit=1,
            )


if __name__ == "__main__":
    unittest.main()
