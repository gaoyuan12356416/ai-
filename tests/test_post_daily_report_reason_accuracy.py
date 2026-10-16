import hashlib
import unittest

from features.post_daily_report import fb, tt, x
from tests import test_post_daily_report_tt as tt_fixture


class ReasonAccuracyTests(unittest.TestCase):
    def test_tt_preparation_ready_and_ambiguous_history_are_distinct(self):
        for task, expected in [
            ({"status": "failed", "error_code": "source_url_not_allowed"}, "source_url_not_allowed"),
            ({"status": "ready"}, "prepared_waiting_publish"),
            ({"status": "retry_wait", "error_code": "tt_publish_outcome_unknown"}, "historical_outcome_hold"),
        ]:
            row = tt._row("a", "a", "a")
            tt._error_reason(row, task, confidence="inferred" if expected == "historical_outcome_hold" else "confirmed")
            self.assertEqual(next(iter(row["_reasons"]))[0], expected)

    def test_legacy_fb_override_requires_exact_immutable_request(self):
        task = {"id": 1, "run_id": 2, "gpu_job_id": "job", "error_code": "invalid_request", "source_media_url": "https://source.example/a.mp4"}
        proof = {"1": {"run_id": 2, "gpu_job_id": "job", "original_error_code": "invalid_request", "source_url_sha256": hashlib.sha256(task["source_media_url"].encode()).hexdigest()}}
        self.assertEqual(fb._verified_preparation_code(task, proof), "fb_auto_source_url_not_allowed")
        for change in [{"gpu_job_id": "different"}, {"source_media_url": "https://source.example/b.mp4"}, {"run_id": 3}, {"error_code": "fb_graph_190"}]:
            self.assertEqual(fb._verified_preparation_code(dict(task, **change), proof), "")

    def test_x_prequeue_block_is_not_claimed_to_be_empty_content(self):
        row = x._row("material_pool")
        run = {"status": "completed", "error_message": "账号6授权或发布开关不可用，本批跳过；批次799/日志3090有未完成或待核对的发布结果"}
        x._missing(row, run, 1, account="6")
        x._missing(row, run, 1, account="21", logs_by_id={3090: {"account_id": 21, "unknown_outcome": 1}})
        x._missing(row, run, 1, account="22")
        self.assertEqual((row["failed"], row["unqueued"]), (3, 3))
        self.assertEqual([r["code"] for r in row["reasons"]], ["x_account_not_publishable", "x_historical_outcome_hold", "x_post_schedule_partial_capacity"])
        self.assertEqual(row["reasons"][-1]["confidence"], "unknown")


class ReconcilePressureReportTests(unittest.TestCase):
    setUp = tt_fixture.TTReportTests.setUp
    tearDown = tt_fixture.TTReportTests.tearDown
    template = tt_fixture.TTReportTests.template
    report = tt_fixture.TTReportTests.report

    def test_pressure_is_reported_as_observation_without_reclassifying_pending(self):
        self.template()
        self.auto.execute("ALTER TABLE tt_auto_event ADD COLUMN task_id INTEGER")
        self.auto.executemany("INSERT INTO tt_auto_event(id,event_type,details_json,created_at,task_id) VALUES(?,'task_claimed_reconcile','{}','2026-09-07T01:00:00Z',3221)", [(i,) for i in range(1, 1001)])
        result = self.report()
        self.assertTrue(any("3221" in warning and "1000" in warning for warning in result["warnings"]))
        row = next(r for r in result["rows"] if r["source"] == "auto_template:random_overlay")
        self.assertEqual((row["expected"], row["pending"], row["failed"]), (2, 2, 0))
