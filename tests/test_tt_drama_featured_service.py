from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from features.tt_drama_featured.service import (
    CONTENT_ID_SQL_PATTERN,
    FeaturedCacheError,
    FeaturedConfig,
    FeaturedDramaRepository,
    FeaturedRefreshError,
    MAX_METADATA_ROWS_PER_CONTENT,
    atomic_write_snapshot,
    build_snapshot,
    previous_source_date,
)


CONTENT_IDS = [
    "DRAMA00001",
    "DRAMA00002",
    "DRAMA00003",
    "DRAMA00004",
    "DRAMA00005",
    "DRAMA00006",
]


def metadata_rows(content_ids=CONTENT_IDS):
    rows = []
    for index, content_id in enumerate(content_ids):
        for episode in (1, 2, 3):
            rows.append(
                {
                    "content_id": content_id,
                    "app": "com.dramawave.app",
                    "country": "US",
                    "language": "en",
                    "title": "Drama %d" % (index + 1),
                    "description": "Description %d" % (index + 1),
                    "cover_url": (
                        "https://static-v1.mydramawave.com/%s.jpg"
                        % content_id
                    ),
                    "sub_number": episode,
                    "updated_at": datetime(2026, 7, 26, 12, episode, 0),
                }
            )
    return rows


def spend_rows(content_ids=CONTENT_IDS):
    return [
        {"content_id": content_id, "spend_n": 1000 - index}
        for index, content_id in enumerate(content_ids)
    ]


class _FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.row = None
        self.closed = False

    def execute(self, sql, params=()):
        self.connection.statements.append((sql, tuple(params)))
        if sql.startswith("SET SESSION"):
            self.rows = []
            self.row = None
        elif "@@read_only" in sql:
            self.row = {"read_only": self.connection.read_only}
            self.rows = [self.row]
        elif "ads_custom_source_insight" in sql:
            self.rows = list(self.connection.spend)
            self.row = self.rows[0] if self.rows else None
        elif "ads_drama_resource" in sql:
            self.rows = list(self.connection.metadata)
            self.row = self.rows[0] if self.rows else None
        else:
            raise AssertionError("unexpected SQL")

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, *, read_only=1):
        self.read_only = read_only
        self.spend = spend_rows()
        self.metadata = metadata_rows()
        self.statements = []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


