import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from features.pipeline_dispatch.cloud_jobs import CloudJobs, CloudLedger, PendingReadback, QueueUnavailable
from features.pipeline_dispatch.executors import ExecutionError


class Provider:
    def __init__(self):
        self.submits, self.jobs = 0, []
        self.lose_ack = self.lose_list = False

    def ci_create_media_jobs(self, Bucket, Jobs, **options):
        self.submits += 1
        receipt = dict(Jobs, JobId="provider-1", State="Success")
        self.jobs.append(receipt)
        if self.lose_ack:
            raise ConnectionError("test")
        return {"JobsDetail": receipt}

    def ci_list_media_jobs(self, **args):
        if not all(args[field].endswith("+0000") for field in ("StartCreationTime", "EndCreationTime")):
            raise ValueError("CI rejects timestamps without a numeric offset")
        if self.lose_list:
            raise ConnectionError("test")
        return {"JobsDetail":self.jobs}

    def ci_get_media_jobs(self, **args):
        return {"JobsDetail":self.jobs[0]}

    def head_object(self, **args):
        raise FileNotFoundError("test")


class PaidRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ci.db"
        self.provider = Provider()
        self.fenced = []
        self.context = SimpleNamespace(check=lambda:None, identity={"job_id":1},
                   client=SimpleNamespace(post=lambda path, value:self.fenced.append((path,value))))

    def run_stage(self, body=None):
        return CloudJobs(CloudLedger(self.path), self.provider, "bucket", "ap-hongkong", "prefix", "queue").run(
            "native-1", "encoded", "Transcode", "source", body or {"Transcode":{}}, self.context)

    def test_missing_ack_and_restart_lookup_without_resubmit(self):
        self.provider.lose_ack = True
        with self.assertRaises(PendingReadback):
            self.run_stage()
        self.provider.lose_list = True
        with self.assertRaises(PendingReadback):
            self.run_stage()
        self.provider.lose_list = False
        self.assertEqual(self.run_stage()[1]["job_id"], "provider-1")
        self.assertEqual(self.run_stage()[1]["job_id"], "provider-1")
        self.assertEqual(self.provider.submits, 1)
        self.assertEqual(self.fenced[0][0], "jobs/cos-intent")

    def test_different_input_or_wrong_provider_output_never_reused(self):
        self.run_stage()
        with self.assertRaises(ExecutionError):
            self.run_stage({"Transcode":{"different":1}})
        self.provider.jobs[0]["Operation"]["Output"]["Object"]="other.mp4"
        with self.assertRaises(PendingReadback):
            self.run_stage()
        self.assertEqual(self.provider.submits, 1)

    def test_queue_throttling_and_individual_uncertainty_are_distinct(self):
        from unittest.mock import patch
        class Throttled(Exception):
            def get_status_code(self): return 429
            def get_error_code(self): return "TooManyRequests"
        with patch.object(self.provider, "ci_create_media_jobs", side_effect=Throttled()):
            with self.assertRaises(QueueUnavailable): self.run_stage()
        with patch.object(CloudJobs, "health", return_value={"queue_ready": False}):
            with self.assertRaises(QueueUnavailable): self.run_stage()
        with patch.object(CloudJobs, "health", return_value={"queue_ready": True}):
            self.assertEqual(self.run_stage()[1]["job_id"], "provider-1")
        self.provider.jobs[0]["Operation"]["Output"]["Region"] = "other-region"
        with self.assertRaises(PendingReadback) as mismatch:
            self.run_stage()
        self.assertNotIsInstance(mismatch.exception, QueueUnavailable)
        self.assertEqual(self.provider.submits, 1)


if __name__ == "__main__":
    unittest.main()
