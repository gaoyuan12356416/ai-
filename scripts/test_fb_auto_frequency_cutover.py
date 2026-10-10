import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore
from features.fb_auto_posts.validation import config_hash
from scripts import fb_auto_post_frequency_cutover as cutover
from scripts.test_fb_auto_validation import payload


NOW = datetime(2026, 10, 10, 14, 35, tzinfo=timezone.utc)


class LocalAPI:
    def __init__(self, store, actor):
        self.store, self.actor = store, actor
        self.posts = []
        self.ambiguous = set()

    def __call__(self, suffix="", raw=None):
        if raw is None:
            return {"ok": True, "template": self.store.get_template(1, self.actor)}
        self.posts.append(suffix)
        data = copy.deepcopy(raw)
        version = data.pop("expected_version")
        if suffix == "":
            template = self.store.update_template(1, data, self.actor, version)
        else:
            template = self.store.set_template_status(1, suffix == "/enable", self.actor, version, **data)
        if suffix in self.ambiguous:
            raise OSError("simulated lost reply after committed request")
        return {"ok": True, "template": template}


class FrequencyCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "publisher.sqlite3"
        self.state = Path(self.temp.name) / "cutover"
        self.store = FBAutoPostStore(self.db, now_fn=lambda: NOW)
        self.actor = ActorScope.from_payload(cutover.ACTOR)
        config = payload()
        config.update(group_ids=["62"], app_id="1479", schedule=copy.deepcopy(cutover.OLD_SCHEDULE), stagger_minutes=40, default_daily_count=0)
        config["page_daily_limits"] = [{"page_id": str(10000 + i), "daily_count": 2 if i < 144 else 0, "name": "", "tier": ""} for i in range(145)]
        self.store.create_template(config, self.actor)
        for version in range(1, 6):
            self.store.update_template(1, config, self.actor, version)
        self.before = self.store.set_template_status(1, True, self.actor, 6)
        self.hash_patch = patch.object(cutover, "EXPECTED_HASH", self.before["config_sha256"])
        self.hash_patch.start()
        self.api = LocalAPI(self.store, self.actor)

    def tearDown(self):
        self.hash_patch.stop()
        self.temp.cleanup()

    def apply(self):
        return cutover.apply_cutover(self.api, self.db, self.state, now_fn=lambda: NOW)

    def test_time_guard_boundaries_and_date(self):
        accepted = (datetime(2026, 10, 10, 14, 30, tzinfo=timezone.utc), datetime(2026, 10, 10, 15, 9, 59, tzinfo=timezone.utc))
        for at in accepted:
            cutover.check_time(at)
        rejected = (datetime(2026, 10, 10, 14, 29, 59, tzinfo=timezone.utc), datetime(2026, 10, 10, 15, 10, tzinfo=timezone.utc), datetime(2026, 10, 11, 14, 35, tzinfo=timezone.utc), datetime(2026, 10, 10, 22, 35))
        for at in rejected:
            with self.assertRaises(cutover.CutoverError):
                cutover.check_time(at)
        cutover.check_time(datetime(2026, 10, 10, 15, 49, 59, tzinfo=timezone.utc), recovery=True)
        for at in (datetime(2026, 10, 10, 15, 50, tzinfo=timezone.utc), datetime(2026, 10, 11, 14, 30, tzinfo=timezone.utc)):
            with self.assertRaises(cutover.CutoverError):
                cutover.check_time(at, recovery=True)

    def test_apply_outside_window_does_not_read_or_write(self):
        with self.assertRaises(cutover.CutoverError):
            cutover.apply_cutover(lambda *_: self.fail("API should not run"), self.db, self.state, now_fn=lambda: datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
        self.assertFalse(self.state.exists())

    def test_exact_candidate_only_replaces_schedule_and_stagger(self):
        candidate = cutover.make_candidate(self.before)
        self.assertEqual(candidate["schedule"], cutover.NEW_SCHEDULE)
        self.assertEqual(candidate["stagger_minutes"], 0)
        for key, value in self.before["config"].items():
            if key not in ("schedule", "stagger_minutes"):
                self.assertEqual(candidate[key], value)
        self.assertEqual(self.before["config"]["schedule"], cutover.OLD_SCHEDULE)

    def test_drift_rejected_even_if_payload_otherwise_valid(self):
        for field, value in (("version", 7), ("config_sha256", "0" * 64), ("id", 2)):
            before = copy.deepcopy(self.before)
            before[field] = value
            with self.assertRaises(cutover.CutoverError):
                cutover.make_candidate(before)
        before = copy.deepcopy(self.before)
        before["config"]["message_template"] = "Changed"
        with self.assertRaises(cutover.CutoverError):
            cutover.make_candidate(before)

    def test_preflight_has_no_production_sqlite_or_api_writes(self):
        before_bytes = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = cutover.preflight(self.api, self.db, self.state)
        self.assertFalse(result["production_state_written"])
        self.assertEqual(self.api.posts, [])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before_bytes)
        self.assertFalse((self.state / "receipt.json").exists())
        self.assertEqual(result["proof"]["page_daily_counts"], {"2": 144, "0": 1})

    def test_cutover_is_idempotent_and_backup_exists(self):
        result = self.apply()
        self.assertEqual(self.api.posts, ["/disable", "", "/enable"])
        self.assertEqual(result["version"], 7)
        self.assertTrue(Path(result["backup"]).is_file())
        first = copy.deepcopy(self.api.posts)
        self.assertEqual(self.apply()["config_sha256"], result["config_sha256"])
        self.assertEqual(self.api.posts, first)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fb_auto_schedule_plan").fetchone()[0], 0)

    def test_ambiguous_committed_replies_are_read_back_without_replay(self):
        self.api.ambiguous = {"/disable", "", "/enable"}
        result = self.apply()
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.posts, ["/disable", "", "/enable"])

    def test_saved_receipt_recovers_enable_only(self):
        self.apply()
        self.store.set_template_status(1, False, self.actor, 7)
        path = self.state / "receipt.json"
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["phase"] = "saved"
        cutover.durable_json(path, receipt)
        self.api.posts.clear()
        late = datetime(2026, 10, 10, 15, 20, tzinfo=timezone.utc)
        self.assertTrue(cutover.apply_cutover(self.api, self.db, self.state, now_fn=lambda: late)["ok"])
        self.assertEqual(self.api.posts, ["/enable"])

    def test_started_operation_may_complete_after_original_window(self):
        clock = [datetime(2026, 10, 10, 15, 9, tzinfo=timezone.utc)] * 2
        later = datetime(2026, 10, 10, 15, 20, tzinfo=timezone.utc)
        result = cutover.apply_cutover(self.api, self.db, self.state, now_fn=lambda: clock.pop() if clock else later)
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.posts, ["/disable", "", "/enable"])

    def test_prepared_receipt_cannot_start_first_disable_after_start_deadline(self):
        def refused_api(suffix="", raw=None):
            if raw is not None:
                raise OSError("simulated rejection before any state change")
            return self.api()
        with self.assertRaises(cutover.CutoverError):
            cutover.apply_cutover(refused_api, self.db, self.state, now_fn=lambda: NOW)
        path = self.state / "receipt.json"
        receipt = json.loads(path.read_text(encoding="utf-8"))
        late = datetime(2026, 10, 10, 15, 20, tzinfo=timezone.utc)
        for phase in ("prepared", "disable_requested"):
            receipt["phase"] = phase
            cutover.durable_json(path, receipt)
            with self.assertRaises(cutover.CutoverError):
                cutover.apply_cutover(self.api, self.db, self.state, now_fn=lambda: late)
            self.assertEqual(self.api.posts, [])
            self.assertEqual(self.store.get_template(1, self.actor)["status"], "enabled")

    def test_owned_receipt_does_not_allow_nextday_mutations(self):
        self.apply()
        self.api.posts.clear()
        with self.assertRaises(cutover.CutoverError):
            cutover.apply_cutover(self.api, self.db, self.state, now_fn=lambda: datetime(2026, 10, 11, 14, 35, tzinfo=timezone.utc))
        self.assertEqual(self.api.posts, [])

    def test_completed_receipt_does_not_reenable_external_disable(self):
        self.apply()
        self.store.set_template_status(1, False, self.actor, 7)
        self.api.posts.clear()
        with self.assertRaises(cutover.CutoverError):
            self.apply()
        self.assertEqual(self.api.posts, [])

    def test_disabled_template_without_receipt_never_adopted(self):
        self.store.set_template_status(1, False, self.actor, 6)
        with self.assertRaises(cutover.CutoverError):
            self.apply()
        self.assertEqual(self.api.posts, [])

    def test_config_drift_after_saved_receipt_does_not_write(self):
        self.apply()
        self.store.set_template_status(1, False, self.actor, 7)
        changed = cutover.raw_payload(self.store.get_template(1, self.actor)["config"])
        changed["name"] += " concurrent"
        self.store.update_template(1, changed, self.actor, 7)
        self.api.posts.clear()
        with self.assertRaises(cutover.CutoverError):
            self.apply()
        self.assertEqual(self.api.posts, [])

    def insert_task(self, status, planned, trigger="auto"):
        with self.store.connect() as conn:
            run_id = conn.execute("INSERT INTO fb_auto_run(template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc) VALUES(1,6,? ,?,'completed','{}',?)", (trigger + planned, trigger, NOW.isoformat())).lastrowid
            task_id = conn.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,planned_publish_at_utc,created_at_utc) VALUES(?,1,6,?,'62',?,?,?)", (run_id, str(run_id), status, planned, NOW.isoformat())).lastrowid
        return task_id

    def test_today_or_future_manual_active_tasks_block(self):
        for index, (status, planned, trigger) in enumerate((("ready", "2026-10-10T13:55:00+00:00", "auto"), ("planned", "2026-10-11T03:00:00+00:00", "manual"), ("preparing", "2026-10-11T04:00:00+00:00", "auto"))):
            task_id = self.insert_task(status, planned, trigger)
            with cutover.read_db(self.db) as conn:
                with self.assertRaises(cutover.CutoverError):
                    cutover.assert_queue_safe(conn)
            with self.store.connect() as conn:
                conn.execute("UPDATE fb_auto_task SET status='skipped' WHERE id=?", (task_id,))

    def test_future_automatic_ready_task_may_be_invalidated_without_identity_loss(self):
        task_id = self.insert_task("ready", "2026-10-11T03:00:00+00:00")
        result = self.apply()
        self.assertTrue(result["audit"]["identities_preserved"])
        with self.store.connect() as conn:
            row = conn.execute("SELECT status,skip_reason FROM fb_auto_task WHERE id=?", (task_id,)).fetchone()
            self.assertEqual(tuple(row), ("skipped", "fb_auto_template_version_changed"))

    def test_reconciliation_change_allowed_but_record_deletion_rejected(self):
        task_id = self.insert_task("submitted", "2026-10-10T13:55:00+00:00")
        with cutover.read_db(self.db) as conn:
            before = cutover.snapshot(conn)
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_auto_task SET status='published',graph_post_id='remote-1' WHERE id=?", (task_id,))
        with cutover.read_db(self.db) as conn:
            self.assertTrue(cutover.verify_snapshot(before, conn)["identities_preserved"])
        with self.store.connect() as conn:
            conn.execute("DELETE FROM fb_auto_task WHERE id=?", (task_id,))
        with cutover.read_db(self.db) as conn:
            with self.assertRaises(cutover.CutoverError):
                cutover.verify_snapshot(before, conn)


if __name__ == "__main__":
    unittest.main()
