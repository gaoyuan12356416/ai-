"""Task-level uncertainty must not become a permanent Facebook Page lock."""
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from features.fb_auto_posts.core import ActorScope, FBAutoPostStore, utc_iso
from features.fb_auto_posts.publisher import AutoPostExecutor, GraphResult
from features.fb_auto_posts.repositories import CandidateSnapshot, PageCredential, PageTarget
from features.fb_auto_posts.hit_material import _skip_reason
from features.fb_auto_posts.manual_batch import build_manifest, candidate
from features.fb_auto_posts.pool_extension import extend_future_runs, scope_fingerprint
from features.fb_video_publish.core import DomainError, Store, stamp
from features.fb_video_publish.guards import automatic_block, manual_block
from features.fb_video_publish.pool_scheduler import PoolScheduler
from features.fb_video_publish.worker import Worker


class PageIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'fb.sqlite3'
        self.now = datetime(2026, 11, 4, 4, tzinfo=timezone.utc)
        self.auto = FBAutoPostStore(self.path, now_fn=lambda: self.now)
        self.manual = Store(self.path, now_fn=lambda: self.now)
        with self.auto.connect() as c:
            c.execute("INSERT INTO fb_auto_template(id,name,status,created_at_utc,updated_at_utc) VALUES(1,'test','enabled',?,?)", (stamp(self.now), stamp(self.now)))
            c.execute("INSERT INTO fb_auto_template_version VALUES(1,1,?,'hash','operator',?)", (json.dumps({'drama_cooldown_hours': 0}), stamp(self.now)))

    def tearDown(self):
        self.temp.cleanup()

    def add_manual(self, material='101', status='queued', *, page='1001', batch=None, sequence=1, **extra):
        task, batch = uuid.uuid4().hex, batch or uuid.uuid4().hex
        at = stamp(self.now - timedelta(minutes=1))
        with self.manual.connect() as c:
            c.execute("INSERT OR IGNORE INTO fb_video_batch VALUES(?,?,?,?,?,?,?,?)", (batch, 'tenant', 'operator', 'Operator', batch, batch, '{}', at))
            row = dict(task_id=task, batch_id=batch, tenant_key='tenant', owner_user_id='operator',
                       page_id=page, material_id=material, content_id='9001', sequence=sequence,
                       page_sequence=sequence, planned_at_utc=at, planned_bj_date='2026-11-04',
                       processing_mode='original', status=status, snapshot_json='{}', prepare_state='ready',
                       link_state='ready', created_at_utc=at, updated_at_utc=at,
                       unknown_outcome=int(status == 'unknown'))
            row.update(extra)
            c.execute('INSERT INTO fb_video_task(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', list(row.values()))
        return task

    def add_auto(self, material='201', status='ready', *, page='1001', **extra):
        at = utc_iso(self.now - timedelta(minutes=1))
        with self.auto.connect() as c:
            run = c.execute("INSERT INTO fb_auto_run(template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc) VALUES(1,1,?,'auto','queued',?,?)", (uuid.uuid4().hex, json.dumps({'drama_cooldown_hours': 0}), at)).lastrowid
            row = dict(run_id=run, template_id=1, template_version=1, page_id=page, group_id='1', status=status,
                       material_id=material, content_id='9001', media_url='https://media.invalid/render.mp4',
                       source_media_url='https://media.invalid/source.mp4', prepared_media_url='https://media.invalid/render.mp4',
                       prepared_at_utc=at, created_at_utc=at, planned_publish_at_utc=at,
                       unknown_outcome=int(status == 'unknown'))
            row.update(extra)
            return c.execute('INSERT INTO fb_auto_task(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')', list(row.values())).lastrowid

    def test_unknown_manual_only_blocks_same_pair(self):
        old = self.add_manual(status='unknown')
        other = self.add_manual('102')
        with self.manual.connect() as c:
            self.assertEqual(manual_block(c, '1001', '101'), 'fb_video_material_unknown')
            self.assertEqual(manual_block(c, '1001', '102'), 'fb_video_material_active')
            self.assertEqual(manual_block(c, '1001', '999'), '')
            self.assertEqual(manual_block(c, '1002', '101'), '')
        self.assertEqual(self.manual.claim('execute', 'worker', task_id=other)['task_id'], other)
        with self.manual.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM fb_video_task WHERE task_id=?', (old,)).fetchone()[0], 'unknown')

    def test_same_material_unknown_cannot_execute_even_from_another_batch(self):
        self.add_manual(status='unknown')
        duplicate = self.add_manual('101')
        self.assertIsNone(self.manual.claim('execute', 'worker', task_id=duplicate))
        request = {'pairs': [{'page_id': '1001', 'material_id': '101', 'config_version': 0, 'config_instance_id': ''}]}
        with self.manual.connect() as c, self.assertRaises(DomainError) as error:
            self.manual.check_plan(c, {'tenant_key': 'tenant'}, request, {'materials': {'101': {}}}, manual=True)
        self.assertEqual(error.exception.code, 'fb_video_material_unknown')
        self.assertIn('此素材', str(error.exception))

    def test_submitted_and_late_receipts_do_not_hold_page_write_mutex(self):
        submitted = self.add_manual(status='submitted', graph_post_id='900001')
        ready = self.add_manual('102')
        claimed = self.manual.claim('execute', 'publisher', task_id=ready)
        reconcile = self.manual.claim('reconcile', 'reader', task_id=submitted)
        self.assertEqual(reconcile['task_id'], submitted)
        self.assertEqual(self.manual.record_outcome(reconcile, GraphResult('success', '900001'), reconcile=True)['status'], 'published')
        self.assertTrue(self.manual.claim_is_current(claimed))
        late = self.add_manual('103', status='unknown')
        with self.manual.connect() as c:
            c.execute("UPDATE fb_video_task SET status='submitted',unknown_outcome=0,graph_post_id='900003' WHERE task_id=?", (late,))
        self.assertTrue(self.manual.claim_is_current(claimed))

    def test_unready_predecessor_does_not_block_ready_original(self):
        for kind in ['manual', 'pool', 'group_pool']:
            with self.subTest(kind=kind):
                page, batch = str(2000 + ['manual', 'pool', 'group_pool'].index(kind)), uuid.uuid4().hex
                self.add_manual('301', page=page, batch=batch, prepare_state='preparing', source_kind=kind, pool_sequence=1)
                target = self.add_manual('302', page=page, batch=batch, sequence=2, source_kind=kind, pool_sequence=2)
                self.assertEqual(self.manual.claim('execute', 'worker', task_id=target)['task_id'], target)

    def test_cross_family_unknown_is_material_scoped_and_live_write_is_serial(self):
        self.add_manual('101', status='unknown')
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_auto('101')
        target = self.add_auto('201')
        self.assertEqual(self.auto.claim_next('auto-worker', task_id=target)['id'], target)
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_manual('102', status='running')
        with self.auto.connect() as c:
            c.execute("UPDATE fb_auto_task SET status='submitted',graph_post_id='900002' WHERE id=?", (target,))
        ready = self.add_manual('102')
        self.assertEqual(self.manual.claim('execute', 'manual-worker', task_id=ready)['task_id'], ready)

    def test_auto_unknown_and_ledger_flag_scope_to_material(self):
        old = self.add_auto('201', status='unknown')
        target = self.add_auto('202', planned_publish_at_utc=stamp(self.now - timedelta(seconds=30)))
        self.assertEqual(self.auto.claim_next('auto-worker', task_id=target)['id'], target)
        with self.auto.connect() as c:
            c.execute("INSERT INTO fb_auto_publish_ledger VALUES(999,'1001','888','failed','',0,1,'old',?,?)", (stamp(self.now), stamp(self.now)))
            self.assertEqual(automatic_block(c, '1001', '888'), 'fb_video_material_unknown')
            self.assertEqual(automatic_block(c, '1001', '889'), '')
            self.assertIn('888', self.auto._cooldown_material_ids(c, '1001', 0))
        ready = self.add_manual('888')
        self.assertIsNone(self.manual.claim('execute', 'worker', task_id=ready))

    def test_missing_or_invalid_unknown_identity_keeps_explicit_page_hold(self):
        for mid in ['', 'invalid', '0', '12 3']:
            with self.subTest(mid=mid):
                page = str(3000 + ['', 'invalid', '0', '12 3'].index(mid))
                self.add_manual(mid, status='failed', page=page, unknown_outcome=1)
                with self.manual.connect() as c:
                    self.assertEqual(manual_block(c, page, '999'), 'fb_video_page_unknown')
        known = DomainError('fb_video_material_unknown', 'known', 409)
        missing = DomainError('fb_video_page_unknown', 'missing', 409)
        self.assertEqual(PoolScheduler._failure(known)[2:], (False, True))
        self.assertEqual(PoolScheduler._failure(missing)[2:], (True, False))

    def test_expired_claim_without_write_intent_settles_failed(self):
        old = self.add_manual(status='running', execution_lease='lease', execution_until=stamp(self.now - timedelta(seconds=1)))
        target = self.add_manual('102')
        result = self.manual.tick()
        self.assertEqual(result['failed_before_publish'], 1)
        self.assertEqual(result['unknown_marked'], 0)
        with self.manual.connect() as c:
            row = c.execute('SELECT status,error_code,unknown_outcome FROM fb_video_task WHERE task_id=?', (old,)).fetchone()
            self.assertEqual(tuple(row), ('failed', 'worker_interrupted_before_publish', 0))
        self.assertEqual(self.manual.claim('execute', 'worker', task_id=target)['task_id'], target)

    def test_expired_claim_with_write_intent_remains_unknown_but_does_not_block_other_material(self):
        old = self.add_manual(status='running', execution_lease='lease', execution_until=stamp(self.now - timedelta(seconds=1)))
        with self.manual.connect() as c:
            c.execute("INSERT INTO fb_video_attempt(task_id,lease_token,credential_id,fb_user_id,result_kind,created_at_utc) VALUES(?,'lease','credential','owner','started',?)", (old, stamp(self.now)))
        target = self.add_manual('102')
        self.assertEqual(self.manual.tick()['unknown_marked'], 1)
        self.assertEqual(self.manual.claim('execute', 'worker', task_id=target)['task_id'], target)
        with self.manual.connect() as c:
            self.assertEqual(manual_block(c, '1001', '101'), 'fb_video_material_unknown')

    def test_pipeline_operation_alone_prevents_no_write_inference(self):
        old = self.add_manual(status='running', execution_lease='lease', execution_until=stamp(self.now - timedelta(seconds=1)))
        with self.manual.connect() as c:
            c.execute('CREATE TABLE pipeline_fb_operation(source_key TEXT)')
            c.execute('INSERT INTO pipeline_fb_operation VALUES(?)', ('fb:video:' + old,))
        self.assertEqual(self.manual.tick()['unknown_marked'], 1)

    def test_original_attempt_can_save_late_acceptance_without_touching_new_writer(self):
        old = self.add_manual()
        claim = self.manual.claim('execute', 'old-worker', task_id=old)
        attempt = self.manual.begin_attempt(claim, 'credential', 'owner')
        self.now += timedelta(minutes=21)
        self.manual.tick()
        target = self.add_manual('102')
        current = self.manual.claim('execute', 'new-worker', task_id=target)
        wrong = dict(claim, claim_token='wrong-lease')
        result = self.manual.record_outcome(wrong, GraphResult('success', '900001'), attempt_id=attempt)
        self.assertEqual(result['status'], 'stale_claim')
        result = self.manual.record_outcome(claim, GraphResult('success', '900001'))
        self.assertEqual(result['status'], 'stale_claim')
        result = self.manual.record_outcome(claim, GraphResult('unknown'), attempt_id=attempt)
        self.assertEqual(result['status'], 'stale_claim')
        with self.manual.connect() as c:
            c.execute("UPDATE fb_video_task SET graph_post_id='different-id' WHERE task_id=?", (old,))
        result = self.manual.record_outcome(claim, GraphResult('success', '900001'), attempt_id=attempt)
        self.assertEqual(result['status'], 'stale_claim')
        with self.manual.connect() as c:
            c.execute("UPDATE fb_video_task SET graph_post_id='' WHERE task_id=?", (old,))
        result = self.manual.record_outcome(claim, GraphResult('success', '900001'), attempt_id=attempt)
        self.assertEqual(result['status'], 'submitted')
        self.assertTrue(self.manual.claim_is_current(current))
        self.assertIsNone(self.manual.begin_attempt(claim, 'credential', 'owner'))
        with self.manual.connect() as c:
            self.assertEqual(tuple(c.execute('SELECT result_kind,graph_post_id FROM fb_video_attempt WHERE id=?', (attempt,)).fetchone()), ('success', '900001'))

    def test_uncertainty_flag_alone_prevents_no_write_inference(self):
        old = self.add_manual(status='running', unknown_outcome=1,
                              execution_lease='lease', execution_until=stamp(self.now - timedelta(seconds=1)))
        result = self.manual.tick()
        self.assertEqual(result['failed_before_publish'], 0)
        self.assertEqual(result['unknown_marked'], 1)
        with self.manual.connect() as c:
            self.assertEqual(manual_block(c, '1001', '101'), 'fb_video_material_unknown')

    def test_concurrent_claims_still_allow_only_one_page_write(self):
        first, second = self.add_manual('101'), self.add_manual('102')
        barrier = threading.Barrier(2)
        def claim(tid):
            store = Store.__new__(Store)
            store.path, store.now_fn, store.daily_limit = str(self.path), lambda: self.now, 1000
            barrier.wait()
            return store.claim('execute', tid, task_id=tid)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, [first, second]))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_overtaken_preparation_preserves_original_batch_publish_interval(self):
        batch = uuid.uuid4().hex
        earlier = self.add_manual(batch=batch, sequence=1, prepare_state='preparing')
        later = self.add_manual('102', batch=batch, sequence=2)
        with self.manual.connect() as c:
            c.execute('UPDATE fb_video_batch SET request_json=? WHERE batch_id=?', (json.dumps({'interval_minutes': 10}), batch))
        claim = self.manual.claim('execute', 'worker', task_id=later)
        self.manual.record_outcome(claim, GraphResult('success', '900001'))
        self.now += timedelta(minutes=1)
        readback = self.manual.claim('reconcile', 'reader', task_id=later)
        self.manual.record_outcome(readback, GraphResult('success', '900001'), reconcile=True)
        with self.manual.connect() as c:
            c.execute("UPDATE fb_video_task SET prepare_state='ready' WHERE task_id=?", (earlier,))
        self.assertIsNone(self.manual.claim('execute', 'worker', task_id=earlier))
        self.now += timedelta(minutes=10)
        self.assertEqual(self.manual.claim('execute', 'worker', task_id=earlier)['task_id'], earlier)

    def test_auto_expired_workers_settle_without_replaying_or_releasing_live_work(self):
        expired = utc_iso(self.now - timedelta(seconds=1))
        live = utc_iso(self.now + timedelta(minutes=20))
        preparing = self.add_auto('201', status='preparing', lease_expires_at_utc=expired)
        running = self.add_auto('202', status='running', page='1002', lease_expires_at_utc=expired)
        active = self.add_auto('203', status='preparing', page='1003', lease_expires_at_utc=live)
        pending = self.add_auto('204', status='planned', page='1004')
        malformed = self.add_auto('205', status='running', page='1005', lease_expires_at_utc='')
        self.assertEqual(self.auto.maintenance_tick(), {'failed_preparing': 1, 'unknown_marked': 1})
        with self.auto.connect() as c:
            states = dict(c.execute('SELECT id,status FROM fb_auto_task'))
            self.assertEqual([states[x] for x in (preparing, running, active, pending, malformed)],
                             ['failed', 'unknown', 'preparing', 'planned', 'running'])
            self.assertEqual(automatic_block(c, '1002', '202'), 'fb_video_material_unknown')
        with self.assertRaises(Exception) as late:
            self.auto.complete_prepare(preparing, {})
        self.assertEqual(getattr(late.exception, 'code', ''), 'fb_auto_prepare_not_claimed')

    @staticmethod
    def page(page_id='1001'):
        return PageTarget('1', ('1',), page_id, 'operator', 'Asia/Shanghai', 'en', 1)

    @staticmethod
    def material(material_id):
        return dict(material_id=material_id, content_id='9001', media_url='https://media.invalid/' + material_id + '.mp4',
                    material_name='Material', drama_name='Drama', language='en', duration_seconds=12,
                    series_code='series', drama_description='Description')

    @staticmethod
    def config():
        return dict(cooldown_days=0, video_template='random_overlay', group_ids=['1'],
                    schedule={'mode': 'fixed', 'times': ['12:00']}, message_template='Test caption')

    def test_hit_material_uses_material_scope_and_preserves_missing_identity_fallback(self):
        old = self.add_auto('201', status='unknown')
        with self.auto.connect() as c:
            self.assertEqual(_skip_reason(c, self.auto, self.config(), self.page(), '202', utc_iso(self.now)), '')
            self.assertEqual(_skip_reason(c, self.auto, self.config(), self.page(), '201', utc_iso(self.now)), 'fb_video_material_unknown')
            c.execute("UPDATE fb_auto_task SET material_id='' WHERE id=?", (old,))
            self.assertEqual(_skip_reason(c, self.auto, self.config(), self.page(), '202', utc_iso(self.now)), 'fb_video_page_unknown')

    def test_exact_material_manifest_skips_uncertain_pairs_not_entire_page(self):
        self.add_auto('201', status='unknown')
        self.add_auto('', status='unknown', page='1002')
        template = dict(id=1, current_version=1, status='enabled', config_json=json.dumps(self.config()), owner_user_id='operator')
        materials = {mid: self.material(mid) for mid in ['201', '202', '203']}
        with self.auto.connect() as c:
            c.execute("INSERT INTO fb_auto_publish_ledger(task_id,page_id,material_id,status,unknown_outcome,created_at_utc,updated_at_utc) VALUES(999,'1001','203','failed',1,?,?)", (utc_iso(self.now), utc_iso(self.now)))
            manifest = build_manifest(c, template, [self.page(), self.page('1002')], materials, list(materials), 'test-operation',
                                      waived_series='series', waive_drama_cooldown=True, now=self.now)
        self.assertEqual([(x['page_id'], x['material_id']) for x in manifest['entries']], [('1001', '202')])
        self.assertEqual(manifest['skipped_pages'], [{'page_id': '1002', 'language': 'en', 'reason': 'page_unknown'}])

    def test_run_planning_skips_unidentified_page_but_keeps_other_page(self):
        self.add_manual('', status='unknown')
        config = self.config()
        choices = tuple(candidate(self.material(mid)) for mid in ['201', '202'])
        pages = SimpleNamespace(list_pages=lambda *args, **kwargs: [self.page(), self.page('1002')], legacy_conflicts=lambda _: [])
        materials = SimpleNamespace(candidate_snapshot=lambda _: CandidateSnapshot(choices, (), ()),
                                    choose_from=lambda options, excluded: next((m for m in options if m.material_id not in excluded), None))
        with self.auto.connect() as c:
            c.execute('UPDATE fb_auto_template_version SET config_json=? WHERE template_id=1', (json.dumps(config),))
        result = self.auto.create_run(1, 'manual-fixture', 'manual', ActorScope('operator', 'Operator', True, ''), pages, materials)
        with self.auto.connect() as c:
            rows = [tuple(row) for row in c.execute('SELECT page_id,status,skip_reason FROM fb_auto_task WHERE run_id=? ORDER BY page_id', (result['run_id'],))]
        self.assertEqual(rows, [('1001', 'skipped', 'fb_video_page_unknown'), ('1002', 'planned', '')])

    def test_future_pool_extension_can_select_other_material_after_unknown(self):
        self.add_auto('201', status='unknown')
        config = self.config()
        materials = tuple(candidate(self.material(mid)) for mid in ['201', '202'])
        repository = SimpleNamespace(candidate_snapshot=lambda _: CandidateSnapshot(materials, (), ()),
                                     choose_from=lambda choices, excluded: next((m for m in choices if m.material_id not in excluded), None))
        with self.auto.connect() as c:
            run_id = c.execute("INSERT INTO fb_auto_run(template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc,planned_publish_at_utc) VALUES(1,1,'future','auto','queued',?,?,?)",
                               (json.dumps(config), utc_iso(self.now), utc_iso(self.now + timedelta(hours=1)))).lastrowid
        pages = [self.page()]
        result = extend_future_runs(self.auto, [run_id], pages, repository, operation_id='test-extension',
                                    expected_scope=scope_fingerprint(pages), expected_version=1)
        self.assertEqual(result[0]['added'], 1)
        with self.auto.connect() as c:
            self.assertEqual(c.execute('SELECT material_id FROM fb_auto_task WHERE run_id=?', (run_id,)).fetchone()[0], '202')

    def test_bounded_readback_keeps_submitted_when_credentials_remain_untried(self):
        credentials = [PageCredential('1001', 'owner', str(i), 'fixture') for i in range(9)]
        pages = SimpleNamespace(eligible_credentials=lambda _: list(credentials))
        rng = SimpleNamespace(shuffle=lambda _: None)
        graph = SimpleNamespace(reconcile_video=lambda post, credential: GraphResult('credential_failure', post))
        manual_id = self.add_manual(status='submitted', graph_post_id='900001')
        worker = Worker(self.manual, pages, None, graph, live_enabled=True, rng=rng)
        worker._task_credentials = lambda _: list(credentials)
        self.assertEqual(worker.reconcile_next(task_id=manual_id, max_credentials=8)['status'], 'submitted')
        auto_id = self.add_auto('201', status='submitted', graph_post_id='900002')
        executor = AutoPostExecutor(self.auto, pages, graph, live_enabled=True, rng=rng, min_request_interval_seconds=0)
        self.assertEqual(executor.reconcile_next('reader', task_id=auto_id, max_credentials=8)['status'], 'submitted')
        # Actual exhaustion of all available credentials retains the explicit
        # manual-reconciliation hold instead of pretending publication failed.
        credentials.pop()
        self.now += timedelta(minutes=6)
        self.assertEqual(worker.reconcile_next(task_id=manual_id, max_credentials=8)['status'], 'unknown')
        self.assertEqual(executor.reconcile_next('reader', task_id=auto_id, max_credentials=8)['status'], 'unknown')
        self.assertIsNone(self.auto.claim_submitted('reader-before-backoff', task_id=auto_id))

    def test_exact_known_id_unknown_can_only_be_claimed_for_readback(self):
        manual_id = self.add_manual(status='unknown', graph_post_id='900001')
        auto_id = self.add_auto('201', status='unknown', graph_post_id='900002')
        self.assertIsNone(self.manual.claim('execute', 'writer', task_id=manual_id))
        self.assertIsNone(self.auto.claim_next('writer', task_id=auto_id))
        self.assertIsNone(self.manual.claim('reconcile', 'generic-reader'))
        self.assertIsNone(self.auto.claim_submitted('generic-reader'))
        manual = self.manual.claim('reconcile', 'exact-reader', task_id=manual_id)
        auto = self.auto.claim_submitted('exact-reader', task_id=auto_id)
        self.assertEqual((manual['status'], manual['unknown_outcome']), ('submitted', 1))
        self.assertEqual((auto['status'], auto['unknown_outcome']), ('submitted', 1))
        self.assertIsNone(self.manual.claim('reconcile', 'second-reader', task_id=manual_id))
        self.assertIsNone(self.auto.claim_submitted('second-reader', task_id=auto_id))
        self.manual.record_outcome(manual, GraphResult('unknown', '900001'), reconcile=True)
        self.auto.reconcile_task(auto_id, 'submitted')
        with self.auto.connect() as c:
            self.assertEqual(manual_block(c, '1001', '101'), 'fb_video_material_unknown')
            self.assertEqual(automatic_block(c, '1001', '201'), 'fb_video_material_unknown')
        self.now += timedelta(minutes=6)
        manual = self.manual.claim('reconcile', 'exact-reader', task_id=manual_id)
        self.auto.claim_submitted('exact-reader', task_id=auto_id)
        self.manual.record_outcome(manual, GraphResult('success', '900001'), reconcile=True)
        self.auto.reconcile_task(auto_id, 'published')
        with self.auto.connect() as c:
            self.assertEqual(manual_block(c, '1001', '101'), '')
            self.assertEqual(automatic_block(c, '1001', '201'), '')

    def test_historical_unidentified_cross_family_fence_allows_receipts_but_no_new_write(self):
        known = self.add_manual(status='unknown', graph_post_id='900001')
        queued = self.add_manual('102')
        running = self.add_manual('103', status='running', execution_lease='old-lease',
                                  execution_until=stamp(self.now - timedelta(seconds=1)))
        auto_known = self.add_auto('201', status='unknown', page='1002', graph_post_id='900002')
        # Historical failed rows can still contain an unresolved publication;
        # their absent material identity protects the whole Page from writes.
        self.add_auto('', status='failed', unknown_outcome=1)
        self.add_manual('', status='failed', page='1002', unknown_outcome=1)
        with self.manual.connect() as c:
            c.execute("INSERT INTO fb_video_attempt(task_id,lease_token,credential_id,fb_user_id,result_kind,created_at_utc) VALUES(?,'old-lease','credential','owner','started',?)", (running, stamp(self.now)))
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE fb_video_task SET status='running' WHERE task_id=?", (queued,))
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE fb_video_task SET status='submitted',material_id='999' WHERE task_id=?", (known,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_manual('299')
        self.assertEqual(self.manual.tick()['unknown_marked'], 1)
        readback = self.manual.claim('reconcile', 'reader', task_id=known)
        self.assertEqual(self.manual.record_outcome(readback, GraphResult('success', '900001'), reconcile=True)['status'], 'published')
        auto_readback = self.auto.claim_submitted('reader', task_id=auto_known)
        self.assertEqual(auto_readback['status'], 'submitted')
        self.assertEqual(self.auto.reconcile_task(auto_known, 'published')['status'], 'published')
        self.assertIsNone(self.manual.claim('execute', 'writer', task_id=queued))

    def test_migration_replaces_old_index_and_triggers_without_losing_history(self):
        old = self.add_manual(status='unknown')
        with self.manual.connect() as c:
            before = tuple(c.execute('SELECT * FROM fb_video_task WHERE task_id=?', (old,)).fetchone())
            c.execute('DROP INDEX uq_fb_video_execution')
            c.execute("CREATE UNIQUE INDEX uq_fb_video_execution ON fb_video_task(page_id) WHERE status IN ('running','submitted','unknown')")
            c.execute('DROP TRIGGER fb_video_cross_auto_insert')
            c.execute("CREATE TRIGGER fb_video_cross_auto_insert BEFORE INSERT ON fb_auto_task WHEN EXISTS(SELECT 1 FROM fb_video_task WHERE page_id=NEW.page_id AND status='unknown') BEGIN SELECT RAISE(ABORT,'old_page_hold'); END")
        Store(self.path, now_fn=lambda: self.now)
        with self.manual.connect() as c:
            self.assertEqual(tuple(c.execute('SELECT * FROM fb_video_task WHERE task_id=?', (old,)).fetchone()), before)
        self.add_manual('102', status='submitted', graph_post_id='900002')
        self.add_manual('103', status='unknown')
        self.add_auto('201', status='submitted', graph_post_id='900003')
        self.add_auto('202', status='unknown', planned_publish_at_utc=stamp(self.now - timedelta(seconds=20)))
        # Restarting either store must never briefly rebuild the retired broad
        # indexes over newly valid multiple submitted / unknown rows.
        FBAutoPostStore(self.path, now_fn=lambda: self.now)
        Store(self.path, now_fn=lambda: self.now)


if __name__ == '__main__':
    unittest.main()
