#!/usr/bin/env python3
"""Offline tests for the scheduled X short-drama selector."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.drama_selector import (  # noqa: E402
    DramaPoolRejection,
    DramaQueryError,
    DramaSelectionError,
    audit_drama,
    build_name_tag,
    select_drama_pool_episodes,
)


def episode_row(
    number,
    *,
    unlocked=3,
    url=None,
    name="Drama Alpha",
    content_id="DRAMA-A",
):
    return {
        "resource_id": "resource%02d" % number,
        "app_id": "1479",
        "app": "DramaWave",
        "content_id": content_id,
        "drama_name": name,
        "drama_description": "A complete drama description.",
        "drama_labels": "Fantasy, Counter Attack, Romance",
        "country": "US",
        "language": "en",
        "series_code": "SERIES-A",
        "data_origin": 0,
        "unlocked_episodes_count": unlocked,
        "sub_number": number,
        "sub_name": "Episode %s" % number,
        "sub_url": url or "http://media.example.test/ep%s.mp4" % number,
    }


def pool_row(
    pool_id,
    content_id,
    created_at,
    candidate_account_id,
    *,
    assigned_account_id=0,
    next_sub_number=1,
):
    return {
        "id": pool_id,
        "content_id": content_id,
        "created_at": created_at,
        "next_sub_number": next_sub_number,
        "assigned_account_id": assigned_account_id,
        "assigned_at": (
            "2026-07-27T00:30:00Z" if assigned_account_id else ""
        ),
        "assigned_source_queue_id": (
            90 + pool_id if assigned_account_id else None
        ),
        "candidate_account_id": candidate_account_id,
    }


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params):
        self.connection.calls.append((sql, tuple(params)))
        if self.connection.fail:
            raise RuntimeError("read connection unavailable")
        self.rows = list(self.connection.rows_by_content.get(str(params[2]), []))

    def fetchall(self):
        return list(self.rows)

    def close(self):
        return None


class FakeConnection:
    def __init__(self, rows=None):
        self.rows_by_content = {"DRAMA-A": list(rows or [])}
        self.calls = []
        self.fail = False

    def cursor(self):
        return FakeCursor(self)


class DramaSelectorTests(unittest.TestCase):
    def test_audit_uses_minimum_unlocked_count_and_https_urls(self):
        rows = [
            episode_row(1, unlocked=3),
            episode_row(2, unlocked=2),
            episode_row(3, unlocked=3),
        ]
        connection = FakeConnection(rows)
        result = audit_drama(connection, "DRAMA-A")
        self.assertEqual(result["free_episode_count"], 2)
        self.assertEqual(
            [item["sub_number"] for item in result["episodes"]], [1, 2]
        )
        self.assertEqual(
            result["episodes"][0]["material_url"],
            "https://media.example.test/ep1.mp4",
        )
        self.assertEqual(
            result["name_tag"],
            "#Drama_Alpha #Fantasy #Counter_Attack",
        )
        sql, params = connection.calls[0]
        self.assertTrue(sql.lstrip().upper().startswith("SELECT"))
        self.assertIn("r.app_id = %s", sql)
        self.assertIn("r.type = %s", sql)
        self.assertEqual(params, (1479, 2, "DRAMA-A"))

    def test_description_line_breaks_are_normalized_for_posting(self):
        row = episode_row(1, unlocked=1)
        row["drama_description"] = "First line.\r\nSecond\tline."
        result = audit_drama(FakeConnection([row]), "DRAMA-A")
        self.assertEqual(
            result["description"],
            "First line. Second line.",
        )

    def test_same_episode_must_have_one_normalized_url(self):
        connection = FakeConnection(
            [
                episode_row(1, unlocked=1, url="https://a.example/ep1.mp4"),
                episode_row(1, unlocked=1, url="https://b.example/ep1.mp4"),
            ]
        )
        with self.assertRaises(DramaPoolRejection) as raised:
            audit_drama(connection, "DRAMA-A")
        self.assertEqual(raised.exception.code, "drama_episode_url_ambiguous")

    def test_identical_duplicate_source_row_is_deterministic(self):
        first = episode_row(1, unlocked=1)
        duplicate = dict(first)
        duplicate["resource_id"] = "resource01copy"
        result = audit_drama(FakeConnection([first, duplicate]), "DRAMA-A")
        self.assertEqual(result["episodes"][0]["resource_id"], "resource01")

    def test_zero_number_platform_metadata_rows_are_ignored(self):
        metadata = episode_row(0, unlocked=1)
        metadata.update(
            {
                "app": "",
                "drama_name": "",
                "drama_description": "",
                "language": "",
                "country": "",
                "sub_url": "",
            }
        )
        result = audit_drama(
            FakeConnection([metadata, episode_row(1, unlocked=1)]),
            "DRAMA-A",
        )
        self.assertEqual(result["free_episode_count"], 1)
        self.assertEqual(
            [item["sub_number"] for item in result["episodes"]],
            [1],
        )

    def test_cross_platform_duplicate_episode_rows_are_allowed(self):
        ios = episode_row(1, unlocked=1)
        ios["app"] = "6670430706"
        android = dict(ios)
        android["resource_id"] = "resource01android"
        android["app"] = "com.dramawave.app"
        result = audit_drama(FakeConnection([ios, android]), "DRAMA-A")
        self.assertEqual(result["app"], "6670430706")
        self.assertEqual(len(result["episodes"]), 1)
        self.assertEqual(result["episodes"][0]["resource_id"], "resource01")

    def test_cross_platform_url_conflict_remains_rejected(self):
        ios = episode_row(
            1,
            unlocked=1,
            url="https://ios.example.test/ep1.mp4",
        )
        ios["app"] = "6670430706"
        android = dict(ios)
        android["resource_id"] = "resource01android"
        android["app"] = "com.dramawave.app"
        android["sub_url"] = "https://android.example.test/ep1.mp4"
        with self.assertRaises(DramaPoolRejection) as raised:
            audit_drama(FakeConnection([ios, android]), "DRAMA-A")
        self.assertEqual(
            raised.exception.code,
            "drama_episode_url_ambiguous",
        )

    def test_cross_platform_metadata_conflict_remains_rejected(self):
        ios = episode_row(1, unlocked=1)
        ios["app"] = "6670430706"
        android = dict(ios)
        android["resource_id"] = "resource01android"
        android["app"] = "com.dramawave.app"
        android["drama_name"] = "Another Drama"
        with self.assertRaises(DramaPoolRejection) as raised:
            audit_drama(FakeConnection([ios, android]), "DRAMA-A")
        self.assertEqual(raised.exception.code, "drama_metadata_ambiguous")

    def test_realistic_zero_row_and_dual_platform_shape_selects_free_episodes(self):
        rows = []
        for app_index, app in enumerate(("6670430706", "com.dramawave.app")):
            metadata = episode_row(0, unlocked=11)
            metadata["resource_id"] = "metadata%s" % app_index
            metadata["app"] = app
            rows.append(metadata)
            for number in range(1, 46):
                row = episode_row(number, unlocked=11)
                row["resource_id"] = "resource%s-%02d" % (app_index, number)
                row["app"] = app
                rows.append(row)
        result = audit_drama(FakeConnection(rows), "DRAMA-A")
        self.assertEqual(result["free_episode_count"], 11)
        self.assertEqual(
            [item["sub_number"] for item in result["episodes"]],
            list(range(1, 12)),
        )

    def test_negative_episode_number_remains_invalid(self):
        with self.assertRaises(DramaPoolRejection) as raised:
            audit_drama(
                FakeConnection([episode_row(-1, unlocked=1)]),
                "DRAMA-A",
            )
        self.assertEqual(raised.exception.code, "drama_resource_invalid")
        self.assertEqual(str(raised.exception), "sub_number is invalid")

    def test_only_zero_number_rows_fail_closed(self):
        with self.assertRaises(DramaPoolRejection) as raised:
            audit_drama(
                FakeConnection([episode_row(0, unlocked=1)]),
                "DRAMA-A",
            )
        self.assertEqual(raised.exception.code, "drama_resource_invalid")
        self.assertEqual(
            str(raised.exception),
            "no positive episode rows were found",
        )

    def test_free_episode_numbers_must_be_continuous(self):
        connection = FakeConnection(
            [episode_row(1, unlocked=3), episode_row(3, unlocked=3)]
        )
        with self.assertRaises(DramaPoolRejection) as raised:
            audit_drama(connection, "DRAMA-A")
        self.assertEqual(raised.exception.code, "drama_episode_gap")

    def test_all_episode_metadata_must_match(self):
        connection = FakeConnection(
            [
                episode_row(1, unlocked=2),
                episode_row(2, unlocked=2, name="Another Drama"),
            ]
        )
        with self.assertRaises(DramaPoolRejection) as raised:
            audit_drama(connection, "DRAMA-A")
        self.assertEqual(raised.exception.code, "drama_metadata_ambiguous")

    def test_account_affinity_selects_one_next_episode_per_account(self):
        connection = FakeConnection(
            [
                episode_row(1, unlocked=3),
                episode_row(2, unlocked=3),
                episode_row(3, unlocked=3),
            ]
        )
        connection.rows_by_content["DRAMA-B"] = [
            episode_row(
                1,
                unlocked=2,
                content_id="DRAMA-B",
                name="Drama Beta",
            ),
            episode_row(
                2,
                unlocked=2,
                content_id="DRAMA-B",
                name="Drama Beta",
            ),
        ]
        selected = select_drama_pool_episodes(
            connection,
            [
                pool_row(
                    10,
                    "DRAMA-A",
                    "2026-07-27T01:00:00Z",
                    2,
                    assigned_account_id=2,
                    next_sub_number=2,
                ),
                pool_row(
                    11,
                    "DRAMA-B",
                    "2026-07-27T02:00:00Z",
                    3,
                ),
            ],
            account_ids=[2, 3],
        )
        self.assertEqual(
            [item["episode_key"] for item in selected],
            ["DRAMA-A:2", "DRAMA-B:1"],
        )
        self.assertEqual(
            [item["candidate_account_id"] for item in selected],
            [2, 3],
        )
        self.assertTrue(all(item["source_type"] == "drama" for item in selected))
        self.assertEqual(
            [item["drama_pool_item_id"] for item in selected],
            [10, 11],
        )
        self.assertTrue(all(item["pool_item_id"] is None for item in selected))
        self.assertTrue(all(item["tag"] == "Fantasy" for item in selected))

    def test_pool_account_order_must_match_configured_accounts(self):
        connection = FakeConnection([episode_row(1, unlocked=1)])
        with self.assertRaises(DramaSelectionError):
            select_drama_pool_episodes(
                connection,
                [
                    pool_row(
                        2,
                        "DRAMA-A",
                        "2026-07-27T02:00:00Z",
                        3,
                    ),
                ],
                account_ids=[2],
            )

    def test_query_error_stops_the_complete_selection(self):
        connection = FakeConnection([episode_row(1, unlocked=1)])
        connection.fail = True
        with self.assertRaises(DramaQueryError):
            audit_drama(connection, "DRAMA-A")

    def test_name_tag_deduplicates_and_limits_hashtags(self):
        self.assertEqual(
            build_name_tag(
                "Drama",
                "Power Romance, power-romance, Family Love, Extra",
            ),
            "#Drama #Power_Romance #Family_Love",
        )
        self.assertEqual(build_name_tag("Drama", ""), "#Drama")

    def test_unlabelled_drama_uses_name_for_required_w2a_tag(self):
        row = episode_row(1, unlocked=1)
        row["drama_labels"] = ""
        selected = select_drama_pool_episodes(
            FakeConnection([row]),
            [
                pool_row(
                    10,
                    "DRAMA-A",
                    "2026-07-27T01:00:00Z",
                    2,
                )
            ],
            account_ids=[2],
        )
        self.assertEqual(selected[0]["name_tag"], "#Drama_Alpha")
        self.assertEqual(selected[0]["tag"], "Drama Alpha")


if __name__ == "__main__":
    unittest.main()
