#!/usr/bin/env python3
"""Offline tests for X auto-post metric refresh and READY cache reads."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_auto_posts.repositories import (  # noqa: E402
    DEFAULT_PLATFORM,
    DEFAULT_PRODUCT,
    DailyMetricRow,
    MetricWindowNotReady,
    MetricWindowRepository,
    ReadOnlyMySQLRepository,
    SourceDataError,
    complete_beijing_dates,
    refresh_metric_day,
)
from features.x_auto_posts.core import XPostAutoStore  # noqa: E402
from scripts.x_auto_post_metric_runner import (  # noqa: E402
    MetricRunnerConfig,
    execute_metric_refresh,
    requested_metric_dates,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.offset = 0
        self.executions = []
        self.fetchmany_sizes = []
        self.closed = False

    def execute(self, sql, params):
        self.executions.append((str(sql), tuple(params)))

    def fetchmany(self, size):
        self.fetchmany_sizes.append(size)
        batch = self.rows[self.offset : self.offset + size]
        self.offset += len(batch)
        return batch

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, rows):
        self.cursor_value = FakeCursor(rows)
        self.cursor_classes = []
        self.closed = False

    def cursor(self, cursor_class=None):
        self.cursor_classes.append(cursor_class)
        return self.cursor_value

    def close(self):
        self.closed = True


def mysql_repo(rows, **kwargs):
    connection = FakeConnection(rows)
    repository = ReadOnlyMySQLRepository(
        lambda: connection,
        stream_batch_size=kwargs.pop("stream_batch_size", 1),
        now_fn=lambda: NOW,
        **kwargs,
    )
    return repository, connection


def cache_row(metric_date, content_id, material_id, spend, revenue, platform=0):
    return {
        "metric_date": metric_date,
        "platform": platform,
        "content_id": str(content_id),
        "material_id": str(material_id),
        "spend": str(spend),
        "af_revenue0": str(revenue),
    }


class FakeCacheStore:
    def __init__(self, rows=(), ready=()):
        self.rows = list(rows)
        self.ready = set(ready)
        self.ready_calls = []
        self.row_calls = []

    def ready_metric_dates(self, platform, dates, *, product=None):
        self.ready_calls.append((platform, tuple(dates), product))
        return self.ready.intersection(dates)

    def iter_ready_metric_rows(
        self,
        platform,
        dates,
        content_ids=None,
        *,
        product=None,
    ):
        self.row_calls.append(
            (platform, tuple(dates), tuple(content_ids or ()), product)
        )
        allowed_dates = set(dates)
        allowed_content = set(content_ids or ())
        return [
            row
            for row in self.rows
            if row["metric_date"] in allowed_dates
            and row["content_id"] in allowed_content
        ]


class StoreResult:
    def __init__(self, **values):
        self.values = dict(values)

    def as_dict(self):
        return dict(self.values)


class FakeGenerationStore:
    def __init__(self, *, fail_after=None):
        self.fail_after = fail_after
        self.record_calls = []
        self.activations = []
        self.events = []
        self.next_id = 1

    def record_metric_generation(self, **kwargs):
        self.events.append("record_started")
        captured = []
        for row in kwargs["rows"]:
            captured.append(dict(row))
            self.events.append("row_%s" % len(captured))
            if self.fail_after is not None and len(captured) >= self.fail_after:
                raise RuntimeError("simulated generation write failure")
        generation_id = self.next_id
        self.next_id += 1
        call = dict(kwargs)
        call["rows"] = captured
        call["generation_id"] = generation_id
        self.record_calls.append(call)
        self.events.append("record_complete")
        return StoreResult(
            id=generation_id,
            platform=kwargs["platform"],
            metric_date=kwargs["metric_date"],
            product=kwargs["product"],
            status="ready",
        )

    def activate_metric_generation(self, generation_id):
        self.events.append("activate")
        self.activations.append(generation_id)
        call = self.record_calls[generation_id - 1]
        return StoreResult(
            id=generation_id,
            platform=call["platform"],
            metric_date=call["metric_date"],
            product=call["product"],
            status="ready",
        )


class FakeMetricSource:
    product = DEFAULT_PRODUCT

    def __init__(self, rows_by_date):
        self.rows_by_date = dict(rows_by_date)
        self.calls = []

    def now_fn(self):
        return NOW

    def stream_metric_day(self, metric_date, *, platform=0):
        self.calls.append((metric_date, platform))
        yield from self.rows_by_date.get(metric_date, ())


def daily(metric_date, content_id="C1", material_id="101", spend="10", revenue="20"):
    return DailyMetricRow(
        metric_date=metric_date,
        platform=0,
        content_id=content_id,
        material_id=material_id,
        spend=Decimal(spend),
        af_revenue0=Decimal(revenue),
    )


class ReadOnlyRepositoryTests(unittest.TestCase):
    def test_daily_metric_query_is_parameterized_day_bounded_and_streamed(self):
        repository, connection = mysql_repo(
            [
                {
                    "content_id": "C1",
                    "material_id": "101",
                    "spend": "10.25",
                    "af_revenue0": "20.50",
                },
                {
                    "content_id": "C2",
                    "material_id": "202",
                    "spend": "3",
                    "af_revenue0": "0",
                },
            ]
        )
        rows = list(repository.stream_metric_day("2026-08-04", platform=0))
        self.assertEqual(rows[0].spend, Decimal("10.25"))
        self.assertEqual(rows[0].af_revenue0, Decimal("20.50"))
        sql, params = connection.cursor_value.executions[0]
        self.assertRegex(sql.lstrip(), r"^SELECT\b")
        self.assertIn("s.dt=%s", sql)
        self.assertIn("GROUP BY TRIM(s.data_source_id),TRIM(s.resource_id)", sql)
        self.assertIn(
            "ORDER BY TRIM(s.data_source_id),\n                      TRIM(s.resource_id)",
            sql,
        )
        self.assertNotIn("CAST(TRIM(s.resource_id) AS UNSIGNED)", sql)
        self.assertNotIn("2026-08-04", sql)
        self.assertEqual(params, (DEFAULT_PRODUCT, DEFAULT_PLATFORM, "2026-08-04"))
        self.assertGreaterEqual(len(connection.cursor_value.fetchmany_sizes), 3)
        self.assertEqual(connection.cursor_classes[0].__name__, "SSDictCursor")
        self.assertTrue(connection.cursor_value.closed)
        self.assertTrue(connection.closed)

    def test_duplicate_daily_identity_fails_closed(self):
        raw = {
            "content_id": "C1",
            "material_id": "101",
            "spend": "1",
            "af_revenue0": "2",
        }
        repository, _connection = mysql_repo([raw, dict(raw)])
        with self.assertRaisesRegex(SourceDataError, "duplicate identity"):
            list(repository.stream_metric_day("2026-08-04"))

    def test_typed_blacklist_is_exact_and_unsupported_type_fails(self):
        repository, connection = mysql_repo(
            [
                {"type": 0, "content_id": "SERIES-1"},
                {"type": 1, "content_id": "C1"},
            ]
        )
        snapshot = repository.blacklist_snapshot()
        self.assertEqual(snapshot.drama_series_codes, frozenset({"SERIES-1"}))
        self.assertEqual(snapshot.material_data_source_ids, frozenset({"C1"}))
        sql, params = connection.cursor_value.executions[0]
        self.assertIn("`ads_setting`.ads_facebook_post_blacklist", sql)
        self.assertEqual(params, (0,))

        invalid, _connection = mysql_repo([{"type": 2, "content_id": "C1"}])
        with self.assertRaisesRegex(SourceDataError, "unsupported"):
            invalid.blacklist_snapshot()

    def test_catalog_queries_enforce_fixed_scope_in_sql_and_params(self):
        drama_repository, drama_connection = mysql_repo(
            [
                {
                    "source_row_id": "1",
                    "content_id": "C1",
                    "series_code": "S1",
                    "language": "en",
                    "resource_type_v2": "2",
                    "deploy_time": 100,
                    "app_id": 1479,
                    "release_status": 1,
                    "name": "Drama",
                    "app": "com.dramawave.app",
                }
            ]
        )
        dramas = drama_repository.list_drama_rows(
            language="en",
            now_epoch=200,
            deploy_since_epoch=50,
            resource_types=("2",),
        )
        self.assertEqual(dramas[0].content_id, "C1")
        drama_sql, drama_params = drama_connection.cursor_value.executions[0]
        self.assertIn("i.release_status=%s", drama_sql)
        self.assertIn("i.deploy_time<=%s", drama_sql)
        self.assertIn("resource_type_v2 AS CHAR) IN (%s)", drama_sql)
        self.assertEqual(drama_params, (1479, 1, "en", 0, 200, 50, "2"))

        material_repository, material_connection = mysql_repo(
            [
                {
                    "material_id": "101",
                    "content_id": "C1",
                    "language": "en",
                    "product": "Dramawave",
                    "material_type": 2,
                    "is_delete": 0,
                    "media_url": "https://cdn.example.test/101.mp4",
                    "material_name": "Material",
                    "video_duration": "30",
                    "data_source": 6,
                    "tag_name": "safe",
                }
            ]
        )
        materials = material_repository.list_material_rows(
            content_id="C1",
            language="en",
        )
        self.assertEqual(materials[0].material_id, "101")
        material_sql, material_params = material_connection.cursor_value.executions[0]
        self.assertIn("FORCE INDEX (idx_source_type_source_id)", material_sql)
        self.assertEqual(
            material_params,
            (6, "C1", "Dramawave", 2, 0, "en", 0, 600),
        )


class MetricWindowTests(unittest.TestCase):
    def test_ratio_of_sums_uses_same_daily_facts_for_both_levels(self):
        dates = ("2026-08-03", "2026-08-04")
        store = FakeCacheStore(
            ready=dates,
            rows=[
                cache_row(dates[0], "C1", "101", "10", "20"),
                cache_row(dates[1], "C1", "101", "90", "0"),
                cache_row(dates[1], "C1", "102", "100", "100"),
            ],
        )
        snapshot = MetricWindowRepository(store).load(
            platform=0,
            metric_dates=dates,
            content_ids=("C1",),
        )
        self.assertEqual(snapshot.material("C1", "101").spend, Decimal("100"))
        self.assertEqual(snapshot.material("C1", "101").d0_roas, Decimal("20"))
        self.assertEqual(snapshot.drama("C1").spend, Decimal("200"))
        self.assertEqual(snapshot.drama("C1").af_revenue0, Decimal("120"))
        self.assertEqual(snapshot.drama("C1").d0_roas, Decimal("60"))
        self.assertEqual(store.ready_calls[0][2], DEFAULT_PRODUCT)
        self.assertEqual(store.row_calls[0][3], DEFAULT_PRODUCT)

    def test_missing_ready_day_fails_before_any_row_read(self):
        dates = ("2026-08-03", "2026-08-04")
        store = FakeCacheStore(ready=(dates[0],))
        with self.assertRaises(MetricWindowNotReady) as caught:
            MetricWindowRepository(store).load(
                platform=0,
                metric_dates=dates,
                content_ids=("C1",),
            )
        self.assertEqual(caught.exception.missing_dates, (dates[1],))
        self.assertEqual(store.row_calls, [])

    def test_ready_empty_day_is_valid_and_zero_spend_roas_is_none(self):
        store = FakeCacheStore(ready=("2026-08-04",))
        snapshot = MetricWindowRepository(store).load(
            platform=0,
            metric_dates=("2026-08-04",),
            content_ids=("C1",),
        )
        self.assertEqual(snapshot.drama("C1").spend, Decimal("0"))
        self.assertIsNone(snapshot.drama("C1").d0_roas)

    def test_cache_row_outside_requested_scope_fails_closed(self):
        date = "2026-08-04"
        store = FakeCacheStore(
            ready=(date,),
            rows=[cache_row(date, "C2", "201", 1, 2)],
        )
        # Simulate a buggy store leaking an out-of-scope row.
        store.iter_ready_metric_rows = lambda *args, **kwargs: list(store.rows)
        with self.assertRaisesRegex(SourceDataError, "outside the requested scope"):
            MetricWindowRepository(store).load(
                platform=0,
                metric_dates=(date,),
                content_ids=("C1",),
            )

        wrong_product = cache_row(date, "C1", "101", 1, 2)
        wrong_product["product"] = "OtherProduct"
        store = FakeCacheStore(ready=(date,), rows=(wrong_product,))
        with self.assertRaisesRegex(SourceDataError, "outside the requested scope"):
            MetricWindowRepository(store).load(
                platform=0,
                metric_dates=(date,),
                content_ids=("C1",),
            )

    def test_duplicate_daily_cache_identity_fails_closed(self):
        date = "2026-08-04"
        row = cache_row(date, "C1", "101", 1, 2)
        store = FakeCacheStore(ready=(date,), rows=(row, dict(row)))
        with self.assertRaisesRegex(SourceDataError, "duplicate daily identity"):
            MetricWindowRepository(store).load(
                platform=0,
                metric_dates=(date,),
                content_ids=("C1",),
            )


class MetricRefreshTests(unittest.TestCase):
    def test_generation_is_fully_written_before_activation(self):
        date = "2026-08-04"
        source = FakeMetricSource(
            {date: [daily(date), daily(date, "C2", "202", "3", "4")]}
        )
        store = FakeGenerationStore()
        result = refresh_metric_day(source, store, date, refreshed_at=NOW)
        self.assertEqual(result["id"], 1)
        self.assertEqual(store.activations, [1])
        self.assertEqual(
            store.events,
            ["record_started", "row_1", "row_2", "record_complete", "activate"],
        )
        self.assertEqual(store.record_calls[0]["product"], DEFAULT_PRODUCT)
        self.assertEqual(store.record_calls[0]["platform"], DEFAULT_PLATFORM)
        self.assertEqual(store.record_calls[0]["refreshed_at_utc"], "2026-08-05T10:00:00Z")

    def test_partial_generation_failure_never_activates(self):
        date = "2026-08-04"
        source = FakeMetricSource({date: [daily(date), daily(date, "C2", "202")]})
        store = FakeGenerationStore(fail_after=1)
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            refresh_metric_day(source, store, date)
        self.assertEqual(store.activations, [])
        self.assertNotIn("activate", store.events)

    def test_runner_refreshes_each_day_as_an_independent_generation(self):
        dates = ("2026-08-03", "2026-08-04")
        source = FakeMetricSource({date: [daily(date)] for date in dates})
        store = FakeGenerationStore()
        result = execute_metric_refresh(
            source,
            store,
            metric_dates=dates,
            platform=0,
            now=NOW,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(source.calls, [(dates[0], 0), (dates[1], 0)])
        self.assertEqual(store.activations, [1, 2])
        self.assertEqual(
            [item["metric_date"] for item in result["completed"]],
            list(dates),
        )

    def test_real_store_refresh_and_window_reader_contract(self):
        date = "2026-08-04"
        source = FakeMetricSource(
            {date: [daily(date, spend="12.5", revenue="5")]}
        )
        with tempfile.TemporaryDirectory() as directory:
            store = XPostAutoStore(str(Path(directory) / "auto.sqlite3"))
            activated = refresh_metric_day(
                source,
                store,
                date,
                platform=0,
                refreshed_at=NOW,
            )
            self.assertEqual(activated["metric_date"], date)
            snapshot = MetricWindowRepository(store).load(
                platform=0,
                metric_dates=(date,),
                content_ids=("C1",),
            )
        self.assertEqual(snapshot.material("C1", "101").spend, Decimal("12.5"))
        self.assertEqual(snapshot.material("C1", "101").d0_roas, Decimal("40"))


class MetricRunnerTests(unittest.TestCase):
    def test_requested_dates_are_complete_beijing_days_only(self):
        expected = list(complete_beijing_dates(NOW, 7))
        self.assertEqual(
            requested_metric_dates(explicit_dates=(), lookback_days=7, now=NOW),
            expected,
        )
        self.assertEqual(
            requested_metric_dates(
                explicit_dates=(expected[-1], expected[0], expected[-1]),
                lookback_days=7,
                now=NOW,
            ),
            [expected[0], expected[-1]],
        )
        with self.assertRaisesRegex(ValueError, "complete Beijing days"):
            requested_metric_dates(
                explicit_dates=("2026-08-05",),
                lookback_days=7,
                now=NOW,
            )

    def test_config_defaults_to_dramawave_platform_zero_and_redacts_secret(self):
        config = MetricRunnerConfig.from_env(
            {
                "X_AUTO_POST_MYSQL_USER": "readonly",
                "X_AUTO_POST_MYSQL_PASSWORD": "top-secret",
            }
        )
        self.assertEqual(config.product, DEFAULT_PRODUCT)
        self.assertEqual(config.platform, DEFAULT_PLATFORM)
        self.assertNotIn("top-secret", repr(config))
        self.assertIn("<redacted>", repr(config))
        with self.assertRaisesRegex(ValueError, "configuration is invalid"):
            MetricRunnerConfig.from_env(
                {
                    "X_AUTO_POST_MYSQL_USER": "readonly",
                    "X_AUTO_POST_MYSQL_PASSWORD": "top-secret",
                    "X_AUTO_POST_PRODUCT": "OtherProduct",
                }
            )
        with self.assertRaisesRegex(ValueError, "configuration is invalid"):
            MetricRunnerConfig.from_env(
                {
                    "X_AUTO_POST_MYSQL_USER": "readonly",
                    "X_AUTO_POST_MYSQL_PASSWORD": "top-secret",
                    "X_AUTO_POST_METRIC_PLATFORM": "1",
                }
            )

    def test_metric_refresh_rejects_nonzero_platform(self):
        source = FakeMetricSource([])
        store = FakeGenerationStore()
        with self.assertRaisesRegex(ValueError, "exactly 0"):
            execute_metric_refresh(
                source,
                store,
                metric_dates=["2026-08-04"],
                platform=1,
                now=NOW,
            )

if __name__ == "__main__":
    unittest.main()
