import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "opay_excellent_creatives", HERE / "opay_excellent_creatives.py"
)
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


class BasicRuleTests(unittest.TestCase):
    def test_app_aliases_and_exclusions(self):
        self.assertEqual(report.app_key("OPay"), "NG OPay")
        self.assertEqual(report.app_key("opay ngn"), "NG OPay")
        self.assertEqual(report.app_key("OPayPakistan"), "PK OPay")
        self.assertEqual(report.app_key("OperaNews"), "")
        self.assertEqual(report.app_key("OPayBusiness"), "")

    def test_spend_is_treated_as_usd_without_second_conversion(self):
        self.assertEqual(report.cents("8542.36"), 854236)
        self.assertEqual(report.dollars(854236), 8542.36)

    def test_strict_cpa_and_ctr_boundaries(self):
        self.assertFalse(report.cpa_strictly_lower(10000, 10, 100000, 100))
        self.assertTrue(report.cpa_strictly_lower(9999, 10, 100000, 100))
        self.assertFalse(report.cpa_strictly_lower(1, 0, 100, 1))
        self.assertFalse(report.ctr_strictly_greater(10, 1000, 100, 10000))
        self.assertTrue(report.ctr_strictly_greater(11, 1000, 100, 10000))
        self.assertFalse(report.ctr_strictly_greater(1, 0, 0, 0))

    def test_rule_b_spend_boundary_is_strict(self):
        platform = {"clicks": 100, "impressions": 10000}
        self.assertFalse(
            report.rule_b_qualifies(
                {"spend_cents": 500000, "clicks": 20, "impressions": 1000}, platform
            )
        )
        self.assertTrue(
            report.rule_b_qualifies(
                {"spend_cents": 500001, "clicks": 20, "impressions": 1000}, platform
            )
        )

    def test_top_half_crossing_and_spend_ties(self):
        materials = [
            {"custom_source_id": 1, "spend_cents": 4000},
            {"custom_source_id": 2, "spend_cents": 3000},
            {"custom_source_id": 3, "spend_cents": 3000},
            {"custom_source_id": 4, "spend_cents": 1000},
        ]
        top, ranks, cumulative = report.top_half_members(materials, 10000)
        self.assertEqual(top, {1, 2, 3})
        self.assertEqual(ranks[2], ranks[3])
        self.assertEqual(cumulative[2], 1.0)
        self.assertNotIn(4, top)


class KeywordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = report.load_keyword_config()
        cls.overrides = {"schema_version": 1, "overrides": {}}

    def test_workbook_conversion_counts_and_statuses(self):
        summary = self.config["summary"]
        self.assertEqual(summary["ng_entries"], 80)
        self.assertEqual(summary["pk_entries"], 10)
        self.assertEqual(summary["unavailable_entries"], 14)
        self.assertEqual(summary["duplicate_display_keyword_entries"], 2)
        self.assertEqual(self.config["config_version"], "2026-08-26-6c65d01ffc03")

    def test_exact_ng_tag_uses_ad_platform_keyword(self):
        points, status, label = report.match_selling_points(
            123,
            "NG OPay",
            ["NUB_AT.N100_UED"],
            "creative.mp4",
            self.config,
            self.overrides,
        )
        self.assertEqual([point["keyword"] for point in points], ["NUB_Airtime.N100"])
        self.assertEqual(status, "unavailable")
        self.assertEqual(label, "过期/不可用")
        self.assertEqual(points[0]["match_source"], "exact_tag")

    def test_boundary_longest_supports_multiple_points(self):
        points, status, _label = report.match_selling_points(
            456,
            "NG OPay",
            ["influencer,new,product"],
            "OPay_Data_Free_2G-YTNight_MGM_N6000_720x1280.mp4",
            self.config,
            self.overrides,
        )
        keywords = {point["keyword"] for point in points}
        self.assertIn("Data_Free_2G-YTNight", keywords)
        self.assertIn("MGM_N6000", keywords)
        self.assertEqual(status, "available")

    def test_manual_override_wins(self):
        override = {
            "schema_version": 1,
            "overrides": {"999": {"selling_point_ids": ["PK-002"]}},
        }
        points, status, _label = report.match_selling_points(
            999, "PK OPay", ["not-a-match"], "file.png", self.config, override
        )
        self.assertEqual(points[0]["keyword"], "CQZ_Free")
        self.assertEqual(points[0]["match_source"], "manual")
        self.assertEqual(status, "available")

    def test_unmatched_is_pending(self):
        points, status, label = report.match_selling_points(
            1000,
            "PK OPay",
            ["unknown"],
            "unknown-file.mp4",
            self.config,
            self.overrides,
        )
        self.assertEqual(points, [])
        self.assertEqual((status, label), ("pending", "待补关键词"))


