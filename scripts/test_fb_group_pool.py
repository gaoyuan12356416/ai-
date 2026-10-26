"""Offline group pool API, scheduler and publishing safety regression tests."""
import copy
import json
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest import main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_fb_video_publish_core import Fixture
from features.fb_video_publish.core import DomainError, Store, stamp
from features.fb_video_publish.group_pool import GroupPoolService, random_times
from features.fb_video_publish.group_scheduler import GroupScheduler
from features.fb_video_publish.group_language import LEGACY_POLICY, POLICY_VERSION, default_selection, normalize_selection
from features.fb_auto_posts.publisher import GraphResult


class GroupSources:
    def __init__(self, existing):
        self.existing = existing
        self.denied = False
        self.excluded = []

    def resolve_scope(self, actor, groups, *, page_language_selection=None, policy_version=LEGACY_POLICY, page_ids=None):
        if self.denied:
            raise DomainError("group_access_denied", "分组权限失效", 403)
        selection = normalize_selection(page_language_selection or default_selection())
        pages, excluded = {}, list(self.excluded)
        summary = {}
        for pid, original in self.existing.pages.items():
            kind = "specific" if original["language"] else "unrestricted"
            summary[(original["language"], kind)] = summary.get((original["language"], kind), 0) + 1
            if page_ids is not None and pid not in page_ids:
                continue
            page = {**original, "group_ids": list(groups), "language_kind": kind}
            if policy_version == LEGACY_POLICY and not original["language"]:
                excluded.append({**page, "code": "page_language_missing", "message": "缺少语言"})
            elif (policy_version == POLICY_VERSION and
                  ((kind == "unrestricted" and not selection["include_blank"])
                   or (kind == "specific" and selection["mode"] == "selected" and page["language"] not in selection["codes"]))):
                excluded.append({**page, "code": "page_language_not_selected", "message": "未选择"})
            elif page["blocked_reason"]:
                excluded.append({**page, "code": "page_unavailable", "message": page["blocked_reason"]})
            else:
                pages[pid] = page
        return {"groups": [{"id": g, "name": "Group " + g} for g in groups], "pages": copy.deepcopy(pages), "excluded": excluded,
                "language_summary": {"items": [{"language": language, "language_kind": kind, "page_count": count}
                                               for (language, kind), count in summary.items()],
                                     "total_page_count": len(self.existing.pages), "conflict_page_count": 0,
                                     "invalid_page_count": 0, "missing_page_count": 0}}

    def resolve_materials(self, mids, need_desc=True):
        return {"items": [copy.deepcopy(self.existing.materials[mid]) for mid in mids if mid in self.existing.materials],
                "errors": [{"material_id": mid, "code": "material_unavailable", "message": "不存在"} for mid in mids if mid not in self.existing.materials]}

    def get_materials(self, mids, need_desc=False):
        return {m["material_id"]: m for m in self.resolve_materials(mids, need_desc)["items"]}


