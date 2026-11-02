"""Real parent/child ledgers must distinguish workflow handoff from live work."""
import json
from datetime import timedelta
import unittest

from scripts import test_pipeline_youtube_adoption as fixtures
from features.pipeline_dispatch.adapters import AdapterRegistry, LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchError, stamp
from features.pipeline_dispatch.cutover import Cutover
from features.pipeline_dispatch.service import Coordinator


class YouTubeHandoffTests(unittest.TestCase):
    def case(self, child_status="scheduled", *, lease=0, child_reference=None, mismatch=False):
        fixture=fixtures.ScheduledAdoptionTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.dispatch.channel_enabled("youtube",False)
        if child_status=="published":
            fixture.change(status="published",video_state="published",comment_status="published",comment_id="existing-comment",comment_attempt_count=1)
        elif child_status=="unknown":
            fixture.change(status="unknown",video_state="unknown",unknown_outcome=1,schedule_status="none",schedule_command_status="")
        elif child_status=="uploading":
            fixture.change(status="uploading",lease_owner="legacy-active",lease_expires_at_utc=stamp(fixture.now+timedelta(minutes=5)))
        if mismatch:fixture.change(preparation_id="another-preparation")
        body={"id":"old-preparation","status":"uploading","current_version":1,
              "publish_id":child_reference if child_reference is not None else fixture.task["id"],
              "channel":{"channel_id":fixture.task["channel_id"]},"publish_at":fixture.due}
        with fixture.workflow.db(True) as c:
            c.execute("INSERT INTO youtube_auto_preparation(id,tenant,owner,operation_id,request_sha,state,version,body,created_at,updated_at,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("old-preparation","tenant","owner","old-operation","sha","uploading",1,json.dumps(body),stamp(fixture.now-timedelta(days=1)),stamp(fixture.now-timedelta(hours=1)),lease))
        parent=LedgerAdapter(LedgerSpec("youtube","preparation",str(fixture.store.db_path),"youtube_auto_preparation",state="state",module="youtube_auto_publish"),object())
        registry=AdapterRegistry([parent,fixture.adapter])
        coordinator=Coordinator(fixture.dispatch,registry,internal_token="i"*32,master_key="m"*32)
        return fixture,parent,Cutover(coordinator)

    @staticmethod
    def rows(fixture):
        with fixture.workflow.db() as c:
            return (dict(c.execute("SELECT * FROM youtube_auto_preparation WHERE id='old-preparation'").fetchone()),
                    dict(c.execute("SELECT * FROM drama_youtube_publish WHERE id=?",(fixture.task["id"],)).fetchone()))

    def test_exact_handoff_allows_enable_and_preserves_child_receipts_and_unknown_fences(self):
        for status in ("published","unknown","scheduled"):
            with self.subTest(child_status=status):
                fixture,parent,control=self.case(status)
                before=self.rows(fixture)
                offered=parent.project(parent.read("old-preparation"),fixture.now)
                self.assertEqual((offered["status"],offered["legacy_inflight"],offered["execution_available"]),("completed",False,False))
                plan=control.plan("youtube",stamp(fixture.now))
                self.assertEqual(plan["paused_source_keys"],[])
                self.assertEqual(control.apply(plan["plan_id"],plan["sha256"])["status"],"held")
                self.assertTrue(control.enable(plan["plan_id"],plan["sha256"])["enabled"])
                self.assertEqual(self.rows(fixture),before)
                with fixture.dispatch.transaction() as c:
                    child=dict(c.execute("SELECT * FROM pipeline_job WHERE source_key=? AND stage='publish'",(fixture.source_key,)).fetchone())
                    if status=="unknown":
                        self.assertEqual(child["status"],"unknown")
                        self.assertGreater(c.execute("SELECT COUNT(*) FROM pipeline_legacy_resource WHERE job_id=?",(child["id"],)).fetchone()[0],0)
                    elif status=="scheduled":
                        self.assertEqual(child["scheduled_at"],stamp(fixture.due))
                        self.assertEqual(json.loads(child["payload_json"])["continuation"]["video_id"],"akR3Ipdx1X4")
                with fixture.workflow.db() as c:
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_youtube_claim").fetchone()[0],0)
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_youtube_operation").fetchone()[0],0)

    def test_active_parent_lease_missing_child_and_mismatch_remain_cutover_blockers(self):
        for args in ({"lease":4102444800},{"child_reference":999999},{"mismatch":True}):
            with self.subTest(args=args):
                fixture,parent,control=self.case(**args)
                before=self.rows(fixture)
                self.assertTrue(parent.project(parent.read("old-preparation"),fixture.now)["legacy_inflight"])
                plan=control.plan("youtube",stamp(fixture.now))
                control.apply(plan["plan_id"],plan["sha256"])
                with self.assertRaisesRegex(DispatchError,"legacy_items_still_running"):
                    control.enable(plan["plan_id"],plan["sha256"])
                self.assertEqual(self.rows(fixture),before)
                self.assertNotIn("youtube",fixture.dispatch.config()["enabled_channels"])

    def test_active_child_still_blocks_even_after_parent_handoff(self):
        fixture,parent,control=self.case("uploading")
        self.assertFalse(parent.project(parent.read("old-preparation"),fixture.now)["legacy_inflight"])
        plan=control.plan("youtube",stamp(fixture.now))
        control.apply(plan["plan_id"],plan["sha256"])
        with self.assertRaisesRegex(DispatchError,"legacy_items_still_running"):
            control.enable(plan["plan_id"],plan["sha256"])


if __name__=="__main__":unittest.main()
