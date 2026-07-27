from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from features.tt_drama_prewarm import (
    CANDIDATE_QUERY_LIMIT,
    CONTENT_ID_SQL_PATTERN,
    MAX_CANDIDATES,
    ActiveDramaCandidateRepository,
    CandidateOverflowError,
    PrewarmCandidateConfig,
    PrewarmSourceError,
    recent_shanghai_date_window,
)
from features.tt_drama_resources import validate_resource_cache_path
from scripts import prewarm_tt_drama_resources as runner


class _FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None
        self.rows = []
        self.closed = False

    def execute(self, sql, params=()):
        self.connection.statements.append((sql, tuple(params)))
        if sql.startswith("SET SESSION"):
            self.row = None
            self.rows = []
        elif "@@read_only" in sql:
            self.row = {"read_only": self.connection.read_only}
            self.rows = [self.row]
        elif "ads_custom_source_insight" in sql:
            self.rows = list(self.connection.candidates)
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
    def __init__(self, candidates=None, read_only=1):
        self.candidates = list(
            candidates
            if candidates is not None
            else [
                {"content_id": "Ag0rfr5F0F", "spend_n": 100},
                {"content_id": "BQ3Y3JcLWA", "spend_n": 50},
            ]
        )
        self.read_only = read_only
        self.statements = []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def _repository(connection, **kwargs):
    return ActiveDramaCandidateRepository(
        host="readonly.example",
        port=63350,
        user="reader",
        password="secret",
        connection_factory=lambda: connection,
        **kwargs,
    )