class GroupPoolTests(Fixture):
    def setUp(self):
        super().setUp()
        self.service.group_pool_enabled = True
        self.gs = GroupSources(self.sources)
        self.pool = GroupPoolService(self.service, sources=self.gs)
        self.pool.identity_resolver = lambda tenant, user: {**self.actor, "tenant_key": tenant, "user_id": user}
        self.service.group_pool = self.pool
        self.scheduler = GroupScheduler(self.pool, lambda tenant, user: {**self.actor, "tenant_key": tenant, "user_id": user})
        self.scheduler.start("test")

    def version(self):
        return self.pool.handle("GET", "/schedule", self.actor)["pool"]["version"]

    def mutate(self, suffix, **payload):
        return self.pool.handle("POST", suffix, self.actor, {"operation_id": uuid.uuid4().hex, "expected_version": self.version(), **payload})

    def configure(self, **changes):
        return self.mutate("/schedule", **{"group_ids": ["1", "2"], "schedule_mode": "fixed", "times": ["18:01", "18:02"],
            "daily_batches": 2, "caption_template": "{name}\n{desc}\n{url}", "processing_mode": "original", **changes})

    def due(self, mids=("21", "22")):
        self.mutate("/items", material_ids=list(mids))
        self.configure()
        self.mutate("/enable")
        self.now += timedelta(minutes=1)
        return self.scheduler.tick("test")

    def count(self, table):
        with self.store.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_default_paused_reads_and_preview_do_not_write(self):
        settings = self.pool.handle("GET", "/schedule", self.actor)
        self.assertEqual("paused", settings["pool"]["status"])
        self.assertEqual(0, self.count("fb_group_pool"))
        self.pool.handle("POST", "/preview", self.actor, {"group_ids": ["1"]})
        self.assertEqual(0, self.count("fb_video_link"))
        self.assertEqual(0, self.count("fb_group_pool"))

    def test_import_duplicate_partial_errors_and_exact_replay(self):
        body = {"operation_id": uuid.uuid4().hex, "expected_version": 0, "material_ids": ["21", "21", "999"]}
        result = self.pool.handle("POST", "/items", self.actor, body)
        self.assertEqual(["21"], [r["material_id"] for r in result["added"]])
        self.assertEqual("999", result["errors"][0]["material_id"])
        self.assertTrue(self.pool.handle("POST", "/items", self.actor, body)["idempotent"])
        self.assertEqual(1, self.version())
        self.assertEqual(result["added"], self.pool.handle("GET", "/operations/" + body["operation_id"], self.actor)["added"])
        with self.assertRaises(DomainError):
            self.pool.handle("POST", "/items", self.actor, {**body, "material_ids": ["22"]})
        self.assertEqual(["21"], self.mutate("/items", material_ids=["21"])["duplicates"])

    def test_version_conflict_and_cross_tenant_operation_isolation(self):
        self.configure()
        with self.assertRaises(DomainError):
            self.pool.handle("POST", "/pause", self.actor, {"operation_id": uuid.uuid4().hex, "expected_version": 0})
        other = {**self.actor, "tenant_key": "t2"}
        self.assertEqual(0, self.pool.handle("GET", "/schedule", other)["pool"]["version"])

    def test_language_newest_routing_and_no_saved_configs_needed(self):
        self.sources.materials["22"]["language"] = "en"
        self.sources.materials["23"] = {**self.sources.materials["21"], "material_id": "23"}
        self.sources.pages["12"]["language"] = "en"
        with self.store.connect() as conn:
            conn.execute("DELETE FROM fb_video_page")
        result = self.due(("21", "22", "23"))
        self.assertEqual(1, result["runs_created"])
        tasks = self.tasks()
        self.assertEqual({("23", "11"), ("22", "12")}, {(r["material_id"], r["page_id"]) for r in tasks})
        self.assertEqual({"group_pool"}, {r["source_kind"] for r in tasks})
        self.assertEqual({0}, {r["pool_sequence"] for r in tasks})
        self.assertEqual(2, len({r["short_url"] for r in tasks}))
        self.assertEqual(2, self.count("fb_group_pool_reservation"))
        self.assertEqual(1, len(self.pool.handle("GET", "", self.actor)["items"]))

    def test_unmatched_material_remains_pending(self):
        self.sources.materials["22"]["language"] = "es"
        self.due()
        self.assertEqual(["21"], [r["material_id"] for r in self.tasks()])
        pending = self.pool.handle("GET", "", self.actor)["items"]
        self.assertEqual(["22"], [r["material_id"] for r in pending])

    def test_quota_consumes_available_subset(self):
        self.store.daily_limit = 1
        self.due()
        self.assertEqual(1, len(self.tasks()))
        run = self.pool.handle("GET", "/runs", self.actor)["items"][0]
        self.assertIn("daily_capacity_exceeded", {r["code"] for r in run["excluded"]})

    def test_no_replay_after_same_slot_or_restart_and_old_times(self):
        self.due()
        self.assertEqual(0, self.scheduler.tick("test")["runs_created"])
        scheduler = GroupScheduler(self.pool, lambda t, u: self.actor)
        self.now += timedelta(seconds=5)
        scheduler.start("restart")
        self.assertEqual(0, scheduler.tick("restart")["runs_created"])
        self.assertEqual(2, len(self.tasks()))

    def test_random_plan_persistent_next_day_and_gap_for_all_counts(self):
        for count in range(1, 25):
            times = random_times("t1", "2026-10-21", 1, count)
            minutes = [int(t[:2]) * 60 + int(t[3:]) for t in times]
            self.assertEqual(count, len(times))
            self.assertTrue(all(m % 60 for m in minutes))
            self.assertTrue(all(b - a >= 60 for a, b in zip(minutes, minutes[1:])))
        self.configure(schedule_mode="random", times=[], daily_batches=24)
        self.mutate("/enable")
        self.scheduler.tick("test")
        first = self.pool.handle("GET", "/schedule", self.actor)
        self.assertTrue(first["pool"]["effective_at_utc"].startswith("2026-10-21T00:00:00"))
        self.assertEqual(["2026-10-21"], [plan["bj_date"] for plan in first["day_plan"]])
        scheduler = GroupScheduler(self.pool, lambda t, u: self.actor)
        scheduler.start("second")
        scheduler.tick("second")
        self.assertEqual(first["day_plan"], self.pool.handle("GET", "/schedule", self.actor)["day_plan"])

    def test_group_change_pauses_enabled_plan(self):
        self.configure()
        self.mutate("/enable")
        result = self.configure(group_ids=["3"])
        self.assertEqual("paused", result["pool"]["status"])
        self.assertEqual("groups_changed", result["pool"]["pause_reason"])

    def test_enable_rechecks_current_identity_and_authorizes_full_scope(self):
        self.configure()
        current = {**self.actor, "user_name": "Fresh Name", "business_owner_id": "999", "sub_user_id": "999"}
        self.pool.identity_resolver = lambda t, u: current
        with patch.object(self.gs, "resolve_scope", wraps=self.gs.resolve_scope) as lookup:
            result = self.mutate("/enable")
        lookup.assert_called_once_with(current, ["1", "2"], policy_version=POLICY_VERSION, page_language_selection=default_selection())
        self.assertEqual("Fresh Name", result["pool"]["publisher_name"])

    def test_enable_missing_revoked_or_wrong_identity_fails_closed(self):
        self.configure()
        version = self.version()
        def revoked(*args):
            raise DomainError("identity_permission_denied", "已撤销", 403)
        for resolver in (None, revoked, lambda t, u: {**self.actor, "tenant_key": "other"}):
            self.pool.identity_resolver = resolver
            with self.assertRaises(DomainError):
                self.mutate("/enable")
        self.assertEqual(version, self.version())
        self.assertEqual("paused", self.pool.handle("GET", "/schedule", self.actor)["pool"]["status"])

    def test_enable_operation_recovery_does_not_reenable_after_identity_revoked(self):
        self.configure()
        body = {"operation_id": uuid.uuid4().hex, "expected_version": self.version()}
        enabled = self.pool.handle("POST", "/enable", self.actor, body)
        self.mutate("/pause")
        self.pool.identity_resolver = None
        recovered = self.pool.handle("POST", "/enable", self.actor, body)
        self.assertEqual(enabled["operation_id"], recovered["operation_id"])
        self.assertTrue(recovered["idempotent"])
        self.assertEqual("paused", self.pool.handle("GET", "/schedule", self.actor)["pool"]["status"])

    def test_frozen_boundary_excludes_concurrent_new_import(self):
        self.mutate("/items", material_ids=["21"])
        self.configure()
        self.mutate("/enable")
        self.now += timedelta(minutes=1)
        candidate = self.scheduler._candidates(self.now)[0]
        self.mutate("/items", material_ids=["22"])
        facts = self.pool.candidates(self.actor, candidate["pool"], seed="test", max_item_id=candidate["max_item_id"])
        self.scheduler._finish(candidate, self.actor, facts, None)
        self.assertEqual(["21"], [r["material_id"] for r in self.tasks()])

    def test_candidate_scan_bounds_newest_5000_and_keeps_older_unmatched(self):
        self.mutate("/items", material_ids=["21"])
        self.configure()
        with self.store.connect() as conn:
            values = []
            for value in range(10000, 15000):
                material = {**self.sources.materials["21"], "material_id": str(value), "language": "es"}
                values.append((self.actor["tenant_key"], str(value), "es", json.dumps(material), self.actor["user_id"], stamp(self.now), stamp(self.now)))
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("""INSERT INTO fb_group_pool_item(tenant_key,material_id,language,material_json,
                added_by,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?)""", values)
            conn.commit()
        with patch.object(self.gs, "resolve_materials", wraps=self.gs.resolve_materials) as resolver:
            result = self.pool.handle("POST", "/preview", self.actor, {})
        resolver.assert_not_called()
        self.assertEqual([], result["pairs"])
        self.assertTrue(result["scan_truncated"])
        self.assertEqual(5000, result["scan_limit"])
        self.assertIn("candidate_scan_limit", {row["code"] for row in result["excluded"]})
        self.assertEqual(5001, self.count("fb_group_pool_item"))
        self.assertEqual(0, self.count("fb_video_task"))
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_group_pool_item SET status='removed' WHERE id=5001")
        result = self.pool.handle("POST", "/preview", self.actor, {})
        self.assertFalse(result["scan_truncated"])
        self.assertEqual(["21"], [row["material_id"] for row in result["pairs"]])

    def test_day_plan_filter_uses_beijing_effective_date(self):
        self.configure()
        self.mutate("/enable")
        self.scheduler.tick("test")
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_group_pool SET effective_at_utc='2026-10-20T16:00:00+00:00' WHERE tenant_key=?", (self.actor["tenant_key"],))
        result = self.pool.handle("GET", "/schedule", self.actor)
        self.assertEqual(["2026-10-21"], [plan["bj_date"] for plan in result["day_plan"]])

    def test_concurrent_schedulers_freeze_only_once(self):
        self.mutate("/items", material_ids=["21", "22"])
        self.configure()
        self.mutate("/enable")
        other = GroupScheduler(self.pool, lambda t, u: self.actor)
        other.start("other")
        self.now += timedelta(minutes=1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda pair: pair[0].tick(pair[1]), [(self.scheduler, "test"), (other, "other")]))
        self.assertEqual(1, sum(r["runs_created"] for r in results))
        self.assertEqual(2, len(self.tasks()))
        self.assertEqual(1, self.count("fb_group_pool_run"))

    def test_freeze_failure_rolls_back_everything(self):
        self.mutate("/items", material_ids=["21", "22"])
        self.configure()
        self.mutate("/enable")
        self.now += timedelta(minutes=1)
        real_freeze = self.store.freeze
        def broken(*args, **kwargs):
            real_freeze(*args, **kwargs)
            raise RuntimeError("injected after freeze")
        with patch.object(self.store, "freeze", side_effect=broken), self.assertRaises(RuntimeError):
            self.scheduler.tick("test")
        for table in ("fb_video_task", "fb_video_batch", "fb_video_link", "fb_group_pool_run", "fb_group_pool_reservation"):
            self.assertEqual(0, self.count(table), table)
        self.assertEqual(2, len(self.pool.handle("GET", "", self.actor)["items"]))

    def test_all_bound_failure_states_are_permanent(self):
        self.due()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='failed'")
        self.now += timedelta(minutes=1)
        self.scheduler.tick("test")
        self.assertEqual(2, len(self.tasks()))
        listing = self.pool.handle("GET", "", self.actor, query={"state": "failed"})
        self.assertEqual(2, len(listing["items"]))
        self.assertTrue(all(not i["can_modify"] for i in listing["items"]))
        with self.assertRaises(DomainError):
            self.mutate("/items/remove", item_ids=[listing["items"][0]["id"]])
        self.assertEqual(2, self.count("fb_group_pool_reservation"))

    def test_historical_fb_all_states_excluded_x_history_irrelevant(self):
        self.submit(self.req([("21", "11")]))
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='cancelled'")
            conn.execute("CREATE TABLE x_post_task(material_id TEXT)")
            conn.execute("INSERT INTO x_post_task VALUES('22')")
        result = self.mutate("/items", material_ids=["21", "22"])
        self.assertEqual(["22"], [r["material_id"] for r in result["added"]])
        self.assertEqual("material_previously_bound", result["errors"][0]["code"])

    def test_existing_auto_history_skipped(self):
        with self.store.connect() as conn:
            conn.execute("CREATE TABLE fb_auto_task(material_id TEXT,page_id TEXT,status TEXT,unknown_outcome INTEGER)")
            conn.execute("INSERT INTO fb_auto_task VALUES('21','11','failed',0)")
        result = self.mutate("/items", material_ids=["21"])
        self.assertFalse(result["added"])

    def test_scope_identity_failure_pauses_and_no_task(self):
        self.mutate("/items", material_ids=["21"])
        self.configure()
        self.mutate("/enable")
        self.gs.denied = True
        self.now += timedelta(minutes=1)
        self.scheduler.tick("test")
        self.assertFalse(self.tasks())
        self.assertEqual("paused", self.pool.handle("GET", "/schedule", self.actor)["pool"]["status"])

    def test_nonowner_shared_progress_hides_task_detail(self):
        self.due()
        other = {**self.actor, "user_id": "u2"}
        items = self.pool.handle("GET", "", other, query={"state": "all"})["items"]
        self.assertTrue(all(not r["task_id"] and not r["batch_id"] for r in items))
        run = self.pool.handle("GET", "/runs", other)["items"][0]
        self.assertFalse(run["pairs"])
        self.assertEqual({"queued": 2}, run["task_counts"])

    def test_material_identity_changes_excluded_before_freeze(self):
        self.mutate("/items", material_ids=["21", "22"])
        self.configure()
        self.mutate("/enable")
        self.sources.materials["22"]["language"] = "es"
        self.now += timedelta(minutes=1)
        self.scheduler.tick("test")
        self.assertEqual(["21"], [r["material_id"] for r in self.tasks()])

    def test_invalid_newest_does_not_hide_valid_older_candidate(self):
        self.sources.pages.pop("12")
        self.mutate("/items", material_ids=["21", "22"])
        self.configure()
        self.mutate("/enable")
        self.sources.materials.pop("22")
        self.now += timedelta(minutes=1)
        self.scheduler.tick("test")
        self.assertEqual(["21"], [r["material_id"] for r in self.tasks()])

    def test_skip_missed_minute_and_fixed_starts_at_next_future_time(self):
        self.mutate("/items", material_ids=["21"])
        self.configure(times=["18:00", "18:01"])
        self.mutate("/enable")
        self.assertEqual(0, self.scheduler.tick("test")["runs_created"])
        self.now += timedelta(minutes=2)
        self.assertEqual(0, self.scheduler.tick("test")["runs_created"])
        self.assertFalse(self.tasks())

    def test_pending_removal_and_reimport_keep_history(self):
        added = self.mutate("/items", material_ids=["21"])["added"][0]
        self.mutate("/items/remove", item_ids=[added["id"]])
        again = self.mutate("/items", material_ids=["21"])["added"][0]
        self.assertNotEqual(added["id"], again["id"])
        self.assertEqual(2, self.count("fb_group_pool_item"))

    def test_disabled_gate_keeps_version_and_operation_records_readable(self):
        result = self.configure()
        self.pool.enabled = False
        read = self.pool.handle("GET", "/schedule", self.actor)
        self.assertFalse(read["feature_enabled"])
        self.assertEqual(result["pool"]["version"], read["pool"]["version"])
        replay = self.pool.handle("GET", "/operations/" + result["operation_id"], self.actor)
        self.assertTrue(replay["idempotent"])

    def group_worker(self):
        worker = self.worker()
        worker.group_sources = self.gs
        worker.group_identity_resolver = lambda t, u: self.actor
        return worker

    def test_worker_group_success_and_policy_frozen(self):
        self.due(("21",))
        self.ready()
        worker = self.group_worker()
        worker.execute_next()
        self.assertEqual(1, len(worker.calls))
        snap = json.loads(self.tasks()[0]["snapshot_json"])
        self.assertEqual(POLICY_VERSION, snap["group_pool"]["policy_version"])
        self.assertNotIn("manual_policy", snap)
        self.assertNotIn("pool", snap)

    def test_worker_membership_and_identity_change_fail_closed(self):
        for change in ("language", "group", "identity", "policy", "source"):
            with self.subTest(change=change):
                self.setUp()
                self.due(("21",))
                self.ready()
                worker = self.group_worker()
                if change == "language":
                    self.sources.pages["11"]["language"] = "es"
                    self.sources.pages["12"]["language"] = "es"
                elif change == "group":
                    self.gs.denied = True
                elif change == "identity":
                    worker.group_identity_resolver = lambda t, u: {**self.actor, "sub_user_id": "999"}
                elif change == "source":
                    self.sources.materials["21"]["media_url"] += "?changed=1"
                else:
                    worker.group_sources = None
                worker.execute_next()
                self.assertFalse(worker.calls)
                self.assertEqual("failed", self.tasks()[0]["status"])

    def test_unknown_outcome_remains_bound_and_no_retry(self):
        self.due(("21",))
        self.ready()
        worker = self.worker([GraphResult("unknown", error_code="test_unknown")])
        worker.group_sources, worker.group_identity_resolver = self.gs, lambda t, u: self.actor
        worker.execute_next()
        worker.execute_next()
        self.assertEqual(1, len(worker.calls))
        self.assertEqual("unknown", self.tasks()[0]["status"])
        self.assertEqual(1, self.count("fb_group_pool_reservation"))

    def test_gate_disabled_still_reads_and_executes_bound_tasks(self):
        self.due(("21",))
        self.pool.enabled = False
        self.assertEqual("group_pool_gate_closed", self.scheduler.tick("test")["status"])
        self.assertEqual(1, len(self.pool.handle("GET", "", self.actor, query={"state": "all"})["items"]))
        with self.assertRaises(DomainError):
            self.mutate("/pause")
        self.ready()
        worker = self.group_worker()
        worker.execute_next()
        self.assertEqual(1, len(worker.calls))

    def test_schema_migration_preserves_existing_tasks(self):
        self.submit()
        before = self.tasks()
        Store(self.store.path)
        self.assertEqual(before, self.tasks())

    def v2_due(self, *, selection=None, mids=("21", "22")):
        self.mutate("/items", material_ids=list(mids))
        self.configure(page_language_selection=selection or {"mode": "all", "codes": [], "include_blank": True})
        self.mutate("/enable")
        self.now += timedelta(minutes=1)
        return self.scheduler.tick("test")

    def test_specific_page_priority_then_blank_gets_newest_remaining(self):
        self.sources.pages["11"]["language"] = "en"
        self.sources.pages["12"]["language"] = ""
        self.sources.materials["21"]["language"] = "en"
        self.sources.materials["22"]["language"] = "es"
        self.v2_due()
        self.assertEqual({("11", "21"), ("12", "22")}, {(t["page_id"], t["material_id"]) for t in self.tasks()})
        run = self.pool.handle("GET", "/runs", self.actor)["items"][0]
        self.assertEqual(["specific", "unrestricted"], [p["language_kind"] for p in run["pairs"]])
        self.assertEqual(["en", ""], [p["page_language"] for p in run["pairs"]])
        self.assertEqual(["en", "es"], [p["language"] for p in run["pairs"]])

    def test_blank_only_pages_accept_any_language_once_newest_first(self):
        for page in self.sources.pages.values():
            page["language"] = ""
        self.sources.materials["21"]["language"] = "zh-tw"
        self.sources.materials["22"]["language"] = "es"
        self.sources.materials["23"] = {**self.sources.materials["21"], "material_id": "23", "language": "ja"}
        self.v2_due(selection={"mode": "selected", "codes": [], "include_blank": True}, mids=("21", "22", "23"))
        self.assertEqual(["23", "22"], [t["material_id"] for t in self.tasks()])
        self.assertEqual(2, len({t["page_id"] for t in self.tasks()}))
        self.assertEqual(2, self.count("fb_group_pool_reservation"))

    def test_selected_language_does_not_restrict_material_language_for_blank(self):
        self.sources.pages["11"]["language"] = "en"
        self.sources.pages["12"]["language"] = ""
        self.sources.materials["21"]["language"] = "es"
        self.v2_due(selection={"mode": "selected", "codes": ["ja"], "include_blank": True}, mids=("21",))
        self.assertEqual([("12", "21")], [(t["page_id"], t["material_id"]) for t in self.tasks()])

    def test_unknown_specific_page_does_not_consume_blank_candidate(self):
        self.submit(self.req([("22", "11")]))
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='unknown',unknown_outcome=1")
        self.sources.pages["11"]["language"] = "en"
        self.sources.pages["12"]["language"] = ""
        self.sources.materials["21"]["language"] = "en"
        self.v2_due(mids=("21",))
        group_tasks = [t for t in self.tasks() if t["source_kind"] == "group_pool"]
        self.assertEqual([("12", "21")], [(t["page_id"], t["material_id"]) for t in group_tasks])

    def test_quota_preserves_specific_priority_and_blank_fallback(self):
        self.sources.pages["11"]["language"] = "en"
        self.sources.pages["12"]["language"] = ""
        self.sources.materials["21"]["language"] = "en"
        self.sources.materials["22"]["language"] = "es"
        self.store.daily_limit = 1
        self.v2_due()
        self.assertEqual([("11", "21")], [(t["page_id"], t["material_id"]) for t in self.tasks()])

    def test_quota_blank_uses_capacity_if_specific_has_no_material(self):
        self.sources.pages["11"]["language"] = "en"
        self.sources.pages["12"]["language"] = ""
        self.sources.materials["21"]["language"] = "es"
        self.store.daily_limit = 1
        self.v2_due(mids=("21",))
        self.assertEqual([("12", "21")], [(t["page_id"], t["material_id"]) for t in self.tasks()])

    def test_language_setting_change_pauses_and_omitted_setting_preserves_v2(self):
        selection = {"mode": "selected", "codes": ["en"], "include_blank": True}
        self.configure(page_language_selection=selection)
        self.mutate("/enable")
        unchanged = self.configure()
        self.assertEqual("enabled", unchanged["pool"]["status"])
        self.assertEqual(selection, unchanged["pool"]["page_language_selection"])
        changed = self.configure(page_language_selection={**selection, "codes": ["es"]})
        self.assertEqual("paused", changed["pool"]["status"])
        self.assertEqual("page_language_selection_changed", changed["pool"]["pause_reason"])

    def test_legacy_row_migration_and_first_v2_save_require_reenable(self):
        self.configure()
        self.mutate("/enable")
        self.scheduler.tick("test")
        with self.store.connect() as conn:
            before_plan = [tuple(r) for r in conn.execute("SELECT * FROM fb_group_pool_day")]
            conn.execute("ALTER TABLE fb_group_pool DROP COLUMN page_language_selection_json")
            conn.execute("ALTER TABLE fb_group_pool DROP COLUMN routing_policy_version")
        Store(self.store.path)
        legacy = self.pool.handle("GET", "/schedule", self.actor)["pool"]
        self.assertEqual(LEGACY_POLICY, legacy["routing_policy_version"])
        self.assertEqual("enabled", legacy["status"])
        self.assertEqual(default_selection(), legacy["page_language_selection"])
        with self.store.connect() as conn:
            self.assertEqual(before_plan, [tuple(r) for r in conn.execute("SELECT * FROM fb_group_pool_day")])
        untouched = self.configure()
        self.assertEqual(LEGACY_POLICY, untouched["pool"]["routing_policy_version"])
        self.assertEqual("enabled", untouched["pool"]["status"])
        migrated = self.configure(page_language_selection=default_selection())
        self.assertEqual(POLICY_VERSION, migrated["pool"]["routing_policy_version"])
        self.assertEqual("paused", migrated["pool"]["status"])
        self.assertEqual("routing_policy_changed", migrated["pool"]["pause_reason"])

    def test_new_pool_defaults_v2_blank_excluded(self):
        pool = self.pool.handle("GET", "/schedule", self.actor)["pool"]
        self.assertEqual(POLICY_VERSION, pool["routing_policy_version"])
        self.assertFalse(pool["page_language_selection"]["include_blank"])
        self.sources.pages["12"]["language"] = ""
        self.due()
        self.assertEqual(["11"], [t["page_id"] for t in self.tasks()])

    def test_pages_preview_readonly_numeric_cursor_and_selected_counts(self):
        self.sources.pages = {
            "9": {**self.sources.pages["11"], "page_id": "9", "page_name": "English", "language": "en"},
            "10": {**self.sources.pages["11"], "page_id": "10", "page_name": "Blank", "language": ""},
            "11": {**self.sources.pages["11"], "page_id": "11", "page_name": "Spanish", "language": "es", "blocked_reason": "授权失效"},
        }
        payload = {"group_ids": ["1"], "page_language_selection": {"mode": "all", "codes": [], "include_blank": True}, "limit": 1}
        first = self.pool.handle("POST", "/pages/preview", self.actor, payload)
        self.assertEqual(["9"], [row["page_id"] for row in first["items"]])
        self.assertEqual((3, 2, 1), (first["total"], first["eligible_count"], first["excluded_count"]))
        second = self.pool.handle("POST", "/pages/preview", self.actor, {**payload, "cursor": first["next_cursor"]})
        self.assertEqual("10", second["items"][0]["page_id"])
        self.assertEqual("unrestricted", second["items"][0]["language_kind"])
        searched = self.pool.handle("POST", "/pages/preview", self.actor, {**payload, "q": "spanish"})
        self.assertEqual(3, searched["total"])
        self.assertEqual(1, searched["matching_count"])
        self.assertFalse(searched["items"][0]["eligible"])
        selected = self.pool.handle("POST", "/pages/preview", self.actor, {**payload, "page_language_selection": {"mode": "selected", "codes": ["en"], "include_blank": False}})
        self.assertEqual((1, 1, 0), (selected["total"], selected["eligible_count"], selected["excluded_count"]))
        self.assertEqual(3, selected["language_summary"]["total_page_count"])
        self.assertEqual(0, self.count("fb_group_pool"))
        self.assertEqual(0, self.count("fb_video_link"))

    def test_pair_preview_selection_is_not_saved(self):
        self.sources.pages["12"]["language"] = ""
        self.mutate("/items", material_ids=["21", "22"])
        self.configure()
        version = self.version()
        preview = self.pool.handle("POST", "/preview", self.actor, {"page_language_selection": {"mode": "selected", "codes": [], "include_blank": True}})
        self.assertEqual(["12"], [pair["page_id"] for pair in preview["pairs"]])
        self.assertEqual("unrestricted", preview["language_capacity"][-1]["language_kind"])
        self.assertEqual(version, self.version())
        self.assertEqual(default_selection(), self.pool.handle("GET", "/schedule", self.actor)["pool"]["page_language_selection"])
        self.assertEqual(0, self.count("fb_video_link"))

    def test_blank_worker_checks_frozen_v2_selection_and_target_only(self):
        self.sources.pages["12"]["language"] = ""
        self.v2_due(selection={"mode": "selected", "codes": [], "include_blank": True}, mids=("21",))
        self.ready()
        worker = self.group_worker()
        with patch.object(self.gs, "resolve_scope", wraps=self.gs.resolve_scope) as resolve:
            worker.execute_next()
        self.assertEqual(1, len(worker.calls))
        resolve.assert_called_once_with(self.actor, ["1", "2"], policy_version=POLICY_VERSION,
            page_language_selection={"mode": "selected", "codes": [], "include_blank": True}, page_ids=["12"])

    def test_blank_worker_fails_closed_after_page_gets_specific_language(self):
        self.sources.pages["12"]["language"] = ""
        self.v2_due(selection={"mode": "all", "codes": [], "include_blank": True}, mids=("21", "22"))
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='cancelled' WHERE page_id='11'")
        self.sources.pages["12"]["language"] = "zh"
        self.ready()
        worker = self.group_worker()
        worker.execute_next()
        self.assertFalse(worker.calls)
        task = next(t for t in self.tasks() if t["page_id"] == "12")
        self.assertEqual("failed", task["status"])
        self.assertEqual("group_pool_membership_changed", task["error_code"])

    def test_v2_worker_rejects_missing_selection_or_language_kind(self):
        self.sources.pages["12"]["language"] = ""
        self.v2_due(selection={"mode": "selected", "codes": [], "include_blank": True}, mids=("21",))
        task = self.tasks()[0]
        for missing in ("selection", "kind"):
            snap = json.loads(task["snapshot_json"])
            if missing == "selection":
                snap["group_pool"].pop("page_language_selection")
            else:
                snap["page"].pop("language_kind")
            with self.assertRaises(DomainError):
                self.group_worker()._group_preflight(task, snap)

    def test_legacy_blank_is_never_promoted_to_unrestricted(self):
        self.configure()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_group_pool SET routing_policy_version=?", (LEGACY_POLICY,))
        for page in self.sources.pages.values():
            page["language"] = ""
        self.due(("21",))
        self.assertFalse(self.tasks())
        self.assertEqual(0, self.count("fb_group_pool_reservation"))

    def test_newest_invalid_blank_candidate_does_not_hide_valid_other_language(self):
        self.sources.pages["11"]["language"] = "en"
        self.sources.pages["12"]["language"] = ""
        self.sources.materials["21"]["language"] = "en"
        self.sources.materials["22"]["language"] = "fr"
        self.sources.materials["23"] = {**self.sources.materials["22"], "material_id": "23", "language": "es"}
        self.mutate("/items", material_ids=["21", "22", "23"])
        self.sources.materials.pop("23")
        self.configure(page_language_selection={"mode": "all", "codes": [], "include_blank": True})
        self.mutate("/enable")
        self.now += timedelta(minutes=1)
        self.scheduler.tick("test")
        self.assertEqual({("11", "21"), ("12", "22")}, {(t["page_id"], t["material_id"]) for t in self.tasks()})

    def test_frozen_legacy_task_remains_strict_and_does_not_need_new_fields(self):
        self.configure()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_group_pool SET routing_policy_version=?", (LEGACY_POLICY,))
        self.due(("21",))
        with self.store.connect() as conn:
            task = self.tasks()[0]
            snap = json.loads(task["snapshot_json"])
            self.assertEqual(LEGACY_POLICY, snap["group_pool"]["policy_version"])
            snap["group_pool"].pop("page_language_selection")
            snap["page"].pop("language_kind")
            conn.execute("UPDATE fb_video_task SET snapshot_json=?", (json.dumps(snap),))
        self.ready()
        worker = self.group_worker()
        worker.execute_next()
        self.assertEqual(1, len(worker.calls))

    def test_random_seed_does_not_change_with_routing_version(self):
        from features.fb_video_publish import group_pool
        before = random_times("tenant", "2026-10-27", 4, 12)
        with patch.object(group_pool, "POLICY_VERSION", "future-routing-version"):
            self.assertEqual(before, random_times("tenant", "2026-10-27", 4, 12))

    def test_selection_change_preserves_version_conflict_and_operation_replay(self):
        self.configure()
        selection = {"mode": "selected", "codes": ["jp", "ja"], "include_blank": True}
        body = {"operation_id": uuid.uuid4().hex, "expected_version": self.version(), "group_ids": ["1", "2"],
                "schedule_mode": "fixed", "times": ["18:01"], "daily_batches": 1,
                "caption_template": "{name}\n{desc}\n{url}", "processing_mode": "original", "page_language_selection": selection}
        result = self.pool.handle("POST", "/schedule", self.actor, body)
        self.assertEqual(["ja"], result["pool"]["page_language_selection"]["codes"])
        self.assertTrue(self.pool.handle("POST", "/schedule", self.actor, body)["idempotent"])
        with self.assertRaises(DomainError):
            self.pool.handle("POST", "/schedule", self.actor, {**body, "operation_id": uuid.uuid4().hex})


if __name__ == "__main__":
    main()
