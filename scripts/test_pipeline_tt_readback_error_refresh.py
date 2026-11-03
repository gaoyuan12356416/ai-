"""Successful readback replaces stale transport errors without releasing unknown work."""
import unittest

from features.pipeline_dispatch.tt_readback import reconcile_next
from features.tt_posts.service import GPUClientError
from scripts import test_pipeline_tt_readback as fixtures


class ReadbackErrorRefreshTests(unittest.TestCase):
    setUp = fixtures.LegacyTTReadbackTests.setUp
    reserved_task = fixtures.LegacyTTReadbackTests.reserved_task
    executor = fixtures.LegacyTTReadbackTests.executor
    pending = fixtures.LegacyTTReadbackTests.pending
    readback_executor = fixtures.LegacyTTReadbackTests.readback_executor
    attempts = fixtures.LegacyTTReadbackTests.attempts

    def test_processing_receipt_clears_old_error_and_preserves_original_attempt(self):
        for index, (unknown, code) in enumerate(((False, "tt_gpu_unreachable"),
                                                (False, "tt_access_token_unavailable"),
                                                (True, "tt_publish_outcome_unknown"))):
            with self.subTest(code=code):
                task = self.pending(suffix="refresh-" + str(index),
                    status="unknown" if unknown else "reconciling", unknown_outcome=int(unknown),
                    error_code=code, error_message="old transport failure")
                executor, gpu = self.readback_executor({"publish_id":task.publish_id,
                    "state":"processing", "status":{"status":"PROCESSING_DOWNLOAD", "downloaded_bytes":0}})
                reconcile_next(executor, "readback", after_task_id=task.id - 1)
                final = self.store.get_task(task.id)
                self.assertEqual((final.error_code, final.error_message), ("", ""))
                self.assertEqual((final.status, final.unknown_outcome, final.publish_id, self.attempts(final)),
                    ("unknown" if unknown else "reconciling", unknown, task.publish_id, 1))
                self.assertEqual(len(gpu.reconcile_calls), 1)
                with self.store._transaction() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM tt_auto_event WHERE task_id=? AND event_type='task_published'", (task.id,)).fetchone()[0], 0)

    def test_failed_readback_and_wrong_id_are_not_treated_as_success(self):
        for index, (response, expected_code) in enumerate((
            (GPUClientError("tt_gpu_unreachable", "new transport failure", 502), "tt_gpu_unreachable"),
            ({"publish_id":"another-publish-id", "state":"processing"}, "tt_auto_publish_id_conflict"))):
            with self.subTest(code=expected_code):
                task = self.pending(suffix="failed-" + str(index), error_code="old_error")
                executor, _ = self.readback_executor(response)
                reconcile_next(executor, "readback", after_task_id=task.id - 1)
                final = self.store.get_task(task.id)
                self.assertEqual((final.status, final.unknown_outcome, final.error_code,
                                  final.publish_id, self.attempts(final)),
                                 ("retry_wait", True, expected_code, task.publish_id, 1))


if __name__ == "__main__":
    unittest.main()
