#!/usr/bin/env python3
"""Focused contracts for the private X auto-template execution bridge."""

from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from features.x_accounts import oauth_service
from features.x_posts import service
from features.x_posts.selector import DramawaveCandidateSelector
from scripts.test_x_post_priority_manual_store import material_candidate


ACTOR = {
    "tenant_key": "tenant-a",
    "user_id": "admin-1",
    "name": "Admin",
    "email": "admin@example.test",
    "role": "admin",
}
BODY_TEMPLATE = "{{drama_name}}\n{{desc}}\n{{url}}"


class XPostAutoTemplateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "x-post.sqlite3"
        self.store = service.XPostStore(self.db_path)

    def tearDown(self):
        self.temporary.cleanup()

    def create_auto(
        self,
        material_id="701",
        account_id=2,
        external_task_key="task-701",
        template_ref="template-17",
        template_version=3,
        body_template=BODY_TEMPLATE,
        actor=ACTOR,
    ):
        return self.store.create_auto_template_run(
            material_id,
            account_id,
            external_task_key,
            template_ref,
            template_version,
            body_template,
            actor,
        )

    def test_auto_identity_is_idempotent_and_source_isolated(self):
        auto = self.create_auto()
        replay = self.create_auto(actor=dict(ACTOR, name="Renamed Admin"))
        self.assertTrue(auto["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(replay["id"], auto["id"])
        self.assertEqual(auto["trigger_source"], "auto_template")
        self.assertEqual(auto["external_task_key"], "task-701")
        self.assertEqual(auto["template_ref"], "template-17")
        self.assertEqual(auto["template_version"], 3)
        self.assertEqual(len(auto["body_template_sha256"]), 64)
        self.assertEqual(auto["publish_mode"], "immediate")
        self.assertEqual(auto["scheduled_at"], "")

        for changed in (
            {"template_version": 4},
            {"body_template": "{{drama_name}}\nALT {{desc}}\n{{url}}"},
            {"material_id": "702"},
        ):
            with self.subTest(changed=changed), self.assertRaises(
                service.XPostError
            ) as conflict:
                self.create_auto(**changed)
            self.assertEqual(
                conflict.exception.code,
                "x_post_auto_template_idempotency_conflict",
            )

        manual = self.store.create_manual_run(
            ["801"],
            [3],
            "manual-801",
            {"user_id": "admin-1", "name": "Admin"},
        )
        claimed_manual = self.store.claim_manual_run("manual")
        self.assertEqual(claimed_manual["run"]["id"], manual["id"])
        self.assertEqual(
            claimed_manual["run"]["trigger_source"],
            "manual",
        )
        claimed_auto = self.store.claim_manual_run("auto_template")
        self.assertEqual(claimed_auto["run"]["id"], auto["id"])
        with self.assertRaises(service.XPostError):
            self.store.get_manual_run(auto["id"], "manual")
        with self.assertRaises(service.XPostError):
            self.store.get_manual_run(manual["id"], "auto_template")
        self.assertEqual(
            set(self.store.active_manual_account_ids()),
            {2, 3},
        )

    def test_manual_and_auto_template_active_reservations_are_mutually_exclusive(self):
        with mock.patch.object(
            service,
            "utc_now",
            return_value="2026-08-12T06:00:00Z",
        ):
            manual = self.store.create_manual_run(
                ["710"],
                [2],
                "manual-scheduled-710",
                ACTOR,
                publish_mode="scheduled",
                scheduled_at="2026-08-12T15:00:00+08:00",
            )
        with self.assertRaises(service.XPostError) as auto_blocked:
            self.create_auto(material_id="710", external_task_key="task-710")
        self.assertEqual(
            auto_blocked.exception.code,
            "x_post_auto_template_material_unavailable",
        )

        auto = self.create_auto(material_id="711", external_task_key="task-711")
        with self.assertRaises(service.XPostError) as manual_blocked:
            self.store.create_manual_run(
                ["711"],
                [3],
                "manual-711",
                ACTOR,
            )
        self.assertEqual(
            manual_blocked.exception.code,
            "x_post_manual_material_unavailable",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            active = conn.execute(
                "SELECT manual_run_id,material_key FROM "
                "x_post_manual_material_reservation WHERE state='active' "
                "ORDER BY material_key"
            ).fetchall()
        self.assertEqual(active, [(manual["id"], "710"), (auto["id"], "711")])

    def test_auto_plan_caps_duration_and_global_queue_labels_and_deduplicates(self):
        auto = self.create_auto()
        too_long = material_candidate(auto, 2, "701")
        too_long["preflight_duration"] = 600.01
        with self.assertRaises(service.XPostError) as duration:
            self.store.create_manual_plan(
                auto["id"],
                [too_long],
                "auto_template",
            )
        self.assertEqual(
            duration.exception.code,
            "x_post_auto_template_duration_exceeded",
        )
        self.assertEqual(
            self.store.get_manual_run(auto["id"], "auto_template")["queues"],
            [],
        )

        candidate = material_candidate(auto, 2, "701")
        candidate["preflight_duration"] = 600.0
        plan = self.store.create_manual_plan(
            auto["id"],
            [candidate],
            "auto_template",
        )
        self.assertEqual(len(plan["queues"]), 1)
        queue = plan["queues"][0]
        logs = self.store.query_logs({"material_id": "701"})["items"]
        self.assertEqual(logs[0]["batch_kind"], "auto_template")

        reused = self.store.create_manual_run(
            ["701"],
            [4],
            "manual-reuse-701",
            {"user_id": "admin-1", "name": "Admin"},
        )
        reused_plan = self.store.create_manual_plan(
            reused["id"],
            [material_candidate(reused, 4, "701")],
        )
        self.assertEqual(len(reused_plan["queues"]), 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue WHERE material_key='701'"
                ).fetchone()[0],
                2,
            )
        with self.assertRaises(service.XPostError) as automatic_reuse:
            self.create_auto(
                material_id="701",
                account_id=5,
                external_task_key="task-701-reuse",
            )
        self.assertEqual(
            automatic_reuse.exception.code,
            "x_post_auto_template_material_unavailable",
        )

        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://example.test/long",
            "https://example.test/short",
            "safe post text",
        )
        self.store.mark_publishing(log["id"])
        self.store.mark_media_uploaded(log["id"], "media701")
        recovered = self.store.recover_auto_template_run(auto["id"])
        # post_creating is already atomically terminalized as needs_review by
        # the canonical ledger; recovery must leave that unknown state intact.
        self.assertFalse(recovered["recovered"])
        readback = self.store.get_manual_run(auto["id"], "auto_template")
        self.assertEqual(readback["status"], "needs_review")
        self.assertEqual(readback["unknown_count"], 1)
        self.assertEqual(readback["queues"][0]["log_id"], log["id"])
        self.assertEqual(readback["queues"][0]["post_id"], "")
        self.assertTrue(readback["queues"][0]["unknown_outcome"])

    def test_unavailable_material_query_includes_operator_pool(self):
        self.store.add_pool_materials(
            ["901"],
            {"user_id": "admin-1", "name": "Admin"},
            validation_checks=[
                {"material_id": "901", "error_code": "", "error_message": ""}
            ],
        )
        self.assertEqual(
            self.store.query_material_keys(
                ["900", "901"],
                include_pool=True,
            ),
            ["901"],
        )
        self.assertEqual(self.store.query_material_keys(["901"]), [])

        reserved = self.store.create_manual_run(
            ["902"],
            [2],
            "manual-reserved-query-902",
            ACTOR,
        )
        self.assertEqual(
            self.store.query_material_keys(["900", "901", "902"]),
            ["902"],
        )
        self.store.record_manual_failure(
            reserved["id"],
            "x_post_manual_source_preflight_failed",
            "offline test release",
        )
        self.assertEqual(self.store.query_material_keys(["902"]), [])

    def test_exact_auto_recovery_never_claims_another_or_manual_run(self):
        first = self.create_auto(
            material_id="911",
            account_id=11,
            external_task_key="task-911",
        )
        second = self.create_auto(
            material_id="912",
            account_id=12,
            external_task_key="task-912",
        )
        first_plan = self.store.create_manual_plan(
            first["id"],
            [material_candidate(first, 11, "911")],
            "auto_template",
        )
        self.store.create_manual_plan(
            second["id"],
            [material_candidate(second, 12, "912")],
            "auto_template",
        )
        queue = first_plan["queues"][0]
        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://example.test/long",
            "https://example.test/short",
            "safe post text",
        )
        self.store.mark_publishing(log["id"])

        recovered = self.store.recover_auto_template_run(first["id"])
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["run"]["status"], "stopped")
        self.assertEqual(
            self.store.get_manual_run(second["id"], "auto_template")["status"],
            "running",
        )
        self.assertEqual(self.store.active_manual_account_ids(), [12])

        manual = self.store.create_manual_run(
            ["913"],
            [13],
            "manual-913",
            {"user_id": "admin-1", "name": "Admin"},
        )
        with self.assertRaises(service.XPostError):
            self.store.recover_auto_template_run(manual["id"])

    def test_exact_recovery_fences_queued_queue_with_or_without_reserved_log(self):
        for suffix, reserve_first in (("no-log", False), ("reserved", True)):
            with self.subTest(case=suffix):
                run = self.create_auto(
                    material_id="92%s" % ("1" if reserve_first else "0"),
                    account_id=21 if reserve_first else 20,
                    external_task_key="task-fence-" + suffix,
                )
                plan = self.store.create_manual_plan(
                    run["id"],
                    [
                        material_candidate(
                            run,
                            21 if reserve_first else 20,
                            "921" if reserve_first else "920",
                        )
                    ],
                    "auto_template",
                )
                queue_id = plan["queues"][0]["id"]
                reserved = (
                    self.store.reserve_log(queue_id) if reserve_first else None
                )
                recovered = self.store.recover_auto_template_run(run["id"])
                self.assertTrue(recovered["recovered"])
                self.assertEqual(recovered["run"]["status"], "stopped")
                queue = recovered["run"]["queues"][0]
                self.assertEqual(queue["status"], "failed")
                self.assertGreater(queue["log_id"], 0)
                if reserved is not None:
                    self.assertEqual(queue["log_id"], reserved["id"])
                self.assertEqual(
                    self.store.get_log(queue["log_id"])["status"],
                    "failed",
                )
                with self.assertRaises(service.XPostError):
                    self.store.assert_auto_template_publishable(
                        queue_id,
                        queue["log_id"],
                    )

    def test_additive_migration_defaults_legacy_rows_to_manual_and_freezes_identity(self):
        legacy_path = Path(self.temporary.name) / "legacy.sqlite3"
        with contextlib.closing(sqlite3.connect(legacy_path)) as conn:
            conn.execute(
                """
                CREATE TABLE x_post_manual_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    run_date TEXT NOT NULL,
                    source_date TEXT NOT NULL,
                    account_ids_json TEXT NOT NULL,
                    material_ids_json TEXT NOT NULL,
                    body_template TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    expected_count INTEGER NOT NULL,
                    queued_count INTEGER NOT NULL DEFAULT 0,
                    published_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    unknown_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO x_post_manual_run("
                "idempotency_key,run_date,source_date,account_ids_json,"
                "material_ids_json,body_template,actor_user_id,actor_name,"
                "expected_count,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-manual",
                    "2026-08-11",
                    "2026-08-10",
                    "[2]",
                    '["501"]',
                    BODY_TEMPLATE,
                    "admin-1",
                    "Admin",
                    1,
                    "2026-08-11T00:00:00Z",
                    "2026-08-11T00:00:00Z",
                ),
            )
            conn.commit()

        legacy = service.XPostStore(legacy_path)
        service.XPostStore(legacy_path)
        row = legacy.get_manual_run(1, "manual")
        self.assertEqual(row["trigger_source"], "manual")
        with contextlib.closing(sqlite3.connect(legacy_path)) as conn:
            columns = {
                value[1]
                for value in conn.execute(
                    "PRAGMA table_info(x_post_manual_run)"
                )
            }
            self.assertTrue(
                {
                    "trigger_source",
                    "external_task_key",
                    "template_ref",
                    "template_version",
                    "body_template_sha256",
                }.issubset(columns)
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE x_post_manual_run SET trigger_source='auto_template' "
                    "WHERE id=1"
                )


class XPostAutoTemplateBoundaryTests(unittest.TestCase):
    @staticmethod
    def account(long_video=True):
        return {
            "id": 202,
            "username": "auto_account",
            "x_user_id": "x-202",
            "display_name": "Auto Account",
            "profile_image_url": "",
            "status": "active",
            "publish_approved": True,
            "publish_eligible": True,
            "subscription_type": "premium" if long_video else "unknown",
            "premium_subscriber": bool(long_video),
            "long_video_eligible": bool(long_video),
            "long_video_publish_eligible": bool(long_video),
        }

    def test_auto_account_verify_boundary_forwards_safe_refresh_guards_and_dto(self):
        account = self.account()
        account.update(
            access_token="must-not-leak",
            refresh_token="must-not-leak-either",
        )
        payload = {
            "only_refresh_required": True,
            "preserve_transient_status": True,
            "require_publish_approved": True,
        }
        with mock.patch.object(
            oauth_service,
            "verify_account",
            return_value=account,
        ) as verify_mock:
            result = oauth_service.verify_auto_template_account_request(payload, 202)

        verify_mock.assert_called_once_with(
            202,
            oauth_service.AUTO_TEMPLATE_ACTOR,
            "all",
            only_refresh_required=True,
            preserve_transient_status=True,
            require_publish_approved=True,
        )
        self.assertIs(result["item"]["publish_approved"], True)
        self.assertIs(result["item"]["publish_eligible"], True)
        self.assertNotIn("access_token", result["item"])
        self.assertNotIn("refresh_token", result["item"])

        for invalid in (
            {"only_refresh_required": "true"},
            {"unexpected": True},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                oauth_service.ServiceError
            ) as caught:
                oauth_service.verify_auto_template_account_request(invalid, 202)
            self.assertEqual(caught.exception.code, "invalid_request")

    def test_auto_selector_uses_strict_existing_compliance_path(self):
        selector = DramawaveCandidateSelector(object())
        candidate = {
            "material_id": "501",
            "source_date": "2026-08-10",
        }
        with mock.patch.object(
            selector,
            "_pool_candidate",
            return_value=dict(candidate),
        ) as hydrate:
            selected, rejected = selector.select_auto_template(
                ["501"],
                "2026-08-10",
            )
        self.assertEqual(rejected, [])
        self.assertEqual(selected[0]["material_id"], "501")
        self.assertEqual(selected[0]["manual_item_id"], 1)
        hydrate.assert_called_once_with(
            "501",
            "2026-08-10",
            allow_long_duration=False,
            max_duration_seconds=600,
        )

    def test_auto_create_accepts_only_fixed_service_actor_and_records_internal_actor(self):
        class FakeStore:
            called = None

            def create_auto_template_run(self, *args):
                self.called = args
                return {
                    "id": 17,
                    "trigger_source": "auto_template",
                    "external_task_key": args[2],
                    "template_ref": args[3],
                    "template_version": args[4],
                    "body_template_sha256": "a" * 64,
                    "run_date": "2026-08-11",
                    "source_date": "2026-08-10",
                    "account_ids": [args[1]],
                    "material_ids": [str(args[0])],
                    "body_template": args[5],
                    "status": "queued",
                    "queues": [],
                }

        store = FakeStore()
        payload = {
            "material_id": "501",
            "account_id": 202,
            "external_task_key": "x-auto-task-9",
            "template_ref": "x-auto-template-3",
            "template_version": 4,
            "body_template": BODY_TEMPLATE,
            "actor": "x_auto_post_service",
        }
        with mock.patch.object(
            oauth_service,
            "_manual_publish_accounts",
            return_value=[{"id": 202}],
        ), mock.patch.object(
            oauth_service,
            "_x_posts_api",
            return_value=(service.XPostError, lambda _path: store, None),
        ):
            result = oauth_service.create_post_auto_template_run_request(payload)
            self.assertEqual(result["item"]["id"], 17)
            self.assertEqual(store.called[-1], oauth_service.AUTO_TEMPLATE_ACTOR)
            with self.assertRaises(oauth_service.ServiceError):
                oauth_service.create_post_auto_template_run_request(
                    dict(payload, actor=ACTOR)
                )

    def test_auto_plan_retains_premium_gate_and_never_exceeds_600(self):
        class FakeStore:
            def __init__(self):
                self.candidates = None

            def get_manual_run(self, run_id, trigger_source):
                return {
                    "id": int(run_id),
                    "trigger_source": trigger_source,
                    "external_task_key": "task-1",
                    "template_ref": "template-1",
                    "template_version": 1,
                    "body_template_sha256": "a" * 64,
                    "run_date": "2026-08-11",
                    "source_date": "2026-08-10",
                    "account_ids": [202],
                    "material_ids": ["501"],
                    "body_template": BODY_TEMPLATE,
                    "status": "running",
                    "expected_count": 1,
                    "queues": [],
                }

            def create_manual_plan(self, run_id, candidates, trigger_source):
                self.candidates = list(candidates)
                result = self.get_manual_run(run_id, trigger_source)
                result["queues"] = []
                return result

        store = FakeStore()
        api = (service.XPostError, lambda _path: store, None)
        with mock.patch.object(oauth_service, "_x_posts_api", return_value=api), mock.patch.object(
            oauth_service,
            "_manual_publish_accounts",
            return_value=[self.account(long_video=False)],
        ):
            with self.assertRaises(oauth_service.ServiceError) as standard:
                oauth_service.create_post_auto_template_plan_request(
                    {
                        "run_id": 1,
                        "candidates": [
                            {"material_id": "501", "preflight_duration": 141.0}
                        ],
                    }
                )
        self.assertEqual(standard.exception.code, "x_long_video_requires_premium")

        with mock.patch.object(oauth_service, "_x_posts_api", return_value=api), mock.patch.object(
            oauth_service,
            "_manual_publish_accounts",
            return_value=[self.account(long_video=True)],
        ), mock.patch.object(oauth_service, "preflight_post_storage_request"):
            accepted = oauth_service.create_post_auto_template_plan_request(
                {
                    "run_id": 1,
                    "candidates": [
                        {"material_id": "501", "preflight_duration": 600.0}
                    ],
                }
            )
            self.assertEqual(accepted["item"]["trigger_source"], "auto_template")
            with self.assertRaises(oauth_service.ServiceError) as exceeded:
                oauth_service.create_post_auto_template_plan_request(
                    {
                        "run_id": 1,
                        "candidates": [
                            {"material_id": "501", "preflight_duration": 600.01}
                        ],
                    }
                )
        self.assertEqual(
            exceeded.exception.code,
            "x_post_auto_template_duration_exceeded",
        )

    def test_auto_publish_path_accepts_only_auto_parent_queue(self):
        class FakeStore:
            trigger_source = "auto_template"
            run_status = "running"

            def get_queue(self, queue_id):
                return {
                    "id": int(queue_id),
                    "run_id": None,
                    "catchup_run_id": None,
                    "schedule_run_id": None,
                    "manual_run_id": 17,
                    "account_id": 202,
                }

            def get_manual_run(self, run_id, trigger_source):
                if trigger_source != self.trigger_source:
                    raise service.XPostError(
                        "x_post_manual_run_not_found",
                        "run not found",
                        404,
                    )
                return {
                    "id": int(run_id),
                    "trigger_source": trigger_source,
                    "account_ids": [202],
                    "status": self.run_status,
                }

            def reserve_log(self, queue_id):
                return {
                    "id": 91,
                    "status": "published",
                    "short_url": "https://example.test/short",
                    "x_post_id": "12345",
                    "x_post_url": "https://x.com/auto/status/12345",
                }

        store = FakeStore()
        api = (service.XPostError, lambda _path: store, None)
        with mock.patch.object(oauth_service, "_x_posts_api", return_value=api):
            published = oauth_service.publish_queue_request(
                81,
                (),
                allow_schedule=False,
                allow_manual=True,
                expected_manual_trigger_source="auto_template",
                require_manual_parent=True,
            )
            self.assertEqual(published["status"], "published")
            store.run_status = "completed"
            replay = oauth_service.publish_queue_request(
                81,
                (),
                allow_schedule=False,
                allow_manual=True,
                expected_manual_trigger_source="auto_template",
                require_manual_parent=True,
            )
            self.assertEqual(replay["status"], "published")
            store.run_status = "running"
            with self.assertRaises(oauth_service.ServiceError):
                oauth_service.publish_queue_request(
                    81,
                    (),
                    allow_schedule=False,
                    allow_manual=True,
                    expected_manual_trigger_source="manual",
                )
            # The pre-existing backend publish helper defaults to the manual
            # source and therefore cannot be used to bypass the dedicated auto
            # bearer/route for an auto-template queue.
            with self.assertRaises(oauth_service.ServiceError):
                oauth_service.publish_queue_request(81)

    def test_exact_recovery_is_busy_while_current_account_publish_lock_is_held(self):
        class FakeStore:
            recover_calls = 0

            def get_manual_run(self, run_id, trigger_source):
                if trigger_source != "auto_template":
                    raise AssertionError("recovery must be source-gated")
                return {
                    "id": int(run_id),
                    "trigger_source": trigger_source,
                    "external_task_key": "x-auto-task-1",
                    "template_ref": "x-auto-template-1",
                    "template_version": 1,
                    "body_template_sha256": "a" * 64,
                    "run_date": "2026-08-11",
                    "source_date": "2026-08-10",
                    "account_ids": [202],
                    "material_ids": ["501"],
                    "body_template": BODY_TEMPLATE,
                    "status": "running",
                    "queues": [{"id": 81, "status": "publishing"}],
                }

            def recover_auto_template_run(self, run_id):
                self.recover_calls += 1
                item = self.get_manual_run(run_id, "auto_template")
                item["status"] = "stopped"
                return {"recovered": True, "run": item}

        store = FakeStore()
        guard = threading.Lock()
        guard.acquire()
        api = (service.XPostError, lambda _path: store, None)
        with mock.patch.object(
            oauth_service,
            "_x_posts_api",
            return_value=api,
        ), mock.patch.object(
            oauth_service,
            "find_account",
            return_value={"id": 202, "x_user_id": "x-202"},
        ), mock.patch.object(
            oauth_service,
            "account_lock",
            return_value=guard,
        ):
            busy = oauth_service.recover_post_auto_template_run_request({}, 17)
            self.assertTrue(busy["item"]["busy"])
            self.assertFalse(busy["item"]["recovered"])
            self.assertEqual(store.recover_calls, 0)

            guard.release()
            recovered = oauth_service.recover_post_auto_template_run_request(
                {}, 17
            )
            self.assertFalse(recovered["item"]["busy"])
            self.assertTrue(recovered["item"]["recovered"])
            self.assertEqual(recovered["item"]["run"]["status"], "stopped")
            self.assertEqual(store.recover_calls, 1)

    def test_delayed_auto_publish_rechecks_recovery_fence_inside_account_lock(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = service.XPostStore(Path(temporary.name) / "race.sqlite3")
        run = store.create_auto_template_run(
            "951",
            51,
            "task-race-951",
            "template-race",
            1,
            BODY_TEMPLATE,
            ACTOR,
        )
        plan = store.create_manual_plan(
            run["id"],
            [material_candidate(run, 51, "951")],
            "auto_template",
        )
        queue_id = plan["queues"][0]["id"]
        upstream_publish = mock.Mock()

        @contextlib.contextmanager
        def credentials(*_args, **_kwargs):
            store.recover_auto_template_run(run["id"])
            yield ({"id": 51}, "secret-token")

        api = (service.XPostError, lambda _path: store, upstream_publish)
        with mock.patch.object(
            oauth_service,
            "_x_posts_api",
            return_value=api,
        ), mock.patch.object(
            oauth_service,
            "publish_credentials",
            side_effect=credentials,
        ):
            with self.assertRaises(oauth_service.ServiceError) as fenced:
                oauth_service.publish_queue_request(
                    queue_id,
                    (),
                    allow_schedule=False,
                    allow_manual=True,
                    expected_manual_trigger_source="auto_template",
                    require_manual_parent=True,
                )
        self.assertEqual(
            fenced.exception.code,
            "x_post_auto_template_recovery_fenced",
        )
        upstream_publish.assert_not_called()
        readback = store.get_manual_run(run["id"], "auto_template")
        self.assertEqual(readback["status"], "stopped")
        self.assertEqual(readback["queues"][0]["status"], "failed")

    def test_manual_source_gate_preserves_daily_catchup_and_schedule_publish(self):
        class FakeStore:
            parent_field = "run_id"

            def get_queue(self, queue_id):
                result = {
                    "id": int(queue_id),
                    "run_id": None,
                    "catchup_run_id": None,
                    "schedule_run_id": None,
                    "manual_run_id": None,
                    "account_id": 202,
                }
                result[self.parent_field] = 7
                return result

            def get_manual_run(self, *_args):
                raise AssertionError("non-manual parents must not query a manual run")

            def reserve_log(self, _queue_id):
                return {
                    "id": 91,
                    "status": "published",
                    "short_url": "https://example.test/short",
                    "x_post_id": "12345",
                    "x_post_url": "https://x.com/account/status/12345",
                }

        store = FakeStore()
        api = (service.XPostError, lambda _path: store, None)
        with mock.patch.object(oauth_service, "_x_posts_api", return_value=api):
            for parent_field in ("run_id", "catchup_run_id", "schedule_run_id"):
                with self.subTest(parent_field=parent_field):
                    store.parent_field = parent_field
                    published = oauth_service.publish_queue_request(
                        81,
                        [202],
                        allow_manual=True,
                        expected_manual_trigger_source="manual",
                    )
                    self.assertEqual(published["status"], "published")

            store.parent_field = "run_id"
            with self.assertRaises(oauth_service.ServiceError):
                oauth_service.publish_queue_request(
                    81,
                    [202],
                    allow_manual=True,
                    expected_manual_trigger_source="auto_template",
                    require_manual_parent=True,
                )

    def test_auto_routes_require_the_distinct_auto_bearer(self):
        original = (
            oauth_service.INTERNAL_TOKEN,
            oauth_service.DAILY_INTERNAL_TOKEN,
            oauth_service.AUTO_INTERNAL_TOKEN,
        )
        oauth_service.INTERNAL_TOKEN = "backend-secret"
        oauth_service.DAILY_INTERNAL_TOKEN = "daily-secret"
        oauth_service.AUTO_INTERNAL_TOKEN = "auto-secret"

        def handler(
            token,
            address="127.0.0.1",
            path="/internal/posts/auto-template/runs/17/recover",
        ):
            item = object.__new__(oauth_service.Handler)
            item.client_address = (address, 12345)
            item.headers = {
                "Authorization": "Bearer " + token,
                "Content-Length": "2",
                "Content-Type": "application/json",
            }
            item.path = path
            item.rfile = io.BytesIO(b"{}")
            item.sent = []
            item.send_json = lambda status, payload: item.sent.append(
                (status, payload)
            )
            return item

        try:
            backend = handler("backend-secret")
            self.assertEqual(backend.require_internal(allow_auto=True), "backend")
            # do_POST applies this explicit second gate for every auto route.
            self.assertNotEqual(backend.internal_role(), "auto")

            daily = handler("daily-secret")
            self.assertEqual(daily.require_internal(allow_auto=True), "")
            self.assertEqual(daily.sent[0][0], 403)

            auto = handler("auto-secret")
            self.assertEqual(auto.require_internal(allow_auto=True), "auto")
            self.assertEqual(auto.sent, [])
            self.assertEqual(auto.require_internal(), "")
            self.assertEqual(auto.sent[0][0], 403)

            remote = handler("auto-secret", "192.0.2.10")
            self.assertEqual(remote.internal_role(), "")

            for denied_token in ("backend-secret", "daily-secret"):
                denied = handler(denied_token)
                denied.do_POST()
                self.assertEqual(denied.sent[0][0], 403)
            allowed = handler("auto-secret")
            with mock.patch.object(
                oauth_service,
                "recover_post_auto_template_run_request",
                return_value={
                    "item": {"busy": False, "recovered": False, "run": {}}
                },
            ) as recover:
                allowed.do_POST()
            self.assertEqual(allowed.sent[0][0], 200)
            recover.assert_called_once_with({}, "17")
        finally:
            (
                oauth_service.INTERNAL_TOKEN,
                oauth_service.DAILY_INTERNAL_TOKEN,
                oauth_service.AUTO_INTERNAL_TOKEN,
            ) = original


if __name__ == "__main__":
    unittest.main()
