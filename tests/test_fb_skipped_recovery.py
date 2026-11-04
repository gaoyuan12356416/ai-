import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, utc_iso
from features.fb_auto_posts.repositories import PageTarget, MaterialCandidate, CandidateSnapshot
from features.fb_auto_posts.repositories import RepositoryError
from features.fb_auto_posts.skipped_recovery import recover_skipped, REASON
from features.fb_auto_posts.strategy import cooldown_content_ids
from features.fb_video_publish.core import Store


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 4, 6, tzinfo=timezone.utc)
        self.store = FBAutoPostStore(Path(self.temp.name) / 'db.sqlite3', now_fn=lambda: self.now)
        self.manual = Store(self.store.path, now_fn=lambda: self.now)
        self.cfg = dict(cooldown_days=7, drama_cooldown_hours=24, video_template='random_overlay',
            group_ids=['1'], schedule={'mode':'fixed','times':['12:00','14:00']}, message_template='Caption',
            page_daily_limits=[{'page_id':'1001','daily_count':2},{'page_id':'1002','daily_count':0}])
        self.config = json.dumps(self.cfg)
        self.pages = [PageTarget('1', ('1',), p, 'owner', 'Asia/Shanghai', 'en', 1) for p in ['1001','1002']]
        self.candidates = tuple(MaterialCandidate(str(i), str(i+1000), 'https://media.invalid/'+str(i)+'.mp4',
            'Material','Drama','en',Decimal(600),Decimal(0),None,Decimal(0),None,'') for i in range(201,211))
        self.snapshots = {'en': CandidateSnapshot(self.candidates, (), ())}
        self.materials = SimpleNamespace(choose_from=lambda rows, excluded: next((x for x in rows if x.material_id not in excluded),None),
            candidate_snapshot=lambda config: self.snapshots['en'])
        with self.store.connect() as c:
            c.execute("INSERT INTO fb_auto_template(id,name,status,created_at_utc,updated_at_utc) VALUES(1,'test','enabled',?,?)",(utc_iso(self.now),utc_iso(self.now)))
            c.execute("INSERT INTO fb_auto_template_version VALUES(1,1,?,'h','operator',?)",(self.config,utc_iso(self.now)))
            for rid in [316,317]:
                at=utc_iso(self.now-timedelta(hours=2 if rid==316 else 1))
                c.execute("INSERT INTO fb_auto_run(id,template_id,template_version,slot_key,trigger_type,status,config_json,total_pages,skipped_tasks,created_at_utc,planned_publish_at_utc) VALUES(?,1,1,?,'auto','completed',?,2,2,?,?)",(rid,str(rid),self.config,at,at))
                for page in self.pages:
                    reason=REASON if page.page_id=='1001' else 'fb_auto_page_frequency_limit'
                    c.execute("INSERT INTO fb_auto_run_page(run_id,page_id,group_id,owner_user_id,language,snapshot_status,skip_reason) VALUES(?,?,'1','owner','en','skipped',?)",(rid,page.page_id,reason))
                    c.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,skip_reason,created_at_utc,completed_at_utc,planned_publish_at_utc,video_template,gpu_job_id) VALUES(?,1,1,?,'1','skipped',?,?,?,?,'random_overlay',?)",(rid,page.page_id,reason,at,at,at,'job'+str(rid)+page.page_id))
        self.operation = 'a'*32

    def tearDown(self): self.temp.cleanup()

    def recovery(self, apply=False):
        return recover_skipped(self.store,[316,317],self.pages,self.materials,self.snapshots,
            operation_id=self.operation,expected_config=self.config,apply=apply)

    def test_preview_apply_and_repeat_preserve_identity_and_paused_page(self):
        with self.store.connect() as c:
            paused=[tuple(r) for r in c.execute("SELECT * FROM fb_auto_task WHERE page_id='1002'")]
        preview=self.recovery()
        self.assertEqual([len(x['restored']) for x in preview['runs']],[1,1])
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM fb_auto_task WHERE status='planned'").fetchone()[0],0)
        result=self.recovery(True)
        self.assertNotEqual(result['runs'][0]['restored'][0]['material_id'],result['runs'][1]['restored'][0]['material_id'])
        self.assertNotEqual(result['runs'][0]['restored'][0]['content_id'],result['runs'][1]['restored'][0]['content_id'])
        with self.store.connect() as c:
            before=[tuple(r) for r in c.execute('SELECT * FROM fb_auto_task ORDER BY id')]
            self.assertEqual(paused,[tuple(r) for r in c.execute("SELECT * FROM fb_auto_task WHERE page_id='1002'")])
        self.now += timedelta(minutes=10)
        repeated=self.recovery(True)
        self.assertEqual([len(r['already_restored']) for r in repeated['runs']],[1,1])
        with self.store.connect() as c:self.assertEqual(before,[tuple(r) for r in c.execute('SELECT * FROM fb_auto_task ORDER BY id')])

    def test_execution_evidence_never_recovered(self):
        with self.store.connect() as c:
            tid=c.execute("SELECT id FROM fb_auto_task WHERE run_id=316 AND page_id='1001'").fetchone()[0]
            c.execute("INSERT INTO fb_auto_publish_attempt(task_id,sequence,result_kind,created_at_utc) VALUES(?,1,'unknown',?)",(tid,utc_iso(self.now)))
        result=self.recovery(True)
        self.assertEqual(len(result['runs'][0]['restored']),0)
        self.assertEqual(result['runs'][0]['retained'][0]['reason'],'existing_execution_evidence')

    def test_one_occupied_slot_does_not_abort_other_recovery(self):
        with self.store.connect() as c:
            c.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,material_id,created_at_utc,planned_publish_at_utc) VALUES(999,1,1,'1001','1','planned','299',?,?)",(utc_iso(self.now),utc_iso(self.now)))
        result=self.recovery(True)
        self.assertEqual(len(result['runs'][0]['restored']),0)
        self.assertEqual(result['runs'][0]['retained'][0]['reason'],'fb_auto_page_task_conflict')
        self.assertEqual(len(result['runs'][1]['restored']),1)

    def test_all_time_published_and_unknown_materials_are_excluded(self):
        old=utc_iso(self.now-timedelta(days=30))
        with self.store.connect() as c:
            c.execute("INSERT INTO fb_auto_publish_ledger(task_id,page_id,material_id,status,graph_post_id,unknown_outcome,created_at_utc,updated_at_utc) VALUES(991,'1001','201','published','post',0,?,?)",(old,old))
            c.execute("INSERT INTO fb_auto_publish_ledger(task_id,page_id,material_id,status,unknown_outcome,created_at_utc,updated_at_utc) VALUES(992,'1001','202','unknown',1,?,?)",(old,old))
        result=self.recovery(True)
        self.assertTrue(all(x['material_id'] not in {'201','202'} for r in result['runs'] for x in r['restored']))

    def test_unresolved_drama_cooldown_expires_but_material_stays_protected(self):
        old=utc_iso(self.now-timedelta(days=2))
        with self.store.connect() as c:
            c.execute("UPDATE fb_auto_task SET status='unknown',skip_reason='',material_id='201',content_id='1201',unknown_outcome=1,started_at_utc=? WHERE run_id=316 AND page_id='1001'",(old,))
            self.assertNotIn('1201',cooldown_content_ids(c,'1001',utc_iso(self.now),24,utc_iso(self.now)))
            self.assertIn('201',self.store._cooldown_material_ids(c,'1001',0))
            c.execute("UPDATE fb_auto_task SET started_at_utc=? WHERE material_id='201'",(utc_iso(self.now),))
            self.assertIn('1201',cooldown_content_ids(c,'1001',utc_iso(self.now),24,utc_iso(self.now)))

    def test_recovery_survives_normal_late_window_then_expires(self):
        result=self.recovery(True);tid=result['runs'][0]['restored'][0]['task_id']
        self.now += timedelta(hours=1)
        self.assertEqual(self.store.claim_prepare_next('worker',task_id=tid)['id'],tid)
        self.assertEqual(self.store.complete_prepare(tid,{'media_url':'https://media.invalid/render.mp4'})['status'],'ready')
        self.assertEqual(self.store.claim_next('publisher',task_id=tid)['id'],tid)
        other=result['runs'][1]['restored'][0]['task_id']
        self.now += timedelta(days=1)
        self.assertIsNone(self.store.claim_prepare_next('worker',task_id=other))
        with self.store.connect() as c:
            self.store._expire_late_auto_work(c,utc_iso(self.now),self.store._late_cutoff())
            self.assertEqual(c.execute('SELECT status FROM fb_auto_task WHERE id=?',(other,)).fetchone()[0],'skipped')

    def test_backlog_does_not_block_next_run_and_concurrent_planners_deduplicate(self):
        self.recovery(True)
        # Two planners freeze source candidates together, then serialize reservations.
        barrier=threading.Barrier(2)
        def snapshot(config):
            barrier.wait(timeout=5)
            return self.snapshots['en']
        materials=SimpleNamespace(candidate_snapshot=snapshot,choose_from=self.materials.choose_from)
        pages=SimpleNamespace(list_pages=lambda *a,**kw:self.pages, legacy_conflicts=lambda *a:[])
        def create(i):
            return self.store.create_run(1,'manual:next:'+str(i),'manual',ActorScope('op','Operator',True,''),pages,materials,
                planned_publish_at_utc=utc_iso(self.now+timedelta(minutes=i+1)))
        with ThreadPoolExecutor(max_workers=2) as pool: runs=list(pool.map(create,[1,2]))
        with self.store.connect() as c:
            ids=[r['run_id'] for r in runs]
            materials=[r[0] for r in c.execute("SELECT material_id FROM fb_auto_task WHERE run_id IN (?,?) AND status='planned'",ids)]
            self.assertEqual(len(materials),2)
            self.assertEqual(len(set(materials)),2)

    def test_language_checkpoint_reuses_success_after_another_language_fails(self):
        from scripts.recover_fb_skipped_runs import checkpoint_snapshots
        pages=[self.pages[0],PageTarget('1',('1',),'1003','owner','Asia/Shanghai','es',1)]
        calls=[]
        failing=[True]
        def snapshot(config):
            language=config['language'];calls.append(language)
            if language=='es' and failing[0]:
                raise RepositoryError('fb_auto_source_query_failed','temporary read failure')
            return self.snapshots['en'] if language=='en' else CandidateSnapshot((),(),())
        materials=SimpleNamespace(candidate_snapshot=snapshot)
        with self.assertRaises(RuntimeError):
            checkpoint_snapshots(Path(self.temp.name),self.cfg,pages,materials,self.operation)
        failing[0]=False
        result=checkpoint_snapshots(Path(self.temp.name),self.cfg,pages,materials,self.operation)
        self.assertEqual(set(result),{'en','es'})
        self.assertEqual(calls.count('en'),1)
        self.assertEqual(calls.count('es'),2)


if __name__=='__main__': unittest.main()
