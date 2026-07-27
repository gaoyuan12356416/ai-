from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from features.tt_drama_featured.service import (
    CONTENT_ID_SQL_PATTERN,
    FeaturedCacheError,
    FeaturedConfig,
    FeaturedDramaRepository,
    FeaturedRefreshError,
    atomic_write_snapshot,
    build_snapshot,
    previous_source_date,
    resolve_ranked_resources,
)
from features.tt_drama_resources import (
    ResourceOutcome,
    ResourceSourceError,
)
from scripts import refresh_tt_drama_featured as refresh_script


CONTENT_IDS = [
    "DRAMA00001",
    "DRAMA00002",
    "DRAMA00003",
    "DRAMA00004",
    "DRAMA00005",
    "DRAMA00006",
]
FIXED_TIME = datetime(
    2026,
    7,
    27,
    18,
    0,
    tzinfo=timezone(timedelta(hours=8)),
)


def spend_rows(content_ids=CONTENT_IDS):
    return [
        {"content_id": content_id, "spend_n": 1000 - index}
        for index, content_id in enumerate(content_ids)
    ]


def resource_item(content_id, *, title=None, cover_url=None):
    return {
        "content_id": content_id,
        "title": title or "Drama %s" % content_id,
        "description": "Description %s" % content_id,
        "cover_url": (
            cover_url
            or "https://cdn.usrgrow.com/storage/icons/%s.jpg" % content_id
        ),
        "country": "",
        "language": "",
        "episode_count": 0,
        "source_updated_at": "2026-07-27T10:00:00+00:00",
    }


def resource_items(content_ids=CONTENT_IDS):
    return [resource_item(content_id) for content_id in content_ids]


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
        self.statements = []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


class _FakeResourceService:
    def __init__(self, outcomes=None):
        self.outcomes = dict(outcomes or {})
        self.calls = []
        self.client = SimpleNamespace(
            allowed_cover_hosts=frozenset(("cdn.usrgrow.com",))
        )

    def resolve(self, content_id, allow_stale=True):
        self.calls.append((content_id, allow_stale))
        result = self.outcomes.get(
            content_id,
            ResourceOutcome(
                True,
                resource_item(content_id),
                "DISK_HIT",
            ),
        )
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeRankingRepository:
    def __init__(self, rows=None):
        self.rows = list(rows or spend_rows())
        self.calls = []

    def fetch_ranked(self, source_date):
        self.calls.append(source_date)
        return list(self.rows)


def complete_snapshot(source_date="2026-07-26"):
    return build_snapshot(
        source_date=source_date,
        generated_at=FIXED_TIME,
        spend_rows=spend_rows(),
        resource_items=resource_items(),
        allowed_cover_hosts=("cdn.usrgrow.com",),
    )