class ActiveDramaCandidateTests(unittest.TestCase):
    def test_window_is_three_shanghai_natural_days_including_today(self):
        now = datetime(2026, 7, 26, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(
            recent_shanghai_date_window(now),
            ("2026-07-25", "2026-07-27"),
        )

    def test_query_contract_is_exact_read_only_and_bounded(self):
        connection = _FakeConnection()
        repository = _repository(connection)
        self.assertEqual(
            repository.fetch("2026-07-25", "2026-07-27"),
            ["Ag0rfr5F0F", "BQ3Y3JcLWA"],
        )
        self.assertTrue(connection.closed)
        self.assertEqual(
            connection.statements[0],
            ("SET SESSION TRANSACTION READ ONLY", ()),
        )
        sql, params = next(
            statement
            for statement in connection.statements
            if "ads_custom_source_insight" in statement[0]
        )
        self.assertIn("MAX_EXECUTION_TIME(30000)", sql)
        self.assertIn("FORCE INDEX (`as`)", sql)
        self.assertIn("i.dt BETWEEN %s AND %s", sql)
        self.assertIn("GROUP BY BINARY i.data_source_id", sql)
        self.assertIn("BINARY i.data_source_id REGEXP %s", sql)
        self.assertIn(
            "HAVING SUM(COALESCE(i.spend, 0)) > 0",
            sql,
        )
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(
            params,
            (
                "[w2a]drama-double",
                "2026-07-25",
                "2026-07-27",
                "Dramawave",
                6,
                CONTENT_ID_SQL_PATTERN,
                5001,
            ),
        )
        self.assertEqual(CANDIDATE_QUERY_LIMIT, MAX_CANDIDATES + 1)

    def test_candidate_overflow_fails_instead_of_truncating(self):
        candidates = [
            {
                "content_id": "DRAMA%05d" % index,
                "spend_n": 1,
            }
            for index in range(CANDIDATE_QUERY_LIMIT)
        ]
        connection = _FakeConnection(candidates=candidates)
        with self.assertRaises(CandidateOverflowError) as raised:
            _repository(connection).fetch("2026-07-25", "2026-07-27")
        self.assertEqual(
            raised.exception.error_code,
            "candidate_limit_exceeded",
        )
        self.assertEqual(
            raised.exception.count_lower_bound,
            CANDIDATE_QUERY_LIMIT,
        )
        payload = runner._error_payload(raised.exception)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(
            payload["error_code"],
            "candidate_limit_exceeded",
        )
        self.assertEqual(
            payload["candidate_count_lower_bound"],
            CANDIDATE_QUERY_LIMIT,
        )
        self.assertEqual(payload["candidate_limit"], 5000)
        self.assertTrue(connection.closed)

    def test_source_must_be_read_only(self):
        connection = _FakeConnection(read_only=0)
        with self.assertRaisesRegex(PrewarmSourceError, "not read-only"):
            _repository(connection).fetch("2026-07-25", "2026-07-27")
        self.assertTrue(connection.closed)

    def test_production_connection_scope_is_fixed_to_63350(self):
        bad_port = ActiveDramaCandidateRepository(
            host="101.32.56.53",
            port=63353,
            user="reader",
            password="secret",
        )
        with self.assertRaisesRegex(PrewarmSourceError, "port 63350"):
            bad_port.fetch("2026-07-25", "2026-07-27")

    def test_scope_configuration_cannot_expand_to_other_products(self):
        with self.assertRaisesRegex(ValueError, "table scope"):
            PrewarmCandidateConfig(insight_table="other_table")
        with self.assertRaisesRegex(ValueError, "index scope"):
            PrewarmCandidateConfig(insight_index="other_index")
        with self.assertRaisesRegex(ValueError, "product scope"):
            PrewarmCandidateConfig(product="Other")
        with self.assertRaisesRegex(ValueError, "app scope"):
            PrewarmCandidateConfig(source_app_id="other")
        with self.assertRaisesRegex(ValueError, "data_source scope"):
            PrewarmCandidateConfig(data_source=4)

    def test_window_must_be_exactly_three_days(self):
        connection = _FakeConnection()
        with self.assertRaisesRegex(PrewarmSourceError, "exactly three"):
            _repository(connection).fetch("2026-07-26", "2026-07-27")
        self.assertEqual(connection.statements, [])

    def test_invalid_or_duplicate_source_ids_fail_closed(self):
        invalid = _FakeConnection(
            candidates=[{"content_id": "wrong id", "spend_n": 1}]
        )
        with self.assertRaisesRegex(PrewarmSourceError, "invalid content_id"):
            _repository(invalid).fetch("2026-07-25", "2026-07-27")

        duplicate = _FakeConnection(
            candidates=[
                {"content_id": "Ag0rfr5F0F", "spend_n": 2},
                {"content_id": "Ag0rfr5F0F", "spend_n": 1},
            ]
        )
        with self.assertRaisesRegex(PrewarmSourceError, "duplicate"):
            _repository(duplicate).fetch("2026-07-25", "2026-07-27")


class _FakeOutcome:
    def __init__(self, found=True, cache_state="ORIGIN_FILL"):
        self.found = bool(found)
        self.cache_state = cache_state


class _FakeCandidateRepository:
    def __init__(self, content_ids):
        self.content_ids = list(content_ids)
        self.calls = []

    def fetch(self, start_date, end_date):
        self.calls.append((start_date, end_date))
        return list(self.content_ids)


class _FakeResourceCache:
    def __init__(self, outcomes=None):
        self.outcomes = dict(outcomes or {})
        self.calls = []

    def peek(self, landing_id, content_id, allow_stale=True):
        self.calls.append((landing_id, content_id, allow_stale))
        return self.outcomes.get(content_id)


class _FakeResourceService:
    landing_id = 2049

    def __init__(self, cached=None, failures=None, resolved=None):
        self.cache = _FakeResourceCache(cached)
        self.failures = set(failures or ())
        self.resolved = dict(resolved or {})
        self.warmup_calls = 0
        self.resolve_calls = []

    def warmup(self):
        self.warmup_calls += 1

    def resolve(self, content_id, force_refresh=False, allow_stale=True):
        self.resolve_calls.append(
            (content_id, force_refresh, allow_stale)
        )
        if content_id in self.failures:
            raise RuntimeError("injected")
        if content_id in self.resolved:
            return self.resolved[content_id]
        return _FakeOutcome(
            found=content_id != "DRAMA00003",
            cache_state="ORIGIN_FILL",
        )


class PrewarmRunnerTests(unittest.TestCase):
    def test_round_robin_batch_advances_without_repeating_top_five_hundred(self):
        candidates = ["DRAMA%05d" % index for index in range(1200)]
        selected, next_id = runner._select_batch(
            candidates,
            500,
            {},
        )
        self.assertEqual(selected, candidates[:500])
        self.assertEqual(next_id, candidates[500])
        selected_again, following_id = runner._select_batch(
            candidates,
            500,
            {"next_content_id": next_id},
        )
        self.assertEqual(selected_again, candidates[500:1000])
        self.assertEqual(following_id, candidates[1000])

    def test_rotation_preserves_spend_order_and_missing_id_uses_saved_index(self):
        candidates = [
            "DRAMA00030",
            "DRAMA00010",
            "DRAMA00020",
        ]
        selected, next_id = runner._select_batch(
            candidates,
            2,
            {
                "next_content_id": "DRAMA00015",
                "next_index": 1,
            },
        )
        self.assertEqual(selected, ["DRAMA00010", "DRAMA00020"])
        self.assertEqual(next_id, "DRAMA00030")

    def test_bootstrap_plans_at_most_three_thousand(self):
        candidates = ["DRAMA%05d" % index for index in range(4000)]
        selected, next_id = runner._select_batch(
            candidates,
            runner.BOOTSTRAP_BATCH_LIMIT,
            {"next_content_id": candidates[3500]},
            bootstrap=True,
        )
        self.assertEqual(len(selected), 3000)
        self.assertEqual(selected[0], candidates[0])
        self.assertEqual(next_id, candidates[3000])

    def test_dry_run_only_queries_and_plans(self):
        repository = _FakeCandidateRepository(
            ["Ag0rfr5F0F", "BQ3Y3JcLWA"]
        )
        service = _FakeResourceService()
        with tempfile.TemporaryDirectory() as directory:
            cursor_path = Path(directory) / "cursor.json"
            result = runner.execute_prewarm(
                repository=repository,
                service=service,
                start_date="2026-07-25",
                end_date="2026-07-27",
                batch_limit=500,
                cursor_path=cursor_path,
                dry_run=True,
            )
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["candidate_count"], 2)
            self.assertEqual(result["planned_count"], 2)
            self.assertEqual(service.warmup_calls, 0)
            self.assertEqual(service.resolve_calls, [])
            self.assertFalse(cursor_path.exists())

    def test_batch_skips_origin_gate_for_fresh_disk_hits_and_writes_cursor(self):
        content_ids = [
            "DRAMA00001",
            "DRAMA00002",
            "DRAMA00003",
        ]
        service = _FakeResourceService(
            cached={
                "DRAMA00001": _FakeOutcome(
                    found=True,
                    cache_state="DISK_HIT",
                ),
                "DRAMA00003": _FakeOutcome(
                    found=False,
                    cache_state="NEGATIVE_HIT",
                ),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            cursor_path = Path(directory) / "cursor.json"
            result = runner.execute_prewarm(
                repository=_FakeCandidateRepository(content_ids),
                service=service,
                start_date="2026-07-25",
                end_date="2026-07-27",
                batch_limit=3,
                cursor_path=cursor_path,
                workers=2,
                qps=2,
                clock=lambda: 0,
                sleep=lambda _delay: None,
                resource_error_types=(RuntimeError,),
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["found_count"], 2)
            self.assertEqual(result["not_found_count"], 1)
            self.assertEqual(
                [call[0] for call in service.resolve_calls],
                ["DRAMA00002"],
            )
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
            self.assertEqual(cursor["next_content_id"], content_ids[0])
            self.assertEqual(cursor["retry_content_ids"], [])
            self.assertNotIn("spend", json.dumps(cursor).lower())

    def test_resource_failure_is_structured_and_returns_partial_error(self):
        content_ids = ["DRAMA00001", "DRAMA00002"]
        service = _FakeResourceService(failures={"DRAMA00002"})
        with tempfile.TemporaryDirectory() as directory:
            cursor_path = Path(directory) / "cursor.json"
            result = runner.execute_prewarm(
                repository=_FakeCandidateRepository(content_ids),
                service=service,
                start_date="2026-07-25",
                end_date="2026-07-27",
                batch_limit=2,
                cursor_path=cursor_path,
                workers=1,
                qps=2,
                clock=lambda: 0,
                sleep=lambda _delay: None,
                resource_error_types=(RuntimeError,),
            )
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "partial_error")
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(
            result["errors"],
            [{"content_id": "DRAMA00002", "error": "RuntimeError"}],
        )
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(cursor["retry_content_ids"], ["DRAMA00002"])

    def test_failed_ids_are_retried_without_starving_rotation_and_removed_on_success(self):
        candidates = ["DRAMA%05d" % index for index in range(10)]
        with tempfile.TemporaryDirectory() as directory:
            cursor_path = Path(directory) / "cursor.json"
            runner._atomic_write_cursor(
                cursor_path,
                next_content_id="DRAMA00005",
                candidate_count=len(candidates),
                retry_content_ids=["DRAMA00001", "DRAMA00002"],
                next_index=5,
            )
            service = _FakeResourceService()
            result = runner.execute_prewarm(
                repository=_FakeCandidateRepository(candidates),
                service=service,
                start_date="2026-07-25",
                end_date="2026-07-27",
                batch_limit=4,
                cursor_path=cursor_path,
                workers=1,
                qps=2,
                clock=lambda: 0,
                sleep=lambda _delay: None,
                resource_error_types=(RuntimeError,),
            )
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [call[0] for call in service.resolve_calls],
            ["DRAMA00001", "DRAMA00002", "DRAMA00005", "DRAMA00006"],
        )
        self.assertEqual(cursor["next_content_id"], "DRAMA00007")
        self.assertEqual(cursor["next_index"], 7)
        self.assertEqual(cursor["retry_content_ids"], [])

    def test_stale_cache_enters_normal_refresh_with_stale_fallback_enabled(self):
        service = _FakeResourceService(
            cached={
                "DRAMA00001": _FakeOutcome(
                    found=True,
                    cache_state="STALE",
                )
            },
            resolved={
                "DRAMA00001": _FakeOutcome(
                    found=True,
                    cache_state="STALE",
                )
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            result = runner.execute_prewarm(
                repository=_FakeCandidateRepository(["DRAMA00001"]),
                service=service,
                start_date="2026-07-25",
                end_date="2026-07-27",
                batch_limit=1,
                cursor_path=Path(directory) / "cursor.json",
                workers=1,
                qps=2,
                clock=lambda: 0,
                sleep=lambda _delay: None,
                resource_error_types=(RuntimeError,),
            )
        self.assertEqual(result["status"], "partial_error")
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(
            result["errors"],
            [
                {
                    "content_id": "DRAMA00001",
                    "error": "stale_fallback",
                }
            ],
        )
        self.assertEqual(
            service.resolve_calls,
            [("DRAMA00001", False, True)],
        )

    def test_cli_rejects_batch_limits_above_bootstrap_bound(self):
        args = runner._parser().parse_args(["--batch-limit", "3001"])
        self.assertEqual(args.batch_limit, 3001)
        self.assertEqual(runner.MAX_BATCH_LIMIT, 3000)

    def test_normal_cli_rejects_more_than_five_hundred_before_querying(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = runner.main(["--batch-limit", "501"])
        self.assertEqual(result, 2)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error_code"], "invalid_batch_limit")
        self.assertIn("1 and 500", payload["error"])

    def test_normal_execution_cannot_exceed_five_hundred(self):
        candidates = ["DRAMA%05d" % index for index in range(700)]
        with tempfile.TemporaryDirectory() as directory:
            result = runner.execute_prewarm(
                repository=_FakeCandidateRepository(candidates),
                service=_FakeResourceService(),
                start_date="2026-07-25",
                end_date="2026-07-27",
                batch_limit=3000,
                cursor_path=Path(directory) / "cursor.json",
                workers=1,
                qps=2,
                clock=lambda: 0,
                sleep=lambda _delay: None,
                resource_error_types=(RuntimeError,),
            )
        self.assertEqual(result["batch_limit"], 500)
        self.assertEqual(result["planned_count"], 500)
        self.assertEqual(result["processed_count"], 500)

    def test_resource_factory_keeps_fixed_landing_and_bounded_defaults(self):
        with mock.patch.dict(
            os.environ,
            {
                "TT_DRAMA_RESOURCE_LANDING_ID": "2049",
                "TT_DRAMA_RESOURCE_HTTP_TIMEOUT_SECONDS": "5",
                "TT_DRAMA_RESOURCE_HTTP_MAX_BYTES": str(512 * 1024),
            },
            clear=True,
        ):
            service = runner._build_resource_service(
                str(Path(tempfile.gettempdir()) / "unused.sqlite3")
            )
        self.assertEqual(service.landing_id, 2049)
        self.assertEqual(service.client.landing_id, 2049)
        self.assertEqual(service.client.timeout_seconds, 5)
        self.assertEqual(service.client.max_html_bytes, 512 * 1024)
        self.assertEqual(service.positive_ttl_seconds, 86400)
        self.assertEqual(service.negative_ttl_seconds, 900)
        self.assertEqual(service.stale_ttl_seconds, 7 * 86400)
        service.close()

        with mock.patch.dict(
            os.environ,
            {"TT_DRAMA_RESOURCE_LANDING_ID": "2050"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                runner.PrewarmRunError,
                "must remain 2049",
            ):
                runner._build_resource_service(
                    str(Path(tempfile.gettempdir()) / "unused.sqlite3")
                )

    def test_data_disk_validator_keyword_matches_real_unmocked_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state" / "resources.sqlite3"
            validated = validate_resource_cache_path(
                target,
                allow_test_path=True,
                expected_mount_uuid=runner.DATA_DISK_UUID,
                min_free_bytes=0,
            )
        self.assertEqual(validated, target)
        script = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "expected_mount_uuid=DATA_DISK_UUID",
            script,
        )
        self.assertNotIn("expected_uuid=DATA_DISK_UUID", script)

    def test_systemd_contract_uses_shared_user_data_disk_and_four_hour_timer(self):
        root = Path(__file__).resolve().parents[1]
        service = (
            root / "deploy" / "tt-drama-resource-prewarm.service"
        ).read_text(encoding="utf-8")
        timer = (
            root / "deploy" / "tt-drama-resource-prewarm.timer"
        ).read_text(encoding="utf-8")
        script = (
            root / "scripts" / "prewarm_tt_drama_resources.py"
        ).read_text(encoding="utf-8")
        self.assertIn("User=tt-drama-featured", service)
        self.assertIn("Group=tt-drama-featured", service)
        self.assertIn(
            "EnvironmentFile=/etc/tt-drama-featured.env",
            service,
        )
        self.assertIn(
            "state/resources.sqlite3",
            service,
        )
        self.assertIn(
            "ExecStartPre=+/usr/bin/install -d -m 2770 "
            "-o tt-drama-featured -g tt-drama-featured "
            "/mnt/data-disk/tt-drama-resource-cache/state",
            service,
        )
        self.assertIn(
            "TT_DRAMA_RESOURCE_PREWARM_BATCH_LIMIT=500",
            service,
        )
        self.assertIn(
            "00,04,08,12,16,20:20:00 Asia/Shanghai",
            timer,
        )
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=5m", timer)
        self.assertIn("prewarm.lock", service)
        self.assertIn("LOCK_NB", script)
        self.assertNotIn("playwright", script.lower())
        self.assertNotIn("selenium", script.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
