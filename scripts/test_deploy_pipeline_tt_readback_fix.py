"""Single-file deployment guards; SQLite fixtures, no live service calls."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import deploy_pipeline_tt_readback_fix as deploy


class LedgerGuards(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.central = self.root / "dispatch.sqlite3"; self.native = self.root / "native.db"
        with contextlib.closing(sqlite3.connect(self.central)) as c, c:
            c.execute("CREATE TABLE pipeline_job(channel TEXT,status TEXT)")
            c.execute("INSERT INTO pipeline_job VALUES('x','running')")
        with contextlib.closing(sqlite3.connect(self.native)) as c, c:
            c.execute("CREATE TABLE tt_auto_task(status TEXT,claim_token TEXT,lease_expires_at_utc TEXT)")
            c.execute("INSERT INTO tt_auto_task VALUES('unknown','','')")
            c.execute("INSERT INTO tt_auto_task VALUES('reconciling','','')")
        self.addCleanup(patch.stopall)
        patch.object(deploy, "DATA", self.root).start(); patch.object(deploy, "NATIVE_DB", self.native).start()

    def test_idle_unknown_and_readback_wait_do_not_modify_ledgers(self):
        before = [p.read_bytes() for p in (self.central, self.native)]
        deploy.quiescent()
        self.assertEqual(before, [p.read_bytes() for p in (self.central, self.native)])

    def test_central_tt_work_and_native_execution_block(self):
        for status in ("running", "legacy_running"):
            with self.subTest(status=status):
                with contextlib.closing(sqlite3.connect(self.central)) as c, c:
                    c.execute("INSERT INTO pipeline_job VALUES('tt',?)", (status,))
                with self.assertRaisesRegex(RuntimeError, "tt_central_work_running"): deploy.quiescent()
                with contextlib.closing(sqlite3.connect(self.central)) as c, c:
                    c.execute("DELETE FROM pipeline_job WHERE channel='tt'")
        for status in ("selecting", "preparing", "publishing"):
            with self.subTest(status=status):
                with contextlib.closing(sqlite3.connect(self.native)) as c, c:
                    c.execute("UPDATE tt_auto_task SET status=?", (status,))
                with self.assertRaisesRegex(RuntimeError, "tt_native_work_active"): deploy.quiescent()

    def test_valid_or_missing_expiry_claim_blocks_even_on_unknown(self):
        for expiry, error in (("", "without_expiry"), ((deploy.now() + dt.timedelta(minutes=5)).isoformat(), "claim_active")):
            with self.subTest(expiry=expiry):
                with contextlib.closing(sqlite3.connect(self.native)) as c, c:
                    c.execute("UPDATE tt_auto_task SET claim_token='frozen',lease_expires_at_utc=?", (expiry,))
                with self.assertRaisesRegex(RuntimeError, error): deploy.quiescent()


class DeploymentGuards(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root = Path(tmp.name)
        self.target = self.root / "tt_readback.py"; self.old = b"old = True\n"; self.new = b"new = True\n"; self.target.write_bytes(self.old)
        self.commands = []; self.state = "active"; self.pid = "100"; self.gate = False
        self.timers = {unit: ("inactive" if i == 1 else "active") for i, unit in enumerate(deploy.TIMERS)}
        self.before = dict(self.timers)
        self.addCleanup(patch.stopall)
        patch.object(deploy, "DATA", self.root).start()
        patch.object(deploy, "graceful_contract", return_value={"TimeoutStopUSec": "6h", "SendSIGKILL": "yes"}).start()
        patch.object(deploy, "runtime_check", side_effect=self.check).start()
        patch.object(deploy, "quiescent").start()
        patch.object(deploy, "show", side_effect=self.show).start()
        patch.object(deploy, "run", side_effect=self.invoke).start()
        patch.object(deploy, "accepted", return_value=False).start()
        patch.object(deploy, "listener", return_value=True).start()
        patch.object(deploy, "gate_present", side_effect=lambda rule: self.gate).start()
        patch.object(deploy, "health", return_value={"status": 200, "ok": True}).start()
        patch.object(deploy.os, "chown", create=True).start()
        patch.object(deploy, "wait_until", side_effect=self.wait).start()

    def check(self, expected):
        deploy.need(self.target.read_bytes() == expected, "old_file_hash_mismatch")
        return self.target

    def show(self, unit, field):
        if unit == deploy.UNIT: return {"ActiveState": self.state, "MainPID": self.pid}[field]
        if unit.endswith("-gate-cleanup.timer"): return "loaded" if field == "LoadState" else "inactive"
        return {"LoadState": "loaded", "ActiveState": self.timers[unit], "UnitFileState": "enabled"}[field]

    def invoke(self, args, **kwargs):
        self.commands.append(args)
        if args[0] == "systemctl":
            if args[-1] == deploy.UNIT:
                if args[1] == "stop": self.state, self.pid = "inactive", "0"
                elif args[1] == "start": self.state, self.pid = "active", "101"
            elif args[-1] in self.timers:
                self.timers[args[-1]] = "active" if args[1] == "start" else "inactive"
        if args[0] == "iptables": self.gate = "-I" in args
        return b""

    def wait(self, predicate, code, seconds=45):
        deploy.need(predicate(), code)

    def audit(self):
        return json.loads(next(self.root.glob("tt-readback-fix-*/deploy.json")).read_text())

    def test_success_replaces_only_target_and_restores_original_timers(self):
        report = deploy.apply("b" * 40, self.old, self.new)
        self.assertEqual(self.target.read_bytes(), self.new)
        self.assertEqual(Path(report["backup"]).read_bytes(), self.old)
        self.assertEqual((report["old_pid"], report["new_pid"], report["status"]), (100, 101, "applied"))
        self.assertEqual(self.timers, self.before); self.assertFalse(self.gate)
        self.assertEqual(report["cleanup_errors"], [])
        self.assertTrue(report["gate_failsafe_cancelled"])
        arm = next(i for i, cmd in enumerate(self.commands) if cmd[0] == "systemd-run")
        gate = next(i for i, cmd in enumerate(self.commands) if cmd[0] == "iptables" and "-I" in cmd)
        self.assertLess(arm, gate)
        self.assertIn("--on-active=120s", self.commands[arm])
        self.assertEqual(self.commands[arm][self.commands[arm].index("--") + 1:], ["/usr/sbin/iptables", "-w", "5", "-D", *self.commands[gate][4:]])
        self.assertIn(["systemctl", "stop", report["gate_failsafe_unit"] + ".timer"], self.commands)
        service_commands = [cmd for cmd in self.commands if cmd[0] == "systemctl" and cmd[-1].endswith(".service")]
        self.assertEqual(service_commands, [["systemctl", "stop", "--no-block", deploy.UNIT], ["systemctl", "start", "--no-block", deploy.UNIT]])

    def test_old_hash_drift_refuses_before_any_service_mutation(self):
        self.target.write_bytes(b"unreviewed\n")
        with self.assertRaisesRegex(RuntimeError, "old_file_hash_mismatch"): deploy.apply("b" * 40, self.old, self.new)
        self.assertEqual(self.commands, [])

    def test_accepted_request_timeout_cleans_gate_without_stopping_service(self):
        with patch.object(deploy, "accepted", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "accepted_requests_not_drained"): deploy.apply("b" * 40, self.old, self.new)
        self.assertEqual(self.target.read_bytes(), self.old); self.assertEqual(self.pid, "100")
        self.assertFalse(self.gate); self.assertEqual(self.timers, self.before)
        self.assertFalse(any(cmd[-1] == deploy.UNIT for cmd in self.commands))

    def test_graceful_stop_pending_does_not_replace_restart_or_kill(self):
        def wait(predicate, code, seconds=45):
            if code == "graceful_stop_pending":
                self.state, self.pid = "deactivating", "100"
                raise RuntimeError(code)
            return self.wait(predicate, code, seconds)
        with patch.object(deploy, "wait_until", side_effect=wait):
            with self.assertRaisesRegex(RuntimeError, "graceful_stop_pending"): deploy.apply("b" * 40, self.old, self.new)
        self.assertEqual(self.target.read_bytes(), self.old); self.assertFalse(self.gate)
        self.assertEqual(self.timers, self.before); self.assertEqual(self.audit()["status"], "stop_pending")
        self.assertFalse(any(cmd[0] == "systemctl" and cmd[1] in ("start", "kill", "restart") and cmd[-1] == deploy.UNIT for cmd in self.commands))

    def test_racing_central_claim_after_gate_refuses_before_stop(self):
        with patch.object(deploy, "quiescent", side_effect=[None, RuntimeError("tt_central_work_running")]):
            with self.assertRaisesRegex(RuntimeError, "tt_central_work_running"): deploy.apply("b" * 40, self.old, self.new)
        self.assertEqual(self.target.read_bytes(), self.old); self.assertFalse(self.gate)
        self.assertEqual(self.timers, self.before)
        self.assertFalse(any(cmd[-1] == deploy.UNIT for cmd in self.commands))

    def test_racing_claim_after_natural_stop_restores_old_code_service(self):
        with patch.object(deploy, "quiescent", side_effect=[None, None, RuntimeError("tt_central_work_running")]) as guard:
            with self.assertRaisesRegex(RuntimeError, "tt_central_work_running"): deploy.apply("b" * 40, self.old, self.new)
        self.assertEqual(guard.call_count, 3)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertTrue(self.audit()["original_start_requested"])
        self.assertEqual(self.state, "active"); self.assertFalse(self.gate)
        self.assertEqual(self.timers, self.before)

    def test_expired_failsafe_window_refuses_before_stop(self):
        def wait(predicate, code, seconds=45):
            result = self.wait(predicate, code, seconds)
            if code == "accepted_requests_not_drained": self.gate = False
            return result
        with patch.object(deploy, "wait_until", side_effect=wait):
            with self.assertRaisesRegex(RuntimeError, "gate_window_expired"): deploy.apply("b" * 40, self.old, self.new)
        self.assertEqual(self.target.read_bytes(), self.old); self.assertEqual(self.timers, self.before)
        self.assertFalse(any(cmd[-1] == deploy.UNIT for cmd in self.commands))

    def test_verify_existing_writes_separate_report_without_original_audit_or_service_changes(self):
        deploy.apply("b" * 40, self.old, self.new)
        audit = next(self.root.glob("tt-readback-fix-*/deploy.json"))
        prior = json.loads(audit.read_text()); prior.update(status="file_replaced", error="HTTPError")
        audit.write_text(json.dumps(prior)); original = audit.read_bytes()
        self.commands.clear()
        with patch.object(deploy, "fetched_source", return_value=(self.old, self.new)):
            result = deploy.verify_existing(audit)
        self.assertEqual(audit.read_bytes(), original)
        self.assertEqual(self.commands, [])
        self.assertEqual(result["original_status"], "file_replaced")
        self.assertEqual(result["original_error"], "HTTPError")
        self.assertFalse(result["restarted"])
        self.assertTrue(audit.with_name("postverify.json").is_file())


class TakeoverHealthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        native = Path(tmp.name) / "native.db"
        with contextlib.closing(sqlite3.connect(native)) as c, c:
            c.execute("CREATE TABLE tt_auto_task(id INTEGER,status TEXT,publish_id TEXT,unknown_outcome INTEGER,published_at_utc TEXT)")
            c.execute("INSERT INTO tt_auto_task VALUES(987,'failed','',0,'')")
        self.addCleanup(patch.stopall)
        patch.object(deploy, "NATIVE_DB", native).start()
        patch.object(deploy, "show", return_value="101").start()
        self.env = {"PIPELINE_TT_DISPATCH_ENABLED": "1", "PIPELINE_TT_CANONICAL_BRIDGE_ENABLED": "1", "TT_AUTO_POST_INTERNAL_TOKEN": "test-only-bearer"}
        patch.object(deploy, "process_contract", return_value=self.env).start()
        self.body = {"service": "tt-auto-post", "ok": False, "automation": {"ready": False, "problems": ["tt-auto-post-runner.path:inactive"]},
                     "gates": {key: True for key in ("direct_audit_approved", "is_open", "live_enabled", "url_property_verified")}}
        self.receipt = {"status": "failed", "publish_id": "", "unknown_outcome": False, "published_at": ""}
        self.native = native

    def test_known_legacy_503_requires_exact_readonly_canonical_receipt(self):
        before = self.native.read_bytes()
        with patch.object(deploy, "request_json", side_effect=[(503, self.body), (200, self.receipt)]) as request:
            result = deploy.health()
        self.assertEqual(result["status"], 503)
        self.assertTrue(result["canonical_readback"]["matches_native"])
        self.assertEqual([call.args[0] for call in request.call_args_list], ["/health", "/internal/pipeline-dispatch/tt/reconcile"])
        self.assertEqual(request.call_args_list[1].kwargs["payload"]["job"]["source_id"], "987")
        self.assertEqual(before, self.native.read_bytes())
        self.assertNotIn("test-only-bearer", json.dumps(result))

    def test_200_still_requires_canonical_interface(self):
        self.body.update(ok=True); self.body["automation"] = {"ready": True, "problems": []}
        with patch.object(deploy, "request_json", side_effect=[(200, self.body), (409, {"code": "bridge_error"})]):
            with self.assertRaisesRegex(RuntimeError, "canonical_readback_mismatch"): deploy.health()

    def test_other_503_problems_or_missing_takeover_flags_are_rejected(self):
        self.body["automation"]["problems"].append("database_unavailable")
        with patch.object(deploy, "request_json", return_value=(503, self.body)) as request:
            with self.assertRaisesRegex(RuntimeError, "unrecognized_health_failure"): deploy.health()
            self.assertEqual(request.call_count, 1)
        self.env["PIPELINE_TT_CANONICAL_BRIDGE_ENABLED"] = "0"
        with patch.object(deploy, "request_json") as request:
            with self.assertRaisesRegex(RuntimeError, "pipeline_takeover_not_enabled"): deploy.health()
            request.assert_not_called()

    def test_canonical_claim_and_legacy_readback_routes_are_not_probe_options(self):
        for path in ("/internal/pipeline-dispatch/tt/reserve", "/internal/pipeline-dispatch/tt/reconcile-legacy-next"):
            with self.assertRaisesRegex(RuntimeError, "non_readonly_probe_path"): deploy.request_json(path, payload={})


if __name__ == "__main__":
    unittest.main()
