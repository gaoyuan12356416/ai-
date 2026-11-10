import json
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from features import social_random_schedule as daily
from features.fb_auto_posts.core import ActorScope, FBAutoPostStore
from features.fb_auto_posts.repositories import CandidateSnapshot, MaterialCandidate, PageTarget
from features.fb_auto_posts import target_schedule
from scripts.test_fb_auto_validation import payload


class Pages:
    def legacy_conflicts(self, _): return []
    def list_pages(self, *_args, **_kwargs):
        return [PageTarget("6", ("6",), pid, "248", "UTC", "en", 1) for pid in ("10001", "10002")]


class Materials:
    def candidate_snapshot(self, _):
        return CandidateSnapshot(tuple(MaterialCandidate(str(501+i), "drama"+str(i), f"https://cdn.example/{i}.mp4", "M", "D", "en", Decimal("30"), Decimal("5"), Decimal("50"), Decimal("10"), Decimal("60"), "1") for i in range(8)), (11,), ("2026-11-09",))
    def choose_from(self, candidates, excluded):
        return next((item for item in candidates if item.material_id not in set(excluded)), None)


class TargetMidnightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 10, 4, tzinfo=timezone.utc)
        self.store = FBAutoPostStore(Path(self.tmp.name)/"fb.sqlite3", now_fn=lambda:self.now)
        self.actor = ActorScope("u", "测试", False, "248")
        raw=payload(); raw["schedule"]={"mode":"random","daily_count":2,"start":"08:00","end":"23:59"}
        self.template=self.store.create_template(raw,self.actor,{"app_id":"1479","product":"Dramawave"})
        self.store.set_template_status(self.template["id"],True,self.actor,1)
        self.now=datetime(2026,11,10,16,tzinfo=timezone.utc)

    def tearDown(self): self.tmp.cleanup()

    def plan(self):
        self.store.enqueue_due_slots(live_enabled=True,prebuild_days_ahead=2)
        results=[]
        while (due:=self.store.claim_due_slot("p")):
            result=self.store.create_run(due["template_id"],due["slot_key"],"auto",self.actor,Pages(),Materials(),planned_publish_at_utc=due["planned_publish_at_utc"],expected_template_version=due["template_version"],expected_due_id=due["id"],expected_due_lease_owner=due["lease_owner"],expected_due_lease_expires_at_utc=due["lease_expires_at_utc"])
            self.store.complete_due_slot(due["id"],run_id=result["run_id"])
            results.append(result)
        return results

    def tasks(self):
        with self.store.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM fb_auto_task ORDER BY page_id,planned_publish_at_utc")]

    def test_midnight_independent_times_and_restart(self):
        self.assertEqual(len(self.plan()),2)
        tasks=self.tasks();self.assertEqual(len(tasks),4)
        self.assertTrue(all(t["schedule_policy"]==daily.POLICY for t in tasks))
        by_page={pid:[t["planned_publish_at_utc"] for t in tasks if t["page_id"]==pid] for pid in ("10001","10002")}
        self.assertNotEqual(by_page["10001"],by_page["10002"])
        for times in by_page.values():
            self.assertGreaterEqual((datetime.fromisoformat(times[1])-datetime.fromisoformat(times[0])).total_seconds(),3600)
            self.assertTrue(all(datetime.fromisoformat(t).minute != 0 for t in times))
        self.assertEqual(self.store.enqueue_due_slots(live_enabled=True,prebuild_days_ahead=2)["enqueued"],0)
        self.assertEqual(self.tasks(),tasks)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM social_random_day WHERE local_date>'2026-11-11'").fetchone()[0],0)

    def test_ready_waits_and_late_preparation_survives_midnight(self):
        self.plan();tasks=self.tasks()
        task=self.store.claim_prepare_next("prepare",task_id=tasks[0]["id"])
        self.assertIsNotNone(task)
        self.store.complete_prepare(task["id"],{"media_url":"https://cdn.example/prepared.mp4","sha256":"a"*64,"size_bytes":10,"duration_seconds":30,"profile":"test"})
        self.assertIsNone(self.store.claim_next("publisher",task_id=task["id"]))
        self.now=datetime(2026,11,12,16,tzinfo=timezone.utc)
        late=self.store.claim_prepare_next("prepare",task_id=tasks[-1]["id"])
        self.assertIsNotNone(late)
        self.store.complete_prepare(late["id"],{"media_url":"https://cdn.example/prepared2.mp4","sha256":"b"*64,"size_bytes":10,"duration_seconds":30,"profile":"test"})
        self.assertIsNotNone(self.store.claim_next("publisher",task_id=late["id"]))

    def test_day_plan_freezes_edits_and_existing_binding(self):
        self.plan();before=self.tasks()
        self.store.set_template_status(self.template["id"],False,self.actor,1)
        raw=payload();raw["schedule"]={"mode":"random","daily_count":3,"start":"08:00","end":"23:59"}
        updated=self.store.update_template(self.template["id"],raw,self.actor,1)
        self.store.set_template_status(self.template["id"],True,self.actor,updated["version"])
        self.assertEqual(self.plan(),[])
        self.assertEqual(self.tasks(),before)
        self.assertIsNotNone(self.store.claim_prepare_next("p",task_id=before[0]["id"]))

    def test_frequency_zero_and_reduced_count(self):
        self.store.set_template_status(self.template["id"],False,self.actor,1)
        raw=payload();raw["schedule"]={"mode":"random","daily_count":2,"start":"08:00","end":"23:59"}
        raw["default_daily_count"]=0;raw["page_daily_limits"]=[{"page_id":"10001","daily_count":1,"tier":"A","name":"One"}]
        config=__import__("features.fb_auto_posts.validation",fromlist=["normalize_template_payload"]).normalize_template_payload(raw)
        with self.store.connect() as conn:
            plan=target_schedule.ensure_plan(conn,99,1,config,"2026-11-11",["10001","10002"],now=self.now)
        self.assertEqual([len(t["slots"]) for t in plan["targets"]],[1,0])

    def test_fixed_calendar_prebuild_is_unchanged(self):
        self.now=datetime(2026,11,10,4,tzinfo=timezone.utc)
        fixed=self.store.create_template(payload(),self.actor,{"app_id":"1479","product":"Dramawave"})
        self.store.set_template_status(fixed["id"],True,self.actor,1)
        self.now=datetime(2026,11,10,16,tzinfo=timezone.utc)
        self.store.enqueue_due_slots(live_enabled=True,prebuild_days_ahead=2)
        with self.store.connect() as conn:
            fixed_rows=list(conn.execute("SELECT * FROM fb_auto_due_slot WHERE template_id=?",(fixed["id"],)))
            random_rows=list(conn.execute("SELECT * FROM fb_auto_due_slot WHERE template_id=?",(self.template["id"],)))
        self.assertEqual(len(fixed_rows),3)
        self.assertTrue(all(not r['schedule_policy'] for r in fixed_rows))
        self.assertEqual(len(random_rows),2)
        self.assertTrue(all(r['schedule_policy']==daily.POLICY for r in random_rows))

    def test_mapped_existing_task_is_not_recreated(self):
        config=self.store.get_template(self.template['id'],self.actor)['config']
        old=self.store.create_run(self.template['id'],'manual:existing','manual',self.actor,Pages(),Materials())
        originals=self.tasks()
        with self.store.connect() as conn:
            plan=target_schedule.ensure_plan(conn,self.template['id'],1,config,'2026-11-11',[t['page_id'] for t in originals],now=self.now)
            by_page={t['target_id']:t for t in plan['targets']}
            for task in originals:
                daily.bind_slot(conn,target_schedule.source_key(self.template['id']),'2026-11-11',task['page_id'],0,'fb_auto_task',task['id'])
                when=by_page[task['page_id']]['slots'][0]['scheduled_at'].replace('Z','+00:00')
                conn.execute('UPDATE fb_auto_task SET planned_publish_at_utc=?,schedule_policy=? WHERE id=?',(when,daily.POLICY,task['id']))
        self.plan()
        self.assertEqual(len(self.tasks()),4)
        self.assertTrue({t['id'] for t in originals}.issubset({t['id'] for t in self.tasks()}))


