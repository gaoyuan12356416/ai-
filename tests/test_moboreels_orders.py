"""Financial behavior uses documented fixtures, not fabricated production orders."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.moboreels_library.orders import (
    AD_DAY_TABLE, AD_TABLE, DAY_MS, ORDER_TABLE, ReportError, ReportStore, Reports,
    get_order_page, normalize_order,
)
from features.moboreels_library.settings import Settings


SOURCE_DAY = "2026-11-10"
PERIOD = {"date_from": SOURCE_DAY, "date_to": SOURCE_DAY}
REAL_SLEEP = time.sleep


def documented_order(ref="order-1", **changes):
    row = {"create_time": SOURCE_DAY + " 17:49:50", "code": "25FA", "status_text": "充值订单",
           "ref_order_id": ref, "story_id": 42, "story_name": "Document fixture", "amount": "3.0000",
           "project_type": 2, "coo_order_status": 1, "institution_user_id": "institution",
           "star_user_id": "provider-account", "real_star_user_id": "our-actor"}
    row.update(changes)
    return row


def documented_ad(code="25FA", amount="100.125"):
    return {"ad_time": SOURCE_DAY, "project_type": 2, "code": code, "amount": amount}


class FakeProvider:
    def __init__(self, orders=None, ads=None):
        self.orders = orders or {-1: [], 0: [], 1: []}
        self.ads = ads or []
        self.calls = []
        self.overrides = {}
        self.blocked = False
        self.entered, self.release = threading.Event(), threading.Event()

    def _post(self, path, payload):
        self.calls.append((path, deepcopy(payload)))
        self.entered.set()
        if self.blocked and not self.release.wait(3):
            raise RuntimeError("test did not release provider")
        key = (path, payload.get("status"), payload["page_index"])
        if key in self.overrides:
            data = self.overrides[key]
        else:
            rows = self.orders[payload["status"]] if path.endswith("getorder") else self.ads
            begin = (payload["page_index"] - 1) * payload["page_size"]
            data = {"total": len(rows), "list": rows[begin:begin + payload["page_size"]]}
        body = {"code": 200, "data": data}
        return SimpleNamespace(status_code=200, json=lambda **kwargs: json.loads(json.dumps(body), **kwargs))


class FakeStore:
    same = staticmethod(ReportStore.same)

    def __init__(self):
        self.rows = {"orders": {}, "ad": {}}
        self.writes = []
        self.reads = []
        self.pointers = {}
        self.published = []
        self.fail_orders = False
        self.commit_then_fail = False
        self.fail_publish = False

    def read_records(self, kind, keys):
        self.reads.append((kind, list(keys)))
        return {key: deepcopy(self.rows[kind][key]) for key in keys if key in self.rows[kind]}

    def write_records(self, kind, rows, run_id, seen_at):
        assert len(rows) <= 20
        if kind == "orders" and self.fail_orders:
            raise RuntimeError("test write failed")
        self.writes.append((kind, [r["row_key"] for r in rows]))
        for row in rows:
            self.rows[kind][row["row_key"]] = dict(deepcopy(row), last_sync_run_id=run_id)
        if self.commit_then_fail:
            self.commit_then_fail = False
            raise RuntimeError("test lost commit acknowledgement")

    def touch_records(self, kind, keys, run_id, seen_at):
        for key in keys:
            self.rows[kind][key]["last_sync_run_id"] = run_id

    def publish_ad_day(self, day, run_id, row_count, source_hash, finished_at):
        if self.fail_publish:
            raise RuntimeError("test publish failed")
        self.pointers[day] = {"snapshot_id": run_id, "row_count": row_count, "source_hash": source_hash}
        self.published.append(day)

    def close_writer(self):
        pass


class OrderNormalizationTest(unittest.TestCase):
    def test_documented_amount_time_and_actor_fields_are_preserved(self):
        raw = documented_order(amount="0.12345678", extra={"future": True})
        row = normalize_order(raw, "institution", {"order-1"}, set())
        self.assertEqual(row["amount_usd"], "0.12345678")
        self.assertEqual(row["source_create_time"], raw["create_time"])
        self.assertEqual(row["source_date"], SOURCE_DAY)
        self.assertEqual(row["real_star_user_id"], "our-actor")
        self.assertEqual(row["star_user_id"], "provider-account")
        self.assertEqual(row["classification"], 0)
        self.assertEqual(row["raw_json"]["extra"], {"future": True})

    def test_classification_uses_numeric_filter_membership_not_status_text(self):
        row = normalize_order(documented_order(status_text="退款订单"), "institution", {"order-1"}, set())
        self.assertEqual(row["classification"], 0)
        refund = normalize_order(documented_order(status_text="充值订单", amount="-3.0000"), "institution", set(), {"order-1"})
        self.assertEqual(refund["classification"], 1)
        self.assertEqual(refund["row_key"], row["row_key"])
        self.assertEqual(refund["amount_usd"], "-3.0000")

    def test_unknown_enum_conflicting_filters_and_unknown_timezone_remain_unclassified(self):
        cases = [(documented_order(coo_order_status=7), {"order-1"}, set(), "unknown_order_state"),
                 (documented_order(project_type="网文"), {"order-1"}, set(), "unknown_business_type"),
                 (documented_order(), {"order-1"}, {"order-1"}, "status_filter_conflict"),
                 (documented_order(create_time="unconfirmed"), {"order-1"}, set(), "unknown_source_time")]
        for raw, recharge, refund, reason in cases:
            with self.subTest(reason=reason):
                row = normalize_order(raw, "institution", recharge, refund)
                self.assertEqual(row["classification"], -1)
                self.assertEqual(row["classification_reason"], reason)
                self.assertEqual(row["raw_json"], raw)

    def test_invalid_amount_or_cross_institution_is_rejected_before_writes(self):
        for changes in ({"amount": "NaN"}, {"amount": "0.123456789"}, {"ref_order_id": ""},
                        {"institution_user_id": "different"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_order(documented_order(**changes), "institution")

    def test_order_request_uses_milliseconds_explicit_filters_and_bounded_window(self):
        provider = FakeProvider()
        end = 1794484800000
        self.assertEqual(get_order_page(provider, end - 31 * DAY_MS + 1, end, -1, 1), {"total": 0, "list": []})
        path, body = provider.calls[0]
        self.assertEqual(path, "/api/story/getorder")
        self.assertEqual(body["project_type"], 2)
        self.assertEqual(body["status"], -1)
        with self.assertRaises(ValueError):
            get_order_page(provider, end - 31 * DAY_MS, end, -1, 1)


class ReportSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.temp.name), "institution", "unused", "unused", "unused", "unused")
        self.store = FakeStore()
        self.sleep = patch("features.moboreels_library.orders.time.sleep", side_effect=lambda value: REAL_SLEEP(min(value, 0.001)))
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.temp.cleanup()

    def complete(self, manager):
        run = manager.start(PERIOD)
        final = manager.wait(run["run_id"], 10)
        self.assertIn(final["status"], ("succeeded", "failed"), final)
        return final

    def manager(self, provider):
        return Reports(self.settings, provider=provider, store=self.store)

    def test_default_sync_uses_31_complete_calendar_days_and_today_stays_bounded(self):
        fixed = datetime(2026, 11, 13, 14, 27, 39, tzinfo=timezone.utc)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        manager = self.manager(FakeProvider())
        with patch("features.moboreels_library.orders.datetime", FixedDateTime), patch("features.moboreels_library.orders.time.time", return_value=fixed.timestamp()), patch.object(manager, "_launch"):
            default = manager.start({})
            self.assertEqual((default["date_from"], default["date_to"]), ("2026-10-13", "2026-11-12"))
            self.assertEqual(default["begin_time"], int(datetime(2026, 10, 13, tzinfo=timezone.utc).timestamp()*1000))
            self.assertEqual(default["end_time"], int(datetime(2026, 11, 13, tzinfo=timezone.utc).timestamp()*1000)-1)
            self.assertEqual(default["end_time"]-default["begin_time"], 31*DAY_MS-1)
            self.assertEqual(len([part for part in default["parts"] if part["kind"] == "ad"]),31)
            manager._update(default["run_id"], status="succeeded")
            explicit = manager.start({"date_from":"2026-10-13","date_to":"2026-11-12"})
            self.assertEqual((explicit["begin_time"],explicit["end_time"]),(default["begin_time"],default["end_time"]))
            manager._update(explicit["run_id"], status="succeeded")
            today = manager.start({"date_from":"2026-11-13","date_to":"2026-11-13"})
            self.assertEqual(today["end_time"],int(fixed.timestamp()*1000))
            self.assertEqual(len(today["parts"]),1)

    def test_complete_orders_and_fractional_cent_ad_snapshot(self):
        order = documented_order()
        manager = self.manager(FakeProvider({-1: [order], 0: [order], 1: []}, [documented_ad()]))
        run = self.complete(manager)
        self.assertEqual(run["status"], "succeeded", run)
        self.assertEqual(run["total"], 2)
        self.assertEqual(run["verified"], 2)
        self.assertEqual(run["completed_parts"], 2)
        ad = next(iter(self.store.rows["ad"].values()))
        self.assertEqual(ad["amount_usd"], "1.00125")
        self.assertEqual(self.store.pointers[SOURCE_DAY]["snapshot_id"], run["run_id"])
        self.assertEqual(manager.last_success_at(), run["finished_at"])

    def test_same_order_status_change_updates_one_current_row(self):
        original = documented_order()
        provider = FakeProvider({-1: [original], 0: [original], 1: []})
        manager = self.manager(provider)
        self.assertEqual(self.complete(manager)["status"], "succeeded")
        changed = documented_order(status_text="退款订单")
        provider.orders = {-1: [changed], 0: [], 1: [changed]}
        self.assertEqual(self.complete(manager)["status"], "succeeded")
        self.assertEqual(len(self.store.rows["orders"]), 1)
        self.assertEqual(next(iter(self.store.rows["orders"].values()))["classification"], 1)

    def test_partial_refund_snapshot_conflict_preserves_old_orders_and_refetches_on_resume(self):
        original = documented_order(amount="10.0000")
        provider = FakeProvider({-1: [original], 0: [original], 1: []})
        manager = self.manager(provider)
        self.assertEqual(self.complete(manager)["status"], "succeeded")
        saved, write_count = deepcopy(self.store.rows["orders"]), len(self.store.writes)
        refunded = documented_order(amount="3.0000", status_text="退款订单")
        provider.orders = {-1: [original], 0: [], 1: [refunded]}
        failed = self.complete(manager)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("快照期间", failed["parts"][0]["error"])
        self.assertEqual(self.store.rows["orders"], saved)
        self.assertEqual(len(self.store.writes), write_count)
        directory = manager.snapshots / failed["run_id"]
        self.assertFalse((directory / "orders-complete.json").exists())
        self.assertEqual(len(list(directory.glob("orders-status-*-attempt-0-*.json"))), 3)
        call_count = len(provider.calls)
        provider.orders = {-1: [refunded], 0: [], 1: [refunded]}
        resumed = self.complete(manager)
        self.assertEqual(resumed["run_id"], failed["run_id"])
        self.assertEqual(resumed["status"], "succeeded", resumed)
        calls = provider.calls[call_count:]
        self.assertEqual([body["status"] for path, body in calls], [-1, 0, 1])
        row = next(iter(self.store.rows["orders"].values()))
        self.assertEqual(row["classification"], 1)
        self.assertEqual(row["amount_usd"], "3.0000")
        self.assertEqual(len(list(directory.glob("orders-status-*-attempt-0-*.json"))), 3)
        self.assertEqual(len(list(directory.glob("orders-status-*-attempt-1-*.json"))), 3)

    def test_filtered_order_financial_identity_and_status_drift_never_write(self):
        original = documented_order()
        provider = FakeProvider()
        manager = self.manager(provider)
        changes = ({"amount": "10"}, {"code": "OTHER"}, {"story_id": 43},
                   {"institution_user_id": "different"}, {"real_star_user_id": "other-owner"},
                   {"star_user_id": "other-account"}, {"coo_order_status": 7},
                   {"status_text": "退款订单"}, {"project_type": 1},
                   {"create_time": SOURCE_DAY + " 18:00:00"})
        for status in (0, 1):
            for change in changes:
                with self.subTest(status=status, change=change):
                    provider.orders = {-1: [original], 0: [], 1: []}
                    provider.orders[status] = [dict(original, **change)]
                    run = self.complete(manager)
                    self.assertEqual(run["status"], "failed")
                    self.assertIn("快照期间", run["parts"][0]["error"])
                    self.assertEqual(self.store.rows["orders"], {})
                    self.assertEqual(self.store.writes, [])

    def test_equivalent_amount_format_does_not_reject_consistent_snapshot(self):
        original = documented_order(amount="3.0000")
        manager = self.manager(FakeProvider({-1: [original], 0: [dict(original, amount=3)], 1: []}))
        self.assertEqual(self.complete(manager)["status"], "succeeded")
        self.assertEqual(next(iter(self.store.rows["orders"].values()))["amount_usd"], "3.0000")

    def test_empty_ad_snapshot_switches_pointer_and_preserves_old_version(self):
        provider = FakeProvider(ads=[documented_ad()])
        manager = self.manager(provider)
        first = self.complete(manager)
        provider.ads = []
        second = self.complete(manager)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.store.pointers[SOURCE_DAY]["snapshot_id"], second["run_id"])
        self.assertEqual(self.store.pointers[SOURCE_DAY]["row_count"], 0)
        self.assertEqual(len(self.store.rows["ad"]), 1)
        self.assertEqual(next(iter(self.store.rows["ad"].values()))["snapshot_id"], first["run_id"])

    def test_failed_ad_version_publication_preserves_last_valid_day(self):
        provider = FakeProvider(ads=[documented_ad()])
        manager = self.manager(provider)
        first = self.complete(manager)
        provider.ads = [documented_ad(amount="200")]
        self.store.fail_publish = True
        failed = self.complete(manager)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.store.pointers[SOURCE_DAY]["snapshot_id"], first["run_id"])
        self.assertEqual(len(self.store.rows["ad"]), 2)
        count = len(provider.calls)
        self.store.fail_publish = False
        resumed = self.complete(manager)
        self.assertEqual(resumed["run_id"], failed["run_id"])
        self.assertEqual(resumed["status"], "succeeded")
        self.assertEqual(self.store.pointers[SOURCE_DAY]["snapshot_id"], failed["run_id"])
        self.assertEqual(len(provider.calls), count)

    def test_duplicate_ad_code_prevents_writes_and_day_publication(self):
        row = documented_ad()
        manager = self.manager(FakeProvider(ads=[row, row]))
        run = self.complete(manager)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.store.rows["ad"], {})
        self.assertEqual(self.store.pointers, {})

    def test_incomplete_order_pages_never_write_orders_but_other_part_can_complete(self):
        rows = [documented_order(str(index)) for index in range(101)]
        provider = FakeProvider({-1: rows, 0: rows, 1: []})
        provider.overrides[("/api/story/getorder", -1, 2)] = {"total": 101, "list": []}
        manager = self.manager(provider)
        failed = self.complete(manager)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.store.rows["orders"], {})
        self.assertEqual(self.store.pointers[SOURCE_DAY]["row_count"], 0)
        provider.overrides.clear()
        retried = self.complete(manager)
        self.assertEqual(retried["run_id"], failed["run_id"])
        self.assertEqual(retried["status"], "succeeded", retried)
        self.assertEqual(len(self.store.rows["orders"]), 101)

    def test_duplicate_order_id_blocks_complete_order_snapshot(self):
        row = documented_order()
        manager = self.manager(FakeProvider({-1: [row, row], 0: [row], 1: []}))
        run = self.complete(manager)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.store.rows["orders"], {})

    def test_unknown_write_reconciles_committed_rows_before_any_replay(self):
        row = documented_order()
        self.store.commit_then_fail = True
        manager = self.manager(FakeProvider({-1: [row], 0: [row], 1: []}))
        self.assertEqual(self.complete(manager)["status"], "succeeded")
        self.assertEqual(len(self.store.writes), 1)
        self.assertGreaterEqual(len(self.store.reads), 3)

    def test_partial_failure_resumes_complete_snapshot_without_refetching(self):
        row = documented_order()
        provider = FakeProvider({-1: [row], 0: [row], 1: []})
        self.store.fail_orders = True
        manager = self.manager(provider)
        failed = self.complete(manager)
        count = len(provider.calls)
        self.assertEqual(failed["status"], "failed")
        self.store.fail_orders = False
        succeeded = self.complete(manager)
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(succeeded["run_id"], failed["run_id"])
        self.assertEqual(len(provider.calls), count)
        self.assertEqual(self.store.published, [SOURCE_DAY])

    def test_manual_and_scheduled_starts_share_running_job(self):
        provider = FakeProvider()
        provider.blocked = True
        manager = self.manager(provider)
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                jobs = list(pool.map(lambda index: manager.start(PERIOD, "manual" if index % 2 else "scheduled"), range(6)))
            self.assertEqual(len({job["run_id"] for job in jobs}), 1)
        finally:
            provider.release.set()
        self.assertEqual(manager.wait(jobs[0]["run_id"], 10)["status"], "succeeded")

    def test_restart_recovers_using_durable_complete_snapshots(self):
        manager = self.manager(FakeProvider())
        old = self.complete(manager)
        connection = sqlite3.connect(manager.db_path)
        try:
            old.update(status="running", finished_at=None)
            for part in old["parts"]:
                part["status"] = "running"
            connection.execute("UPDATE report_run SET payload=?", (json.dumps(old),))
            connection.commit()
        finally:
            connection.close()
        provider = FakeProvider()
        recovered = self.manager(provider)
        self.assertEqual(recovered.wait(old["run_id"], 10)["status"], "succeeded")
        self.assertEqual(provider.calls, [])


class ReportStoreTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(Path("unused"), "institution", "unused", "unused", "unused", "unused")

    def test_fixed_writer_table_small_batches_and_decimal_replica_read(self):
        reader = SimpleNamespace(_read=lambda sql: [])
        store = ReportStore(self.settings, reader)
        row = normalize_order(documented_order(), "institution", {"order-1"})
        with patch.object(store.writer, "_write") as write:
            store.write_records("orders", [row], "a" * 32, "2026-11-10T01:00:00Z")
            self.assertTrue(write.call_args.args[0].startswith("INSERT INTO " + ORDER_TABLE))
            with self.assertRaises(ValueError):
                store.write_records("orders", [row] * 21, "a" * 32, "2026-11-10T01:00:00Z")
        with patch.object(reader, "_read", return_value=[]) as read:
            store.read_records("orders", [row["row_key"]])
            self.assertIn("CAST(`amount_usd` AS CHAR)", read.call_args.args[0])
        with self.assertRaises(ValueError):
            store.read_records("other-table", [])

    def test_day_pointer_commit_unknown_is_verified_without_second_write(self):
        reader = SimpleNamespace(_read=lambda sql: [{"snapshot_id": "a" * 32, "row_count": 0, "source_hash": "b" * 64}])
        store = ReportStore(self.settings, reader)
        with patch.object(store.writer, "_write", side_effect=RuntimeError("lost acknowledgement")) as write:
            store.publish_ad_day(SOURCE_DAY, "a" * 32, 0, "b" * 64, "2026-11-10T01:00:00Z")
        self.assertEqual(write.call_count, 1)
        self.assertTrue(write.call_args.args[0].startswith("INSERT INTO " + AD_DAY_TABLE))

    def report_reader(self):
        self.queries = []
        self.mapping = {"id": 123, "star_user_id": "our-actor", "story_id": "42", "koc_code": "25FA", "request_id": "request",
                        "star_name": "Owner", "tenant_key": "tenant", "user_id": "user"}
        base = {"_kind": "order", "date": SOURCE_DAY, "code": "25FA", "story_id": "42", "story_name": "Title",
                "real_star_user_id": "our-actor", "classification_reason": "confirmed", "source_status_text": "原文",
                "coo_order_status_raw": "1", "source_project_type": "2", "undated_count": 0, "count": 1}
        self.source = [dict(base, classification=0, amount_usd="10.0000", absolute_amount_usd="10.0000"),
                       dict(base, classification=1, amount_usd="-3.0000", absolute_amount_usd="3.0000"),
                       dict(base, classification=-1, amount_usd="9.1250", absolute_amount_usd="9.1250", coo_order_status_raw="99"),
                       {"_kind": "ad", "date": SOURCE_DAY, "code": "25FA", "amount_usd": "0.00125", "count": 1},
                       {"_kind": "ad", "date": SOURCE_DAY, "code": "UNMATCHED", "amount_usd": "2.00", "count": 1},
                       {"_kind": "coverage", "ad_data_as_of": SOURCE_DAY, "complete_days": 1}]

        def read(sql):
            self.queries.append(sql)
            if "FROM " + ORDER_TABLE in sql:
                return deepcopy(self.source)
            if "FROM ads_ai.cdreader_koc_promotion" in sql:
                return [deepcopy(self.mapping)]
            return []

        return SimpleNamespace(_read=read)

    def test_report_keeps_units_refunds_unknowns_unattributed_and_totals_separate(self):
        store = ReportStore(self.settings, self.report_reader())
        report = store.query(dict(PERIOD, page_size=1))
        summary = report["summary"]
        self.assertEqual(summary["order_recharge_usd"], "10.0000")
        self.assertEqual(summary["order_refund_usd"], "3.0000")
        self.assertEqual(summary["order_net_usd"], "7.0000")
        self.assertEqual(summary["unclassified_order_usd"], "9.1250")
        self.assertEqual(summary["advertising_revenue_usd"], "2.00125")
        self.assertEqual(summary["unmatched_attribution_usd"], "2.00")
        self.assertEqual(summary["total_revenue_usd"], "9.00125")
        self.assertEqual(len(report["list"]), 1)
        self.assertEqual(report["total"], 2)
        self.assertIsNone(report["list"][0]["upstream_promotion_id"])
        matched = next(row for row in store.query(PERIOD)["list"] if row["koc_code"] == "25FA")
        self.assertEqual(matched["promotion_record_id"], 123)
        self.assertIsNone(matched["upstream_promotion_id"])
        self.assertEqual(report["source_timezone"], "unspecified")
        self.assertIn("s.snapshot_id=a.snapshot_id", self.queries[0])

    def test_report_day_story_filters_and_owner_scope(self):
        store = ReportStore(self.settings, self.report_reader())
        report = store.query(dict(PERIOD, group_by="day", q="42"), star_id="our-actor")
        self.assertEqual(len(report["list"]), 1)
        self.assertEqual(report["list"][0]["date"], SOURCE_DAY)
        self.assertEqual(report["summary"]["advertising_revenue_usd"], "0.00125")
        self.assertIn("real_star_user_id=", self.queries[0])
        by_story = store.query(dict(PERIOD, group_by="story", story_id="42"))
        self.assertEqual(by_story["list"][0]["story_id"], "42")
        self.assertEqual(by_story["list"][0]["koc_code"], "")

    def test_ambiguous_shared_code_ad_income_is_not_multiplied(self):
        reader = self.report_reader()
        original = reader._read

        def ambiguous(sql):
            result = original(sql)
            if "FROM ads_ai.cdreader_koc_promotion" in sql:
                result.append(dict(self.mapping, star_user_id="other-owner", story_id="43", request_id="other"))
            return result

        reader._read = ambiguous
        report = ReportStore(self.settings, reader).query(PERIOD)
        self.assertEqual(report["summary"]["advertising_revenue_usd"], "2.00125")
        self.assertEqual(report["summary"]["unmatched_ad_count"], 2)
        self.assertEqual(report["summary"]["unmatched_attribution_usd"], "2.00125")


if __name__ == "__main__":
    unittest.main()
