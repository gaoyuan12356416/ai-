"""Accuracy checks for the read-only, finite 145-Page server audit."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from scripts.fb_page_readiness_watch import assess, health


class ReadinessAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        self.expected = {str(10000 + index): "en" for index in range(145)}

    def task(self, index, *, run_id=119, status="published", seconds=3600, language="en"):
        return {"id": run_id * 1000 + index, "run_id": run_id,
                "page_id": str(10000 + index), "status": status,
                "planned_publish_at_utc": (self.now + timedelta(seconds=seconds)).isoformat(),
                "language": language, "error_code": "", "skip_reason": ""}

    def report(self, tasks, expected=None, run_ids=None, seconds_per_task=100):
        return assess(tasks, self.expected if expected is None else expected,
                      [119] if run_ids is None else run_ids, self.now,
                      seconds_per_task=seconds_per_task)

    @staticmethod
    def issues(report, kind):
        return [item for item in report["issues"] if item["kind"] == kind]

    def test_all_145_published_with_matching_languages_have_complete_coverage(self):
        report = self.report([self.task(index, seconds=-3600) for index in range(145)])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["states"], {"published": 145})
        self.assertEqual((report["expected_pages"], report["task_count"]), (145, 145))

    def test_unknown_failures_and_skips_are_never_a_healthy_complete_batch(self):
        for status in ("unknown", "failed", "failed_without_retry", "skipped"):
            with self.subTest(status=status):
                tasks = [self.task(index, status=status) for index in range(145)]
                tasks[0]["error_code"] = "confirmed-test-reason"
                report = self.report(tasks)
                failures = self.issues(report, "task_terminal_issue")
                self.assertEqual(len(failures), 145)
                self.assertEqual(failures[0]["reason"], "confirmed-test-reason")
                self.assertEqual(report["states"].get("published", 0), 0)

    def test_submitted_is_reported_separately_from_confirmed_published(self):
        tasks = [self.task(index, status="submitted") for index in range(145)]
        report = self.report(tasks)
        self.assertEqual(report["states"], {"submitted": 145})
        self.assertEqual(report["states"].get("published", 0), 0)

    def test_empty_tasks_do_not_count_as_successful_run(self):
        report = self.report([])
        self.assertEqual(self.issues(report, "run_page_coverage"), [
            {"kind": "run_page_coverage", "run_id": 119, "task_count": 0},
        ])

    def test_each_requested_run_must_cover_all_145_pages(self):
        tasks = [self.task(index) for index in range(145)]
        tasks += [self.task(index, run_id=120) for index in range(144)]
        report = self.report(tasks, run_ids=[119, 120])
        self.assertEqual(self.issues(report, "run_page_coverage"), [
            {"kind": "run_page_coverage", "run_id": 120, "task_count": 144},
        ])

    def test_145_rows_with_duplicate_page_and_missing_page_fail_coverage(self):
        tasks = [self.task(index) for index in range(145)]
        tasks[-1]["page_id"] = tasks[0]["page_id"]
        report = self.report(tasks)
        self.assertEqual(len(self.issues(report, "run_page_coverage")), 1)

    def test_extra_duplicate_row_fails_even_when_every_page_is_present(self):
        tasks = [self.task(index) for index in range(145)]
        tasks.append({**tasks[0], "id": 999999})
        report = self.report(tasks)
        self.assertEqual(self.issues(report, "run_page_coverage")[0]["task_count"], 146)

    def test_wrong_and_missing_language_are_reported_per_task(self):
        tasks = [self.task(index) for index in range(145)]
        tasks[0]["language"] = "es"
        tasks[1].pop("language")
        report = self.report(tasks)
        self.assertEqual({item["task_id"] for item in self.issues(report, "page_language_mismatch")},
                         {tasks[0]["id"], tasks[1]["id"]})

    def test_eta_accumulates_unprepared_work_from_earlier_batches(self):
        earlier = [self.task(index, status="planned", seconds=1500) for index in range(2)]
        later = [self.task(index, status="preparing", seconds=1800) for index in range(2, 10)]
        expected = {task["page_id"]: "en" for task in earlier + later}
        report = self.report(later + earlier, expected=expected)
        self.assertEqual([batch["estimated_prepare_slack_seconds"] for batch in report["batches"]], [1300, 800])
        risks = self.issues(report, "preparation_deadline_risk")
        self.assertEqual(len(risks), 1)
        self.assertEqual((risks[0]["planned_at_utc"], risks[0]["estimated_slack_seconds"]),
                         (later[0]["planned_publish_at_utc"], 800))

    def test_prepared_and_published_work_do_not_consume_preparation_eta(self):
        tasks = [self.task(index, status="planned", seconds=1500) for index in range(2)]
        tasks += [self.task(index, status="ready", seconds=1800) for index in range(2, 7)]
        tasks += [self.task(index, status="published", seconds=1800) for index in range(7, 10)]
        expected = {task["page_id"]: "en" for task in tasks}
        report = self.report(tasks, expected=expected)
        self.assertEqual([batch["unprepared"] for batch in report["batches"]], [2, 0])
        self.assertEqual(report["batches"][1]["estimated_prepare_slack_seconds"], 1600)

    def test_longer_source_videos_in_earlier_slot_reduce_later_slot_eta_slack(self):
        earlier = [self.task(index, status="planned", seconds=1500) for index in range(2)]
        later = [self.task(index, status="preparing", seconds=2000) for index in range(2, 10)]
        tasks = earlier + later
        expected = {task["page_id"]: "en" for task in tasks}
        baseline = self.report(tasks, expected=expected)
        longer_inputs = {str(task["id"]): 2 * 261.257874 for task in earlier}
        scaled = assess(tasks, expected, [119], self.now, seconds_per_task=100,
                        source_seconds_by_task=longer_inputs)
        self.assertEqual([batch["estimated_prepare_slack_seconds"] for batch in baseline["batches"]], [1300, 1000])
        self.assertEqual([batch["estimated_prepare_slack_seconds"] for batch in scaled["batches"]], [1100, 800])
        self.assertEqual(self.issues(baseline, "preparation_deadline_risk"), [])
        self.assertEqual(len(self.issues(scaled, "preparation_deadline_risk")), 1)
        self.assertEqual(self.issues(scaled, "preparation_deadline_risk")[0]["planned_at_utc"], later[0]["planned_publish_at_utc"])

    def test_unclaimed_planned_preparing_and_ready_work_is_overdue_after_grace(self):
        for status in ("planned", "preparing", "ready"):
            with self.subTest(status=status):
                task = self.task(0, status=status, seconds=-1860)
                report = self.report([task], expected={task["page_id"]: "en"})
                self.assertEqual(len(self.issues(report, "publish_claim_overdue")), 1)

    def test_unclaimed_work_inside_thirty_minute_grace_is_not_overdue(self):
        task = self.task(0, status="ready", seconds=-1740)
        report = self.report([task], expected={task["page_id"]: "en"})
        self.assertEqual(self.issues(report, "publish_claim_overdue"), [])

    def test_claimed_work_is_not_misreported_as_unclaimed_overdue(self):
        for status in ("running", "submitted", "published"):
            with self.subTest(status=status):
                task = self.task(0, status=status, seconds=-3600)
                report = self.report([task], expected={task["page_id"]: "en"})
                self.assertEqual(self.issues(report, "publish_claim_overdue"), [])

    def test_unconfirmed_publication_is_flagged_only_after_two_hour_boundary(self):
        for status in ("running", "submitted"):
            for seconds, overdue in ((-7199, False), (-7200, False), (-7201, True)):
                with self.subTest(status=status, seconds=seconds):
                    task = self.task(0, status=status, seconds=seconds)
                    before = dict(task)
                    report = self.report([task], expected={task["page_id"]: "en"})
                    self.assertEqual(bool(self.issues(report, "publication_confirmation_overdue")), overdue)
                    self.assertEqual(task, before)
                    self.assertEqual(self.issues(report, "publish_claim_overdue"), [])
        confirmed = self.task(0, status="published", seconds=-10000)
        report = self.report([confirmed], expected={confirmed["page_id"]: "en"})
        self.assertEqual(self.issues(report, "publication_confirmation_overdue"), [])


class ReadinessHealthTests(unittest.TestCase):
    def test_health_requires_both_http_200_and_boolean_ok(self):
        for status, body, expected in ((200, b'{"ok":true}', True),
                                       (503, b'{"ok":true}', False),
                                       (200, b'{"ok":false}', False),
                                       (200, b'{"ok":"true"}', False),
                                       (200, b'not json', False)):
            with self.subTest(status=status, body=body):
                connection = Mock()
                connection.getresponse.return_value = Mock(status=status, read=Mock(return_value=body))
                with patch("scripts.fb_page_readiness_watch.http.client.HTTPConnection", return_value=connection):
                    self.assertIs(health(18835), expected)
                connection.close.assert_called_once()

    def test_unavailable_service_is_not_healthy(self):
        connection = Mock()
        connection.request.side_effect = TimeoutError("simulated service timeout")
        with patch("scripts.fb_page_readiness_watch.http.client.HTTPConnection", return_value=connection):
            self.assertFalse(health(18835))
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
