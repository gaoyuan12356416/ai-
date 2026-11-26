"""Isolated scheduler/selection integration, including the original publish deadline."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from features.x_auto_posts.core import XAutoPostStore, PREP_RUN_PREFIX
from features.x_auto_posts.publisher import AutoLiveGates, AutoPostExecutor
from features.x_auto_posts.service import XAutoPostService
from features.x_auto_posts.selector import NoEligibleMaterial

class SlotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 11, 25, 15, 40, tzinfo=timezone.utc)
        self.slot = self.now + timedelta(minutes=30)
        self.store = XAutoPostStore(Path(self.tmp.name)/"plan.sqlite3", now_fn=lambda: self.now)
        config = {"schedule": {"mode":"fixed", "times":["00:10"]}, "account_ids":["640"],
                  "body_template":"{{drama_name}} {{desc}} {{url}}","platform":0,"metric_window_days":7,"language_source":"account"}
        t = self.store.create_template(name="test",description="",config=config,confirmation={"accepted":True})
        self.t = self.store.set_template_enabled(t.id,True,expected_version=t.version)
        selector = SimpleNamespace(source=SimpleNamespace(blacklist_snapshot=lambda: SimpleNamespace(
            loaded_at_utc=self.now.isoformat(),source_row_count=0,sha256="a"*64)))
        self.executor = AutoPostExecutor(self.store,selector,Mock(),lambda *a:{},lambda *a:{},
            gates=AutoLiveGates(True,True,True),now_fn=lambda:self.now)
        self.svc = XAutoPostService(self.store,Mock(),selector,self.executor,now_fn=lambda:self.now)
        def accounts(run, config):
            existing = self.store.list_tasks(run_id=run.id)
            return existing or [self.store.create_task(run_id=run.id, account_id="640",
                language="en",account_snapshot_version=2,account_snapshot={"username":"test","language_source":"account","drama_language":"en"})]
        self.svc._run_tasks = accounts
        def select(task, token):
            self.store.reserve_material(task_id=task.id,material_id="101",content_id="7",
                series_code="S",selection_snapshot={"material":{"material_id":"101","content_id":"7"}},
                claim_token=token)
            return self.store.get_task(task.id)
        self.executor._select = select

    def test_midnight_selection_is_reserved_until_original_slot(self):
        run_id, = self.svc.tick()["created_runs"]
        self.assertEqual(self.svc.tick()["created_runs"], [])
        result = self.executor.execute_next("offline")
        self.assertEqual(result["task"]["status"], "reserved")
        self.assertFalse(self.executor.execute_next("offline")["claimed"])
        task, = self.store.list_tasks(run_id=run_id)
        self.assertEqual(task.scheduled_at_utc, self.slot.isoformat())
        self.assertIsNone(task.execution_queue_id)
        self.now = self.slot
        self.assertFalse(self.executor.execute_next("offline")["claimed"])
        self.svc.tick()
        self.svc.tick()
        self.assertFalse(self.store.get_run(run_id).run_key.startswith(PREP_RUN_PREFIX))
        claim = self.store.claim_next_executable_task(worker_id="offline",lease_seconds=120,now=self.now)
        self.assertEqual((claim.task.id,claim.claim_phase),(task.id,"prepare"))
        self.assertFalse(self.executor.bridge.mock_calls)

    def test_disable_retires_only_provisional_reservation(self):
        rid, = self.svc.tick()["created_runs"]
        self.executor.execute_next("offline")
        self.store.set_template_enabled(self.t.id,False,expected_version=self.t.version)
        self.svc.tick()
        self.assertEqual(self.store.get_run(rid).status,"canceled")
        self.assertFalse(self.store.material_is_reserved("101"))
        self.now = self.slot
        self.assertFalse(self.executor.execute_next("offline")["claimed"])

    def test_preview_miss_retries_at_original_slot(self):
        self.svc.tick()
        with patch.object(self.executor,"_select", side_effect=NoEligibleMaterial({"missing":1})):
            self.assertEqual(self.executor.execute_next("offline")["task"]["status"],"retry_wait")
        self.assertFalse(self.executor.execute_next("offline")["claimed"])
        self.now = self.slot
        self.svc.tick()
        claim = self.store.claim_next_executable_task(worker_id="offline",lease_seconds=120,now=self.now)
        self.assertEqual(claim.claim_phase,"selection")

