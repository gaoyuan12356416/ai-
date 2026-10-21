"""Offline clock/SQLite/Graph contract tests; no network or platform writes."""
import copy
import json
import sqlite3
import sys
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest import main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_fb_video_publish_core import Fixture
from features.fb_video_publish.core import DomainError, Store, normalize_batch, stamp
from features.fb_video_publish.pool_scheduler import PoolScheduler
from features.fb_video_publish.service import PREFIX, Service
from features.fb_auto_posts.publisher import GraphResult


class V2Tests(Fixture):
    def v2(self, pairs=None, schedules=None):
        r = self.req(pairs)
        for k in ("schedule_mode", "scheduled_at_utc", "interval_minutes"):
            del r[k]
        return {**r, "schema_version": 2, "material_schedules": schedules or {mid: None for mid in r["material_ids"]}}

    def test_v2_mixed_same_page_sorts_before_sequence(self):
        r = self.v2([("21", "11"), ("22", "11")], {"21": stamp(self.now + timedelta(hours=2)), "22": None})
        preview = self.preflight(r)
        self.assertEqual(["22", "21"], [i["material_id"] for i in preview["items"]])
        self.assertEqual(["immediate", "scheduled"], [i["schedule_kind"] for i in preview["items"]])
        self.submit(r); self.ready()
        task = self.store.claim("execute", "test")
        self.assertEqual("22", task["material_id"])
        self.assertEqual(1, task["page_sequence"])

    def test_v2_same_material_multiple_pages_exact_same_time(self):
        when = stamp(self.now + timedelta(hours=1))
        r = self.v2([("21", "11"), ("21", "12")], {"21": when})
        self.submit(r)
        self.assertEqual({when}, {r["planned_at_utc"] for r in self.tasks()})

    def test_v2_cross_bj_midnight_quota_and_preview(self):
        self.now = self.now.replace(hour=15, minute=59)
        r = self.v2([("21", "11"), ("22", "12")], {"21": None, "22": stamp(self.now + timedelta(minutes=2))})
        before = self.preflight(r)
        self.submit(r)
        rows = self.tasks()
        self.assertEqual(["2026-10-20", "2026-10-21"], [r["planned_bj_date"] for r in rows])
        self.assertEqual([r["planned_at_utc"] for r in rows], [r["planned_at_utc"] for r in before["items"]])
        self.store.daily_limit = 1
        with self.assertRaises(DomainError):
            self.submit(self.v2([("22", "11")]))

    def test_v2_immediate_is_submission_time(self):
        r = self.v2()
        pre = self.preflight(r)
        self.now += timedelta(seconds=45)
        self.service.handle("POST", PREFIX + "/batches", self.actor, {**r, "preview_token": pre["preview_token"], "allow_repeat": False})
        self.assertEqual(stamp(self.now), self.tasks()[0]["planned_at_utc"])

    def test_preflight_capacity_and_midnight_submit_recheck(self):
        self.now = self.now.replace(hour=15, minute=59)
        self.store.daily_limit = 1
        tomorrow = stamp(self.now + timedelta(minutes=10))
        self.submit(self.v2([("22", "12")], {"22": tomorrow}))
        r = self.v2()
        pre = self.preflight(r)
        self.now += timedelta(minutes=2)
        with self.assertRaises(DomainError) as raised:
            self.service.handle("POST", PREFIX + "/batches", self.actor,
                                {**r, "preview_token": pre["preview_token"], "allow_repeat": False})
        self.assertEqual("daily_capacity_exceeded", raised.exception.code)
        with self.assertRaises(DomainError) as raised:
            self.preflight(r)
        self.assertEqual("daily_capacity_exceeded", raised.exception.code)
        self.assertEqual(1, len(self.tasks()))

    def test_v2_complete_map_timezone_and_expiry(self):
        r = self.v2()
        for schedules in ({}, {"21": ""}, {"21": "2026-10-21T18:00:00"}, {"21": None, "22": None}, {"21": 123}):
            with self.assertRaises(DomainError):
                self.preflight({**r, "material_schedules": schedules})
        with self.assertRaises(DomainError):
            self.preflight({**r, "material_schedules": {"21": stamp(self.now)}})

    def test_cross_batch_future_does_not_block_immediate(self):
        self.submit(self.v2(schedules={"21": stamp(self.now + timedelta(days=1))}))
        self.submit(self.v2([("22", "11")]))
        self.ready()
        self.assertEqual("22", self.store.claim("execute", "test")["material_id"])

    def test_disable_v2_retains_read_replay_and_old_protocol(self):
        r = self.v2(); result = self.submit(r)
        self.service.v2_enabled = False
        with self.assertRaises(DomainError):
            self.preflight(self.v2([("22", "12")]))
        replay = self.service.handle("POST", PREFIX + "/batches", self.actor,
                                    {**r, "preview_token": "expired", "allow_repeat": False})
        self.assertEqual(result["batch_id"], replay["batch_id"])
        self.submit(self.req([("22", "12")]))

    def test_creator_name_immutable_on_edit_and_new_on_recreate(self):
        c = self.configs[0]
        other = {**self.actor, "user_id": "u2", "user_name": "Other"}
        edited = self.store.save_configs(other, ["11"], "zh", "{name}", expected=(c["config_instance_id"], c["version"]))[0]
        self.assertEqual(("u1", "Tester"), (edited["created_by"], edited["created_by_name"]))
        self.store.delete_config(other, "11", c["config_instance_id"], edited["version"])
        new = self.store.save_configs(other, ["11"], "zh", "{name}")[0]
        self.assertEqual(("u2", "Other"), (new["created_by"], new["created_by_name"]))

    def test_repeat_store_initialization_preserves_all_existing_facts(self):
        self.submit()
        with self.store.connect() as c:
            before = {table: [tuple(r) for r in c.execute("SELECT * FROM " + table)]
                      for table in ("fb_video_page", "fb_video_page_version", "fb_video_task", "fb_video_link", "fb_video_batch")}
        Store(self.store.path)
        with self.store.connect() as c:
            after = {table: [tuple(r) for r in c.execute("SELECT * FROM " + table)] for table in before}
        self.assertEqual(before, after)

    def test_true_pre_pool_schema_additive_migration_preserves_old_columns(self):
        self.submit()
        with self.store.connect() as c:
            c.execute("DROP INDEX idx_fb_video_pool_task_sequence")
            c.execute("DROP INDEX idx_fb_video_pool_page_sequence")
            for name in ("pool_operation", "pool_slot", "pool_item", "pool"):
                c.execute("DROP TABLE fb_video_" + name)
            c.execute("ALTER TABLE fb_video_page DROP COLUMN created_by_name")
            for column in ("source_kind", "pool_instance_id", "pool_sequence"):
                c.execute("ALTER TABLE fb_video_task DROP COLUMN " + column)
            tables = ("fb_video_page", "fb_video_page_version", "fb_video_task", "fb_video_link", "fb_video_batch")
            columns = {table: [r[1] for r in c.execute("PRAGMA table_info(" + table + ")")] for table in tables}
            before = {table: [tuple(r) for r in c.execute("SELECT * FROM " + table)] for table in tables}
        Store(self.store.path)
        with self.store.connect() as c:
            after = {table: [tuple(r) for r in c.execute("SELECT " + ",".join(columns[table]) + " FROM " + table)] for table in tables}
            self.assertEqual("", c.execute("SELECT created_by_name FROM fb_video_page LIMIT 1").fetchone()[0])
        self.assertEqual(before, after)


