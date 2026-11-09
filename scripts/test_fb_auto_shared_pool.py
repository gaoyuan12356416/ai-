"""Shared template pools retain batch limits, reservations and Page write locks."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, StoreError
from features.fb_auto_posts.hit_material import publish_material
from features.fb_auto_posts.repositories import CandidateSnapshot, MaterialCandidate, PageGroup, PageTarget
from features.fb_auto_posts.service import Runtime, ServiceError
from features.fb_video_publish.guards import automatic_block
from scripts.test_fb_auto_material_product import payload, TARGET


NOW = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)


class Pages:
    def __init__(self):
        self.rows = [PageTarget('6', ('6',), str(i), '248', 'UTC', 'en', 1) for i in (1001, 1002, 1003)]
    def list_groups(self, **kwargs):
        return [PageGroup('6', '248', 0, 'Shared', '1479', 'Dramawave', 3, 3)]
    def list_pages(self, groups, **kwargs): return self.rows
    def legacy_conflicts(self, groups): return []


class Materials:
    def __init__(self):
        self.rows = [MaterialCandidate(str(i), 'drama'+str(i), 'https://cdn.example/'+str(i)+'.mp4',
                    'material'+str(i), 'drama'+str(i), 'en', Decimal(600), Decimal(1), None,
                    Decimal(1), None, '1', 'Description') for i in (10, 20, 30)]
    def candidate_snapshot(self, config): return CandidateSnapshot(tuple(self.rows), (1,), ('2026-11-08',))
    def choose_from(self, rows, excluded): return next((r for r in rows if r.material_id not in excluded), None)
    def exact_material(self, config, material_id): return next(r for r in self.rows if r.material_id == material_id)


class SharedPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = FBAutoPostStore(Path(self.tmp.name)/'test.sqlite3', now_fn=lambda:NOW)
        self.actor = ActorScope('test', 'Test', True, '248')
        self.pages, self.materials = Pages(), Materials()
        self.runtime = Runtime(self.store, self.pages, self.materials,
            SimpleNamespace(live_enabled=True), SimpleNamespace(), 'x'*32,
            metric_store=SimpleNamespace(load_metric_window=lambda **kwargs:None),
            max_jobs_per_slot=3, max_daily_jobs=1000, prebuild_enabled=True)
        self.templates=[]
        for name in ('A', 'B'):
            raw=payload();raw.update(name=name,drama_cooldown_hours=24)
            template=self.store.create_template(raw,self.actor,TARGET)
            summary=self.runtime.validate_activation(template)
            template=self.store.set_template_status(template['id'],True,self.actor,1,
                expected_enabled_fingerprint=summary['_enabled_fingerprint'])
            self.templates.append(template)

    def run_template(self, index, key, seconds=0):
        return self.store.create_run(self.templates[index]['id'],key,'manual',self.actor,
            self.pages,self.materials,planned_publish_at_utc=(NOW+timedelta(seconds=seconds)).isoformat(),
            max_jobs_per_slot=3,max_daily_jobs=1000,max_publishable_pages=500)

    def test_shared_group_can_enable_without_nominal_double_count_gate(self):
        self.assertEqual([t['status'] for t in self.templates],['enabled','enabled'])
        summary=self.runtime.validate_activation(self.templates[1])
        self.assertEqual(summary['estimated_jobs_per_slot'],3)
        self.assertEqual(summary['estimated_global_jobs_per_slot'],6)
        self.assertEqual(summary['capacity_scope'],'template_slot')

    def test_individual_batch_limit_is_still_enforced(self):
        self.runtime.max_jobs_per_slot=2
        with self.assertRaises(ServiceError) as caught:self.runtime.validate_activation(self.templates[1])
        self.assertEqual(caught.exception.code,'fb_auto_capacity_exceeded')
        with self.assertRaises(StoreError) as caught:
            self.store.create_run(self.templates[1]['id'],'over-cap','manual',self.actor,self.pages,self.materials,max_jobs_per_slot=2)
        self.assertEqual(caught.exception.code,'fb_auto_capacity_exceeded')

    def test_independent_runs_reserve_different_materials_on_same_page(self):
        first=self.run_template(0,'first')
        second=self.run_template(1,'second',60)
        self.assertEqual(first['summary']['queued_tasks'],3)
        self.assertEqual(second['summary']['queued_tasks'],3)
        with self.store.connect() as c:
            rows=c.execute("SELECT page_id,COUNT(DISTINCT material_id) FROM fb_auto_task WHERE status='planned' GROUP BY page_id").fetchall()
            self.assertTrue(all(row[1]==2 for row in rows))
        self.assertTrue(self.run_template(1,'second',60)['idempotent'])

    def test_same_page_running_mutex_still_blocks_only_actual_execution(self):
        self.run_template(0,'first');self.run_template(1,'second',60)
        with self.store.connect() as c:
            first=c.execute('SELECT MIN(id) FROM fb_auto_task WHERE page_id=?',('1001',)).fetchone()[0]
            c.execute("UPDATE fb_auto_task SET status='running' WHERE id=?",(first,))
            self.assertEqual(automatic_block(c,'1001',execution=True,exclude_task=str(first+3)),'fb_video_page_busy')
            self.assertEqual(automatic_block(c,'1002',execution=True),'')
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE fb_auto_task SET status='running' WHERE page_id='1001' AND id<>?",(first,))

    def test_unknown_material_fence_survives_shared_pool(self):
        self.run_template(0,'first')
        with self.store.connect() as c:
            c.execute("UPDATE fb_auto_task SET status='unknown',unknown_outcome=1 WHERE page_id='1001'")
            self.assertEqual(automatic_block(c,'1001','10'),'fb_video_material_unknown')
            self.assertEqual(automatic_block(c,'1001','20'),'')
        second=self.run_template(1,'second',60)
        self.assertEqual(second['summary']['queued_tasks'],3)

    def test_exact_material_route_accepts_shared_pool_and_keeps_idempotency(self):
        request=dict(expected_version=1,material_id='30',operation_id='shared-pool-test')
        result=publish_material(self.runtime,self.templates[1]['id'],self.actor,request)
        self.assertEqual(result['queued'],3)
        again=publish_material(self.runtime,self.templates[1]['id'],self.actor,request)
        self.assertTrue(again['idempotent']);self.assertEqual(result['run_id'],again['run_id'])


if __name__=='__main__':unittest.main()
