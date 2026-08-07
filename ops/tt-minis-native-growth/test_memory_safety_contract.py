import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "tt_minis_multi_dim_dashboard.py"


def load_generator():
    sys.modules.setdefault("opera_product_daily_dashboard", types.ModuleType("opera_product_daily_dashboard"))
    spec = importlib.util.spec_from_file_location("tt_minis_memory_safety_generator", str(MODULE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_row(module, day, campaign_id, account_id, spend):
    row = {column: "" for column in module.SOURCE_COLUMNS}
    row.update(
        {
            "metric_level": "campaign",
            "dt": day,
            "optimizer_id": "248",
            "optimizer_name": "optimizer",
            "ad_account_id": account_id,
            "app_id": "dramawaveminis",
            "product": "Dramawave",
            "minis_id": module.DRAMAWAVE_TT_MINIS_ID,
            "series_code": "series",
            "data_source_id": "content",
            "resource_id": "resource",
            "resource_name": "resource name",
            "country": "US",
            "country_group": "Area-US",
            "language": "en",
            "drama_language": "en",
            "campaign_id": campaign_id,
            "campaign_name": "campaign " + campaign_id,
            "adset_id": "campaign层级不可用",
            "adset_name": "campaign层级不可用",
            "ad_id": "campaign层级不可用",
            "ad_name": "campaign层级不可用",
            "resource_type": "video",
            "source_type": "upload",
            "bid_type": "auto",
            "status": "ACTIVE",
            "op_status": "ENABLE",
            "ad_created_at": day + " 00:00:00",
            "spend": spend,
            "revenue": spend / 2.0,
            "installs": int(spend),
            "impressions": int(spend * 100),
            "clicks": int(spend * 10),
            "ad_impression": int(spend * 40),
            "row_count": 1,
            "roas": 0.5,
            "cpi": 1.0,
            "ctr": 0.1,
        }
    )
    return row


class MemorySafetyContractTest(unittest.TestCase):
    def setUp(self):
        self.module = load_generator()
        self.module.bj_now = lambda: datetime(2026, 8, 7, 16, 0, 0)
        self.validation = {"type": "skipped", "checks": [], "warnings": []}
        self.rows = [
            sample_row(self.module, "2026-08-06", "1001", "2001", 10.25),
            sample_row(self.module, "2026-08-06", "1001", "2001", 5.75),
            sample_row(self.module, "2026-08-07", "1002", "2002", 3.50),
        ]

    def test_timeout_error_redacts_command_arguments(self):
        secret = "do-not-log-this-password"

        def raise_timeout():
            raise subprocess.TimeoutExpired(
                ["mysql", "--password=" + secret],
                300,
                output="stdout-" + secret,
                stderr="stderr-" + secret,
            )

        self.module.main = raise_timeout
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = self.module.run_cli()

        message = stderr.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertIn("timed out after 300 seconds", message)
        self.assertIn("published report was not replaced", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("--password", message)

    def test_run_cli_preserves_success_and_non_timeout_failures(self):
        self.module.main = lambda: 17
        self.assertEqual(17, self.module.run_cli())

        def raise_value_error():
            raise ValueError("unrelated failure")

        self.module.main = raise_value_error
        with self.assertRaisesRegex(ValueError, "unrelated failure"):
            self.module.run_cli()

    def test_summary_payload_skips_rows_without_changing_summary(self):
        full = self.module.build_payload(
            self.rows,
            "2026-08-06",
            "2026-08-07",
            self.validation,
            include_rows=True,
        )
        summary = self.module.build_payload(
            self.rows,
            "2026-08-06",
            "2026-08-07",
            self.validation,
            include_rows=False,
        )

        self.assertEqual({}, summary["dicts"])
        self.assertEqual([], summary["rows"])
        for key in (
            "meta",
            "default_level",
            "levels",
            "dimensions",
            "metrics",
            "columns",
            "dict_columns",
            "totals",
            "daily_totals",
            "validation",
        ):
            self.assertEqual(full[key], summary[key], key)

    def test_cache_reader_streams_cursor_instead_of_fetchall_copy(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        start = source.index("def fetch_rows_from_cache")
        end = source.index("\n\ndef aggregate_totals", start)
        function_source = source[start:end]
        self.assertIn("for item in cursor:", function_source)
        self.assertNotIn(".fetchall()", function_source)

    def test_streamed_cache_summary_matches_materialized_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.module.CACHE_DB = Path(temp_dir) / "fixture.sqlite3"
            with self.module.cache_conn() as conn:
                self.module.ensure_cache_schema(conn)
                placeholders = ",".join(["?"] * (len(self.module.SOURCE_COLUMNS) + 1))
                conn.executemany(
                    "INSERT INTO tt_minis_multi_dim_rows (%s, refreshed_at) VALUES (%s)"
                    % (",".join(self.module.SOURCE_COLUMNS), placeholders),
                    (
                        [row.get(column, "") for column in self.module.SOURCE_COLUMNS]
                        + ["2026-08-07 16:00:00"]
                        for row in self.rows
                    ),
                )
                conn.commit()
            conn.close()

            materialized_rows = self.module.fetch_rows_from_cache("2026-08-06", "2026-08-07", "campaign")
            materialized = self.module.build_payload(
                materialized_rows,
                "2026-08-06",
                "2026-08-07",
                self.validation,
                include_rows=False,
            )
            summary = self.module.summarize_cache_range("2026-08-06", "2026-08-07", "campaign")
            streamed = self.module.build_payload(
                [],
                "2026-08-06",
                "2026-08-07",
                self.validation,
                include_rows=False,
                summary=summary,
            )

            for key in ("meta", "totals", "daily_totals", "validation", "levels"):
                self.assertEqual(materialized[key], streamed[key], key)

    def test_failed_publish_keeps_previous_manifest_references_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            old_rel = "data/campaign/2026-08-05.json"
            old_data = output_dir / old_rel
            old_data.parent.mkdir(parents=True)
            old_data.write_text(json.dumps({"old": True}), encoding="utf-8")
            old_data_bytes = old_data.read_bytes()
            old_manifest = {"data_files": {"campaign": {"2026-08-05": {"path": old_rel}}}}
            old_latest = json.dumps(old_manifest, separators=(",", ":"))
            (output_dir / "latest.json").write_text(old_latest, encoding="utf-8")

            payload = self.module.build_payload(
                self.rows,
                "2026-08-06",
                "2026-08-07",
                self.validation,
                include_rows=False,
            )
            self.module.fetch_rows_from_cache = lambda day, _end, _level: self.rows[:1]
            real_atomic_write = self.module.atomic_write
            calls = {"count": 0}

            def fail_on_second_detail(path, content, binary=False):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise RuntimeError("injected publish failure")
                return real_atomic_write(path, content, binary=binary)

            self.module.atomic_write = fail_on_second_detail
            with self.assertRaisesRegex(RuntimeError, "injected publish failure"):
                self.module.publish_from_cache(payload, output_dir, "2026-08-07", "2026-08-07")

            self.assertEqual(old_latest, (output_dir / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(old_data_bytes, old_data.read_bytes())
            previous = json.loads((output_dir / "latest.json").read_text(encoding="utf-8"))
            for files in previous["data_files"].values():
                for item in files.values():
                    referenced = output_dir / item["path"]
                    self.assertTrue(referenced.is_file())
                    json.loads(referenced.read_text(encoding="utf-8"))

    def test_successful_publish_uses_a_versioned_detail_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            payload = self.module.build_payload(
                self.rows,
                "2026-08-07",
                "2026-08-07",
                self.validation,
                include_rows=False,
            )
            self.module.fetch_rows_from_cache = lambda day, _end, _level: self.rows[:1]

            self.module.publish_from_cache(payload, output_dir, "2026-08-07", "2026-08-07")

            manifest = json.loads((output_dir / "latest.json").read_text(encoding="utf-8"))
            paths = [item["path"] for files in manifest["data_files"].values() for item in files.values()]
            self.assertEqual(2, len(paths))
            for rel in paths:
                self.assertRegex(rel, r"^data/20260807160000-\d+/(campaign|ad)/2026-08-07\.json$")
                self.assertTrue((output_dir / rel).is_file())

    def test_stale_cleanup_runs_after_grace_and_preserves_live_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            data_dir = output_dir / "data"
            keep = data_dir / "campaign" / "keep.json"
            stale = data_dir / "campaign" / "stale.json"
            recent = data_dir / "campaign" / "recent.json"
            keep.parent.mkdir(parents=True)
            for path in (keep, stale, recent):
                path.write_text("{}", encoding="utf-8")
            now = 2_000_000_000
            old_time = now - self.module.PUBLISHED_FILE_STALE_GRACE_SECONDS - 1
            os.utime(str(keep), (old_time, old_time))
            os.utime(str(stale), (old_time, old_time))
            os.utime(str(recent), (now, now))

            removed = self.module.prune_stale_published_files(
                data_dir,
                {keep.relative_to(output_dir).as_posix()},
                now=now,
            )

            self.assertEqual(1, removed)
            self.assertTrue(keep.exists())
            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())


if __name__ == "__main__":
    unittest.main()
