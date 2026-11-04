"""Native SQLite concurrency: slow FB sources never serialize other Pages."""
import gc
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.fb_auto_posts.core import FBAutoPostStore
from features.fb_auto_posts.repositories import PageCredential
from features.fb_video_publish.core import Store as FBVideoStore
from features.fb_video_publish.links import build_attribution_url, short_url
from features.pipeline_dispatch.adapters import AdapterError
from features.pipeline_dispatch.fb_native import FBNativeBridge


class FBSourceLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 11, 3, tzinfo=timezone.utc)
        path = str(Path(self.temp.name) / 'native.sqlite')
        self.auto = FBAutoPostStore(path, now_fn=lambda: self.now)
        self.video = FBVideoStore(path, now_fn=lambda: self.now)
        credentials = lambda page: [PageCredential(page, 'user', 'credential', 'fixture-private')]
        self.runtime = SimpleNamespace(store=self.auto, executor=SimpleNamespace(live_enabled=True),
            pages=SimpleNamespace(eligible_credentials=credentials),
            manual_worker=SimpleNamespace(store=self.video, sources=None,
                _task_credentials=lambda task: credentials(task['page_id'])))
        self.bridge = FBNativeBridge(self.runtime)
        self.claim = {'claim_token': 'a' * 32, 'node_id': 'fixture-node'}
        instant = self.now.isoformat()
        with self.auto.connect() as conn:
            conn.execute("INSERT INTO fb_auto_template(id,name,status,created_at_utc,updated_at_utc) VALUES(1,'t','enabled',?,?)", (instant, instant))
            conn.execute("INSERT INTO fb_auto_template_version(template_id,version,config_json,config_sha256,created_at_utc) VALUES(1,1,'{}',?,?)", ('a' * 64, instant))
            conn.execute("INSERT INTO fb_auto_run(id,template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc) VALUES(1,1,1,'slot','manual','queued','{}',?)", (instant,))
            conn.execute("INSERT INTO fb_video_batch VALUES('batch','tenant','owner','Owner','op','hash','{}',?)", (instant,))

    def video_job(self, identity, page='100'):
        task_id, instant, ledger = format(identity, '032x'), self.now.isoformat(), identity + 1000
        url = 'https://media.example.test/source-%s.mp4' % identity
        snapshot = json.dumps({'material': {'media_url': url}, 'title': 'Frozen title'})
        context = dict(page_id=page, material_id=str(identity), sub_user_id='1', task_id=task_id,
                       language='en', drama_name='Drama', tag='tag', content_id='content',
                       page_name='Page', material_name='Material')
        with self.video.connect() as conn:
            conn.execute("""INSERT INTO fb_video_task(task_id,batch_id,tenant_key,owner_user_id,page_id,material_id,content_id,
                sequence,page_sequence,planned_at_utc,planned_bj_date,processing_mode,status,snapshot_json,
                prepare_state,link_state,prepared_json,ledger_id,short_url,long_url,caption,created_at_utc,updated_at_utc)
                VALUES(?,'batch','tenant','owner',?,?,'content',?,? ,?,'2026-11-03','original','queued',?,
                'ready','ready',?,?,?,?, 'Frozen caption',?,?)""",
                (task_id, page, str(identity), identity, identity, instant, snapshot,
                 json.dumps({'media_url': url, 'profile': 'original'}), ledger, short_url(ledger),
                 build_attribution_url(context, ledger, instant), instant, instant))
        return dict(source_key='fb:video:' + task_id, source_id=task_id, channel='fb', family='video', stage='publish', generation=1)

    def auto_job(self, identity, page):
        instant = self.now.isoformat()
        with self.auto.connect() as conn:
            conn.execute("""INSERT INTO fb_auto_task(id,run_id,template_id,template_version,page_id,group_id,status,
                material_id,content_id,created_at_utc,media_url,source_media_url,prepared_media_url,prepared_at_utc,planned_publish_at_utc)
                VALUES(?,1,1,1,?,'group','ready',?,'content',?,'https://media.example.test/final.mp4',
                'https://media.example.test/source.mp4','https://media.example.test/final.mp4',?,?)""",
                (identity, page, str(identity), instant, instant, instant))
        return dict(source_key='fb:auto:' + str(identity), source_id=str(identity), channel='fb', family='auto', stage='publish', generation=1)

    def background(self, action):
        done, values, errors = threading.Event(), [], []
        def run():
            try: values.append(action())
            except BaseException as exc: errors.append(exc)
            finally: done.set()
        thread = threading.Thread(target=run)
        thread.start()
        return thread, done, values, errors

    def operation(self, descriptor, suffix='one'):
        return {'operation': 'fb_video_publish', 'operation_id': 'fixture-operation-' + suffix,
                'account_resource': descriptor['account_resource'], 'request': descriptor['request'], 'request_sha256': 'a' * 64}

    def test_slow_preflight_does_not_block_other_page_or_late_receipt(self):
        slow, other = self.video_job(1, '101'), self.video_job(2, '102')
        descriptor = self.bridge.reserve(other, self.claim)
        operation = self.operation(descriptor)
        self.bridge.begin_operation(other, self.claim, operation)
        # A known accepted receipt may arrive after the native interruption mark.
        with self.video.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='unknown',unknown_outcome=1 WHERE task_id=?", (other['source_id'],))
        entered, release, threads = threading.Event(), threading.Event(), []
        original = self.bridge._video_preflight
        def preflight(task):
            if task['task_id'] == slow['source_id']:
                entered.set()
                if not release.wait(5): raise RuntimeError('fixture release timeout')
            return original(task)
        receipt = {'operation_id': operation['operation_id'], 'kind': 'success', 'post_id': '123456'}
        with patch.object(self.bridge, '_video_preflight', side_effect=preflight):
            try:
                first = self.background(lambda: self.bridge.reserve(slow, self.claim)); threads.append(first[0])
                self.assertTrue(entered.wait(2))
                def deliver():
                    self.bridge.checkpoint(other, self.claim, 'fb_publish_attempt', receipt)
                    return self.bridge.complete(other, receipt, self.claim)
                second = self.background(deliver); threads.append(second[0])
                self.assertTrue(second[1].wait(2), 'late receipt blocked by another Page preflight')
                self.assertFalse(first[1].is_set())
                self.assertEqual(second[3], [])
            finally:
                release.set()
                for thread in threads: thread.join(5)
        self.assertEqual(first[3], [])
        self.assertEqual(self.bridge.reconcile(other), {'status': 'submitted', 'graph_post_id': '123456', 'unknown_outcome': 0})
        with self.video.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fb_video_attempt').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_fb_operation').fetchone()[0], 1)

    def test_concurrent_same_source_reserves_once(self):
        job = self.video_job(3)
        entered, release, threads = threading.Event(), threading.Event(), []
        original = self.bridge._video_preflight
        def preflight(task):
            entered.set()
            if not release.wait(5): raise RuntimeError('fixture release timeout')
            return original(task)
        with patch.object(self.bridge, '_video_preflight', side_effect=preflight) as prepare, \
                patch.object(self.video, 'claim', wraps=self.video.claim) as claim:
            try:
                first = self.background(lambda: self.bridge.reserve(job, self.claim)); threads.append(first[0])
                self.assertTrue(entered.wait(2))
                second = self.background(lambda: self.bridge.reserve(job, self.claim)); threads.append(second[0])
                self.assertFalse(second[1].wait(.1))
            finally:
                release.set()
                for thread in threads: thread.join(5)
            self.assertEqual((claim.call_count, prepare.call_count), (1, 1))
        self.assertEqual(first[3] + second[3], [])
        self.assertEqual(first[2], second[2])
        with self.video.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_fb_claim').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fb_video_attempt').fetchone()[0], 0)

    def test_same_page_native_fence_survives_parallel_sources(self):
        slow, blocked, other = self.video_job(4, '104'), self.video_job(5, '104'), self.video_job(6, '106')
        entered, release, threads = threading.Event(), threading.Event(), []
        original = self.bridge._video_preflight
        def preflight(task):
            if task['task_id'] == slow['source_id']:
                entered.set()
                if not release.wait(5): raise RuntimeError('fixture release timeout')
            return original(task)
        with patch.object(self.bridge, '_video_preflight', side_effect=preflight):
            try:
                first = self.background(lambda: self.bridge.reserve(slow, self.claim)); threads.append(first[0])
                self.assertTrue(entered.wait(2))
                second = self.background(lambda: self.bridge.reserve(blocked, self.claim)); threads.append(second[0])
                third = self.background(lambda: self.bridge.reserve(other, self.claim)); threads.append(third[0])
                self.assertTrue(second[1].wait(2)); self.assertTrue(third[1].wait(2))
                self.assertEqual([str(e) for e in second[3]], ['canonical_account_busy'])
                self.assertEqual(third[3], [])
                self.assertFalse(first[1].is_set())
            finally:
                release.set()
                for thread in threads: thread.join(5)
        self.assertEqual(first[3], [])
        with self.video.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM fb_video_task WHERE task_id=?", (blocked['source_id'],)).fetchone()[0], 'queued')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fb_video_attempt').fetchone()[0], 0)

    def test_cross_family_same_page_fences_remain_authoritative(self):
        manual, automatic = self.video_job(7, '107'), self.auto_job(8, '107')
        self.bridge.reserve(manual, self.claim)
        with self.assertRaisesRegex(AdapterError, 'canonical_account_busy'):
            self.bridge.reserve(automatic, self.claim)
        automatic2, manual2 = self.auto_job(9, '109'), self.video_job(10, '109')
        self.bridge.reserve(automatic2, self.claim)
        with self.assertRaisesRegex(AdapterError, 'canonical_account_busy'):
            self.bridge.reserve(manual2, self.claim)
        with self.video.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_fb_claim').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_fb_operation').fetchone()[0], 0)

    def test_stage_generation_share_fence_without_retaining_idle_history(self):
        job = self.video_job(11)
        lock = self.bridge._source_lock(job)
        later = dict(job, stage='prepare', generation=4)
        self.assertIs(self.bridge._source_lock(later), lock)
        entered = threading.Event()
        def callback():
            with self.bridge._source_lock(later): entered.set()
        with lock:
            with self.bridge._source_lock(later): pass
            thread, done, _, errors = self.background(callback)
            self.assertFalse(entered.wait(.1))
        thread.join(3)
        self.assertTrue(done.is_set()); self.assertTrue(entered.is_set()); self.assertEqual(errors, [])
        del lock
        for identity in range(1000):
            key = dict(job, source_key='fb:video:' + str(identity), source_id=str(identity))
            with self.bridge._source_lock(key): pass
        gc.collect()
        self.assertEqual(len(self.bridge._source_locks), 0)


if __name__ == '__main__': unittest.main()
