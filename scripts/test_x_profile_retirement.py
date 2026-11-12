"""Retirement safety tests on temporary copies of the actual SQLite schemas."""
import contextlib
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts.service import XPostStore
from features.x_auto_posts.core import XPostAutoStore
from features.x_tag_publish import retirement as r

STAMP = "2026-11-12T04:00:00Z"


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = {k: str(self.root / (k + ".sqlite3")) for k in
                      ("ordinary_post", "ordinary_auto", "profile_post", "profile_auto")}
        for lane, path in self.paths.items():
            (XPostStore if lane.endswith("post") else XPostAutoStore)(path)
        for lane in ("ordinary_post", "profile_post"):
            with contextlib.closing(sqlite3.connect(self.paths[lane])) as db, db:
                db.execute("CREATE TABLE pipeline_x_operation(operation_id TEXT PRIMARY KEY,source_key TEXT NOT NULL,generation INTEGER NOT NULL,operation TEXT NOT NULL,request_sha256 TEXT NOT NULL,created_at TEXT NOT NULL)")
                db.execute("CREATE TABLE pipeline_x_claim(source_key TEXT NOT NULL,stage TEXT NOT NULL,generation INTEGER NOT NULL,claim_hash TEXT NOT NULL,queue_id INTEGER NOT NULL,log_id INTEGER NOT NULL,descriptor_json TEXT NOT NULL,PRIMARY KEY(source_key,stage,generation))")
                db.execute("CREATE TABLE pipeline_x_checkpoint(source_key TEXT NOT NULL,generation INTEGER NOT NULL,phase TEXT NOT NULL,receipt_json TEXT NOT NULL,PRIMARY KEY(source_key,generation,phase))")

    def insert(self, lane, table, **values):
        with contextlib.closing(sqlite3.connect(self.paths[lane])) as db, db:
            columns = db.execute('PRAGMA table_info("%s")' % table).fetchall()
            data = {}
            for _, name, kind, required, default, primary in columns:
                if name in values:
                    data[name] = values[name]
                elif required and default is None and not (primary and kind.upper() == "INTEGER"):
                    data[name] = 1 if kind.upper() in ("INTEGER", "REAL") else ""
            data.update(values)
            cols = list(data)
            cur = db.execute('INSERT INTO "%s"(%s) VALUES(%s)' %
                             (table, ",".join('"%s"' % c for c in cols), ",".join("?" for _ in cols)),
                             [data[c] for c in cols])
            return cur.lastrowid

    def pool(self, mid, lane="ordinary_post", **kwargs):
        values = dict(material_key=str(mid), material_id=str(mid), created_at="2026-10-29T03:00:00Z",
                      updated_at="2026-10-29T03:00:00Z", created_by_user_id="author-1", created_by_name="Author",
                      source_material_language="en", last_error_code="", status="unpublished")
        values.update(kwargs)
        return self.insert(lane, "x_post_material_pool", **values)

    def queue(self, mid, *, trigger="auto_template", status="reserved", attempt=0,
              unknown=0, post_id="", lane="ordinary_post", pool_id=None, relay=None):
        if pool_id is None and trigger != "manual":
            found = self.query(lane, "SELECT id FROM x_post_material_pool WHERE material_id=?", (str(mid),))
            pool_id = found[0][0] if found else None
        schedule_id = None
        if relay:
            schedule_id = self.insert(lane, "x_post_schedule_run", slot_key="relay-" + str(mid), source_type="material",
                                      run_date="2026-11-12", publish_time="12:00", config_version=1,
                                      account_ids_json="[1]", status="running", expected_count=1,
                                      created_at=STAMP, updated_at=STAMP)
        manual_id = None if relay else self.insert(lane, "x_post_manual_run", idempotency_key="run-" + str(mid),
                                trigger_source=trigger, external_task_key="task-" + str(mid) if trigger == "auto_template" else "",
                                run_date="2026-11-12", source_date="2026-11-11",
                                account_ids_json="[1]", material_ids_json=json.dumps([str(mid)]),
                                status="running", created_at=STAMP, updated_at=STAMP)
        qid = self.insert(lane, "x_post_queue", idempotency_key="queue-" + str(mid),
                          manual_run_id=manual_id, schedule_run_id=schedule_id,
                          run_date="2026-11-12", source_date="2026-11-11",
                          material_id=str(mid), material_key=str(mid), account_id=1,
                          account_username="account", status=status if status in ("published", "failed") else "reserved",
                          pool_item_id=pool_id, pool_created_at="2026-10-29T03:00:00Z" if pool_id else "",
                          delivery_mode="premium_relay_repost" if relay else "direct",
                          relay_account_id=2 if relay else 0, relay_account_username="relay" if relay else "",
                          preflight_duration=141 if relay else 0,
                          created_at=STAMP, updated_at=STAMP)
        lid = None if status is None else self.insert(lane, "x_post_publish_log", queue_id=qid, account_id=1,
                          status=status, attempt_count=attempt, unknown_outcome=unknown,
                          x_post_id=post_id, created_at=STAMP, updated_at=STAMP)
        if relay:
            self.insert(lane, "x_post_repost_ledger", queue_id=qid, target_account_id=1,
                        relay_account_id=2, run_date="2026-11-12", created_at=STAMP, updated_at=STAMP, **relay)
        return qid, lid, manual_id

    def auto_task(self, mid, queue_id=None, *, status="ready", attempt=0, lane="ordinary_auto"):
        tid = self.insert(lane, "x_auto_template", name="Template " + str(mid), enabled=1,
                          current_version=1, created_at=STAMP, updated_at=STAMP)
        self.insert(lane, "x_auto_template_version", template_id=tid, version=1,
                    config_json="{}", config_sha256="sha", created_at=STAMP)
        rid = self.insert(lane, "x_auto_run", run_key="auto-" + str(mid), template_id=tid,
                          template_version=1, config_sha256="sha", trigger_type="auto", status="running",
                          scheduled_at_utc=STAMP, shanghai_date="2026-11-12", publish_time="12:00",
                          created_at=STAMP, updated_at=STAMP)
        taskid = self.insert(lane, "x_auto_task", run_id=rid, template_id=tid, template_version=1,
                             account_id="1", trigger_priority=0, scheduled_at_utc=STAMP,
                             status=status, language="en", material_id=str(mid), execution_queue_id=queue_id,
                             publish_attempt_count=attempt, created_at=STAMP, updated_at=STAMP)
        self.insert(lane, "x_auto_material_ledger", material_id=str(mid), task_id=taskid,
                    run_id=rid, template_id=tid, content_id="drama", reserved_at_utc=STAMP,
                    canonical_queue_id=queue_id, confirmed_at_utc=STAMP if queue_id else "",
                    last_task_status=status, updated_at=STAMP)
        return taskid

    def query(self, lane, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.paths[lane])) as db:
            return db.execute(sql, params).fetchall()

    def plan(self, reviewed=()):
        return r.make_plan(self.paths, reviewed_no_post_queue_ids=reviewed, now=STAMP)

    def apply(self, plan, **kwargs):
        return r.apply_plan(plan, backup_directory=self.root / "backup", **kwargs)

    def evidence(self, qid, lid, operation="x:POST:/2/media/upload/initialize", generation=1):
        self.insert("ordinary_post", "pipeline_x_claim", source_key="x:queue:" + str(qid), stage="publish",
                    generation=generation, claim_hash="hash", queue_id=qid, log_id=lid, descriptor_json="{}")
        self.insert("ordinary_post", "pipeline_x_operation", operation_id="op-%s-%s" % (qid, generation),
                    source_key="x:queue:" + str(qid), generation=generation,
                    operation=operation, request_sha256="request", created_at=STAMP)

    def test_dry_run_is_read_only_and_copy_preserves_author_time_and_error(self):
        self.pool("100", last_error_code="material_duration_missing")
        before = {k: hashlib.sha256(Path(p).read_bytes()).hexdigest() for k, p in self.paths.items()}
        plan = self.plan()
        after = {k: hashlib.sha256(Path(p).read_bytes()).hexdigest() for k, p in self.paths.items()}
        self.assertEqual(before, after)
        self.assertEqual(len(plan["migrate"]), 1)
        self.assertEqual(self.apply(plan)["migrated"], 1)
        source = self.query("ordinary_post", "SELECT material_id,created_at,created_by_user_id,created_by_name,last_error_code FROM x_post_material_pool")
        self.assertEqual(source, self.query("profile_post", "SELECT material_id,created_at,created_by_user_id,created_by_name,last_error_code FROM x_post_material_pool"))
        self.assertEqual(self.apply(plan)["already_migrated"], 1)

    def test_cancel_keeps_queue_binding_and_permanent_auto_reservation(self):
        pid = self.pool("101")
        qid, _, _ = self.queue("101", pool_id=pid)
        self.auto_task("101", qid)
        plan = self.plan()
        self.assertEqual(self.apply(plan)["migrated"], 1)
        self.assertEqual(self.query("ordinary_post", "SELECT status,pool_item_id FROM x_post_queue"), [("failed", pid)])
        self.assertEqual(self.query("ordinary_auto", "SELECT status FROM x_auto_task"), [("canceled",)])
        self.assertEqual(self.query("ordinary_post", "SELECT status,queued_count,published_count,failed_count,unknown_count FROM x_post_manual_run"), [("stopped", 1, 0, 1, 0)])
        self.assertEqual(self.query("ordinary_auto", "SELECT canonical_queue_id,confirmed_at_utc,last_task_status FROM x_auto_material_ledger"), [(qid, STAMP, "canceled")])
        self.assertEqual(self.apply(plan)["already_migrated"], 1)

    def test_unknown_source_post_and_manual_are_never_cancelled_or_migrated(self):
        for mid in ("201", "202", "203"):
            self.pool(mid)
        self.queue("201", status="post_creating", unknown=1, attempt=1)
        self.queue("202", status="repost_creating", attempt=1, post_id="123456")
        self.queue("203", trigger="manual")
        before = self.query("ordinary_post", "SELECT * FROM x_post_publish_log")
        plan = self.plan()
        self.assertEqual(plan["migrate"], [])
        self.apply(plan)
        self.assertEqual(before, self.query("ordinary_post", "SELECT * FROM x_post_publish_log"))
        self.assertEqual(len(plan["excluded"]), 3)

    def test_cross_lane_history_and_active_manual_reservation_excluded(self):
        for mid in ("301", "302", "303", "304"):
            self.pool(mid)
        self.pool("301", lane="profile_post")
        self.queue("302", lane="profile_post", status="failed")
        self.auto_task("303", lane="profile_auto", status="canceled")
        manual_id = self.insert("ordinary_post", "x_post_manual_run", idempotency_key="reserved-manual",
                                trigger_source="manual", run_date="2026-11-12", source_date="2026-11-11",
                                account_ids_json="[1]", material_ids_json='["304"]',
                                status="queued", created_at=STAMP, updated_at=STAMP)
        self.insert("ordinary_post", "x_post_manual_material_reservation", manual_run_id=manual_id,
                    material_key="304", state="active", created_at=STAMP, updated_at=STAMP)
        plan = self.plan()
        self.assertEqual(len(plan["excluded"]), 4)
        self.assertEqual(plan["migrate"], [])

    def test_reviewed_attempt_requires_exact_media_only_operation_history(self):
        qid, lid, _ = self.queue("401", attempt=1)
        self.auto_task("401", qid, attempt=1)
        self.assertTrue(any(x["reason"] == "prior_attempt_requires_review" for x in self.plan()["held"]))
        self.assertTrue(any(x["reason"] == "reviewed_media_only_evidence_missing" for x in self.plan([qid])["held"]))
        self.evidence(qid, lid)
        plan = self.plan([qid])
        self.assertEqual(plan["held"], [])
        self.apply(plan)
        self.assertEqual(self.query("ordinary_post", "SELECT status,attempt_count FROM x_post_publish_log"), [("failed", 1)])

    def test_new_tweet_operation_after_dry_run_blocks_before_any_cancel(self):
        qid, lid, _ = self.queue("402", attempt=1)
        self.evidence(qid, lid)
        plan = self.plan([qid])
        self.evidence(qid, lid, "x:POST:/2/tweets", generation=2)
        with self.assertRaisesRegex(r.RetirementError, "pipeline_evidence_changed"):
            self.apply(plan)
        self.assertEqual(self.query("ordinary_post", "SELECT status FROM x_post_publish_log"), [("reserved",)])

    def test_row_drift_and_manifest_tamper_block_before_mutation(self):
        qid, lid, _ = self.queue("403")
        plan = self.plan()
        with contextlib.closing(sqlite3.connect(self.paths["ordinary_post"])) as db, db:
            db.execute("UPDATE x_post_publish_log SET unknown_outcome=1 WHERE id=?", (lid,))
        with self.assertRaisesRegex(r.RetirementError, "changed_row"):
            self.apply(plan)
        plan["changes"] = []
        with self.assertRaisesRegex(r.RetirementError, "manifest_hash_mismatch"):
            self.apply(plan)

    def test_resume_after_source_commit_does_not_duplicate_or_unfence(self):
        self.pool("501")
        qid, _, _ = self.queue("501")
        self.auto_task("501", qid)
        plan = self.plan()
        def crash(phase):
            if phase == "ordinary_post":
                raise RuntimeError("simulated process crash")
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            self.apply(plan, after_phase=crash)
        self.assertEqual(self.query("ordinary_post", "SELECT status FROM x_post_queue"), [("failed",)])
        self.assertEqual(self.query("profile_post", "SELECT COUNT(*) FROM x_post_material_pool"), [(0,)])
        self.assertEqual(self.apply(plan)["migrated"], 1)
        self.assertEqual(self.apply(plan)["already_migrated"], 1)

    def test_target_reservation_after_plan_prevents_pool_admission(self):
        self.pool("601")
        plan = self.plan()
        self.auto_task("601", lane="profile_auto")
        with self.assertRaisesRegex(r.RetirementError, "migration_conflict"):
            self.apply(plan)
        self.assertEqual(self.query("profile_post", "SELECT COUNT(*) FROM x_post_material_pool"), [(0,)])

    def test_relay_source_post_is_excluded_without_unknown_flag(self):
        self.pool("701")
        self.queue("701", relay={"status": "source_published", "source_post_id": "123456"})
        plan = self.plan()
        self.assertEqual(plan["migrate"], [])
        self.assertEqual(plan["held"][0]["reason"], "published_or_source_post")
        self.apply(plan)
        self.assertEqual(self.query("ordinary_post", "SELECT source_post_id,status FROM x_post_repost_ledger"), [("123456", "source_published")])

    def test_mixed_material_schedule_preserves_unknown_and_updates_counts(self):
        sid = self.insert("ordinary_post", "x_post_schedule_run", slot_key="schedule-1", source_type="material",
                          run_date="2026-11-12", publish_time="12:00", config_version=1,
                          account_ids_json="[1,2]", status="needs_review", expected_count=2,
                          created_at=STAMP, updated_at=STAMP)
        for account, mid, status, unknown in ((1, "801", "reserved", 0), (2, "802", "post_creating", 1)):
            pid = self.pool(mid)
            qid = self.insert("ordinary_post", "x_post_queue", idempotency_key="q-" + mid,
                              schedule_run_id=sid, run_date="2026-11-12", source_date="2026-11-11",
                              material_id=mid, material_key=mid, account_id=account, account_username="a" + mid,
                              pool_item_id=pid, pool_created_at="2026-10-29T03:00:00Z", status="reserved",
                              created_at=STAMP, updated_at=STAMP)
            self.insert("ordinary_post", "x_post_publish_log", queue_id=qid, account_id=account,
                        status=status, unknown_outcome=unknown, created_at=STAMP, updated_at=STAMP)
        plan = self.plan()
        self.assertEqual([x["material_id"] for x in plan["migrate"]], ["801"])
        self.apply(plan)
        self.assertEqual(self.query("ordinary_post", "SELECT status,queued_count,published_count,failed_count,unknown_count FROM x_post_schedule_run"), [("needs_review", 2, 0, 1, 1)])
        self.assertEqual(self.query("ordinary_post", "SELECT status,unknown_outcome FROM x_post_publish_log ORDER BY id"), [("failed", 0), ("post_creating", 1)])

    def test_unknown_auto_receipt_protects_apparently_unattempted_queue(self):
        self.pool("901")
        qid, _, _ = self.queue("901")
        self.auto_task("901", qid, status="unknown")
        plan = self.plan()
        self.assertEqual(plan["migrate"], [])
        self.assertFalse(any(c["table"] in ("x_post_queue", "x_post_publish_log") for c in plan["changes"]))
        self.apply(plan)
        self.assertEqual(self.query("ordinary_post", "SELECT status,attempt_count FROM x_post_publish_log"), [("reserved", 0)])

    def test_duplicate_source_material_identity_is_migrated_once(self):
        self.pool("902")
        self.pool("902", material_key="legacy-key-902")
        plan = self.plan()
        self.assertEqual(len(plan["migrate"]), 1)
        self.assertEqual(plan["excluded"][0]["reason"], "duplicate_source_candidate")
        self.assertEqual(self.apply(plan)["migrated"], 1)

    def test_legacy_cancelled_zero_attempt_queues_migrate_without_rewriting_binding(self):
        for mid, status, log_status in (("903", "cancelled", "reserved"), ("904", "canceled", None)):
            pid = self.pool(mid)
            qid, _, _ = self.queue(mid, status=log_status, pool_id=pid)
            with contextlib.closing(sqlite3.connect(self.paths["ordinary_post"])) as db, db:
                db.execute("UPDATE x_post_queue SET status=? WHERE id=?", (status, qid))
        before = self.query("ordinary_post", "SELECT id,pool_item_id,status FROM x_post_queue ORDER BY id")
        plan = self.plan()
        self.assertEqual(len(plan["migrate"]), 2)
        self.assertEqual(self.apply(plan)["migrated"], 2)
        self.assertEqual(self.query("ordinary_post", "SELECT id,pool_item_id,status FROM x_post_queue ORDER BY id"), before)
        self.assertEqual(self.query("ordinary_post", "SELECT status,attempt_count FROM x_post_publish_log"), [("failed", 0)])

    def test_legacy_cancelled_prior_attempt_still_requires_evidence(self):
        self.pool("905")
        qid, _, _ = self.queue("905", status="failed", attempt=1)
        with contextlib.closing(sqlite3.connect(self.paths["ordinary_post"])) as db, db:
            db.execute("UPDATE x_post_queue SET status='cancelled' WHERE id=?", (qid,))
        plan = self.plan()
        self.assertEqual(plan["migrate"], [])
        self.assertIn("prior_attempt_requires_review", plan["excluded"][0]["reason"])

    def media_failure(self, mid, code="x_media_processing_failed", message="X媒体处理失败", *, operations=True):
        self.pool(mid)
        qid, lid, _ = self.queue(mid, status="failed", attempt=1)
        with contextlib.closing(sqlite3.connect(self.paths["ordinary_post"])) as db, db:
            db.execute("UPDATE x_post_publish_log SET error_code=?,error_message=? WHERE id=?", (code, message, lid))
        if operations:
            self.evidence(qid, lid)
            self.evidence(qid, lid, "x:POST:/2/media/upload/987654/finalize", generation=2)
            self.evidence(qid, lid, "x:GET:/2/media/upload", generation=3)
            self.insert("ordinary_post", "pipeline_x_checkpoint", source_key="x:queue:" + str(qid),
                        generation=1, phase="x_media_initialized", receipt_json='{"media_id":"987654"}')
        return qid, lid

    def test_confirmed_finalize_processing_failure_migrates_without_retry(self):
        self.media_failure("906")
        before = self.query("ordinary_post", "SELECT * FROM x_post_publish_log")
        plan = self.plan()
        self.assertEqual(len(plan["migrate"]), 1)
        self.assertEqual(self.apply(plan)["migrated"], 1)
        self.assertEqual(self.query("ordinary_post", "SELECT * FROM x_post_publish_log"), before)

    def test_post_intent_or_post_checkpoint_blocks_media_failure_migration(self):
        qid, lid = self.media_failure("907")
        self.evidence(qid, lid, "x:POST:/2/tweets", generation=4)
        qid2, _ = self.media_failure("908")
        self.insert("ordinary_post", "pipeline_x_checkpoint", source_key="x:queue:" + str(qid2),
                    generation=2, phase="x_media_uploaded", receipt_json='{}')
        plan = self.plan()
        self.assertEqual(plan["migrate"], [])
        self.assertEqual(len(plan["excluded"]), 2)

    def test_legacy_media_error_requires_exact_stage_and_unknown_remains_blocked(self):
        self.media_failure("909", "x_upstream_error", "X媒体初始化失败(HTTP 500): unavailable", operations=False)
        self.media_failure("910", "x_upstream_error", "X发帖失败(HTTP 500)", operations=False)
        _, lid = self.media_failure("911")
        with contextlib.closing(sqlite3.connect(self.paths["ordinary_post"])) as db, db:
            db.execute("UPDATE x_post_publish_log SET unknown_outcome=1 WHERE id=?", (lid,))
        self.media_failure("912", "x_token_invalid", "Token失效，请重新登陆", operations=False)
        plan = self.plan()
        self.assertEqual([x["material_id"] for x in plan["migrate"]], ["909"])
        self.assertEqual(len(plan["excluded"]), 3)

    def test_audited_upload_interruption_requires_nonempty_media_operation_evidence(self):
        message = "视频上传因服务切换中断；操作账本确认未发帖，成片与原尝试保留"
        self.media_failure("913", "upload_interrupted_no_post", message, operations=True)
        self.media_failure("914", "upload_interrupted_no_post", message, operations=False)
        plan = self.plan()
        self.assertEqual([x["material_id"] for x in plan["migrate"]], ["913"])


if __name__ == "__main__":
    unittest.main()
