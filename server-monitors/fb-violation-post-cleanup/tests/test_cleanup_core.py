import copy
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "cleanup_core.py"
SPEC = importlib.util.spec_from_file_location("cleanup_core_under_test", MODULE_PATH)
core = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core)
START = "2026-07-28T09:14:18+00:00"
END = "2026-10-26T09:14:18+00:00"
PUBLISHED = "2026-09-01T01:00:00+00:00"


def evidence(page="100", number=1, **kwargs):
    row = {
        "post_id": "{}_{}".format(page, number), "page_id": str(page),
        "material_id": str(500 + number), "source_row_id": str(900 + number),
        "group_ids": [3], "origin": "normal", "log_id": number,
        "published_at_utc": PUBLISHED, "fb_violation": 1,
    }
    row.update(kwargs)
    return row


def manifest(count=8, pages=1):
    return core.canonicalize_evidence([
        evidence(page=str(100 + number % pages), number=number)
        for number in range(1, count + 1)
    ], START, END)


class FakeAdapter:
    def __init__(self, ledger=None):
        self.ledger = ledger
        self.deleted = set()
        self.delete_calls = []
        self.prechecks = {}
        self.precheck_delays = {}
        self.inspections = {}
        self.results = {}
        self.readbacks = {}
        self.crash = set()
        self.delay = 0
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.active_pages = set()
        self.page_overlap = False
        self.closed_clients = []

    def for_target(self, target):
        adapter = self
        post_id = target["post_id"]

        class Bound:
            def precheck(self):
                if adapter.precheck_delays.get(post_id):
                    time.sleep(adapter.precheck_delays[post_id])
                return adapter.prechecks.get(post_id, {"ok": True, "credential_id": "fb_user:42"})

            def reconcile_precheck(self):
                return {"ok": True, "credential_id": "fb_user:42"}

            def close(self):
                adapter.closed_clients.append(post_id)

            def inspect(self, requested_id):
                if requested_id != post_id:
                    raise AssertionError("wrong Post routed to a bound Page credential")
                if requested_id in adapter.deleted:
                    return adapter.readbacks.get(post_id, {"ok": False, "absent": True, "unavailable": True})
                return adapter.inspections.get(post_id, {
                    "ok": True, "id": post_id, "page_id": target["page_id"], "created_at": PUBLISHED,
                })

            def delete(self, requested_id):
                if adapter.ledger is not None:
                    with contextlib.closing(sqlite3.connect(adapter.ledger.path)) as independent_connection:
                        row = independent_connection.execute(
                            "SELECT status,credential_id FROM targets WHERE post_id=?", (requested_id,)).fetchone()
                        intents = independent_connection.execute(
                            "SELECT COUNT(*) FROM events WHERE post_id=? AND event='delete_intent'", (requested_id,)).fetchone()[0]
                        if row != ("deleting", "fb_user:42") or intents != 1:
                            raise AssertionError("DELETE ran before durable committed intent")
                with adapter.lock:
                    adapter.delete_calls.append(requested_id)
                    adapter.active += 1
                    adapter.max_active = max(adapter.max_active, adapter.active)
                    if target["page_id"] in adapter.active_pages:
                        adapter.page_overlap = True
                    adapter.active_pages.add(target["page_id"])
                try:
                    if adapter.delay:
                        time.sleep(adapter.delay)
                    if requested_id in adapter.crash:
                        raise KeyboardInterrupt("simulated process death during DELETE")
                    result = adapter.results.get(post_id, {"kind": "success", "responseSafe": {"success": True}})
                    if isinstance(result, Exception):
                        raise result
                    if result.get("kind") == "success":
                        adapter.deleted.add(requested_id)
                    return result
                finally:
                    with adapter.lock:
                        adapter.active -= 1
                        adapter.active_pages.remove(target["page_id"])

        return Bound()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = core.Ledger(Path(self.temp.name) / "cleanup.sqlite")
        self.addCleanup(self.ledger.close)

    def test_canonical_merge_keeps_cross_group_and_cross_log_evidence(self):
        first = evidence()
        second = evidence(group_ids=[4, 24], origin="ai", log_id=66)
        # Same source row number in another namespace must not merge distinct Posts.
        third = evidence(number=2, source_row_id=first["source_row_id"], group_ids=[25])
        result = core.canonicalize_evidence([first, second, first, third], START, END)
        self.assertEqual(2, len(result["targets"]))
        self.assertEqual([3, 4, 24], result["targets"][0]["groups"])
        self.assertEqual(2, len(result["targets"][0]["evidence"]))
        self.assertEqual(["501"], result["targets"][0]["material_ids"])

    def test_video_id_is_never_promoted_to_post_id(self):
        result = core.canonicalize_evidence([evidence(post_id="987654321", real_post_id="")], START, END)
        self.assertEqual([], result["targets"])
        self.assertEqual("real_post_id_unresolved", result["unresolved"][0]["reason"])

    def test_identity_conflicts_and_violation_five_are_skipped(self):
        rows = [evidence(), evidence(material_id="700"),
                evidence(number=2, page_id="999"), evidence(number=3, fb_violation=5)]
        result = core.canonicalize_evidence(rows, START, END)
        self.assertEqual([
            "conflicting_material_identity", "conflicting_page_identity", "material_not_currently_violating",
        ], [row["skip_reason"] for row in result["targets"]])
        adapter = FakeAdapter()
        core.execute(result, self.ledger, adapter)
        self.assertEqual([], adapter.delete_calls)

    def test_frozen_manifest_hash_and_ledger_binding(self):
        frozen = manifest()
        raw = core.manifest_bytes(frozen)
        path = Path(self.temp.name) / "manifest.json"
        path.write_bytes(raw)
        self.assertEqual(frozen, core.load_manifest(path, hashlib.sha256(raw).hexdigest()))
        path.write_bytes(raw.replace(b'"version":1', b'"version":2'))
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            core.load_manifest(path, hashlib.sha256(raw).hexdigest())
        self.ledger.import_manifest(frozen)
        changed = copy.deepcopy(frozen)
        changed["targets"].pop()
        with self.assertRaisesRegex(ValueError, "different frozen manifest"):
            self.ledger.import_manifest(changed)

    def test_unscoped_manifest_and_long_window_rejected(self):
        frozen = manifest()
        frozen["allowed_groups"].append(88)
        with self.assertRaisesRegex(ValueError, "authorized Page groups"):
            self.ledger.import_manifest(frozen)
        frozen = manifest()
        frozen["window_start"] = "2026-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "90 days"):
            self.ledger.import_manifest(frozen)
        with self.assertRaisesRegex(ValueError, "explicit timezone"):
            core.canonicalize_evidence([], "2026-01-01", END)

    def test_canary_commits_intent_before_delete_and_gates_remaining(self):
        frozen = manifest(8)
        adapter = FakeAdapter(self.ledger)
        blocked = core.execute(frozen, self.ledger, adapter, stage="remaining")
        self.assertEqual("canary_gate_not_satisfied", blocked["halted"])
        self.assertEqual([], adapter.delete_calls)
        canary = core.execute(frozen, self.ledger, adapter, stage="canary")
        self.assertEqual(5, canary["canary_verified"])
        self.assertEqual(5, len(adapter.delete_calls))
        complete = core.execute(frozen, self.ledger, adapter, stage="remaining")
        self.assertEqual(8, complete["counts"]["deleted"])
        core.execute(frozen, self.ledger, adapter)
        self.assertEqual(8, len(adapter.delete_calls))

    def test_canary_skips_changed_violation_without_consuming_five_requests(self):
        frozen = manifest(7)
        adapter = FakeAdapter(self.ledger)
        adapter.prechecks["100_1"] = {"ok": False, "reason": "material_violation_cleared"}
        summary = core.execute(frozen, self.ledger, adapter, stage="canary")
        self.assertEqual(5, summary["actual_requests"])
        self.assertEqual(5, summary["canary_verified"])
        self.assertEqual(1, summary["counts"]["skipped"])
        self.assertNotIn("100_1", adapter.delete_calls)

    def test_final_source_rejection_after_intent_is_not_counted_as_delete(self):
        frozen = manifest(7)
        adapter = FakeAdapter(self.ledger)
        adapter.results["100_1"] = {"kind": "failure", "response": {"sent": False, "reason": "violation_cleared"}}
        summary = core.execute(frozen, self.ledger, adapter, stage="canary")
        self.assertEqual(5, summary["actual_requests"])
        self.assertEqual(6, summary["intent_count"])
        self.assertEqual(1, summary["not_sent_after_intent"])
        self.assertEqual(5, summary["canary_verified"])
        self.assertEqual("skipped", self.ledger.get("100_1")["status"])

    def test_bad_graph_identity_and_outside_actual_publish_time_never_delete(self):
        frozen = manifest(8)
        adapter = FakeAdapter(self.ledger)
        adapter.inspections["100_1"] = {"ok": True, "id": "100_1", "page_id": "999", "created_at": PUBLISHED}
        adapter.inspections["100_2"] = {"ok": True, "id": "100_2", "page_id": "100", "created_at": "2026-07-28T09:14:17+00:00"}
        adapter.inspections["100_3"] = {"ok": True, "id": "100_3", "page_id": "100", "created_at": "2026-10-26T09:14:19+00:00"}
        # Exact lower and upper bounds are eligible regardless of log timestamps.
        adapter.inspections["100_4"] = {"ok": True, "id": "100_4", "page_id": "100", "created_at": START}
        adapter.inspections["100_5"] = {"ok": True, "id": "100_5", "page_id": "100", "created_at": END}
        summary = core.execute(frozen, self.ledger, adapter)
        self.assertEqual(3, summary["counts"]["skipped"])
        self.assertEqual(5, summary["counts"]["deleted"])
        self.assertEqual("post_page_identity_mismatch", self.ledger.get("100_1")["result"]["reason"])

    def test_failed_unknown_and_visible_canary_stop_without_retry(self):
        for kind in ("failure", "unknown", "visible", "unavailable_only"):
            with self.subTest(kind=kind):
                path = Path(self.temp.name) / (kind + ".sqlite")
                ledger = core.Ledger(path)
                try:
                    frozen = manifest(8)
                    adapter = FakeAdapter(ledger)
                    if kind in ("failure", "unknown"):
                        adapter.results["100_1"] = {"kind": kind, "error": {"code": 100, "error_subcode": 33}}
                    else:
                        adapter.readbacks["100_1"] = {"ok": kind == "visible", "unavailable": True}
                    result = core.execute(frozen, ledger, adapter)
                    self.assertEqual("canary_failed_unknown_or_unverified", result["halted"])
                    self.assertEqual(["100_1"], adapter.delete_calls)
                    core.execute(frozen, ledger, adapter)
                    self.assertEqual(["100_1"], adapter.delete_calls)
                finally:
                    ledger.close()

    def test_acknowledgement_and_live_page_receipt_validate_without_claiming_absence(self):
        frozen = manifest(8)
        adapter = FakeAdapter(self.ledger)
        adapter.readbacks["100_1"] = {
            "ok": False, "receipt_validated": True,
            "page_identity": {"ok": True, "response": {"id": "100", "http_status": 200}},
            "response": {"http_status": 400, "error": {"code": 10}},
        }
        canary = core.execute(frozen, self.ledger, adapter, stage="canary")
        self.assertEqual(5, canary["canary_validated"])
        self.assertEqual(4, canary["canary_verified"])
        self.assertEqual(1, canary["canary_readback_unconfirmed"])
        self.assertEqual("unconfirmed_after_ack", self.ledger.get("100_1")["verification"])
        self.assertFalse(self.ledger.get("100_1")["readback"].get("absent", False))
        complete = core.execute(frozen, self.ledger, adapter, stage="remaining")
        self.assertEqual(8, complete["counts"]["deleted"])
        self.assertIsNone(complete["halted"])

    def test_receipt_validation_cannot_bypass_unknown_visible_or_identity_failure(self):
        for case in ("unknown", "failure", "visible", "identity_unreadable"):
            with self.subTest(case=case):
                ledger = core.Ledger(Path(self.temp.name) / ("receipt_" + case + ".sqlite"))
                try:
                    frozen = manifest(8)
                    adapter = FakeAdapter(ledger)
                    readback = {"ok": case == "visible", "receipt_validated": True,
                                "page_identity": {"ok": case != "identity_unreadable"},
                                "response": {"http_status": 400, "error": {"code": 10}}}
                    adapter.readbacks["100_1"] = readback
                    if case in ("unknown", "failure"):
                        adapter.results["100_1"] = {"kind": case, "error": {"code": 10}}
                    result = core.execute(frozen, ledger, adapter)
                    if case == "unknown":
                        ledger.record_readback("100_1", readback)
                    self.assertEqual(0, ledger.summary()["canary_validated"])
                    self.assertEqual("canary_failed_unknown_or_unverified", result["halted"])
                    self.assertEqual(["100_1"], adapter.delete_calls)
                finally:
                    ledger.close()

    def test_crash_after_intent_becomes_unknown_and_is_not_retried(self):
        frozen = manifest(6)
        adapter = FakeAdapter(self.ledger)
        adapter.crash.add("100_1")
        with self.assertRaises(KeyboardInterrupt):
            core.execute(frozen, self.ledger, adapter)
        self.assertEqual("deleting", self.ledger.get("100_1")["status"])
        core.execute(frozen, self.ledger, adapter)
        self.assertEqual("unknown", self.ledger.get("100_1")["status"])
        self.assertEqual(["100_1"], adapter.delete_calls)
        self.assertEqual(1, self.ledger.summary()["actual_requests"])

    def test_unknown_delete_exception_and_reconcile_remain_unknown(self):
        frozen = manifest(6)
        adapter = FakeAdapter(self.ledger)
        adapter.results["100_1"] = TimeoutError("response lost")
        summary = core.execute(frozen, self.ledger, adapter)
        self.assertEqual(1, summary["counts"]["unknown"])
        adapter.deleted.add("100_1")
        core.reconcile(frozen, self.ledger, adapter)
        row = self.ledger.get("100_1")
        self.assertEqual("unknown", row["status"])
        self.assertEqual("unknown_unchanged", row["verification"])
        self.assertEqual(["100_1"], adapter.delete_calls)

    def test_reconcile_can_finish_acknowledged_canary_readback(self):
        frozen = manifest(6)
        adapter = FakeAdapter(self.ledger)
        adapter.readbacks["100_1"] = {"ok": False, "error": {"code": 10}}
        first = core.execute(frozen, self.ledger, adapter)
        self.assertEqual(0, first["canary_verified"])
        adapter.readbacks.clear()
        core.reconcile(frozen, self.ledger, adapter)
        complete = core.execute(frozen, self.ledger, adapter)
        self.assertEqual(6, complete["counts"]["deleted"])
        self.assertEqual(6, len(set(adapter.delete_calls)))

    def test_max_two_workers_and_serial_requests_for_each_page(self):
        frozen = manifest(17, pages=3)
        adapter = FakeAdapter(self.ledger)
        adapter.delay = 0.015
        summary = core.execute(frozen, self.ledger, adapter)
        self.assertEqual(17, summary["counts"]["deleted"])
        self.assertEqual(2, adapter.max_active)
        self.assertFalse(adapter.page_overlap)

    def test_failures_in_remaining_continue_without_retry(self):
        frozen = manifest(8)
        adapter = FakeAdapter(self.ledger)
        adapter.results["100_6"] = {"kind": "failure", "error": {"code": 10}}
        adapter.results["100_7"] = {"kind": "unknown", "reason": "network_timeout"}
        result = core.execute(frozen, self.ledger, adapter)
        self.assertEqual(1, result["counts"]["failed"])
        self.assertEqual(1, result["counts"]["unknown"])
        self.assertEqual(6, result["counts"]["deleted"])
        core.execute(frozen, self.ledger, adapter)
        self.assertEqual(8, len(adapter.delete_calls))

    def test_request_limit_is_global_across_page_workers(self):
        frozen = manifest(20, pages=4)
        adapter = FakeAdapter(self.ledger)
        adapter.delay = 0.003
        summary = core.execute(frozen, self.ledger, adapter, limit=9)
        self.assertEqual(9, summary["actual_requests"])
        self.assertEqual(11, summary["counts"]["pending"])

    def test_slow_skipped_precheck_does_not_reserve_delete_budget(self):
        frozen = core.canonicalize_evidence([
            evidence(page="100", number=1), evidence(page="200", number=2), evidence(page="300", number=3),
        ], START, END)
        adapter = FakeAdapter(self.ledger)
        adapter.precheck_delays["200_2"] = 0.04
        adapter.prechecks["200_2"] = {"ok": False, "reason": "violation_cleared"}
        result = core.execute(frozen, self.ledger, adapter, canary_size=1, limit=2)
        self.assertEqual(2, result["actual_requests"])
        self.assertEqual(0, result["counts"]["pending"])
        self.assertEqual({"100_1", "300_3"}, set(adapter.delete_calls))

    def test_reconcile_remains_read_only_when_deletion_scope_has_changed(self):
        frozen = manifest(6)
        adapter = FakeAdapter(self.ledger)
        core.execute(frozen, self.ledger, adapter, stage="canary")
        adapter.prechecks["100_1"] = {"ok": False, "reason": "violation_cleared"}
        summary = core.reconcile(frozen, self.ledger, adapter)
        self.assertEqual(5, summary["canary_verified"])
        self.assertEqual(5, len(adapter.delete_calls))
        self.assertEqual(2, adapter.closed_clients.count("100_1"))

    def test_unknown_response_shape_is_never_success(self):
        frozen = manifest(6)
        adapter = FakeAdapter(self.ledger)
        adapter.results["100_1"] = {"success": True}
        result = core.execute(frozen, self.ledger, adapter)
        self.assertEqual(1, result["counts"]["unknown"])
        self.assertEqual(0, result["counts"]["deleted"])

    def test_ledger_redacts_secret_fields_and_querystrings(self):
        frozen = manifest(6)
        adapter = FakeAdapter(self.ledger)
        adapter.prechecks["100_1"] = {"ok": True, "credential_id": "fb_user:42", "access_token": "secret-value"}
        adapter.results["100_1"] = {"kind": "failure", "error": {"url": "https://graph.invalid/x?access_token=query-secret&fields=id"}}
        core.execute(frozen, self.ledger, adapter)
        content = json.dumps(self.ledger.events())
        self.assertNotIn("secret-value", content)
        self.assertNotIn("query-secret", content)
        self.assertIn("REDACTED", content)


if __name__ == "__main__":
    unittest.main()
