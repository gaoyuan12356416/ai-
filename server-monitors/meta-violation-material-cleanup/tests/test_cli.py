import contextlib
from datetime import datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("cleanup_cli", HERE / "run.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
from meta_violation_cleanup.runtime import Runtime as RealRuntime


class FakeDb:
    def close(self):
        pass


class FakeRuntime:
    def __init__(self):
        self.rows, self.notices, self.artifacts = {}, {}, {}
        self.db = FakeDb()
        self.recoveries = []

    def begin(self, run_id, mode, data):
        if run_id in self.rows:
            return False
        self.rows[run_id] = dict(id=run_id, mode=mode, status="running", started="now", ended=None, data=dict(data))
        return True

    def get(self, run_id):
        return self.rows[run_id]

    def list(self):
        return list(self.rows.values())

    def update(self, run_id, status=None, **data):
        run = self.rows[run_id]
        run["data"].update(data)
        if status:
            run["status"] = status
            if status != "running":
                run["ended"] = "finished"

    def recover_interrupted(self):
        return {"run_ids": self.recoveries}

    def notice(self, run_id, phase):
        return self.notices.get((run_id, phase))

    def artifact(self, run_id, name, value):
        self.artifacts[(run_id, name)] = value

    def load_artifact(self, run_id, name):
        return self.artifacts[(run_id, name)]


class FakeSource:
    safe_metrics = {"query_count": 2}
    def __init__(self, config):
        pass
    def credentials(self):
        return {"803": "synthetic-token-never-print"}
    def close(self):
        pass


class FakeGraph:
    def __init__(self, tokens, **kwargs):
        self.tokens = tokens
        self.reads = []
    def reconcile(self, kind, identifier, user, account):
        self.reads.append((kind, identifier, user, account))
        return {"status": "deleted_verified", "object": {"id": identifier, "status": "DELETED"}}
    def delete(self, *args):
        raise AssertionError("reconcile/preview must never DELETE")
    def close(self):
        pass


class FakeStore:
    def __init__(self):
        self.job = {"actor": cli.ACTOR, "objects": [], "runs": []}
        self.reconciled, self.finished = [], []
    def get_job(self, job_id):
        return self.job
    def reconcile_object(self, *args):
        self.reconciled.append(args)
    def finish_run(self, *args):
        self.finished.append(args)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.store = FakeStore()
        self.events = []
        self.fail_start = self.fail_scan = self.fail_end = False
        self.config = dict(runtime_dir="/mnt/data-disk/cleanup", shared_ledger="/mnt/data-disk/shared.db",
                           env_file="/private/.env", feishu_config="/private/feishu.json", legacy_lock="/tmp/legacy.lock",
                           live_enabled=True)
        test = self

        class FakeNotifier:
            def __init__(self, runtime, path):
                self.runtime = runtime
            def send(self, run_id, phase, body):
                test.events.append(phase)
                if phase.startswith("start") and test.fail_start or phase.startswith("end") and test.fail_end:
                    raise RuntimeError("synthetic-token-never-print")
                self.runtime.notices[(run_id, phase)] = dict(status="sent", body=body, receipt='{"message_id":"om_synthetic"}')
                return {"message_id": "om_synthetic"}

        class FakeRunner:
            def __init__(self, config, source, graph, runtime, store, notifier, stop=lambda: False):
                self.source, self.graph, self.runtime, self.store, self.stop = source, graph, runtime, store, stop
            def check_stop(self):
                if self.stop():
                    raise InterruptedError("stop")
            def start_text(self, *args):
                return "start body"
            def end_text(self, run_id):
                return "end body " + self.runtime.get(run_id)["status"]
            def scan(self, run_id, start, end):
                test.events.append("scan")
                if test.fail_scan:
                    raise ValueError("synthetic-token-never-print")
                manifest = dict(run_id=run_id, window_start=start, window_end=end,
                                frozen_at=datetime.now(cli.CST).isoformat(), candidates=[])
                manifest["sha256"] = cli.digest(manifest)
                self.runtime.artifact(run_id, "manifest.json", manifest)
                self.runtime.update(run_id, candidate_ads=0, manifest_sha256=manifest["sha256"])
                return manifest
            def execute(self, run_id, manifest, max_ads=None, start_phase="start"):
                test.events.append("execute")
                test.assertEqual(self.runtime.notice(run_id, start_phase)["status"], "sent")
                self.runtime.update(run_id, shared_job_id="policy-cleanup-" + run_id,
                                    result_counts={"ad_deleted_verified": 1})
                return "partial" if max_ads else "completed"

        replacements = dict(load_config=lambda path: dict(self.config), validate_paths=lambda config: None,
                            Runtime=lambda root: self.runtime, process_lock=lambda path: contextlib.nullcontext(),
                            SqlSource=FakeSource, GraphClient=FakeGraph, Notifier=FakeNotifier,
                            Runner=FakeRunner, open_store=lambda config: self.store)
        for name, value in replacements.items():
            item = patch.object(cli, name, value)
            item.start()
            self.addCleanup(item.stop)

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["--config", "private-config", *args])
        text = output.getvalue()
        self.assertNotIn("synthetic-token", text)
        self.assertNotIn("private-config", text)
        return code, json.loads(text)

    def test_preview_no_notification_no_shared_mutation_no_execution(self):
        code, result = self.invoke("preview", "--run-id", "p1")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "previewed")
        self.assertEqual(self.events, ["scan"])
        self.assertEqual(self.store.reconciled + self.store.finished, [])

    def test_live_start_receipt_precedes_scan_and_execute_final_notice(self):
        code, result = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 0)
        self.assertEqual(self.events, ["start", "scan", "execute", "end"])
        self.assertEqual(result["status"], "completed")

    def test_disabled_live_never_opens_source_or_notifies(self):
        self.config["live_enabled"] = False
        code, result = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "live_execution_disabled")
        self.assertEqual(self.events, [])

    def test_failed_start_blocks_scan_and_delete_but_sends_end(self):
        self.fail_start = True
        code, result = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 1)
        self.assertEqual(self.events, ["start", "end"])
        self.assertEqual(result["status"], "failed")

    def test_scan_failure_is_recorded_and_finally_notified(self):
        self.fail_scan = True
        code, result = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 1)
        self.assertEqual(result["error_type"], "ValueError")
        self.assertEqual(self.events, ["start", "scan", "end"])

    def test_completed_run_id_is_idempotent(self):
        self.invoke("run", "--run-id", "r1")
        self.events.clear()
        code, _ = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 0)
        self.assertEqual(self.events, [])

    def test_preview_conversion_and_partial_continuation_reuse_manifest(self):
        self.invoke("preview", "--run-id", "r1")
        self.events.clear()
        self.invoke("run", "--run-id", "r1", "--max-ads", "1")
        self.assertEqual(self.events, ["start", "execute", "end"])
        self.events.clear()
        self.invoke("run", "--run-id", "r1")
        self.assertEqual(self.events, ["start-2", "execute", "end-2"])

    def test_expired_manifest_never_notifies_or_executes(self):
        self.invoke("preview", "--run-id", "r1")
        manifest = self.runtime.artifacts[("r1", "manifest.json")]
        manifest["frozen_at"] = (datetime.now(cli.CST) - timedelta(hours=2)).isoformat()
        manifest["sha256"] = cli.digest({k: v for k, v in manifest.items() if k != "sha256"})
        self.events.clear()
        code, result = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "manifest_expired_start_new_scan")
        self.assertEqual(self.events, [])

    def test_notify_only_retries_saved_message_without_scanning(self):
        self.invoke("run", "--run-id", "r1")
        self.events.clear()
        self.invoke("notify", "--run-id", "r1", "--phase", "end")
        self.assertEqual(self.events, ["end"])

    def test_reconcile_only_unknown_objects_with_get_and_saved_identity(self):
        self.invoke("run", "--run-id", "r1")
        self.events.clear()
        self.store.job["objects"] = [dict(status="unknown", kind="ad", object_id="123", key="ad:123",
                                          result={"credential_user_id": "803"}, user_ids=["803"], account_ids=["456"]),
                                     dict(status="deleted", kind="ad", object_id="124")]
        code, result = self.invoke("reconcile", "--run-id", "r1")
        self.assertEqual(code, 0)
        self.assertEqual(result["reconciliation_counts"], {"deleted_verified": 1})
        self.assertEqual(self.store.reconciled[0][:3], ("policy-cleanup-r1", "ad:123", "deleted"))
        self.assertEqual(self.events, [])

    def test_status_output_omits_stored_secret_fields(self):
        self.invoke("preview", "--run-id", "p1")
        self.runtime.rows["p1"]["data"]["access_token"] = "synthetic-token-never-print"
        self.events.clear()
        code, result = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(self.events, [])

    def test_interrupted_run_stops_before_notification_or_execution(self):
        args = cli.parser().parse_args(["--config", "x", "run", "--run-id", "r1"])
        stop = threading.Event()
        stop.set()
        result, code = cli.scan_or_run(args, self.config, self.runtime, stop)
        self.assertEqual(code, 130)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(self.events, ["end"])

    def test_invalid_run_id_and_max_ads_rejected_before_io(self):
        self.assertEqual(self.invoke("preview", "--run-id", "../../bad")[0], 1)
        self.assertEqual(self.invoke("run", "--max-ads", "0")[0], 1)
        self.assertEqual(self.events, [])

    def test_manifest_tampering_blocks_new_notice_and_execution(self):
        self.invoke("preview", "--run-id", "r1")
        self.runtime.artifacts[("r1", "manifest.json")]["candidates"] = [{"ad_id": "999"}]
        self.events.clear()
        code, result = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "manifest_hash_mismatch")
        self.assertEqual(self.events, [])

    def test_end_notification_failure_does_not_erase_execution_outcome(self):
        self.fail_end = True
        code, result = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["end_notification_failed"])

    def test_process_lock_conflict_never_starts_work(self):
        with patch.object(cli, "process_lock", side_effect=BlockingIOError("secret-lock-path")):
            code, result = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 1)
        self.assertEqual(result["error_type"], "BlockingIOError")
        self.assertEqual(self.events, [])

    def test_only_owned_recovered_shared_run_is_finished(self):
        self.invoke("run", "--run-id", "r1")
        self.runtime.update("r1", shared_run_id="shared-run-1")
        self.runtime.recoveries = ["r1"]
        self.store.job["runs"] = [{"run_id": "shared-run-1"}]
        self.events.clear()
        code, _ = self.invoke("notify", "--run-id", "r1")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.finished[0][0:2], ("shared-run-1", "interrupted"))
        self.assertEqual(self.events, ["end"])

    def test_long_scan_window_is_allowed_when_manifest_was_just_frozen(self):
        self.invoke("preview", "--run-id", "r1")
        manifest = self.runtime.artifacts[("r1", "manifest.json")]
        manifest["window_end"] = (datetime.now(cli.CST) - timedelta(hours=3)).isoformat()
        manifest["sha256"] = cli.digest({k: v for k, v in manifest.items() if k != "sha256"})
        self.events.clear()
        code, result = self.invoke("run", "--run-id", "r1")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")

    def test_recovery_sends_missing_end_without_replaying_deletion(self):
        self.invoke('run', '--run-id', 'r1')
        self.runtime.notices.pop(('r1', 'end'))
        self.runtime.update('r1', status='interrupted', shared_run_id='shared-run-1')
        self.runtime.recoveries = ['r1']
        self.store.job['runs'] = [{'run_id': 'shared-run-1'}]
        self.events.clear()
        cli.recover_owned(self.runtime, self.config)
        self.assertEqual(self.events, ['end'])
        self.assertEqual(self.runtime.notice('r1', 'end')['status'], 'sent')

    def test_real_runtime_preview_to_live_and_saved_receipts(self):
        class DurableNotifier:
            def __init__(self, runtime, path):
                self.runtime = runtime
            def send(self, run_id, phase, body):
                existing = self.runtime.claim_notice(run_id, phase, body)
                if existing["status"] == "sent":
                    return json.loads(existing["receipt"])
                notice = self.runtime.start_notice(run_id, phase)
                if not notice["claimed"]:
                    raise RuntimeError("notice_not_claimed")
                receipt = {"message_id": "om_durable"}
                self.runtime.finish_notice(run_id, phase, "sent", receipt)
                return receipt
        with tempfile.TemporaryDirectory() as directory:
            self.config["runtime_dir"] = directory
            with patch.object(cli, "Runtime", RealRuntime), patch.object(cli, "Notifier", DurableNotifier):
                code, _ = self.invoke("preview", "--run-id", "durable")
                self.assertEqual(code, 0)
                code, result = self.invoke("run", "--run-id", "durable", "--max-ads", "1")
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["notifications"]["end"]["status"], "sent")
                code, result = self.invoke("run", "--run-id", "durable")
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["notifications"]["end-2"]["status"], "sent")
                code, _ = self.invoke("status", "--run-id", "durable")
                self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
