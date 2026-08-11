#!/usr/bin/env python3
"""Contract tests for the independent X automatic publishing ledger."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_auto_posts.core import (  # noqa: E402
    AuditActor,
    XPostAutoStore,
    XAutoPostStoreError,
    ensure_storage,
)
UTC = timezone.utc


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class AutoPostStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "auto" / "x-auto.sqlite3"
        self.clock = MutableClock(datetime(2026, 8, 5, 0, 0, tzinfo=UTC))
        self.store = XPostAutoStore(self.db_path, now_fn=self.clock)
        self.actor = AuditActor("803", "operator")

    def create_template(self, name: str = "Template A"):
        return self.store.create_template(
            name=name,
            description="automatic X publishing",
            config={
                "account_ids": ["640"],
                "language": "en",
                "body_template": "{{drama_name}}\n{{desc}}\n{{url}}",
                "platform": 0,
                "metric_window_days": 7,
            },
            actor=self.actor,
            confirmation={"accepted": True, "version": "x-auto-v1"},
        )

    def create_run(self, template, key: str, trigger: str = "auto"):
        if trigger == "auto" and not self.store.get_template(template.id).enabled:
            template = self.store.set_template_enabled(
                template.id,
                True,
                expected_version=template.version,
                actor=self.actor,
            )
        return self.store.create_run(
            run_key=key,
            template_id=template.id,
            template_version=template.version,
            trigger_type=trigger,
            scheduled_at_utc="2026-08-05T00:00:00+00:00",
            shanghai_date="2026-08-05",
            publish_time="08:00",
            blacklist_snapshot={"active_count": 25, "sha256": "a" * 64},
            actor=self.actor,
        )

    def create_task(self, run, account_id: str = "640"):
        return self.store.create_task(
            run_id=run.id,
            account_id=account_id,
            language="en",
            account_snapshot_version=3,
            account_snapshot={"username": "account_%s" % account_id},
        )

    def test_schema_initialization_is_idempotent(self):
        ensure_storage(self.db_path)
        ensure_storage(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue(
            {
                "x_auto_template",
                "x_auto_template_version",
                "x_auto_random_plan",
                "x_auto_run",
                "x_auto_task",
                "x_auto_material_ledger",
                "x_auto_event",
                "x_auto_metric_generation",
                "x_auto_metric_daily",
                "x_auto_metric_active_pointer",
            }.issubset(names)
        )

    def test_template_requires_language_and_frozen_x_body(self):
        with self.assertRaises(XAutoPostStoreError) as missing_language:
            self.store.create_template(
                name="Missing language",
                config={
                    "body_template": "{{drama_name}} {{desc}}",
                    "platform": 0,
                },
            )
        self.assertEqual(
            missing_language.exception.code,
            "x_auto_language_required",
        )

        template = self.create_template()
        task = self.create_task(self.create_run(template, "frozen-body"))
        self.assertEqual(task.language, "en")
        self.assertEqual(
            task.body_template,
            "{{drama_name}}\n{{desc}}\n{{url}}",
        )
        self.assertEqual(len(task.body_sha256), 64)

    def test_execution_bridge_ids_are_unique_and_immutable(self):
        first = self.create_task(
            self.create_run(self.create_template("First bridge"), "bridge:first"),
            "640",
        )
        second = self.create_task(
            self.create_run(self.create_template("Second bridge"), "bridge:second"),
            "641",
        )
        first = self.store.transition_task(
            first.id,
            "ready",
            expected_statuses={"pending"},
            updates={
                "execution_run_id": 9001,
                "execution_queue_id": 9002,
                "execution_log_id": 9003,
            },
        )
        self.assertEqual(first.execution_run_id, 9001)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.transition_task(
                second.id,
                "ready",
                expected_statuses={"pending"},
                updates={"execution_run_id": 9001},
            )

    def test_template_version_copy_enable_and_confirmation(self):
        template = self.create_template()
        enabled = self.store.set_template_enabled(
            template.id,
            True,
            expected_version=1,
            actor=self.actor,
        )
        self.assertTrue(enabled.enabled)

        updated = self.store.update_template(
            template.id,
            expected_version=1,
            config={
                "account_ids": ["640", "641"],
                "language": "en",
                "body_template": "{{drama_name}}\n{{desc}}",
                "platform": 0,
            },
            actor=self.actor,
        )
        self.assertEqual(updated.version, 2)
        self.assertFalse(updated.enabled)
        self.assertFalse(updated.confirmed)
        with self.assertRaises(XAutoPostStoreError) as caught:
            self.store.set_template_enabled(
                template.id,
                True,
                expected_version=2,
                actor=self.actor,
            )
        self.assertEqual(caught.exception.code, "x_auto_template_version_unconfirmed")
        confirmed = self.store.confirm_template_version(
            template.id,
            2,
            confirmation={"accepted": True, "version": "x-auto-v2"},
            actor=self.actor,
        )
        self.assertTrue(confirmed.confirmed)

        copied = self.store.copy_template(template.id, actor=self.actor)
        self.assertNotEqual(copied.id, template.id)
        self.assertFalse(copied.enabled)
        self.assertFalse(copied.confirmed)

    def test_random_plan_is_immutable_and_idempotent(self):
        template = self.create_template()
        first = self.store.put_random_plan(
            template.id,
            template.version,
            "2026-08-05",
            ["15:30", "09:10", "15:30"],
        )
        self.assertEqual(first, ["09:10", "15:30"])
        self.assertEqual(
            self.store.put_random_plan(
                template.id,
                template.version,
                "2026-08-05",
                ["09:10", "15:30"],
            ),
            first,
        )
        with self.assertRaises(XAutoPostStoreError) as caught:
            self.store.put_random_plan(
                template.id,
                template.version,
                "2026-08-05",
                ["10:00"],
            )
        self.assertEqual(caught.exception.code, "x_auto_random_plan_conflict")

    def test_run_and_run_account_task_are_idempotent(self):
        template = self.create_template()
        run = self.create_run(template, "auto:1:2026-08-05:08:00")
        replay = self.create_run(template, "auto:1:2026-08-05:08:00")
        self.assertEqual(replay.id, run.id)
        task = self.create_task(run)
        replay_task = self.create_task(run)
        self.assertEqual(replay_task.id, task.id)
        self.assertEqual(len(self.store.list_runs(template_id=template.id)), 1)
        self.assertEqual(len(self.store.list_tasks(run_id=run.id)), 1)

    def test_auto_run_creation_rechecks_enabled_version_atomically(self):
        template = self.create_template()
        enabled = self.store.set_template_enabled(
            template.id,
            True,
            expected_version=template.version,
            actor=self.actor,
        )
        self.store.set_template_enabled(
            template.id,
            False,
            expected_version=enabled.version,
            actor=self.actor,
        )
        with self.assertRaises(XAutoPostStoreError) as caught:
            self.store.create_run(
                run_key="auto-disabled-race",
                template_id=template.id,
                template_version=template.version,
                trigger_type="auto",
                scheduled_at_utc="2026-08-05T00:00:00+00:00",
                shanghai_date="2026-08-05",
                publish_time="08:00",
                blacklist_snapshot={"sha256": "a" * 64},
                actor=self.actor,
            )
        self.assertEqual(
            caught.exception.code,
            "x_auto_template_not_enabled_for_slot",
        )

    def test_store_rejects_run_key_reuse_with_different_frozen_facts(self):
        template = self.create_template()
        run = self.create_run(template, "auto:1:2026-08-05:08:00:volatile")
        with self.assertRaises(XAutoPostStoreError) as caught:
            self.store.create_run(
                run_key=run.run_key,
                template_id=template.id,
                template_version=template.version,
                trigger_type="auto",
                scheduled_at_utc="2026-08-05T00:00:59+00:00",
                shanghai_date="2026-08-05",
                publish_time="08:00",
                blacklist_snapshot={
                    "active_count": 26,
                    "loaded_at_utc": "2026-08-05T00:00:59+00:00",
                    "sha256": "b" * 64,
                },
                actor=self.actor,
            )
        self.assertEqual(caught.exception.code, "x_auto_run_idempotency_conflict")

    def test_global_material_reservation_is_permanent_after_failure(self):
        first_template = self.create_template("First")
        second_template = self.create_template("Second")
        first_task = self.create_task(self.create_run(first_template, "run:first"), "640")
        second_task = self.create_task(self.create_run(second_template, "run:second"), "641")

        reservation = self.store.reserve_material(
            task_id=first_task.id,
            run_id=first_task.run_id,
            template_id=first_task.template_id,
            template_version=first_task.template_version,
            account_id=first_task.account_id,
            material_id="5391678",
            content_id="AbC123",
            series_code="10001",
            reserved_at_utc="2026-08-05T00:00:00+00:00",
            selection_snapshot={"drama_rank": 1, "material_rank": 1},
        )
        replay = self.store.reserve_material(
            task_id=first_task.id,
            material_id="5391678",
            content_id="AbC123",
        )
        self.assertEqual(replay, reservation)
        confirmed = self.store.confirm_material_reservation(
            first_task.id,
            7001,
        )
        self.assertEqual(confirmed.canonical_queue_id, 7001)
        self.assertTrue(confirmed.confirmed_at_utc)
        self.assertEqual(
            self.store.confirm_material_reservation(first_task.id, 7001),
            confirmed,
        )

        self.store.transition_task(
            first_task.id,
            "failed",
            expected_statuses={"reserved"},
            updates={"error_code": "prepare_failed", "error_message": "terminal"},
        )
        self.assertTrue(self.store.material_is_reserved("5391678"))
        with self.assertRaises(XAutoPostStoreError) as caught:
            self.store.reserve_material(
                task_id=second_task.id,
                material_id="5391678",
                content_id="OtherDrama",
            )
        self.assertEqual(caught.exception.code, "x_auto_material_already_reserved")

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM x_auto_material_ledger WHERE material_id='5391678'"
                )

    def test_provisional_reservation_can_be_released_before_x_queue_exists(self):
        template = self.create_template("Provisional")
        task = self.create_task(self.create_run(template, "provisional:first"))
        self.store.reserve_material(
            task_id=task.id,
            material_id="provisional-material",
            content_id="provisional-drama",
        )
        self.store.transition_task(
            task.id,
            "preparing",
            expected_statuses={"reserved"},
            updates={"execution_run_id": 8001},
        )
        released = self.store.release_provisional_material(
            task.id,
            reason="authoritative X bridge returned no queue rows",
        )
        self.assertEqual(released.status, "pending")
        self.assertEqual(released.material_id, "")
        self.assertIsNone(released.execution_run_id)
        self.assertFalse(self.store.material_is_reserved("provisional-material"))

        second = self.create_task(
            self.create_run(template, "provisional:second"),
            "641",
        )
        self.store.reserve_material(
            task_id=second.id,
            material_id="provisional-material",
            content_id="provisional-drama",
        )
        self.store.confirm_material_reservation(second.id, 8002)
        with self.assertRaises(XAutoPostStoreError) as permanent:
            self.store.release_provisional_material(
                second.id,
                reason="must not release confirmed queue",
            )
        self.assertEqual(
            permanent.exception.code,
            "x_auto_material_reservation_permanent",
        )

    def test_drama_cooldown_is_template_scoped_and_binary_exact(self):
        first_template = self.create_template("First")
        second_template = self.create_template("Second")
        task = self.create_task(self.create_run(first_template, "cooldown:first"))
        self.store.reserve_material(
            task_id=task.id,
            material_id="m-1",
            content_id="CaseSensitive",
            reserved_at_utc="2026-08-05T00:00:00+00:00",
        )
        self.assertEqual(
            self.store.cooldown_content_ids(
                first_template.id,
                ["CaseSensitive", "casesensitive", "Other"],
                "2026-08-04T00:00:00+00:00",
            ),
            {"CaseSensitive"},
        )
        self.assertEqual(
            self.store.cooldown_content_ids(
                second_template.id,
                ["CaseSensitive"],
                "2026-08-04T00:00:00+00:00",
            ),
            set(),
        )
        self.assertTrue(
            self.store.template_drama_in_cooldown(
                first_template.id,
                "CaseSensitive",
                1,
                now="2026-08-05T12:00:00+00:00",
            )
        )
        self.assertFalse(
            self.store.template_drama_in_cooldown(
                first_template.id,
                "CaseSensitive",
                0,
            )
        )

    def test_reservation_queries_claim_fence_and_transactional_cooldown(self):
        template = self.create_template()
        first_task = self.create_task(self.create_run(template, "reserve-contract:first"), "640")
        self.store.reserve_material(
            task_id=first_task.id,
            material_id="already-used",
            content_id="recent-drama",
            reserved_at_utc="2026-08-05T00:00:00+00:00",
        )
        self.store.transition_task(
            first_task.id,
            "failed",
            expected_statuses={"reserved"},
        )

        second_task = self.create_task(self.create_run(template, "reserve-contract:second"), "641")
        claim = self.store.claim_next_executable_task(
            worker_id="selector-contract",
            lease_seconds=60,
        )
        self.assertEqual(claim.task.id, second_task.id)
        with self.assertRaises(XAutoPostStoreError) as missing:
            self.store.reserve_material(
                task_id=second_task.id,
                material_id="candidate-missing-token",
                content_id="new-drama",
            )
        self.assertEqual(missing.exception.code, "x_auto_task_claim_required")
        with self.assertRaises(XAutoPostStoreError) as wrong:
            self.store.reserve_material(
                task_id=second_task.id,
                material_id="candidate-wrong-token",
                content_id="new-drama",
                claim_token="wrong-token",
            )
        self.assertEqual(wrong.exception.code, "x_auto_task_claim_conflict")
        with self.assertRaises(XAutoPostStoreError) as cooling:
            self.store.reserve_material(
                task_id=second_task.id,
                material_id="candidate-cooling",
                content_id="recent-drama",
                cooldown_since_utc="2026-08-04T00:00:00+00:00",
                claim_token=claim.reveal_claim_token(),
            )
        self.assertEqual(cooling.exception.code, "x_auto_drama_in_cooldown")

        reservation = self.store.reserve_material(
            task_id=second_task.id,
            material_id="selected-material",
            content_id="new-drama",
            selection={"drama_rank": 1, "material_rank": 2},
            cooldown_since_utc="2026-08-04T00:00:00+00:00",
            claim_token=claim.reveal_claim_token(),
        )
        self.assertEqual(dict(reservation)["material_id"], "selected-material")
        self.assertEqual(
            self.store.reserved_material_ids(
                ["missing", "selected-material", "selected-material"]
            ),
            {"selected-material"},
        )
        self.assertEqual(self.store.get_task_reservation(second_task.id), reservation)
        with self.assertRaises(XAutoPostStoreError) as missing_confirm_claim:
            self.store.confirm_material_reservation(second_task.id, 9100)
        self.assertEqual(
            missing_confirm_claim.exception.code,
            "x_auto_task_claim_conflict",
        )
        confirmed = self.store.confirm_material_reservation(
            second_task.id,
            9100,
            claim_token=claim.reveal_claim_token(),
        )
        self.assertEqual(confirmed.canonical_queue_id, 9100)
        self.assertEqual(
            self.store.renew_task_claim(
                second_task.id,
                claim.reveal_claim_token(),
                lease_seconds=60,
            ).status,
            "reserved",
        )

    def test_manual_pending_task_wins_and_account_remains_serial(self):
        template = self.create_template()
        auto_task = self.create_task(self.create_run(template, "auto-run", "auto"))
        manual_task = self.create_task(self.create_run(template, "manual-run", "manual"))
        claim = self.store.claim_next_executable_task(
            worker_id="selector-1",
            lease_seconds=60,
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim.task.id, manual_task.id)
        self.assertEqual(claim.claim_phase, "selection")
        self.store.reserve_material(
            task_id=manual_task.id,
            material_id="manual-material",
            content_id="manual-drama",
            claim_token=claim.reveal_claim_token(),
        )
        blocked = self.store.claim_next_executable_task(
            worker_id="selector-2",
            lease_seconds=60,
        )
        self.assertIsNone(blocked)
        self.store.release_task_claim(
            manual_task.id,
            claim.reveal_claim_token(),
        )
        prepare_claim = self.store.claim_next_executable_task(
            worker_id="prepare-1",
            lease_seconds=60,
        )
        self.assertIsNotNone(prepare_claim)
        self.assertEqual(prepare_claim.task.id, manual_task.id)
        self.assertEqual(prepare_claim.claim_phase, "prepare")
        self.assertNotEqual(prepare_claim.task.id, auto_task.id)

    def test_expired_publish_claim_recovers_as_unknown_reconcile_only(self):
        template = self.create_template()
        task = self.create_task(self.create_run(template, "recover-publish"))
        self.store.reserve_material(
            task_id=task.id,
            material_id="m-publish",
            content_id="d-publish",
        )
        self.store.transition_task(
            task.id,
            "ready",
            expected_statuses={"reserved"},
            updates={
                "execution_run_id": 101,
                "execution_queue_id": 201,
                "execution_log_id": 301,
            },
        )
        publish_claim = self.store.claim_next_executable_task(
            worker_id="publisher-1",
            lease_seconds=60,
        )
        self.assertEqual(publish_claim.claim_phase, "publish")
        self.assertEqual(publish_claim.task.status, "publishing")

        self.clock.value += timedelta(seconds=61)
        recovered = self.store.claim_next_executable_task(
            worker_id="reconciler-1",
            lease_seconds=60,
        )
        self.assertEqual(recovered.task.id, task.id)
        self.assertEqual(recovered.claimed_from_status, "publishing")
        self.assertEqual(recovered.claim_phase, "reconcile")
        self.assertEqual(recovered.task.status, "unknown")
        self.assertTrue(recovered.task.unknown_outcome)

    def test_publish_evidence_is_irreversible_and_reconcile_only(self):
        template = self.create_template()
        task = self.create_task(
            self.create_run(template, "publish-evidence", "manual")
        )
        self.store.reserve_material(
            task_id=task.id,
            material_id="m-evidence",
            content_id="d-evidence",
        )
        with self.assertRaises(XAutoPostStoreError) as unsafe_first_write:
            self.store.transition_task(
                task.id,
                "retry_wait",
                expected_statuses={"reserved"},
                updates={
                    "publish_id": "publish-evidence-1",
                    "claim_phase": "publish",
                },
            )
        self.assertEqual(
            unsafe_first_write.exception.code,
            "x_auto_publish_reconcile_required",
        )

        reconciler = self.store.transition_task(
            task.id,
            "reconciling",
            expected_statuses={"reserved"},
            updates={
                "publish_id": "publish-evidence-1",
                "claim_phase": "reconcile",
            },
        )
        with self.assertRaises(XAutoPostStoreError) as reinitialize:
            self.store.transition_task(
                task.id,
                "publishing",
                expected_statuses={reconciler.status},
            )
        self.assertEqual(
            reinitialize.exception.code,
            "x_auto_publish_reconcile_required",
        )
        with self.assertRaises(XAutoPostStoreError) as identity_change:
            self.store.transition_task(
                task.id,
                "reconciling",
                expected_statuses={reconciler.status},
                updates={"publish_id": "publish-evidence-2"},
            )
        self.assertEqual(
            identity_change.exception.code,
            "x_auto_task_identity_immutable",
        )

    def test_claim_renew_and_release_use_exact_token(self):
        template = self.create_template()
        task = self.create_task(self.create_run(template, "claim-renew"))
        claim = self.store.claim_next_executable_task(
            worker_id="selector",
            lease_seconds=60,
        )
        renewed = self.store.renew_task_claim(
            task.id,
            claim.reveal_claim_token(),
            lease_seconds=120,
        )
        self.assertEqual(renewed.status, "selecting")
        with self.assertRaises(XAutoPostStoreError):
            self.store.renew_task_claim(task.id, "wrong-token", lease_seconds=60)
        released = self.store.release_task_claim(
            task.id,
            claim.reveal_claim_token(),
            message="unit test",
        )
        self.assertEqual(released.status, "selecting")

    def test_metric_day_generations_keep_independent_active_pointers(self):
        day_one = self.store.record_metric_generation(
            platform=0,
            metric_date="2026-08-03",
            product="1479",
            rows=[
                {
                    "content_id": "drama-1",
                    "material_id": "material-1",
                    "spend": 10,
                    "af_revenue0": 5,
                }
            ],
            refreshed_at_utc="2026-08-05T00:00:00+00:00",
        )
        day_two = self.store.record_metric_generation(
            platform=0,
            metric_date="2026-08-04",
            product="1479",
            rows=[
                {
                    "content_id": "drama-1",
                    "material_id": "material-1",
                    "spend": 20,
                    "af_revenue0": 15,
                },
                {
                    "content_id": "drama-1",
                    "material_id": "material-2",
                    "spend": 0,
                    "af_revenue0": 0,
                },
            ],
            refreshed_at_utc="2026-08-05T00:01:00+00:00",
        )
        self.store.activate_metric_generation(day_one.id)
        self.store.activate_metric_generation(day_two.id)
        dates = ["2026-08-03", "2026-08-04"]
        self.assertEqual(
            self.store.ready_metric_dates(0, metric_dates=dates),
            set(dates),
        )
        rows = list(
            self.store.iter_ready_metric_rows(
                0,
                metric_dates=dates,
                content_ids=["drama-1"],
            )
        )
        self.assertEqual(len(rows), 3)
        spend = sum(Decimal(str(row["spend"])) for row in rows)
        revenue = sum(Decimal(str(row["af_revenue0"])) for row in rows)
        self.assertEqual(spend, Decimal("30"))
        self.assertEqual(
            revenue / spend * Decimal("100"),
            Decimal("66.66666666666666666666666667"),
        )

        with self.assertRaises(XAutoPostStoreError) as caught:
            list(
                self.store.iter_ready_metric_rows(
                    0,
                    ["2026-08-02", "2026-08-03"],
                )
            )
        self.assertEqual(caught.exception.code, "x_auto_metric_window_incomplete")

    def test_metric_batch_write_is_streamed_and_atomic(self):
        generation = self.store.create_metric_generation(
            generation_key="streamed:valid",
            platform=0,
            metric_date="2026-08-04",
            product="1479",
        )

        def valid_rows():
            for index in range(1_005):
                yield {
                    "content_id": "drama-%d" % (index % 7),
                    "material_id": "material-%04d" % index,
                    "spend": index,
                    "af_revenue0": index / 2,
                }

        self.assertEqual(self.store.upsert_metric_daily(generation.id, valid_rows()), 1_005)

        invalid_generation = self.store.create_metric_generation(
            generation_key="streamed:rollback",
            platform=0,
            metric_date="2026-08-03",
            product="1479",
        )

        def invalid_rows():
            for index in range(1_000):
                yield {
                    "content_id": "drama",
                    "material_id": "rollback-%04d" % index,
                    "spend": 1,
                    "af_revenue0": 1,
                }
            yield {"content_id": "drama", "material_id": "invalid", "spend": -1}

        with self.assertRaises(XAutoPostStoreError) as caught:
            self.store.upsert_metric_daily(invalid_generation.id, invalid_rows())
        self.assertEqual(caught.exception.code, "x_auto_metric_row_invalid")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM x_auto_metric_daily WHERE generation_id=?",
                (invalid_generation.id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_metric_precision_is_decimal_and_old_generations_are_bounded(self):
        latest_spend = ""
        for index in range(5):
            latest_spend = "0.10000000000000000%d" % index
            generation = self.store.record_metric_generation(
                platform=0,
                metric_date="2026-08-04",
                product="Dramawave",
                rows=[
                    {
                        "content_id": "precision-drama",
                        "material_id": "9001",
                        "spend": latest_spend,
                        "af_revenue0": "0.033333333333333333",
                    }
                ],
                refreshed_at_utc=(
                    "2026-08-05T00:00:0%d+00:00" % index
                ),
            )
            self.store.activate_metric_generation(generation.id)

        rows = list(
            self.store.iter_ready_metric_rows(
                0,
                metric_dates=["2026-08-04"],
                content_ids=["precision-drama"],
                product="Dramawave",
            )
        )
        self.assertEqual(rows[0]["spend"], latest_spend)
        self.assertEqual(rows[0]["af_revenue0"], "0.033333333333333333")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            generation_count = conn.execute(
                """
                SELECT COUNT(*) FROM x_auto_metric_generation
                WHERE platform=0 AND metric_date='2026-08-04' AND product='Dramawave'
                """
            ).fetchone()[0]
            daily_count = conn.execute(
                "SELECT COUNT(*) FROM x_auto_metric_daily"
            ).fetchone()[0]
        self.assertEqual(generation_count, 3)
        self.assertEqual(daily_count, 3)

    def test_events_cover_template_run_task_and_reservation(self):
        template = self.create_template()
        run = self.create_run(template, "events")
        task = self.create_task(run)
        self.store.reserve_material(
            task_id=task.id,
            material_id="event-material",
            content_id="event-drama",
        )
        events = self.store.list_events(run_id=run.id)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["run_created", "task_created", "material_reserved"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