class PoolTests(Fixture):
    def setUp(self):
        super().setUp()
        self.identities = []
        def identity(tenant, user):
            self.identities.append((tenant, user))
            return {**self.actor, "tenant_key": tenant, "user_id": user}
        self.scheduler = PoolScheduler(self.service, identity)
        self.scheduler.start("scheduler-a")

    def call(self, suffix="", payload=None, method="POST", actor=None, query=None, pid="11"):
        return self.service.handle(method, PREFIX + "/pages/" + pid + "/pool" + suffix,
                                   actor or self.actor, payload, query)

    def listing(self, state="pending", **query):
        return self.call(method="GET", query={"state": state, **query})

    def base(self):
        pool = self.listing()["pool"]
        return {"config_instance_id": pool["config_instance_id"], "expected_version": pool["version"]}

    def settings(self, times=None, mode="original"):
        return self.call("/settings", {**self.base(), "times": times or ["18:01", "18:02", "18:03"], "processing_mode": mode})

    def add(self, ids=None, repeat=False):
        raw = {**self.base(), "material_ids": ids or ["21"]}
        preview = self.call("/items/preview", raw)
        payload = {**raw, "operation_id": uuid.uuid4().hex, "preview_token": preview["preview_token"], "allow_repeat": repeat}
        return self.call("/items", payload), payload

    def enabled(self, ids=None, mode="original"):
        self.settings(mode=mode)
        self.add(ids)
        self.call("/enable", self.base())

    def due(self, minutes=1):
        self.now += timedelta(minutes=minutes)
        return self.scheduler.tick("scheduler-a")

    def counts(self):
        with self.store.connect() as c:
            return {name: c.execute("SELECT COUNT(*) FROM fb_video_" + name).fetchone()[0]
                    for name in ("pool_slot", "task", "link", "batch")}

    def test_empty_get_is_readonly_and_first_add_cas_zero(self):
        self.assertEqual(0, self.listing()["pool"]["version"])
        with self.store.connect() as c:
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM fb_video_pool").fetchone()[0])
        result, _ = self.add()
        self.assertEqual(1, result["pool"]["version"])
        self.assertEqual("paused", result["pool"]["status"])

    def test_admin_pool_read_without_mapping_keeps_live_scope_and_tenant_checks(self):
        admin = {**self.actor, "is_admin": True, "business_owner_id": "", "sub_user_id": ""}
        with patch.object(self.sources, "list_pages", wraps=self.sources.list_pages) as pages, \
             patch.object(self.sources, "get_materials", side_effect=AssertionError("not needed for pool read")):
            result = self.call(method="GET", actor=admin)
            self.assertEqual(0, result["pool"]["version"])
            pages.assert_called_once_with(admin, page_ids=["11"])
        with self.assertRaises(DomainError) as other:
            self.call(method="GET", actor={**admin, "tenant_key": "another-tenant"})
        self.assertEqual("page_not_found", other.exception.code)
        self.sources.pages.pop("11")
        with self.assertRaises(DomainError) as revoked:
            self.call(method="GET", actor=admin)
        self.assertEqual("page_not_found", revoked.exception.code)
        with self.store.connect() as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM fb_video_pool").fetchone()[0])

    def test_settings_times_strict_and_enable_requires_times(self):
        self.add()
        with self.assertRaises(DomainError): self.call("/enable", self.base())
        for times in ([], ["18:00", "18:00"], ["24:00"], ["8:00"], "18:00"):
            with self.assertRaises(DomainError): self.call("/settings", {**self.base(), "times": times, "processing_mode": "original"})

    def test_pool_cas_and_admission_replay_after_version_changes(self):
        initial = self.base()
        result, payload = self.add()
        self.settings()
        replay = self.call("/items", payload)
        self.assertEqual(result["items"], replay["items"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(result["items"], self.call("/operations/" + payload["operation_id"], method="GET")["items"])
        with self.assertRaises(DomainError):
            self.call("/settings", {**initial, "times": ["18:00"], "processing_mode": "original"})
        with self.assertRaises(DomainError):
            self.call("/items", {**payload, "material_ids": ["22"]})
        with self.assertRaises(DomainError):
            self.call("/operations/" + payload["operation_id"], method="GET", actor={**self.actor, "user_id": "u2"})

    def test_move_remove_and_cursor_version(self):
        self.add(["21", "22"])
        first = self.listing(limit=1)
        rows = self.listing()["items"]
        self.call("/move", {**self.base(), "item_id": rows[1]["id"], "direction": "top"})
        self.assertEqual("22", self.listing()["items"][0]["material_id"])
        with self.assertRaises(DomainError):
            self.listing(limit=1, cursor=first["next_cursor"])
        self.call("/move", {**self.base(), "item_id": rows[1]["id"], "direction": "down"})
        self.assertEqual("21", self.listing()["items"][0]["material_id"])
        self.call("/remove", {**self.base(), "item_ids": [rows[0]["id"]]})
        self.assertEqual(1, self.listing()["counts"]["removed"])

    def test_slot_once_atomic_latest_caption_and_identity_attribution(self):
        self.enabled()
        cfg = self.store.configs(self.actor)["11"]
        self.store.save_configs(self.actor, ["11"], "zh", "NEW {name} {url}", expected=(cfg["config_instance_id"], cfg["version"]))
        self.scheduler.identity_resolver = lambda t, u: {**self.actor, "sub_user_id": "222", "user_name": "Renamed"}
        self.assertEqual(1, self.due()["slots_created"])
        self.assertEqual(0, self.scheduler.tick("scheduler-a")["slots_created"])
        task = self.tasks()[0]
        self.assertTrue(task["caption"].startswith("NEW"))
        self.assertIn("af_channel=222", task["long_url"])
        self.assertEqual("Renamed", json.loads(task["snapshot_json"])["actor"]["user_name"])
        self.assertEqual({"pool_slot": 1, "task": 1, "link": 1, "batch": 1}, self.counts())

    def test_empty_slot_never_backfills_after_adding_material(self):
        self.settings(); self.call("/enable", self.base())
        self.assertEqual("empty", self.due()["results"][0]["status"])
        self.add()
        self.assertEqual(0, self.scheduler.tick("scheduler-a")["slots_created"])
        self.assertEqual(1, self.due()["slots_created"])

    def test_start_cutoff_restart_and_sixty_second_boundary(self):
        self.enabled(["21", "22"])
        self.now += timedelta(seconds=70)
        restarted = PoolScheduler(self.service, self.scheduler.identity_resolver)
        restarted.start("new")
        self.assertEqual(0, restarted.tick("new")["slots_created"])
        self.now += timedelta(seconds=50)
        self.assertEqual(1, restarted.tick("new")["slots_created"])
        self.now += timedelta(seconds=120)
        self.assertEqual(0, restarted.tick("new")["slots_created"])
        self.assertEqual(1, self.counts()["task"])

    def test_stopped_reenabled_and_recreated_page_same_slot_never_duplicate(self):
        self.enabled()
        self.due()
        self.call("/pause", self.base()); self.call("/enable", self.base())
        self.assertEqual(0, self.scheduler.tick("scheduler-a")["slots_created"])
        cfg = self.store.configs(self.actor)["11"]
        self.store.delete_config(self.actor, "11", cfg["config_instance_id"], cfg["version"])
        self.store.save_configs(self.actor, ["11"], "zh", "{name}")
        self.settings(); self.add(["22"]); self.call("/enable", self.base())
        self.assertEqual(0, self.scheduler.tick("scheduler-a")["slots_created"])
        self.assertEqual(1, self.counts()["task"])

    def test_explicit_failure_does_not_retry_next_slot_takes_next(self):
        self.enabled(["21", "22"]); self.due(); self.ready()
        worker = self.worker(outcomes=[GraphResult("definite_failure", error_code="fb_graph_100"),
                                       GraphResult("definite_failure", error_code="fb_graph_368")])
        worker.execute_next()
        self.assertEqual(2, len(worker.calls))
        self.assertEqual(1, self.listing("failed")["counts"]["failed"])
        self.assertEqual(1, self.due()["slots_created"])
        self.assertEqual(["21", "22"], [t["material_id"] for t in self.tasks()])

    def test_success_consumes_once_and_shared_status_privacy(self):
        self.enabled(); self.due(); self.ready()
        worker = self.worker(); worker.execute_next()
        self.now += timedelta(seconds=61); worker.reconcile_next()
        self.assertEqual(1, self.listing("published")["counts"]["published"])
        other = {**self.actor, "user_id": "u2"}
        result = self.call(method="GET", query={"state": "published"}, actor=other)
        self.assertFalse(result["items"][0]["can_view_task"])
        self.assertEqual("", result["items"][0]["task_id"])
        self.assertNotIn("caption", result["items"][0])
        self.assertEqual(0, self.scheduler.tick("scheduler-a")["slots_created"])
        with self.assertRaises(DomainError): self.add()
        result, _ = self.add(repeat=True)
        self.assertEqual("pending", result["items"][0]["status"])

    def test_unknown_pauses_plan_and_does_not_consume_next(self):
        self.enabled(["21", "22"]); self.due(); self.ready()
        self.worker(outcomes=[GraphResult("unknown", error_code="timeout")]).execute_next()
        self.assertEqual(0, self.due()["slots_created"])
        listing = self.listing()
        self.assertEqual("paused", listing["pool"]["status"])
        self.assertEqual("22", listing["items"][0]["material_id"])
        with self.assertRaises(DomainError): self.call("/enable", self.base())

    def test_identity_revoked_pauses_but_transient_only_skips_slot(self):
        self.enabled()
        def revoked(*args): raise DomainError("identity_permission_denied", "revoked", 403)
        self.scheduler.identity_resolver = revoked
        self.due()
        self.assertEqual("paused", self.listing()["pool"]["status"])
        self.call("/enable", self.base())
        def transient(*args): raise DomainError("identity_unavailable", "temporary", 503)
        self.scheduler.identity_resolver = transient
        self.due()
        self.assertEqual("enabled", self.listing()["pool"]["status"])
        self.assertEqual(1, self.listing()["counts"]["pending"])
        self.assertEqual(0, self.counts()["task"])

    def test_permanent_material_and_macro_failures_consume_only_one(self):
        self.enabled(["21", "22"])
        self.sources.materials["21"]["drama_description"] = ""
        self.due()
        self.assertEqual(1, self.listing()["counts"]["failed"])
        self.assertEqual("22", self.listing()["items"][0]["material_id"])
        self.assertEqual(0, self.counts()["task"])
        self.assertEqual(1, self.due()["slots_created"])

    def test_daily_quota_skips_without_consumption(self):
        self.store.daily_limit = 1
        self.submit(self.req([("22", "12")]))
        self.enabled(); self.due()
        self.assertEqual(1, self.listing()["counts"]["pending"])
        self.assertEqual("daily_capacity_exceeded", self.listing("slots")["items"][0]["reason"])
        self.assertEqual(1, self.counts()["task"])

    def test_delete_archives_pool_keeps_frozen_task_language_change_pauses(self):
        self.enabled(["21", "22"]); self.due()
        frozen = copy.deepcopy(self.tasks()[0])
        cfg = self.store.configs(self.actor)["11"]
        changed = self.store.save_configs(self.actor, ["11"], "en", "{name}", expected=(cfg["config_instance_id"], cfg["version"]))[0]
        self.assertEqual("paused", self.listing()["pool"]["status"])
        self.assertTrue(self.listing()["items"][0]["language_mismatch"])
        with self.assertRaises(DomainError): self.call("/enable", self.base())
        self.store.delete_config(self.actor, "11", changed["config_instance_id"], changed["version"])
        self.assertEqual(frozen, self.tasks()[0])
        with self.store.connect() as c:
            self.assertEqual("archived", c.execute("SELECT status FROM fb_video_pool").fetchone()[0])

    def test_queue_bound_mutations_rejected_cancel_uses_task_owner(self):
        self.enabled(); before = self.listing()["items"][0]; self.due()
        with self.assertRaises(DomainError):
            self.call("/remove", {**self.base(), "item_ids": [before["id"]]})
        tid = self.tasks()[0]["task_id"]
        with self.assertRaises(DomainError): self.store.cancel({**self.actor, "user_id": "u2"}, tid)
        self.store.cancel(self.actor, tid)
        self.assertEqual(1, self.listing("failed")["counts"]["cancelled"])

    def test_cross_slot_processing_order_blocks_faster_later_task(self):
        self.enabled(["21", "22"], mode="random_overlay"); self.due(); self.due()
        rows = sorted(self.tasks(), key=lambda r: r["pool_sequence"])
        with self.store.connect() as c:
            c.execute("UPDATE fb_video_task SET link_state='ready'")
            c.execute("UPDATE fb_video_task SET prepare_state='ready' WHERE task_id=?", (rows[1]["task_id"],))
        self.assertIsNone(self.store.claim("execute", "test"))
        with self.store.connect() as c:
            c.execute("UPDATE fb_video_task SET status='failed' WHERE task_id=?", (rows[0]["task_id"],))
        self.assertEqual(rows[1]["task_id"], self.store.claim("execute", "test")["task_id"])

    def test_pool_sequence_survives_deleted_recreated_config_instance(self):
        self.enabled(mode="random_overlay"); self.due()
        old = self.tasks()[0]
        config = self.store.configs(self.actor)["11"]
        self.store.delete_config(self.actor, "11", config["config_instance_id"], config["version"])
        self.store.save_configs(self.actor, ["11"], "zh", "{name}")
        self.enabled(["22"], mode="random_overlay"); self.due()
        rows = sorted(self.tasks(), key=lambda r: r["pool_sequence"])
        self.assertNotEqual(rows[0]["pool_instance_id"], rows[1]["pool_instance_id"])
        with self.store.connect() as c:
            c.execute("UPDATE fb_video_task SET link_state='ready'")
            c.execute("UPDATE fb_video_task SET prepare_state='ready' WHERE task_id=?", (rows[1]["task_id"],))
        self.assertIsNone(self.store.claim("execute", "test"))
        with self.store.connect() as c:
            c.execute("UPDATE fb_video_task SET status='failed' WHERE task_id=?", (old["task_id"],))
        self.assertEqual("22", self.store.claim("execute", "test")["material_id"])

    def test_repeated_history_changes_after_admission_fail_closed(self):
        self.enabled()
        result = self.submit()
        with self.store.connect() as c:
            c.execute("UPDATE fb_video_task SET status='published',graph_post_id='receipt'")
        self.due()
        self.assertEqual(1, self.listing()["counts"]["failed"])
        self.assertEqual(1, self.counts()["task"])
        self.assertEqual("pool_repeat_changed", self.listing("slots")["items"][0]["reason"])

    def test_atomic_slot_freeze_failure_leaves_no_orphan_batch_or_link(self):
        self.enabled()
        original = self.store.freeze
        def faulty(*args, **kwargs):
            original(*args, **kwargs)
            raise DomainError("attribution_invalid", "invalid", 409)
        with patch.object(self.store, "freeze", side_effect=faulty):
            self.due()
        self.assertEqual({"pool_slot": 1, "task": 0, "link": 0, "batch": 0}, self.counts())
        self.assertEqual(1, self.listing()["counts"]["failed"])

    def test_concurrent_candidate_same_slot_and_cas_drift(self):
        self.enabled()
        self.now += timedelta(minutes=1)
        candidate = self.scheduler._candidates(self.now)[0]
        actor, resolved = self.scheduler._resolve(candidate)
        self.assertEqual("created", self.scheduler._finish(candidate, actor, resolved, None)["status"])
        self.assertEqual("already_recorded", self.scheduler._finish(candidate, actor, resolved, None)["status"])
        self.assertEqual(1, self.counts()["task"])

    def test_two_scheduler_connections_create_exactly_one_slot_task(self):
        self.enabled()
        self.now += timedelta(minutes=1)
        candidate = self.scheduler._candidates(self.now)[0]
        actor, resolved = self.scheduler._resolve(candidate)
        barrier = threading.Barrier(2)
        def finish():
            barrier.wait(timeout=5)
            return self.scheduler._finish(candidate, actor, resolved, None)["status"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [f.result() for f in (executor.submit(finish), executor.submit(finish))]
        self.assertEqual(["already_recorded", "created"], sorted(results))
        self.assertEqual({"pool_slot": 1, "task": 1, "link": 1, "batch": 1}, self.counts())

    def test_concurrent_same_admission_operation_has_one_item(self):
        raw = {**self.base(), "material_ids": ["21"]}
        preview = self.call("/items/preview", raw)
        payload = {**raw, "operation_id": uuid.uuid4().hex, "preview_token": preview["preview_token"], "allow_repeat": False}
        original = self.sources.get_materials
        barrier = threading.Barrier(2)
        def materials(*args, **kwargs):
            barrier.wait(timeout=5)
            return original(*args, **kwargs)
        with patch.object(self.sources, "get_materials", side_effect=materials), ThreadPoolExecutor(max_workers=2) as executor:
            results = [f.result() for f in (executor.submit(self.call, "/items", payload), executor.submit(self.call, "/items", payload))]
        self.assertEqual([False, True], sorted(r["idempotent"] for r in results))
        self.assertEqual(1, self.listing()["counts"]["pending"])

    def test_unexpected_freeze_failure_rolls_back_entire_slot_and_is_recoverable(self):
        self.enabled()
        original = self.store.freeze
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated process interruption before commit")
        with patch.object(self.store, "freeze", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.due()
        self.assertEqual({"pool_slot": 0, "task": 0, "link": 0, "batch": 0}, self.counts())
        self.assertEqual(1, self.scheduler.tick("scheduler-a")["slots_created"])

    def test_source_lookup_outside_sqlite_write_transaction(self):
        self.enabled()
        original = self.sources.get_materials
        def checked(*args, **kwargs):
            with self.store.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                c.rollback()
            return original(*args, **kwargs)
        with patch.object(self.sources, "get_materials", side_effect=checked):
            self.assertEqual(1, self.due()["slots_created"])

    def test_enable_after_exact_slot_does_not_round_back(self):
        self.settings(); self.add()
        self.now += timedelta(minutes=1, microseconds=500000)
        self.call("/enable", self.base())
        self.assertEqual(0, self.scheduler.tick("scheduler-a")["slots_created"])

    def test_long_source_lookup_skips_late_slot_without_binding(self):
        self.enabled()
        original = self.sources.get_materials
        def delayed(*args, **kwargs):
            self.now += timedelta(seconds=61)
            return original(*args, **kwargs)
        with patch.object(self.sources, "get_materials", side_effect=delayed):
            self.assertEqual(0, self.due()["slots_created"])
        self.assertEqual(1, self.listing()["counts"]["pending"])
        self.assertEqual("slot_expired", self.listing("slots")["items"][0]["reason"])

    def test_latest_template_actual_overflow_consumes_failed_head(self):
        self.enabled(["21", "22"])
        self.sources.materials["21"]["drama_name"] = "A" * 200
        cfg = self.store.configs(self.actor)["11"]
        self.store.save_configs(self.actor, ["11"], "zh", "B" * 4900 + "{name}",
                                expected=(cfg["config_instance_id"], cfg["version"]))
        self.due()
        self.assertEqual("text_too_long", self.listing("slots")["items"][0]["reason"])
        self.assertEqual(1, self.listing()["counts"]["failed"])
        self.assertEqual(1, self.due()["slots_created"])

    def test_enabled_plan_edit_preserves_publisher_explicit_enable_changes_it(self):
        self.enabled()
        other = {**self.actor, "user_id": "u2", "user_name": "Other"}
        self.call("/settings", {**self.base(), "times": ["18:01"], "processing_mode": "original"}, actor=other)
        self.assertEqual("u1", self.listing()["pool"]["publisher_user_id"])
        self.call("/enable", self.base(), actor=other)
        self.assertEqual("u2", self.listing()["pool"]["publisher_user_id"])
        self.due()
        self.assertEqual("u2", self.tasks()[0]["owner_user_id"])

    def test_config_edit_during_source_lookup_consumes_slot_without_task(self):
        self.enabled()
        original = self.sources.get_materials
        def changed(*args, **kwargs):
            cfg = self.store.configs(self.actor)["11"]
            self.store.save_configs(self.actor, ["11"], "zh", "changed", expected=(cfg["config_instance_id"], cfg["version"]))
            return original(*args, **kwargs)
        with patch.object(self.sources, "get_materials", side_effect=changed):
            self.due()
        self.assertEqual(0, self.counts()["task"])
        self.assertEqual("configuration_changed", self.listing("slots")["items"][0]["reason"])
        self.assertEqual(1, self.listing()["counts"]["pending"])

    def test_disabled_pool_still_reads_replays_and_executes_frozen_tasks(self):
        self.settings(); _, payload = self.add(); self.call("/enable", self.base()); self.due()
        self.service.pool_enabled = False
        self.assertEqual(0, self.due()["slots_created"])
        self.assertEqual(1, self.listing("processing")["counts"]["processing"])
        self.assertTrue(self.call("/items", payload)["idempotent"])
        with self.assertRaises(DomainError): self.call("/settings", {**self.base(), "times": ["19:00"], "processing_mode": "original"})
        self.ready(); self.assertEqual("submitted", self.worker().execute_next()["status"])

    def test_untrusted_task_error_is_not_disclosed_to_shared_viewer(self):
        self.enabled(); self.due()
        with self.store.connect() as c:
            c.execute("UPDATE fb_video_task SET status='failed',error_message='private caption https://secret.example token'")
        data = self.call(method="GET", actor={**self.actor, "user_id": "u2"}, query={"state": "failed"})
        self.assertNotIn("secret.example", json.dumps(data))
        self.assertEqual("", data["items"][0]["task_id"])


if __name__ == "__main__":
    main()