class FeaturedDramaServiceTests(unittest.TestCase):
    def test_previous_source_date_is_fixed_to_shanghai(self):
        utc = datetime(2026, 7, 26, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(previous_source_date(utc), "2026-07-26")
        naive_shanghai = datetime(2026, 7, 27, 0, 30)
        self.assertEqual(previous_source_date(naive_shanghai), "2026-07-26")

    def test_repository_queries_only_yesterday_spend_ranking(self):
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
        ranked = repository.fetch_ranked("2026-07-26")
        self.assertEqual(ranked, spend_rows())
        self.assertTrue(connection.closed)

        statements = [sql for sql, _params in connection.statements]
        self.assertFalse(any("ads_drama_resource" in sql for sql in statements))
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

    def test_repository_fetch_alias_returns_ranked_rows_only(self):
        connection = _FakeConnection()
        repository = FeaturedDramaRepository(
            host="readonly.example",
            port=63350,
            user="reader",
            password="secret",
            config=FeaturedConfig(),
            connection_factory=lambda: connection,
        )
        self.assertEqual(repository.fetch("2026-07-26"), spend_rows())

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
            repository.fetch_ranked("2026-07-26")
        self.assertTrue(connection.closed)

    def test_repository_rejects_unverified_production_source(self):
        for kwargs, message in (
            ({"host": "readonly.example", "port": 63350}, "verified read-only host"),
            ({"host": "101.32.56.53", "port": 63353}, "port 63350"),
        ):
            repository = FeaturedDramaRepository(
                user="reader",
                password="secret",
                config=FeaturedConfig(),
                **kwargs,
            )
            with self.assertRaisesRegex(FeaturedRefreshError, message):
                repository.fetch_ranked("2026-07-26")

        repository = FeaturedDramaRepository(
            host="101.32.56.53",
            port=63350,
            user="reader",
            password="secret",
            config=FeaturedConfig(database="other_database"),
        )
        with self.assertRaisesRegex(FeaturedRefreshError, "verified database"):
            repository.fetch_ranked("2026-07-26")

    def test_candidate_limit_is_capped_at_verified_top_twenty(self):
        self.assertEqual(FeaturedConfig(candidate_limit=50).candidate_limit, 20)

    def test_featured_production_scope_cannot_be_expanded(self):
        for kwargs in (
            {"insight_table": "other_table"},
            {"insight_index": "other_index"},
            {"product": "Other"},
            {"source_app_id": "[w2a]other"},
            {"data_source": 7},
        ):
            with self.assertRaisesRegex(ValueError, "cannot be expanded"):
                FeaturedConfig(**kwargs)

    def test_resource_resolution_skips_not_found_and_fills_from_lower_rank(self):
        service = _FakeResourceService(
            {
                CONTENT_IDS[1]: ResourceOutcome(
                    False,
                    None,
                    "NEGATIVE_HIT",
                )
            }
        )
        selected = resolve_ranked_resources(
            spend_rows(),
            service,
            allowed_cover_hosts=("cdn.usrgrow.com",),
        )
        self.assertEqual(
            [item["content_id"] for item in selected],
            [
                CONTENT_IDS[0],
                CONTENT_IDS[2],
                CONTENT_IDS[3],
                CONTENT_IDS[4],
                CONTENT_IDS[5],
            ],
        )
        self.assertEqual(
            [content_id for content_id, _allow_stale in service.calls],
            CONTENT_IDS,
        )
        self.assertTrue(all(allow_stale for _content_id, allow_stale in service.calls))

    def test_resource_resolution_fails_closed_if_fewer_than_five(self):
        service = _FakeResourceService(
            {
                content_id: ResourceOutcome(False, None, "NEGATIVE_HIT")
                for content_id in CONTENT_IDS[2:]
            }
        )
        with self.assertRaisesRegex(FeaturedRefreshError, "only 2 of 5"):
            resolve_ranked_resources(
                spend_rows(),
                service,
                allowed_cover_hosts=("cdn.usrgrow.com",),
            )

    def test_resource_source_error_fails_refresh_instead_of_changing_rank(self):
        service = _FakeResourceService(
            {CONTENT_IDS[1]: ResourceSourceError("injected")}
        )
        with self.assertRaisesRegex(FeaturedRefreshError, "ResourceSourceError"):
            resolve_ranked_resources(
                spend_rows(),
                service,
                allowed_cover_hosts=("cdn.usrgrow.com",),
            )

    def test_resource_invalid_outcome_fails_closed(self):
        service = _FakeResourceService({CONTENT_IDS[0]: None})
        with self.assertRaisesRegex(FeaturedRefreshError, "invalid outcome"):
            resolve_ranked_resources(
                spend_rows(),
                service,
                allowed_cover_hosts=("cdn.usrgrow.com",),
            )

    def test_resource_mismatched_id_and_unsafe_cover_fail_closed(self):
        mismatched = resource_item(CONTENT_IDS[0])
        mismatched["content_id"] = CONTENT_IDS[1]
        with self.assertRaisesRegex(FeaturedRefreshError, "mismatched"):
            resolve_ranked_resources(
                spend_rows(),
                _FakeResourceService(
                    {
                        CONTENT_IDS[0]: ResourceOutcome(
                            True,
                            mismatched,
                            "DISK_HIT",
                        )
                    }
                ),
                allowed_cover_hosts=("cdn.usrgrow.com",),
            )

        unsafe = resource_item(
            CONTENT_IDS[0],
            cover_url="http://evil.example/cover.jpg",
        )
        with self.assertRaisesRegex(FeaturedRefreshError, "required"):
            resolve_ranked_resources(
                spend_rows(),
                _FakeResourceService(
                    {
                        CONTENT_IDS[0]: ResourceOutcome(
                            True,
                            unsafe,
                            "DISK_HIT",
                        )
                    }
                ),
                allowed_cover_hosts=("cdn.usrgrow.com",),
            )

    def test_snapshot_preserves_public_v1_schema_and_omits_spend(self):
        snapshot = complete_snapshot()
        self.assertEqual(
            set(snapshot),
            {"schema_version", "source_date", "generated_at", "items"},
        )
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(len(snapshot["items"]), 5)
        self.assertEqual(
            {item["content_id"] for item in snapshot["items"]},
            set(CONTENT_IDS[:5]),
        )
        self.assertTrue(
            all(
                set(item)
                == {
                    "content_id",
                    "title",
                    "cover_url",
                    "language",
                    "episode_count",
                }
                for item in snapshot["items"]
            )
        )
        payload = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("spend", payload.lower())
        self.assertNotIn("description", payload.lower())
        self.assertTrue(
            all(
                item["cover_url"].startswith("https://cdn.usrgrow.com/")
                for item in snapshot["items"]
            )
        )

    def test_snapshot_order_is_stable_per_date_and_can_change_next_day(self):
        common = {
            "generated_at": FIXED_TIME,
            "spend_rows": spend_rows(),
            "resource_items": resource_items(),
            "allowed_cover_hosts": ("cdn.usrgrow.com",),
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

    def test_snapshot_selection_follows_spend_rank_not_resource_input_order(self):
        snapshot = build_snapshot(
            source_date="2026-07-26",
            generated_at=FIXED_TIME,
            spend_rows=spend_rows(),
            resource_items=list(reversed(resource_items())),
            allowed_cover_hosts=("cdn.usrgrow.com",),
        )
        self.assertEqual(
            {item["content_id"] for item in snapshot["items"]},
            set(CONTENT_IDS[:5]),
        )

    def test_atomic_write_is_idempotent_and_failure_preserves_old_file(self):
        first = complete_snapshot()
        second = dict(first)
        second["generated_at"] = "2026-07-27T19:00:00+08:00"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            self.assertTrue(atomic_write_snapshot(target, first))
            original = target.read_bytes()
            self.assertFalse(atomic_write_snapshot(target, second))
            self.assertEqual(target.read_bytes(), original)

            changed = complete_snapshot("2026-07-27")
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

    def test_atomic_write_repairs_legacy_or_invalid_snapshot(self):
        snapshot = complete_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "current.json"
            legacy = json.loads(json.dumps(snapshot))
            legacy["spend"] = 999
            target.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertTrue(atomic_write_snapshot(target, snapshot))
            self.assertNotIn(
                "spend",
                json.loads(target.read_text(encoding="utf-8")),
            )

            invalid = json.loads(json.dumps(snapshot))
            invalid["generated_at"] = "not-a-time"
            target.write_text(json.dumps(invalid), encoding="utf-8")
            self.assertTrue(atomic_write_snapshot(target, snapshot))

    def test_refresh_uses_rank_repository_and_shared_resource_service(self):
        repository = _FakeRankingRepository()
        resources = _FakeResourceService()
        result = refresh_script.refresh(
            source_date="2026-07-26",
            cache_path="/unused/current.json",
            dry_run=True,
            repository=repository,
            resource_service=resources,
            generated_at=FIXED_TIME,
        )
        self.assertEqual(result["item_count"], 5)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["changed"])
        self.assertEqual(repository.calls, ["2026-07-26"])
        self.assertEqual(
            [content_id for content_id, _allow_stale in resources.calls],
            CONTENT_IDS[:5],
        )

    def test_featured_resource_builder_rejects_other_landing_id(self):
        with mock.patch.dict(
            "os.environ",
            {"TT_DRAMA_RESOURCE_LANDING_ID": "2050"},
            clear=False,
        ):
            with self.assertRaisesRegex(FeaturedRefreshError, "landing_id 2049"):
                refresh_script._build_resource_service()

    def test_dry_run_help_discloses_resource_cache_fill(self):
        help_text = " ".join(refresh_script._parser().format_help().split())
        self.assertIn("public featured JSON", help_text)
        self.assertIn("resource cache may still be filled", help_text)

    def test_refresh_never_overwrites_when_resources_are_incomplete(self):
        repository = _FakeRankingRepository()
        resources = _FakeResourceService(
            {
                content_id: ResourceOutcome(False, None, "NEGATIVE_HIT")
                for content_id in CONTENT_IDS[4:]
            }
        )
        with mock.patch.object(
            refresh_script,
            "atomic_write_snapshot",
        ) as write_snapshot:
            with self.assertRaisesRegex(FeaturedRefreshError, "only 4 of 5"):
                refresh_script.refresh(
                    source_date="2026-07-26",
                    cache_path="/unused/current.json",
                    repository=repository,
                    resource_service=resources,
                    generated_at=FIXED_TIME,
                )
        write_snapshot.assert_not_called()

    def test_featured_unit_runs_shared_release_and_grants_both_state_paths(self):
        root = Path(__file__).resolve().parents[1]
        unit = (
            root / "deploy" / "tt-drama-featured.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "WorkingDirectory=/mnt/data-disk/tt-drama-resource-cache/current",
            unit,
        )
        self.assertIn(
            "ExecStart=/usr/bin/python3 "
            "/mnt/data-disk/tt-drama-resource-cache/current/"
            "scripts/refresh_tt_drama_featured.py",
            unit,
        )
        self.assertIn(
            "TT_DRAMA_RESOURCE_DB_PATH="
            "/mnt/data-disk/tt-drama-resource-cache/state/resources.sqlite3",
            unit,
        )
        self.assertIn(
            "/mnt/data-disk/tt-drama-resource-cache/state",
            unit,
        )
        self.assertIn(
            "ExecStartPre=+/usr/bin/install -d -m 2770 "
            "-o tt-drama-featured -g tt-drama-featured "
            "/mnt/data-disk/tt-drama-resource-cache/state",
            unit,
        )
        self.assertIn("/mnt/data-disk/tt-drama-featured/public", unit)
        self.assertIn("TimeoutStartSec=180s", unit)
        self.assertNotIn(
            "WorkingDirectory=/mnt/data-disk/tt-drama-featured/current",
            unit,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