class FeaturedDramaServiceTests(unittest.TestCase):
    def test_previous_source_date_is_fixed_to_shanghai(self):
        utc = datetime(2026, 7, 26, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(previous_source_date(utc), "2026-07-26")
        naive_shanghai = datetime(2026, 7, 27, 0, 30)
        self.assertEqual(previous_source_date(naive_shanghai), "2026-07-26")

    def test_repository_uses_bounded_scope_before_metadata_lookup(self):
        connection = _FakeConnection()
        config = FeaturedConfig()
        repository = FeaturedDramaRepository(
            host="readonly.example",
            port=63350,
            user="reader",
            password="secret",
            config=config,
            connection_factory=lambda: connection,
        )
        ranked, metadata = repository.fetch("2026-07-26")
        self.assertEqual(len(ranked), 6)
        self.assertEqual(len(metadata), 18)
        self.assertTrue(connection.closed)

        insight_sql, insight_params = next(
            statement
            for statement in connection.statements
            if "ads_custom_source_insight" in statement[0]
        )
        self.assertIn("FORCE INDEX (`as`)", insight_sql)
        self.assertIn("GROUP BY BINARY i.data_source_id", insight_sql)
        self.assertIn("BINARY i.data_source_id REGEXP %s", insight_sql)
        self.assertIn(
            "ORDER BY spend_n DESC, MIN(BINARY i.data_source_id) ASC",
            insight_sql,
        )
        self.assertNotIn("platform", insight_sql.lower())
        self.assertEqual(
            insight_params,
            (
                "[w2a]drama-double",
                "2026-07-26",
                "Dramawave",
                6,
                CONTENT_ID_SQL_PATTERN,
                20,
            ),
        )
        metadata_sql, metadata_params = next(
            statement
            for statement in connection.statements
            if "ads_drama_resource" in statement[0]
        )
        self.assertIn("FORCE INDEX (content_id)", metadata_sql)
        self.assertEqual(metadata_params[-2], "1479")
        self.assertEqual(
            metadata_params[-1],
            len(CONTENT_IDS) * MAX_METADATA_ROWS_PER_CONTENT + 1,
        )
        self.assertEqual(metadata_params[:-2], tuple(CONTENT_IDS))

    def test_repository_rejects_non_read_only_endpoint(self):
        connection = _FakeConnection(read_only=0)
        repository = FeaturedDramaRepository(
            host="readonly.example",
            port=63350,
            user="reader",
            password="secret",
            config=FeaturedConfig(),
            connection_factory=lambda: connection,
        )
        with self.assertRaisesRegex(FeaturedRefreshError, "not read-only"):
            repository.fetch("2026-07-26")
        self.assertTrue(connection.closed)

    def test_repository_rejects_non_verified_production_port(self):
        repository = FeaturedDramaRepository(
            host="101.32.56.53",
            port=63353,
            user="reader",
            password="secret",
            config=FeaturedConfig(),
        )
        with self.assertRaisesRegex(FeaturedRefreshError, "port 63350"):
            repository.fetch("2026-07-26")

    def test_repository_rejects_non_verified_production_host_and_database(self):
        bad_host = FeaturedDramaRepository(
            host="readonly.example",
            port=63350,
            user="reader",
            password="secret",
            config=FeaturedConfig(),
        )
        with self.assertRaisesRegex(FeaturedRefreshError, "verified read-only host"):
            bad_host.fetch("2026-07-26")

        bad_database = FeaturedDramaRepository(
            host="101.32.56.53",
            port=63350,
            user="reader",
            password="secret",
            config=FeaturedConfig(database="other_database"),
        )
        with self.assertRaisesRegex(FeaturedRefreshError, "verified database"):
            bad_database.fetch("2026-07-26")

    def test_repository_rejects_metadata_above_row_budget(self):
        connection = _FakeConnection()
        connection.spend = spend_rows(CONTENT_IDS[:5])
        connection.metadata = [
            dict(metadata_rows(CONTENT_IDS[:1])[0])
            for _unused in range(
                (5 * MAX_METADATA_ROWS_PER_CONTENT) + 1
            )
        ]
        repository = FeaturedDramaRepository(
            host="readonly.example",
            port=63350,
            user="reader",
            password="secret",
            config=FeaturedConfig(candidate_limit=5),
            connection_factory=lambda: connection,
        )
        with self.assertRaisesRegex(FeaturedRefreshError, "row budget"):
            repository.fetch("2026-07-26")

    def test_candidate_limit_is_capped_at_verified_top_twenty(self):
        self.assertEqual(FeaturedConfig(candidate_limit=50).candidate_limit, 20)

    def test_snapshot_selects_five_and_never_serializes_spend(self):
        snapshot = build_snapshot(
            source_date="2026-07-26",
            generated_at=datetime(
                2026,
                7,
                27,
                18,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            ),
            spend_rows=spend_rows(),
            metadata_rows=metadata_rows(),
        )
        self.assertEqual(snapshot["source_date"], "2026-07-26")
        self.assertEqual(len(snapshot["items"]), 5)
        self.assertEqual(
            {item["content_id"] for item in snapshot["items"]},
            set(CONTENT_IDS[:5]),
        )
        payload = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("spend", payload.lower())
        self.assertTrue(
            all(
                item["cover_url"].startswith(
                    "https://static-v1.mydramawave.com/"
                )
                for item in snapshot["items"]
            )
        )

    def test_snapshot_order_is_stable_per_date_and_can_change_next_day(self):
        common = {
            "generated_at": datetime(
                2026,
                7,
                27,
                18,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            ),
            "spend_rows": spend_rows(),
            "metadata_rows": metadata_rows(),
        }
        first = build_snapshot(source_date="2026-07-26", **common)
        second = build_snapshot(source_date="2026-07-26", **common)
        next_day = build_snapshot(source_date="2026-07-27", **common)
        first_ids = [item["content_id"] for item in first["items"]]
        self.assertEqual(
            first_ids,
            [item["content_id"] for item in second["items"]],
        )
        self.assertNotEqual(
            first_ids,
            [item["content_id"] for item in next_day["items"]],
        )

    def test_description_is_not_required_for_cover_cards(self):
        rows = metadata_rows()
        for row in rows:
            row["description"] = ""
        snapshot = build_snapshot(
            source_date="2026-07-26",
            generated_at=datetime.now(timezone.utc),
            spend_rows=spend_rows(),
            metadata_rows=rows,
        )
        self.assertEqual(len(snapshot["items"]), 5)

    def test_incomplete_or_unsafe_metadata_does_not_produce_partial_cache(self):
        rows = metadata_rows(CONTENT_IDS[:5])
        for row in rows:
            if row["content_id"] == CONTENT_IDS[4]:
                row["cover_url"] = "http://evil.example/cover.jpg"
        with self.assertRaisesRegex(FeaturedRefreshError, "only 4 of 5"):
            build_snapshot(
                source_date="2026-07-26",
                generated_at=datetime.now(timezone.utc),
                spend_rows=spend_rows(CONTENT_IDS[:5]),
                metadata_rows=rows,
            )

    def test_atomic_write_is_idempotent_and_failure_preserves_old_file(self):
        first = build_snapshot(
            source_date="2026-07-26",
            generated_at=datetime.now(timezone.utc),
            spend_rows=spend_rows(),
            metadata_rows=metadata_rows(),
        )
        second = dict(first)
        second["generated_at"] = "2026-07-27T19:00:00+08:00"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            self.assertTrue(atomic_write_snapshot(target, first))
            original = target.read_bytes()
            self.assertFalse(atomic_write_snapshot(target, second))
            self.assertEqual(target.read_bytes(), original)

            changed = json.loads(json.dumps(first))
            changed["source_date"] = "2026-07-27"
            with mock.patch(
                "features.tt_drama_featured.service.os.replace",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(FeaturedCacheError):
                    atomic_write_snapshot(target, changed)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_atomic_write_rejects_private_spend_key(self):
        snapshot = {
            "schema_version": 1,
            "source_date": "2026-07-26",
            "generated_at": "2026-07-27T18:00:00+08:00",
            "items": [{"content_id": CONTENT_IDS[0], "spend": 999}],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FeaturedCacheError, "private spend"):
                atomic_write_snapshot(Path(directory) / "current.json", snapshot)

    def test_atomic_write_replaces_legacy_file_with_private_extra_fields(self):
        snapshot = build_snapshot(
            source_date="2026-07-26",
            generated_at=datetime.now(timezone.utc),
            spend_rows=spend_rows(),
            metadata_rows=metadata_rows(),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            legacy = json.loads(json.dumps(snapshot))
            legacy["spend"] = 999
            target.write_text(
                json.dumps(legacy, ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertTrue(atomic_write_snapshot(target, snapshot))
            cleaned = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn("spend", cleaned)
            self.assertEqual(set(cleaned), {
                "schema_version",
                "source_date",
                "generated_at",
                "items",
            })

    def test_atomic_write_repairs_invalid_generated_at(self):
        snapshot = build_snapshot(
            source_date="2026-07-26",
            generated_at=datetime.now(timezone.utc),
            spend_rows=spend_rows(),
            metadata_rows=metadata_rows(),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            invalid = json.loads(json.dumps(snapshot))
            invalid["generated_at"] = "not-a-time"
            target.write_text(
                json.dumps(invalid, ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertTrue(atomic_write_snapshot(target, snapshot))
            repaired = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(repaired["generated_at"], snapshot["generated_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
