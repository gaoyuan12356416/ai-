import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "ai_game_performance_dashboard.py"
SPEC = importlib.util.spec_from_file_location("ai_game_performance_dashboard_test_target", MODULE_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def manual_row(source_id, **overrides):
    raw = {
        "source_id": source_id,
        "dt": "2026-08-24",
        "conversion_country": "US",
        "game_name": "Dino Bros",
        "game_id": "2070453956790943744",
        "channel": "googleadwords_int",
        "campaign_id": "24084098776",
        "adset_id": "202074209947",
        "ad_id": "",
        "campaign_name": "campaign",
        "adset_name": "adset",
        "ad_name": "ad",
        "manual_cost": "0",
        "manual_installs": "1",
        "d1_retained": "0",
        "play_duration_seconds": "10",
        "day0_revenue": "0",
        "day1_revenue": "0",
        "updated_at": "2026-08-25 00:00:00",
    }
    raw.update(overrides)
    return dashboard.normalize_manual_row(raw)


def delivery_row(source_id, **overrides):
    raw = {
        "source_id": source_id,
        "dt": "2026-08-24",
        "platform": 1,
        "channel": "googleadwords_int",
        "source_country": "AIG-WW",
        "campaign_id": "24084098776",
        "adset_id": "202074209947",
        "ad_id": "202074209947",
        "source_spend": 100,
        "source_installs": 25,
        "source_impressions": 1000,
        "source_clicks": 100,
        "updated_at": "2026-08-25 00:00:00",
    }
    raw.update(overrides)
    return raw


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache_db = self.root / "cache.sqlite3"
        self.connection = dashboard.cache_conn(self.cache_db)
        dashboard.ensure_cache_schema(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_extract_project_id_is_strict_and_detects_ambiguity(self):
        self.assertEqual(dashboard.extract_project_id("name projectid[12345]"), "12345")
        self.assertEqual(dashboard.extract_project_id("name projectid[x]"), "")
        self.assertEqual(
            dashboard.extract_project_id("projectid[11] and projectid[22]"),
            dashboard.AMBIGUOUS_GAME_ID,
        )

    def test_normalize_manual_row_uses_project_id_and_generic_label(self):
        row = manual_row(
            1,
            game_id="",
            game_name="",
            ad_name="creative projectid[123456789]",
            manual_installs="3",
        )
        self.assertEqual(row["game_id"], "123456789")
        self.assertEqual(row["game_name"], "游戏 123456789")
        generic = manual_row(2, game_id=dashboard.GENERIC_GAME_ID, game_name="")
        self.assertEqual(generic["game_name"], "通用素材")

    def test_duration_column_detection_prefers_live_name_and_supports_legacy(self):
        with mock.patch.object(
            dashboard,
            "run_mysql",
            return_value=[["avg_play_duration_seconds"], ["play_duration_seconds"]],
        ):
            self.assertEqual(dashboard.detect_manual_duration_column(), "play_duration_seconds")
        with mock.patch.object(dashboard, "run_mysql", return_value=[["avg_play_duration_seconds"]]):
            self.assertEqual(dashboard.detect_manual_duration_column(), "avg_play_duration_seconds")
        with mock.patch.object(dashboard, "run_mysql", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "duration column"):
                dashboard.detect_manual_duration_column()

    def test_manual_query_uses_mysql_date_format_tokens(self):
        raw_row = [
            "1",
            "2026-08-24",
            "US",
            "Dino Bros",
            "2070453956790943744",
            "googleadwords_int",
            "24084098776",
            "202074209947",
            "",
            "campaign",
            "adset",
            "ad",
            "0",
            "1",
            "0",
            "10",
            "0",
            "0",
            "2026-08-25 00:00:00",
        ]
        with mock.patch.object(dashboard, "run_mysql", return_value=[raw_row]) as query:
            rows = dashboard.fetch_manual_day("2026-08-24", "play_duration_seconds")
        sql = query.call_args.args[0]
        self.assertNotIn("%%Y", sql)
        self.assertIn("DATE_FORMAT(stat_date, '%Y-%m-%d')", sql)
        self.assertIn("DATE_FORMAT(updated_at, '%Y-%m-%d %H:%i:%s')", sql)
        self.assertIn("play_duration_seconds AS play_duration_seconds", sql)
        self.assertEqual(rows[0]["dt"], "2026-08-24")
        with mock.patch.object(dashboard, "run_mysql", return_value=[raw_row]) as legacy_query:
            dashboard.fetch_manual_day("2026-08-24", "avg_play_duration_seconds")
        self.assertIn(
            "(avg_play_duration_seconds * install) AS play_duration_seconds",
            legacy_query.call_args.args[0],
        )

    def test_read_only_gate_fails_closed(self):
        with mock.patch.object(dashboard, "run_mysql", return_value=[["1"]]):
            dashboard.assert_read_only()
        with mock.patch.object(dashboard, "run_mysql", return_value=[["0"]]):
            with self.assertRaisesRegex(RuntimeError, "read_only"):
                dashboard.assert_read_only()

    def test_unique_ambiguous_and_unmapped_game_assignment(self):
        rows = [manual_row(1)]
        dashboard.replace_manual_day(self.connection, "2026-08-24", rows)
        keys, names = dashboard.build_game_mapping(self.connection)
        game_id, _, status = dashboard.assign_delivery_game(delivery_row(10), keys, names)
        self.assertEqual((game_id, status), ("2070453956790943744", "mapped"))

        other = manual_row(2, game_id="2082282824310779904", game_name="Bring Them Home")
        dashboard.replace_manual_day(self.connection, "2026-08-24", rows + [other])
        keys, names = dashboard.build_game_mapping(self.connection)
        game_id, _, status = dashboard.assign_delivery_game(delivery_row(11), keys, names)
        self.assertEqual((game_id, status), (dashboard.AMBIGUOUS_GAME_ID, "ambiguous"))

        game_id, _, status = dashboard.assign_delivery_game(
            delivery_row(12, campaign_id="999", adset_id="888"), keys, names
        )
        self.assertEqual((game_id, status), (dashboard.UNMAPPED_GAME_ID, "unmapped"))

    def test_overview_keeps_delivery_spend_single_and_uses_total_play_duration(self):
        manual = [
            manual_row(1, manual_installs="2", d1_retained="1", play_duration_seconds="20"),
            manual_row(
                2,
                conversion_country="ID",
                manual_installs="3",
                d1_retained="2",
                play_duration_seconds="60",
            ),
            manual_row(
                3,
                game_id="999",
                game_name="Unity Game",
                channel="unityads_int",
                campaign_id="",
                adset_id="",
                manual_cost="25",
                manual_installs="4",
                d1_retained="1",
                play_duration_seconds="120",
            ),
        ]
        dashboard.replace_manual_day(self.connection, "2026-08-24", manual)
        keys, names = dashboard.build_game_mapping(self.connection)
        dashboard.replace_delivery_day(
            self.connection, "2026-08-24", [delivery_row(10)], keys, names
        )
        rows = dashboard.overview_rows_for_day(self.connection, "2026-08-24")
        google = next(row for row in rows if row["channel"] == "googleadwords_int")
        unity = next(row for row in rows if row["channel"] == "unityads_int")
        self.assertEqual(google["source_spend"], 100)
        self.assertEqual(google["effective_spend"], 100)
        self.assertEqual(google["manual_installs"], 5)
        self.assertEqual(google["d1_retained"], 3)
        self.assertEqual(google["play_total_seconds"], 80)
        self.assertEqual(google["avg_play_duration_seconds"], 16)
        self.assertEqual(google["d1_retention_rate"], 0.6)
        self.assertEqual(unity["effective_spend"], 25)
        self.assertEqual(unity["spend_source"], "manual_fallback")

    def test_replace_day_is_transactional_and_empty_day_removes_old_rows(self):
        original = [manual_row(1)]
        dashboard.replace_manual_day(self.connection, "2026-08-24", original)
        duplicate = [manual_row(2), manual_row(2, conversion_country="ID")]
        with self.assertRaises(sqlite3.IntegrityError):
            dashboard.replace_manual_day(self.connection, "2026-08-24", duplicate)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM manual_conversion_fact WHERE dt='2026-08-24'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        dashboard.replace_manual_day(self.connection, "2026-08-24", [])
        count = self.connection.execute(
            "SELECT COUNT(*) FROM manual_conversion_fact WHERE dt='2026-08-24'"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_quality_counts_mapping_without_distribution(self):
        dashboard.replace_manual_day(self.connection, "2026-08-24", [manual_row(1)])
        keys, names = dashboard.build_game_mapping(self.connection)
        dashboard.replace_delivery_day(
            self.connection,
            "2026-08-24",
            [delivery_row(10), delivery_row(11, campaign_id="999", adset_id="888", source_spend=20)],
            keys,
            names,
        )
        quality = dashboard.quality_for_range(self.connection, "2026-08-24", "2026-08-24")
        self.assertEqual(quality["source_rows"], 2)
        self.assertEqual(quality["mapped_rows"], 1)
        self.assertEqual(quality["unmapped_rows"], 1)
        self.assertAlmostEqual(quality["mapped_spend_ratio"], 100 / 120, places=6)

    def test_encode_rows_round_trip_shape(self):
        rows = dashboard.overview_rows_for_day(self.connection, "2026-08-24")
        encoded = dashboard.encode_rows(rows, dashboard.OVERVIEW_COLUMNS)
        self.assertEqual(encoded["columns"], dashboard.OVERVIEW_COLUMNS)
        self.assertIn("game_name", encoded["dict_columns"])
        self.assertNotIn("effective_spend", encoded["dict_columns"])

    def test_source_query_is_bounded_to_product_date_and_platform(self):
        with mock.patch.object(dashboard, "run_mysql", return_value=[]) as query:
            self.assertEqual(dashboard.fetch_delivery_day("2026-08-24"), [])
        sql = query.call_args.args[0]
        self.assertIn("FORCE INDEX(pss)", sql)
        self.assertIn("product = 'Neonarcade'", sql)
        self.assertIn("dt = '2026-08-24'", sql)
        self.assertIn("platform IN (0,1,3)", sql)

    def seed_publish_cache(self):
        dashboard.replace_manual_day(self.connection, "2026-08-24", [manual_row(1)])
        keys, names = dashboard.build_game_mapping(self.connection)
        dashboard.replace_delivery_day(
            self.connection, "2026-08-24", [delivery_row(10)], keys, names
        )

    def test_publish_writes_version_files_and_latest_manifest(self):
        self.seed_publish_cache()
        output = self.root / "public"
        result = dashboard.publish_from_cache(self.cache_db, output)
        manifest = json.loads((output / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["meta"]["data_version"], result["data_version"])
        self.assertTrue((output / manifest["data_files"]["overview"]["2026-08-24"]).exists())
        self.assertTrue((output / manifest["data_files"]["delivery"]["2026-08-24"]).exists())
        self.assertTrue((output / manifest["data_files"]["conversion"]["2026-08-24"]).exists())
        audit = self.connection.execute("SELECT COUNT(*) FROM publish_audit").fetchone()[0]
        self.assertEqual(audit, 1)

    def test_publish_failure_does_not_replace_latest_commit_point(self):
        self.seed_publish_cache()
        output = self.root / "public"
        output.mkdir(parents=True)
        (output / "latest.json").write_text("old-latest", encoding="utf-8")
        original = dashboard.atomic_write

        def fail_latest(path, content):
            if Path(path).name == "latest.json":
                raise OSError("simulated latest failure")
            return original(path, content)

        with mock.patch.object(dashboard, "atomic_write", side_effect=fail_latest):
            with self.assertRaisesRegex(OSError, "simulated"):
                dashboard.publish_from_cache(self.cache_db, output)
        self.assertEqual((output / "latest.json").read_text(encoding="utf-8"), "old-latest")

    def test_mysql_error_redaction(self):
        message = dashboard.redact_mysql_error(
            "mysql --password=secret -pother SELECT token", ("token", "secret")
        )
        self.assertNotIn("secret", message)
        self.assertNotIn("other", message)
        self.assertNotIn("token", message)

    def test_mysql_module_is_loaded_from_explicit_base_directory(self):
        module_dir = self.root / "base-module"
        module_dir.mkdir()
        (module_dir / "opera_product_daily_dashboard.py").write_text(
            "def mysql_cmd():\n    return ['mysql', '--password=server-secret', '-N']\n",
            encoding="utf-8",
        )
        sys.modules.pop("opera_product_daily_dashboard", None)
        with mock.patch.object(dashboard, "BASE_MODULE_DIR", module_dir):
            command, environment, secrets = dashboard.mysql_command_env()
        sys.modules.pop("opera_product_daily_dashboard", None)
        self.assertEqual(command, ["mysql", "-N"])
        self.assertEqual(environment["MYSQL_PWD"], "server-secret")
        self.assertEqual(secrets[-1], "server-secret")

    def test_version_prune_ignores_unrelated_directories(self):
        data = self.root / "public" / "data"
        current = "20260825T120000123456+0800"
        old = data / "20260824T120000123456+0800"
        unrelated = data / "operator-notes"
        for path in (data / current, old, unrelated):
            path.mkdir(parents=True)
        old_time = 1_700_000_000
        import os

        os.utime(old, (old_time, old_time))
        os.utime(unrelated, (old_time, old_time))
        removed = dashboard.prune_published_versions(
            data, current, now=old_time + dashboard.PUBLISHED_FILE_STALE_GRACE_SECONDS + 1
        )
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
