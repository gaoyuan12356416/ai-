#!/usr/bin/env python3
"""Offline owner/batch/reservation/copy contracts. No upstream requests."""
from __future__ import annotations

import copy
import contextlib
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.service import XPostError, XPostStore, build_post_text
from features.x_posts import service as post_service
from features.x_video_publish.core import PersonalStore, read_personal_task, assert_personal_run_publishable
from features.x_video_publish.templates import normalize_caption, render_caption, weighted_length
from scripts.test_x_post_priority_manual_store import material_candidate
from scripts.test_x_posts import ScriptedHttpClient, response, valid_probe_payload
from scripts.x_post_manual_runner import _manual_identity
from features.x_accounts import oauth_service
from features.x_video_publish.errors import PublishingError

ACTOR = {"tenant_key": "tenant-a", "user_id": "operator-a", "name": "Operator"}
OTHER = {"tenant_key": "tenant-b", "user_id": "operator-a", "name": "Other"}
NOW = datetime(2030, 1, 2, 1, 0, tzinfo=timezone.utc)


class PersonalPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "posts.sqlite3"
        self.store = PersonalStore(self.path)
        self.legacy = XPostStore(self.path)
        self.accounts = {str(i): {"id": i, "owner_tenant_key": ACTOR["tenant_key"], "owner_user_id": ACTOR["user_id"], "status": "active", "publish_approved": True, "publish_eligible": True, "drama_language": "en", "username": "account%s" % i} for i in (1, 2, 3)}
        self.materials = {str(i): {"material_id": str(i), "drama_name": "Drama", "drama_description": "An interesting story", "language": "en"} for i in (501, 502, 503)}

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return [{"account_id": "1", "material_id": "501", "caption": "First"}, {"account_id": "2", "material_id": "501", "caption": "Second"}, {"account_id": "1", "material_id": "502", "caption": "Third"}]

    def preview(self, rows=None, **kwargs):
        return self.store.preview(ACTOR, {"source": kwargs.pop("source", "manual"), "rows": rows or self.rows()}, kwargs.pop("accounts", self.accounts), self.materials, now=kwargs.pop("now", NOW))

    def commit(self, preview=None, **kwargs):
        preview = preview or self.preview()
        return self.store.commit(ACTOR, preview["preview_token"], kwargs.pop("key", "core-test-001"), self.accounts, self.materials, now=kwargs.pop("now", NOW), **kwargs)

    def freeze(self, task):
        run = self.legacy.get_manual_run(task["manual_run_id"])
        candidate = material_candidate(run, int(task["account_id"]), task["material_id"])
        return self.legacy.create_manual_plan(run["id"], [candidate])

    def test_three_explicit_pairs_create_three_children_two_anchors(self):
        batch = self.commit()
        self.assertEqual(3, len(batch["tasks"]))
        self.assertEqual(["First", "Second", "Third"], [r["caption"] for r in batch["tasks"]])
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM x_post_manual_material_reservation WHERE state='active'").fetchone()[0])
            self.assertEqual(3, conn.execute("SELECT COUNT(*) FROM x_post_manual_run WHERE copy_contract_version=2").fetchone()[0])
        for task in batch["tasks"]:
            run = self.legacy.get_manual_run(task["manual_run_id"])
            self.assertEqual(1, len(run["account_ids"]))
            self.assertEqual(1, len(run["material_ids"]))

    def test_exact_response_loss_replay_survives_expiry_and_day_rollover(self):
        preview = self.preview()
        first = self.commit(preview)
        second = self.commit(preview, now=NOW + timedelta(days=3))
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["created"])
        with self.assertRaisesRegex(XPostError, "已生成批次"):
            self.commit(preview, key="another-key")

    def test_other_owner_cannot_read_preview_batch_operation_or_draft(self):
        preview = self.preview()
        batch = self.commit(preview)
        for method, args in ((self.store.get_preview, (preview["preview_token"],)), (self.store.get_batch, (batch["id"],))):
            with self.assertRaises(XPostError):
                method(OTHER, *args)
        self.assertIsNone(self.store.operation(OTHER, "core-test-001"))
        self.store.save_draft(ACTOR, {"data": {"caption": "private"}})
        self.assertIsNone(self.store.get_draft(OTHER)["data"])

    def test_owner_account_and_language_are_authoritative(self):
        changed = copy.deepcopy(self.accounts)
        changed["1"]["owner_tenant_key"] = "someone-else"
        self.assertEqual(1, self.preview(accounts=changed)["valid_count"])
        preview = self.preview()
        changed = copy.deepcopy(self.accounts)
        changed["1"]["drama_language"] = "de"
        with self.assertRaisesRegex(XPostError, "语言"):
            self.store.commit(ACTOR, preview["preview_token"], "language-key", changed, self.materials, now=NOW)

    def test_default_version_drift_blocks_only_unedited_caption(self):
        account = self.accounts["1"]
        self.store.save_settings(ACTOR, 1, {"default_copy": "Default", "version": 0}, account)
        default = self.preview([{"account_id": 1, "material_id": "501", "caption": ""}])
        edited = self.preview([{"account_id": 1, "material_id": "502", "caption": "My edited caption"}])
        self.store.save_settings(ACTOR, 1, {"default_copy": "New default", "version": 1}, account)
        with self.assertRaisesRegex(XPostError, "默认文案"):
            self.commit(default)
        self.assertEqual("My edited caption", self.commit(edited)["tasks"][0]["caption"])

    def test_draft_roundtrip_and_revision_conflict(self):
        data = {"materials": ["501"], "pairs": {"501:1": "First"}, "times": {"501": "2030-01-03T12:00"}}
        self.store.save_draft(ACTOR, {"version": 0, "data": data})
        self.assertEqual(data, PersonalStore(self.path).get_draft(ACTOR)["data"])
        with self.assertRaises(XPostError):
            self.store.save_draft(ACTOR, {"version": 0, "data": {}})

    def test_invalid_first_duplicate_preserves_first_valid_row(self):
        rows = [{"account_id": 1, "material_id": "501", "caption": "{bad}"}, {"account_id": 1, "material_id": "501", "caption": "Good"}, {"account_id": 1, "material_id": "501", "caption": "Duplicate"}]
        preview = self.preview(rows, source="excel")
        self.assertEqual(1, preview["valid_count"])
        self.assertEqual("x_video_duplicate_row", preview["rows"][2]["error_code"])
        self.assertEqual("Good", self.commit(preview)["tasks"][0]["caption"])

    def test_manual_same_material_time_and_excel_independent_time(self):
        rows = self.rows()[:2]
        rows[0]["scheduled_at"] = "2030-01-02 10:00"
        rows[1]["scheduled_at"] = "2030-01-02 11:00"
        self.assertEqual(1, self.preview(rows)["valid_count"])
        preview = self.preview(rows, source="excel")
        self.assertEqual(2, preview["valid_count"])
        batch = self.commit(preview)
        self.assertEqual(["2030-01-02T02:00:00Z", "2030-01-02T03:00:00Z"], [t["scheduled_at"] for t in batch["tasks"]])
        with mock.patch("features.x_posts.service.utc_now", return_value="2030-01-02T01:59:59Z"):
            self.assertFalse(self.legacy.claim_manual_run()["found"])
        with mock.patch("features.x_posts.service.utc_now", return_value="2030-01-02T02:00:00Z"):
            self.assertEqual(batch["tasks"][0]["manual_run_id"], self.legacy.claim_manual_run()["run"]["id"])

    def test_confirmation_rechecks_time_and_ttl(self):
        preview = self.preview([dict(self.rows()[0], scheduled_at="2030-01-02 09:02")])
        with self.assertRaisesRegex(XPostError, "发布时间已过"):
            self.commit(preview, now=NOW + timedelta(minutes=3))
        preview = self.preview()
        with self.assertRaisesRegex(XPostError, "已过期"):
            self.commit(preview, now=NOW + timedelta(minutes=11))

    def test_browser_iso_milliseconds_and_excel_seconds_reach_due_child(self):
        actual_day = datetime(2026, 10, 22, tzinfo=timezone.utc)
        examples = (
            ("2026-10-22T01:02:00.000Z", "2026-10-22T01:02:00Z"),
            ("2026-10-22T01:02:37.000Z", "2026-10-22T01:02:37Z"),
            ("2026-10-22 09:02:59", "2026-10-22T01:02:59Z"),
        )
        for index, (input_value, normalized) in enumerate(examples):
            with self.subTest(value=input_value):
                preview = self.preview([dict(self.rows()[0], scheduled_at=input_value)], source="excel", now=actual_day)
                self.assertEqual(1, preview["valid_count"])
                self.assertEqual(normalized, preview["rows"][0]["scheduled_at"])
                batch = self.commit(preview, key="iso-seconds-%d" % index, now=actual_day)
                run_id = batch["tasks"][0]["manual_run_id"]
                run = self.legacy.get_manual_run(run_id)
                self.assertEqual(normalized, run["scheduled_at"])
                self.assertEqual(normalized, _manual_identity(run)["scheduled_at"])
                due = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
                before = (due - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                with mock.patch("features.x_posts.service.utc_now", return_value=before):
                    self.assertFalse(self.legacy.claim_manual_run()["found"])
                with mock.patch("features.x_posts.service.utc_now", return_value=normalized):
                    self.assertEqual(run_id, self.legacy.claim_manual_run()["run"]["id"])
                self.legacy.record_manual_failure(run_id, "offline_test_complete", "No platform request")

    def test_nonzero_milliseconds_rejected_instead_of_early_rounding(self):
        preview = self.preview([dict(self.rows()[0], scheduled_at="2030-01-02T01:00:00.001Z")])
        self.assertEqual(0, preview["valid_count"])
        self.assertEqual("x_video_schedule_invalid", preview["rows"][0]["error_code"])

    def test_legacy_schedule_stays_minute_precision(self):
        with self.assertRaisesRegex(XPostError, "分钟"):
            self.legacy.create_manual_run(["501"], [1], "legacy-seconds-denied", ACTOR,
                publish_mode="scheduled", scheduled_at="2030-01-03T01:02:37Z")
        legacy = {"id": 999, "trigger_source": "manual", "copy_contract_version": 1,
            "account_ids": [1], "material_ids": ["501"], "expected_count": 1,
            "publish_mode": "scheduled", "scheduled_at": "2030-01-03T01:02:37Z", "scheduled_timezone": "Asia/Shanghai",
            "run_date": "2030-01-03", "source_date": "2030-01-02", "status": "queued", "body_template": "{{drama_name}} {{desc}}", "queues": []}
        with self.assertRaises(Exception) as invalid:
            _manual_identity(legacy)
        self.assertEqual("x_post_manual_invalid_response", invalid.exception.code)

    def test_anchor_blocks_old_manual_and_pool_admission(self):
        self.commit()
        with self.assertRaises(XPostError):
            self.legacy.create_manual_run(["501"], [3], "legacy-conflict", ACTOR)
        with contextlib.closing(self.store.connect()) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO x_post_material_pool(material_id,material_key,status,created_at,updated_at) VALUES('501','501','unpublished','2030-01-02T01:00:00Z','2030-01-02T01:00:00Z')")

    def test_anchor_failure_keeps_future_sibling_reservation(self):
        batch = self.commit()
        first, second = batch["tasks"][:2]
        self.legacy.record_manual_failure(first["manual_run_id"], "failed_media", "bad media")
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual("active", conn.execute("SELECT state FROM x_post_manual_material_reservation WHERE material_key='501'").fetchone()[0])
        self.freeze(second)
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual("consumed", conn.execute("SELECT state FROM x_post_manual_material_reservation WHERE material_key='501'").fetchone()[0])

    def test_all_siblings_fail_releases_reservation_once(self):
        batch = self.commit()
        for task in batch["tasks"][:2]:
            self.legacy.record_manual_failure(task["manual_run_id"], "bad_media", "bad")
        with contextlib.closing(self.store.connect()) as conn:
            row = conn.execute("SELECT state,release_reason FROM x_post_manual_material_reservation WHERE material_key='501'").fetchone()
            self.assertEqual(("released", "x_video_batch_preflight_failed"), tuple(row))

    def test_same_batch_sibling_can_freeze_but_other_batch_cannot_borrow(self):
        batch = self.commit()
        one = self.freeze(batch["tasks"][0])
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual("active", conn.execute("SELECT state FROM x_post_manual_material_reservation WHERE material_key='501'").fetchone()[0])
        two = self.freeze(batch["tasks"][1])
        self.assertEqual(1, len(one["queues"]))
        self.assertEqual(1, len(two["queues"]))
        with contextlib.closing(self.store.connect()) as conn:
            self.assertEqual("consumed", conn.execute("SELECT state FROM x_post_manual_material_reservation WHERE material_key='501'").fetchone()[0])
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM x_post_queue WHERE material_key='501' AND account_drama_language_frozen=1 AND copy_contract_version=2").fetchone()[0])

    def test_direct_sql_unrelated_child_cannot_borrow_anchor(self):
        batch = self.commit()
        run = self.freeze(batch["tasks"][0])
        with mock.patch("features.x_posts.service.utc_now", return_value="2030-01-02T01:00:00Z"):
            unrelated = self.legacy.create_manual_run(["503"], [3], "unrelated-run", ACTOR)
        with contextlib.closing(self.store.connect()) as conn:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(x_post_queue)") if r[1] != "id"]
            expressions = {"idempotency_key": "'tampered-unrelated-queue'", "manual_run_id": str(unrelated["id"]), "account_id": "3", "copy_contract_version": "1"}
            sql = "INSERT INTO x_post_queue(" + ",".join(columns) + ") SELECT " + ",".join(expressions.get(c, c) for c in columns) + " FROM x_post_queue WHERE id=?"
            with self.assertRaisesRegex(sqlite3.IntegrityError, "reserved by manual run"):
                conn.execute(sql, (run["queues"][0]["id"],))

    def test_frozen_mapping_and_copy_version_cannot_change(self):
        batch = self.commit()
        with contextlib.closing(self.store.connect()) as conn:
            for sql in ("UPDATE x_video_publish_task SET account_id=3", "DELETE FROM x_video_publish_task", "UPDATE x_video_publish_batch SET user_id='other'", "UPDATE x_post_manual_run SET copy_contract_version=1"):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)

    def test_unknown_holds_siblings_and_other_owner_cannot_bypass(self):
        batch = self.commit()
        run = self.freeze(batch["tasks"][0])
        queue_id = run["queues"][0]["id"]
        log = self.legacy.reserve_log(queue_id)
        self.legacy.mark_failed(log["id"], "x_post_unknown_outcome", "ambiguous", unknown_outcome=True)
        self.assertEqual("needs_review", self.store.get_batch(ACTOR, batch["id"])["status"])
        with self.assertRaises(XPostError):
            self.store.assert_run_publishable(batch["tasks"][1]["manual_run_id"], self.accounts["2"])
        self.assertFalse(self.legacy.claim_manual_run()["found"])
        preview = self.preview([dict(self.rows()[0], material_id="503")])
        self.assertEqual("x_post_unknown_outcome", preview["rows"][0]["error_code"])

    def test_explicit_stopped_parent_is_not_presented_as_queued(self):
        batch = self.commit()
        with contextlib.closing(self.store.connect()) as conn:
            conn.execute("UPDATE x_post_manual_run SET status='stopped',error_code='x_rate_limit' WHERE id=?", (batch["tasks"][0]["manual_run_id"],))
            conn.commit()
        self.assertEqual("stopped", self.store.get_batch(ACTOR, batch["id"])["status"])
        self.assertFalse(self.legacy.claim_manual_run()["found"])

    def test_distinct_next_batch_allowed_while_first_waits(self):
        self.commit(self.preview(self.rows()[:1]))
        next_batch = self.commit(self.preview([dict(self.rows()[0], material_id="502")]), key="next-batch-key")
        self.assertTrue(next_batch["created"])

    def test_external_transaction_rolls_back_children_and_token(self):
        preview = self.preview()
        conn = self.store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.commit(preview, conn=conn)
            conn.rollback()
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM x_video_publish_batch").fetchone()[0])
        finally:
            conn.close()
        self.assertTrue(self.commit(preview)["created"])

    def test_first_commit_requires_current_trusted_data_but_replay_does_not(self):
        preview = self.preview()
        with self.assertRaisesRegex(XPostError, "可信账号"):
            self.store.commit(ACTOR, preview["preview_token"], "core-test-001", now=NOW)
        batch = self.commit(preview)
        replay = self.store.commit(ACTOR, preview["preview_token"], "core-test-001", now=NOW + timedelta(days=2))
        self.assertEqual(batch["id"], replay["id"])

    def test_manual_partial_invalid_is_atomic_excel_can_submit_valid_rows(self):
        rows = [self.rows()[0], dict(self.rows()[1], caption="{bad}")]
        with self.assertRaisesRegex(XPostError, "无效"):
            self.commit(self.preview(rows))
        self.assertEqual(0, self.store.list_batches(ACTOR)["total"])
        batch = self.commit(self.preview(rows, source="excel"))
        self.assertEqual(1, len(batch["tasks"]))

    def test_sql_tamper_cannot_unseal_or_append_mapping(self):
        preview = self.preview()
        batch = self.commit(preview)
        with contextlib.closing(self.store.connect()) as conn:
            for sql in (
                "UPDATE x_video_publish_preview SET rows_json='[]'",
                "UPDATE x_video_publish_preview SET batch_id=NULL",
                "UPDATE x_post_manual_run SET actor_tenant_key='other'",
                "INSERT INTO x_video_publish_task(batch_id,manual_run_id,account_id,material_id,drama_language,caption,caption_source,config_version,scheduled_at,processing,row_number,source_snapshot_json,account_snapshot_json,created_at) SELECT batch_id,manual_run_id,account_id,material_id,drama_language,caption,caption_source,config_version,scheduled_at,processing,row_number,source_snapshot_json,account_snapshot_json,created_at FROM x_video_publish_task LIMIT 1",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)

    def test_legacy_manual_api_cannot_opt_into_personal_copy(self):
        with self.assertRaises(TypeError):
            self.legacy.create_manual_run(["501"], [1], "bad-contract", ACTOR, copy_contract_version=2)
        run = self.legacy.create_manual_run(["501"], [1], "legacy-normal", ACTOR)
        self.assertEqual(1, run["copy_contract_version"])
        candidate = material_candidate(run, 1, "501")
        candidate.update(body_template="Free text", copy_contract_version=2)
        plan = self.legacy.create_manual_plan(run["id"], [candidate])
        queue = self.legacy.get_queue(plan["queues"][0]["id"])
        self.assertEqual(1, queue["copy_contract_version"])
        self.assertEqual(run["body_template"], queue["body_template"])

    def test_delayed_macro_source_drift_fails_before_queue_freeze(self):
        batch = self.commit(self.preview([dict(self.rows()[0], caption="Watch {name}")]))
        run = self.legacy.get_manual_run(batch["tasks"][0]["manual_run_id"])
        candidate = material_candidate(run, 1, "501")
        with self.assertRaisesRegex(XPostError, "文案数据已变更"):
            self.legacy.create_manual_plan(run["id"], [candidate])
        candidate["drama_name"] = "Drama"
        self.assertEqual(1, len(self.legacy.create_manual_plan(run["id"], [candidate])["queues"]))

    def test_final_publisher_sends_frozen_personal_copy_and_log_short_link(self):
        batch = self.commit(self.preview([dict(self.rows()[0], caption="My exact text {url}")]))
        run = self.legacy.get_manual_run(batch["tasks"][0]["manual_run_id"])
        candidate = material_candidate(run, 1, "501")
        candidate.update(preflight_sha256=hashlib.sha256(b"video").hexdigest(), preflight_size=5)
        queue_id = self.legacy.create_manual_plan(run["id"], [candidate])["queues"][0]["id"]
        client = ScriptedHttpClient([
            post_service.HttpResponse(200, {"content-type": "video/mp4", "content-length": "5"}, body=b"video"),
            response(200, {"data": {"id": "media1"}}),
            response(200, {"data": {"expires_at": 1}}),
            response(200, {"data": {"id": "media1"}}),
            response(201, {"data": {"id": "190002", "text": "ok"}}),
        ])
        with mock.patch.object(post_service, "probe_media", return_value=valid_probe_payload(duration="30.0")), mock.patch.dict("os.environ", {"X_POST_ATTRIBUTION_V2_ENABLED": "0"}):
            result = post_service.publish_canary(
                db_path=self.path, queue_id=queue_id, account={"id": 1, "username": "account1"},
                access_token="isolated-test-token", public_root=Path(self.tmp.name) / "s2l",
                short_base_url="https://gy.g2flow.com/s2l", allowed_media_hosts=["media.example.test"],
                http_client=client, sleeper=lambda seconds: None, timeout=1,
            )
        self.assertEqual("published", result["status"])
        sent = json.loads(client.requests[-1]["body"].decode())
        self.assertEqual("My exact text https://gy.g2flow.com/s2l/%s.html" % result["log_id"], sent["text"])
        self.assertNotIn("preview.invalid", sent["text"])
        with mock.patch.object(post_service, "UrllibHttpClient", side_effect=AssertionError("network forbidden")):
            repeat = post_service.publish_canary(
                db_path=self.path, queue_id=queue_id, account={"id": 1, "username": "account1"},
                access_token="isolated-test-token", public_root=Path(self.tmp.name) / "s2l",
                short_base_url="https://gy.g2flow.com/s2l", allowed_media_hosts=[],
            )
        self.assertEqual(result["post_id"], repeat["post_id"])

    def test_row_count_limit_and_long_material_ids_are_exact(self):
        with self.assertRaises(XPostError):
            self.preview(self.rows() * 67)
        exact = "123456789012345678901234567890"
        self.materials[exact] = dict(self.materials["501"], material_id=exact)
        preview = self.preview([dict(self.rows()[0], material_id=exact)])
        self.assertEqual(exact, preview["rows"][0]["material_id"])
        batch = self.commit(preview)
        self.assertEqual(exact, batch["tasks"][0]["material_id"])

    def test_sidecar_guard_resolves_frozen_owner_then_current_account(self):
        batch = self.commit(self.preview(self.rows()[:1]))
        run_id = batch["tasks"][0]["manual_run_id"]
        with mock.patch.object(oauth_service, "POST_DB_PATH", self.path), mock.patch("features.x_video_publish.transport.broker", return_value={"actor": ACTOR}) as broker, mock.patch.object(oauth_service, "find_scoped_account_row", return_value=self.accounts["1"]) as scoped, mock.patch.object(oauth_service, "row_to_item", side_effect=lambda row: row):
            task = oauth_service._personal_run_guard(run_id, self.accounts["1"])
        self.assertEqual(ACTOR["user_id"], task["user_id"])
        self.assertEqual({"tenant_key": ACTOR["tenant_key"], "user_id": ACTOR["user_id"]}, broker.call_args.args[1])
        scoped.assert_called_once_with(1, ACTOR, "mine")

    def test_sidecar_guard_rejects_revoked_permission_and_transferred_owner(self):
        batch = self.commit(self.preview(self.rows()[:1]))
        run_id = batch["tasks"][0]["manual_run_id"]
        with mock.patch.object(oauth_service, "POST_DB_PATH", self.path), mock.patch("features.x_video_publish.transport.broker", side_effect=PublishingError("navigation_denied", "permission revoked", 403)):
            with self.assertRaises(oauth_service.ServiceError) as denied:
                oauth_service._personal_run_guard(run_id)
            self.assertEqual(403, denied.exception.status)
        transferred = dict(self.accounts["1"], owner_user_id="new-owner")
        with mock.patch.object(oauth_service, "POST_DB_PATH", self.path), mock.patch("features.x_video_publish.transport.broker", return_value={"actor": ACTOR}), mock.patch.object(oauth_service, "find_scoped_account_row", return_value=transferred), mock.patch.object(oauth_service, "row_to_item", side_effect=lambda row: row):
            with self.assertRaises((oauth_service.ServiceError, XPostError)):
                oauth_service._personal_run_guard(run_id)

    def test_sidecar_final_credentials_snapshot_is_rechecked(self):
        batch = self.commit(self.preview(self.rows()[:1]))
        run_id = batch["tasks"][0]["manual_run_id"]
        with mock.patch.object(oauth_service, "POST_DB_PATH", self.path), mock.patch("features.x_video_publish.transport.broker", return_value={"actor": ACTOR}), mock.patch.object(oauth_service, "find_scoped_account_row", return_value=self.accounts["1"]), mock.patch.object(oauth_service, "row_to_item", side_effect=lambda row: row):
            with self.assertRaises((oauth_service.ServiceError, XPostError)):
                oauth_service._personal_run_guard(run_id, dict(self.accounts["1"], drama_language="de"))

    def test_legacy_manual_guard_does_not_add_personal_identity_dependency(self):
        run = self.legacy.create_manual_run(["501"], [1], "legacy-guard", ACTOR)
        with mock.patch.object(oauth_service, "POST_DB_PATH", self.path), mock.patch("features.x_video_publish.transport.broker", side_effect=AssertionError("legacy must not call personal broker")):
            self.assertIsNone(oauth_service._personal_run_guard(run["id"]))

    def test_legacy_upload_requests_use_only_readonly_lookup_with_feature_disabled(self):
        run = self.legacy.create_manual_run(["501"], [1], "legacy-upload-readonly", ACTOR)
        statements, connections = [], []
        original_connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            connections.append((args, kwargs))
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        @contextlib.contextmanager
        def credentials(*args, **kwargs):
            yield self.accounts["1"], "isolated-token"
        with mock.patch.object(oauth_service, "POST_DB_PATH", self.path), \
             mock.patch.dict("os.environ", {"X_VIDEO_PUBLISH_ENABLED": "0"}), \
             mock.patch.object(post_service, "ensure_storage", side_effect=AssertionError("runtime must not migrate")), \
             mock.patch("features.x_video_publish.transport.broker", side_effect=AssertionError("legacy must not call personal broker")), \
             mock.patch.object(oauth_service, "verify_account"), \
             mock.patch.object(oauth_service, "publish_credentials", side_effect=credentials), \
             mock.patch.object(sqlite3, "connect", side_effect=traced_connect):
            provider = oauth_service.publishing_token_provider(1, ACTOR, "en", personal_run_id=run["id"])
            self.assertEqual("isolated-token", provider())
            self.assertEqual("isolated-token", provider(verify_now=True))
            self.assertIsNone(oauth_service._personal_run_guard(run["id"]))
        self.assertEqual(3, len(connections))
        self.assertTrue(all(args[0].endswith("?mode=ro") and kwargs["uri"] for args, kwargs in connections))
        self.assertTrue(statements)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT ") for sql in statements))

    def test_readonly_lookup_handles_legacy_schema_and_fails_closed_on_bad_db(self):
        old_path = Path(self.tmp.name) / "legacy-only.sqlite"
        with contextlib.closing(sqlite3.connect(old_path)) as conn:
            conn.execute("CREATE TABLE legacy_fixture(id INTEGER)")
            conn.commit()
        before = old_path.read_bytes()
        self.assertIsNone(read_personal_task(old_path, 1))
        self.assertIsNone(assert_personal_run_publishable(old_path, 1))
        self.assertEqual(before, old_path.read_bytes())
        missing = Path(self.tmp.name) / "missing.sqlite"
        corrupt = Path(self.tmp.name) / "corrupt.sqlite"
        corrupt.write_bytes(b"not a sqlite database")
        for path in (missing, corrupt):
            with self.subTest(path=path.name), self.assertRaises(XPostError) as error:
                read_personal_task(path, 1)
            self.assertEqual("x_video_ledger_unavailable", error.exception.code)
        self.assertFalse(missing.exists())
        with contextlib.closing(sqlite3.connect(old_path)) as conn:
            conn.execute("CREATE TABLE x_video_publish_task(id INTEGER)")
            conn.commit()
        with self.assertRaises(XPostError):
            read_personal_task(old_path, 1)

    def test_readonly_real_task_retains_owner_snapshot_language_and_hold_guards(self):
        batch = self.commit()
        first, sibling = batch["tasks"][:2]
        task = read_personal_task(self.path, first["manual_run_id"])
        self.assertEqual(ACTOR["tenant_key"], task["tenant_key"])
        self.assertEqual(ACTOR["user_id"], task["user_id"])
        self.assertEqual("account1", task["account_snapshot"]["username"])
        with self.assertRaises(XPostError):
            assert_personal_run_publishable(self.path, first["manual_run_id"], dict(self.accounts["1"], drama_language="de"))
        run = self.freeze(first)
        log = self.legacy.reserve_log(run["queues"][0]["id"])
        self.legacy.mark_failed(log["id"], "x_post_unknown_outcome", "ambiguous", unknown_outcome=True)
        with mock.patch.object(post_service, "ensure_storage", side_effect=AssertionError("runtime must not migrate")):
            with self.assertRaises(XPostError) as error:
                assert_personal_run_publishable(self.path, sibling["manual_run_id"], self.accounts["2"])
            self.assertTrue(error.exception.unknown_outcome)

    def test_legacy_query_owner_rejection_needs_no_storage_initialization(self):
        batch = self.commit(self.preview(self.rows()[:1]))
        with mock.patch.object(oauth_service, "POST_DB_PATH", self.path), \
             mock.patch.object(oauth_service, "_material_pool_actor", return_value=OTHER), \
             mock.patch.object(post_service, "ensure_storage", side_effect=AssertionError("unauthorized query must not migrate")):
            with self.assertRaises(oauth_service.ServiceError) as error:
                oauth_service.query_post_manual_run_request({}, batch["tasks"][0]["manual_run_id"])
        self.assertEqual(404, error.exception.status)

    def test_no_valid_rows_do_not_create_batch(self):
        preview = self.preview([dict(self.rows()[0], caption="{unknown}")])
        with self.assertRaises(XPostError):
            self.commit(preview)
        self.assertEqual(0, self.store.list_batches(ACTOR)["total"])

    def test_success_repeat_requires_explicit_confirmation(self):
        batch = self.commit(self.preview(self.rows()[:1]))
        run = self.freeze(batch["tasks"][0])
        log = self.legacy.reserve_log(run["queues"][0]["id"])
        self.legacy.prepare_log(log["id"], "https://example.test/long", "https://example.test/1.html", "First")
        self.legacy.mark_publishing(log["id"])
        self.legacy.mark_media_uploaded(log["id"], "111")
        self.legacy.mark_published(log["id"], "111", "222", "https://x.com/account1/status/222")
        preview = self.preview(self.rows()[:1])
        self.assertTrue(preview["rows"][0]["repeat_required"])
        with self.assertRaises(XPostError):
            self.commit(preview, key="repeat-key")
        self.assertTrue(self.commit(preview, key="repeat-key", allow_repeat_success=True)["created"])


class CaptionTests(unittest.TestCase):
    def test_optional_macros_and_free_text(self):
        self.assertEqual("Hello", render_caption("Hello", "", "", "https://short.test/12.html"))
        self.assertEqual("Drama: Story", render_caption("{name}: {desc}", "Drama", "Story", "https://short.test/12.html"))
        self.assertEqual("https://short.test/12.html", render_caption("{url}", "", "", "https://short.test/12.html"))

    def test_unknown_macro_and_weighted_limit_fail(self):
        for text in ("{drama_name}", "{{name}}", "Oops {", ""):
            with self.assertRaises(XPostError):
                normalize_caption(text)
        with self.assertRaises(XPostError):
            render_caption("中" * 141, "", "", "https://short.test/12.html")
        self.assertEqual(23, weighted_length("https://short.test/" + "a" * 100))

    def test_legacy_macro_validation_is_unchanged(self):
        with self.assertRaises(XPostError):
            build_post_text("https://short.test/12.html", "Drama", "Story", "Free text")
        self.assertEqual("Free text", build_post_text("https://short.test/12.html", "Drama", "Story", "Free text", copy_contract_version=2))


if __name__ == "__main__":
    unittest.main()
