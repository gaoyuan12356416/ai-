"""Offline contract tests for midnight preparation and immutable daily draws."""
import datetime as dt
import sqlite3
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from features import social_random_schedule as schedules
from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec


class DailyPlanTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        schedules.ensure_schema(self.conn, now=dt.datetime(2026, 11, 10, 3, tzinfo=dt.timezone.utc))

    def test_activation_is_persisted_next_beijing_day(self):
        self.assertEqual(schedules.activation_date(self.conn), "2026-11-11")
        schedules.ensure_schema(self.conn, now=dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc))
        self.assertEqual(schedules.activation_date(self.conn), "2026-11-11")
        self.assertFalse(schedules.is_active(self.conn,"2026-11-10"))
        self.assertTrue(schedules.is_active(self.conn,"2026-11-11"))

    def test_config_and_target_draws_frozen_once(self):
        first = schedules.ensure_day_plan(self.conn,"x:auto:1","2026-11-11",2,{"daily_count":2},["1","2"],
                                         lambda target: ["01:15","21:25"] if target=="1" else ["02:35","20:45"])
        second = schedules.ensure_day_plan(self.conn,"x:auto:1","2026-11-11",3,{"daily_count":1},["9"],
                                          lambda _: self.fail("existing draw must not be recalculated"))
        self.assertEqual(first,second)
        self.assertEqual(len(first["targets"]),2)
        self.assertEqual(first["prepare_at"],"2026-11-10T16:00:00Z")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM social_random_slot").fetchone()[0],4)

    def test_different_targets_can_share_time_and_bind_independently(self):
        schedules.ensure_day_plan(self.conn,"fb:auto:1","2026-11-11",1,{},["a","b"],lambda _: ["03:12"])
        schedules.bind_slot(self.conn,"fb:auto:1","2026-11-11","a",0,"fb_auto_task",7)
        schedules.bind_slot(self.conn,"fb:auto:1","2026-11-11","b",0,"fb_auto_task",8)
        schedules.bind_slot(self.conn,"fb:auto:1","2026-11-11","a",0,"fb_auto_task",7)
        with self.assertRaisesRegex(ValueError,"already_bound"):
            schedules.bind_slot(self.conn,"fb:auto:1","2026-11-11","a",0,"fb_auto_task",9)

    def test_schema_does_not_commit_caller_transaction(self):
        self.conn.commit()
        self.conn.execute("BEGIN")
        schedules.ensure_schema(self.conn)
        schedules.ensure_day_plan(self.conn,"tt:auto:1","2026-11-11",1,{},["a"],lambda _: ["03:12"])
        self.conn.rollback()
        self.assertIsNone(schedules.get_day_plan(self.conn,"tt:auto:1","2026-11-11"))


class PreparationProjectionTests(unittest.TestCase):
    def project(self, channel, status, *, policy=schedules.POLICY, publish="2026-11-11T15:30:00Z", retry=""):
        adapter=LedgerAdapter(LedgerSpec(channel,"auto",":memory:",channel+"_auto_task"),driver=object())
        row={"id":1,"status":status,"account_id":"a","page_id":"p","scheduled_at":publish,
             "created_at":"2026-11-10T16:00:04Z","schedule_policy":policy,
             "next_attempt_at_utc":retry,"next_prepare_at_utc":retry,
             "prepared_media_url":"https://media.example/v.mp4" if status=="ready" else ""}
        return adapter.project(row,now=dt.datetime(2026,11,10,16,1,tzinfo=dt.timezone.utc))

    def test_midnight_preparation_but_publish_at_frozen_time(self):
        for channel,pending in (("x","pending"),("tt","pending"),("fb","planned")):
            with self.subTest(channel=channel):
                preparation=self.project(channel,pending)
                self.assertEqual(preparation["stage"],"prepare")
                self.assertEqual(preparation["scheduled_at"],"2026-11-10T16:00:04Z")
                self.assertEqual(preparation["cutover_due_at"],"2026-11-11T15:30:00Z")
                ready=self.project(channel,"ready")
                self.assertEqual(ready["stage"],"publish")
                self.assertEqual(ready["scheduled_at"],"2026-11-11T15:30:00Z")

    def test_next_day_does_not_prepare_today(self):
        self.assertEqual(self.project("tt","pending",publish="2026-11-12T15:30:00Z")["scheduled_at"],"2026-11-11T16:00:00Z")

    def test_retry_backoff_remains_local(self):
        self.assertEqual(self.project("tt","pending",retry="2026-11-10T17:00:00Z")["scheduled_at"],"2026-11-10T17:00:00Z")

    def test_old_fixed_task_keeps_existing_lookahead(self):
        self.assertEqual(self.project("tt","pending",policy="")["scheduled_at"],"2026-11-11T15:30:00Z")


class NativeBoundaryTests(unittest.TestCase):
    def test_x_preparation_allowed_but_future_publish_has_no_native_claim(self):
        from scripts.test_pipeline_x_native import NativeTests
        fixture=NativeTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        queue=fixture.queue(auto=True)
        with fixture.bridge.connect() as c:
            c.execute('UPDATE x_post_manual_run SET schedule_policy=?,planned_publish_at_utc=? WHERE id=?',
                      (schedules.POLICY,'2099-11-11T12:00:00+00:00',queue['manual_run_id']))
        preparation,_,artifact=fixture.preflight(queue)
        fixture.bridge.complete(preparation,{'artifact':artifact},preparation)
        job=fixture.job(queue,'publish')
        with self.assertRaisesRegex(Exception,'发布时间'):
            fixture.bridge.reserve(job,job)
        with fixture.bridge.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM pipeline_x_claim WHERE stage='publish'").fetchone()[0],0)
            self.assertEqual(c.execute('SELECT attempt_count FROM x_post_publish_log WHERE queue_id=?',(queue['id'],)).fetchone()[0],0)

    def test_episode_dependency_defers_one_job_without_platform_attempt(self):
        from features.pipeline_dispatch.adapters import AdapterError
        from features.pipeline_dispatch.core import DispatchStore,stamp
        from features.pipeline_dispatch.service import Coordinator
        from scripts.test_pipeline_coordinator import NativeLedger
        with tempfile.TemporaryDirectory() as temporary:
            now=dt.datetime(2026,11,11,tzinfo=dt.timezone.utc)
            store=DispatchStore(Path(temporary)/'dispatch.sqlite3',lambda:now)
            native=NativeLedger();service=Coordinator(store,native,internal_token='i'*32,master_key='m'*32)
            store.register_node('cpu-main',['publish:x','backend:cpu']);store.worker_heartbeat('cpu-main','one','publish');store.channel_enabled('x',True)
            for ident in (1,2):store.offer({'source_key':'x:queue:'+str(ident),'source_id':str(ident),'channel':'x','family':'queue','stage':'publish','resource_keys':['x:account:'+str(ident)],'capabilities':['publish:x'],'scheduled_at':stamp(now),'source_created_at':stamp(now)})
            job=store.claim('cpu-main','one','publish')['job']
            with patch.object(native,'reserve',side_effect=AdapterError('x_post_drama_previous_episode_pending')):
                self.assertIsNone(service.reserve_job(job)['job'])
            saved=store.get_job(job['id'])
            self.assertEqual(saved['status'],'queued');self.assertEqual(saved['external_intent'],0)
            self.assertEqual(store.claim('cpu-main','one','publish')['job']['source_id'],'2')


if __name__ == "__main__":
    unittest.main()