class MappingAndAggregationTests(unittest.TestCase):
    def make_connection(self, root):
        return report.cache_conn(Path(root) / "cache.sqlite3")

    def insert_material_dim(self, connection, material_id, product="OPay", name=None):
        connection.execute(
            """
            INSERT INTO material_dim(
              custom_source_id,product,material_type,name,source_url,cover_url,designer,
              maker,maker_source,tag_name,created_at,updated_at,is_delete,fetched_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                material_id,
                product,
                2,
                name or "MGM_N6000.mp4",
                "https://example.myqcloud.com/source.mp4",
                "https://example.myqcloud.com/cover.jpg",
                "283",
                "胡杰",
                "admin_id",
                "MGM_N6000",
                "2026-01-01 00:00:00",
                "2026-01-01 00:00:00",
                0,
                "2026-08-26T00:00:00+08:00",
            ),
        )

    def test_multi_material_ad_day_is_excluded_and_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            connection = self.make_connection(tmp)
            for source_id, material_id in ((1001, 1), (1002, 2)):
                connection.execute(
                    "INSERT INTO ads_source_dim VALUES(?,?,?,?)",
                    (source_id, 3, material_id, "now"),
                )
                self.insert_material_dim(connection, material_id)
            connection.commit()
            insight_rows = []
            for index, (source_id, material_id, spend) in enumerate(
                ((1001, 1, "100.00"), (1002, 2, "200.00")), 1
            ):
                insight_rows.append(
                    {
                        "id": str(index),
                        "dt": "2026-07-01",
                        "platform": "0",
                        "app_id": "OPay NGN",
                        "campaign_id": "10",
                        "adset_id": "20",
                        "ad_id": "30",
                        "resource_id": str(material_id),
                        "source_id": str(source_id),
                        "source_type": "3",
                        "resource_type": "2",
                        "resource_name": "creative.mp4",
                        "resource_tag": "MGM_N6000",
                        "spend": spend,
                        "impressions": "1000",
                        "clicks": "10",
                        "installs": "1",
                        "auto_publish_dt": "",
                        "updated_at": "2026-07-02 00:00:00",
                    }
                )
            report.process_day(connection, "2026-07-01", insight_rows, [])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM material_daily").fetchone()[0], 0)
            audit = connection.execute(
                "SELECT ambiguous_spend_cents,ambiguous_ad_days FROM daily_audit WHERE dt='2026-07-01' AND platform=0 AND app='NG OPay'"
            ).fetchone()
            self.assertEqual(tuple(audit), (30000, 1))
            connection.close()

    def test_build_payload_applies_or_rules_and_coverage_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            connection = self.make_connection(tmp)
            for material_id in (1, 2, 3):
                self.insert_material_dim(connection, material_id)
            connection.executemany(
                "INSERT INTO platform_daily VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("2026-07-01", 0, "NG OPay", 1000000, 10000, 100, 500, 10),
                    ("2026-07-01", 3, "NG OPay", 2000000, 20000, 200, 800, 20),
                ],
            )
            connection.executemany(
                "INSERT INTO af_daily VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("2026-07-01", 0, "NG OPay", "1", "2", "3", 100, 1),
                    ("2026-07-01", 3, "NG OPay", "4", "5", "6", 200, 1),
                ],
            )
            connection.executemany(
                "INSERT INTO material_daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("2026-07-01", 0, "NG OPay", 1, "1", "2", "3", 400000, 4000, 80, 50, 50, "2026-06-20", '["MGM_N6000"]', 1, "exact"),
                    ("2026-07-01", 0, "NG OPay", 2, "1", "2", "4", 200000, 3000, 10, 20, 0, "", '["MGM_N6000"]', 1, "exact"),
                    ("2026-07-01", 3, "NG OPay", 3, "4", "5", "6", 600001, 10000, 200, 30, 0, "", '["MGM_N6000"]', 1, "exact"),
                ],
            )
            connection.executemany(
                "INSERT INTO daily_audit VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("2026-07-01", 0, "NG OPay", 1000000, 600000, 0, 400000, 0, 0, 10, 2, 1, 0, 100, 50),
                    ("2026-07-01", 3, "NG OPay", 2000000, 600001, 0, 1399999, 0, 0, 20, 1, 1, 0, 200, 0),
                ],
            )
            connection.commit()
            payload = report.build_month_payload(
                connection,
                "2026-07",
                "final",
                report.load_keyword_config(),
                {"schema_version": 1, "overrides": {}},
            )
            by_id = {row["custom_source_id"]: row for row in payload["rows"]}
            self.assertEqual(set(by_id), {1, 3})
            self.assertEqual(by_id[1]["selection_rule"], "A")
            self.assertEqual(by_id[3]["selection_rule"], "B")
            self.assertFalse(by_id[3]["evidence"]["rule_a_available"])
            google_audits = [audit for audit in payload["audits"] if audit["channel"] == "Google"]
            self.assertEqual(len(google_audits), 2)
            self.assertTrue(all(audit["selected_count"] == 0 for audit in google_audits))
            connection.close()


class ConfigurationAndMediaTests(unittest.TestCase):
    def test_month_refresh_reloads_mutable_dimensions(self):
        insight = [{"platform": "0", "source_id": "101", "resource_id": "201"}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            report, "assert_read_only"
        ), mock.patch.object(
            report, "load_product_config", return_value={8: "NG OPay", 995: "PK OPay"}
        ), mock.patch.object(
            report, "each_date", return_value=iter(["2026-07-01"])
        ), mock.patch.object(
            report, "fetch_insight_day", return_value=insight
        ), mock.patch.object(
            report, "fetch_af_day", return_value=[]
        ), mock.patch.object(
            report, "ensure_dimensions"
        ) as ensure, mock.patch.object(
            report,
            "process_day",
            return_value={
                "day": "2026-07-01",
                "insight_rows": 1,
                "af_rows": 0,
                "exact_material_ad_rows": 0,
                "ambiguous_ad_days": 0,
            },
        ):
            report.refresh_month("2026-07", Path(tmp) / "cache.sqlite3")
        self.assertTrue(ensure.call_args.kwargs["force"])

    def test_maker_resolution_accepts_admin_id_and_username(self):
        by_id = {283: "胡杰"}
        by_username = {"zxs-1": "张小双"}
        self.assertEqual(report.resolve_maker("283", by_id, by_username), ("胡杰", "admin_id"))
        self.assertEqual(
            report.resolve_maker("zxs-1", by_id, by_username), ("张小双", "username")
        )
        self.assertEqual(
            report.resolve_maker("unknown", by_id, by_username), ("未登记", "unresolved")
        )

    def test_product_event_is_dynamic_and_required(self):
        with mock.patch.object(
            report,
            "run_mysql",
            return_value=[["8", "OPay", "First_Transaction"], ["995", "OPayPakistan", "First_Transaction"]],
        ):
            self.assertEqual(report.load_product_config(), {8: "NG OPay", 995: "PK OPay"})
        with mock.patch.object(
            report,
            "run_mysql",
            return_value=[["8", "OPay", "other"], ["995", "OPayPakistan", "First_Transaction"]],
        ):
            with self.assertRaises(RuntimeError):
                report.load_product_config()

    def test_read_only_port_guard(self):
        with mock.patch.object(
            report, "mysql_command_env", return_value=(["mysql", "-P63350"], {}, ())
        ), mock.patch.object(report, "run_mysql", return_value=[["1"]]):
            report.assert_read_only()
        with mock.patch.object(
            report, "mysql_command_env", return_value=(["mysql", "--port=63353"], {}, ())
        ), mock.patch.object(report, "run_mysql", return_value=[["1"]]):
            with self.assertRaises(RuntimeError):
                report.assert_read_only()
        with mock.patch.object(
            report, "mysql_command_env", return_value=(["mysql", "-P", "63350"], {}, ())
        ), mock.patch.object(report, "run_mysql", return_value=[["0"]]):
            with self.assertRaises(RuntimeError):
                report.assert_read_only()

    def test_media_url_https_upgrade_and_ssrf_guard(self):
        url, status = report.normalize_media_url(
            "http://advertising-1.cos.ap-hongkong.myqcloud.com/a/video.mp4"
        )
        self.assertTrue(url.startswith("https://"))
        self.assertEqual(status, "safe")
        self.assertEqual(report.normalize_media_url("http://127.0.0.1/file")[1], "unsafe_host")
        self.assertEqual(report.normalize_media_url("file:///etc/passwd")[1], "unsafe_url")
        self.assertEqual(report.normalize_media_url("https://example.myqcloud.com:bad/a")[1], "invalid_url")

    def test_unsafe_media_degrades_row_without_removal(self):
        row = {
            "custom_source_id": 1,
            "material_type": "PIC",
            "source_url": "",
            "source_status": "pending",
            "source_status_detail": "",
            "thumbnail_url": "",
            "thumbnail_status": "pending",
            "_media": {"source_url": "http://127.0.0.1/file.jpg", "cover_url": "", "updated_at": ""},
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = report.build_thumbnail(row, Path(tmp))
        self.assertEqual(result["custom_source_id"], 1)
        self.assertEqual(result["source_status"], "unsafe")
        self.assertEqual(result["thumbnail_status"], "unavailable")

    def test_stage_diff_tracks_added_removed_and_changed(self):
        base = {
            "stage": "initial",
            "rows": [
                {"channel": "Meta", "app": "NG OPay", "custom_source_id": 1, "spend": 1},
                {"channel": "Meta", "app": "NG OPay", "custom_source_id": 2, "spend": 2},
            ],
        }
        current = {
            "stage": "final",
            "rows": [
                {"channel": "Meta", "app": "NG OPay", "custom_source_id": 1, "spend": 3},
                {"channel": "Meta", "app": "NG OPay", "custom_source_id": 3, "spend": 3},
            ],
        }
        diff = report.compute_stage_diff(base, current)
        self.assertEqual((diff["added_count"], diff["removed_count"], diff["changed_count"]), (1, 1, 1))


class SnapshotAndPublishTests(unittest.TestCase):
    def test_final_snapshot_is_frozen_and_publish_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_db = root / "cache.sqlite3"
            data_root = root / "data"
            output = root / "public"
            first = report.save_month_snapshot(
                "2026-07",
                "final",
                cache_db=cache_db,
                data_root=data_root,
                media_enabled=False,
            )
            self.assertEqual(first["status"], "success")
            second = report.save_month_snapshot(
                "2026-07",
                "final",
                cache_db=cache_db,
                data_root=data_root,
                media_enabled=False,
            )
            self.assertEqual(second["status"], "skipped_frozen")
            output.mkdir(parents=True)
            latest = output / "latest.json"
            latest.write_bytes(b"old-manifest")
            original_atomic_write = report.atomic_write

            def fail_latest(path, content, **kwargs):
                if Path(path).name == "latest.json":
                    raise RuntimeError("injected commit failure")
                return original_atomic_write(path, content, **kwargs)

            with mock.patch.object(report, "atomic_write", side_effect=fail_latest):
                with self.assertRaises(RuntimeError):
                    report.publish_visible_state(
                        cache_db=cache_db, data_root=data_root, output_dir=output
                    )
            self.assertEqual(latest.read_bytes(), b"old-manifest")

    def test_successful_publish_has_standard_json_and_all_six_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_db = root / "cache.sqlite3"
            data_root = root / "data"
            output = root / "public"
            report.save_month_snapshot(
                "2026-07",
                "final",
                cache_db=cache_db,
                data_root=data_root,
                media_enabled=False,
            )
            published = report.publish_visible_state(
                cache_db=cache_db, data_root=data_root, output_dir=output
            )
            manifest = json.loads((output / "latest.json").read_text(encoding="utf-8"))
            payload = json.loads(
                (output / "data" / published["data_version"] / "2026-07.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["access"], "public_no_auth")
            self.assertEqual(len(payload["audits"]), 6)
            self.assertEqual(payload["rows"], [])
            self.assertNotIn("Infinity", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