class GroupTargetMidnightTests(unittest.TestCase):
    def setUp(self):
        from features.fb_video_publish.core import Store, canonical, stamp
        from features.fb_video_publish.group_pool import GroupPoolService
        from features.fb_video_publish.group_scheduler import GroupScheduler
        from features.fb_video_publish.group_language import POLICY_VERSION
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=datetime(2026,11,10,4,tzinfo=timezone.utc)
        self.store=Store(Path(self.tmp.name)/'group.sqlite',now_fn=lambda:self.now)
        self.actor=dict(tenant_key="tenant",user_id="user",user_name="Tester",is_admin=False,business_owner_id="803",sub_user_id="100")
        self.pages={pid:{"page_id":pid,"page_name":"Page "+pid,"language":"en","language_kind":"specific","eligible_token_count":1,"group_ids":["1"]} for pid in ("11","12")}
        self.materials={str(i):{"material_id":str(i),"material_name":"Material "+str(i),"content_id":str(i+100),"language":"en","drama_name":"Drama","drama_description":"Description","material_tag":"","duration_seconds":30,"media_url":f"https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/test/{i}.mp4"} for i in range(21,29)}
        sources=SimpleNamespace(resolve_scope=lambda *args,**kwargs:{"pages":self.pages,"excluded":[]},resolve_materials=lambda mids,**kwargs:{"items":[self.materials[mid] for mid in mids],"errors":[]})
        service=SimpleNamespace(store=self.store,live_enabled=True,group_pool_enabled=True)
        self.pool=GroupPoolService(service,sources=sources)
        self.scheduler=GroupScheduler(self.pool,lambda tenant,user:self.actor)
        now=stamp(self.now)
        with self.store.connect() as conn:
            conn.execute("INSERT INTO fb_group_pool(tenant_key,version,schedule_revision,status,group_ids_json,schedule_mode,daily_batches,caption_template,publisher_user_id,effective_at_utc,created_at_utc,updated_at_utc,routing_policy_version) VALUES('tenant',1,1,'enabled','[\"1\"]','random',2,'{name}','user','2026-11-10T16:00:00+00:00',?,?,?)",(now,now,POLICY_VERSION))
            for mid, material in self.materials.items():
                conn.execute("INSERT INTO fb_group_pool_item(tenant_key,material_id,language,material_json,added_by,created_at_utc,updated_at_utc) VALUES('tenant',?,'en',?,'user',?,?)",(mid,canonical(material),now,now))
        self.now=datetime(2026,11,10,16,7,tzinfo=timezone.utc)
        self.scheduler.start('test')

    def test_midnight_catchup_prepares_all_rounds_without_early_publish(self):
        result=self.scheduler.tick('test');self.assertEqual(result['runs_created'],2)
        with self.store.connect() as conn:
            tasks=[dict(r) for r in conn.execute('SELECT * FROM fb_video_task ORDER BY page_id,planned_at_utc')]
            conn.execute("UPDATE fb_video_task SET link_state='ready'")
        self.assertEqual(len(tasks),4)
        self.assertTrue(all(t['processing_mode']=='original' and t['prepare_state']=='ready' for t in tasks))
        self.assertTrue(all(json.loads(t['snapshot_json'])['group_pool']['schedule_policy']==daily.POLICY for t in tasks))
        times={pid:[t['planned_at_utc'] for t in tasks if t['page_id']==pid] for pid in ('11','12')}
        self.assertNotEqual(times['11'],times['12'])
        self.assertEqual(self.scheduler.tick('test')['runs_created'],0)
        self.assertIsNone(self.store.claim('execute','publisher'))
        earliest=min(datetime.fromisoformat(t['planned_at_utc']) for t in tasks)
        self.now=earliest
        self.assertIsNotNone(self.store.claim('execute','publisher'))

    def test_same_day_config_edit_does_not_redraw(self):
        self.scheduler.tick('test')
        with self.store.connect() as conn:
            before=[tuple(r) for r in conn.execute('SELECT task_id,planned_at_utc FROM fb_video_task ORDER BY task_id')]
            conn.execute("UPDATE fb_group_pool SET daily_batches=3,schedule_revision=2,effective_at_utc='2026-11-11T16:00:00+00:00'")
        self.assertEqual(self.scheduler.tick('test')['runs_created'],0)
        with self.store.connect() as conn:
            after=[tuple(r) for r in conn.execute('SELECT task_id,planned_at_utc FROM fb_video_task ORDER BY task_id')]
        self.assertEqual(before,after)


if __name__=="__main__":unittest.main()
