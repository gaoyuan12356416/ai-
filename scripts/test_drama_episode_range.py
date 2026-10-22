"""Exercise actual app functions against disposable SQLite; never run workers."""
import ast
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.drama_job_identity import find_duplicate

SOURCE = Path(os.environ.get('DRAMA_RANGE_APP_SOURCE', str(ROOT / 'app.py')))
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))
FUNCTIONS = {n.name: n for n in TREE.body if isinstance(n, ast.FunctionDef)}
SCHEMA = next(n.value for n in ast.walk(TREE) if isinstance(n, ast.Constant)
              and isinstance(n.value, str) and 'CREATE TABLE IF NOT EXISTS drama_material_job (' in n.value)


class EpisodeRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'jobs.sqlite3')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(SCHEMA)
        self.dispatched = []
        self.recipes = []
        self.ns = self.namespace()

    def tearDown(self):
        self.temp.cleanup()

    def namespace(self):
        def validate(app_id, content_id, start, end):
            return dict(app_id=str(app_id), total_episodes=100, cover_source_url='',
                        app='test', country='', language='es', drama_name='test',
                        available_episode_start=1, available_episode_end=100)
        ns = dict(json=json, uuid=uuid, JOB_DB_LOCK=threading.Lock(),
                  get_job_db_connection=lambda: sqlite3.connect(self.db, timeout=10),
                  is_job_deleted=lambda _: False, clear_job_deleted_marker=lambda _: None,
                  normalize_outputs=lambda x: {'random_template_video': False, **x},
                  normalize_advanced_options=lambda x: x, clamp_progress=lambda x: x,
                  progress_for_status=lambda _: 2, utc_now_text=lambda: '2026-10-22 00:00:00',
                  now_text=lambda: '2026-10-22 00:00:00', validate_content_request=validate,
                  run_job_async=self.dispatched.append,
                  freeze_random_recipe=lambda **kw: {'job_id': kw['job_id']},
                  drama_random_template_catalog=lambda: {},
                  DRAMA_SYNTHESIS_STORE=SimpleNamespace(freeze_recipe=lambda *a: self.recipes.append(a)))
        for name in ('find_duplicate_job', 'ensure_no_duplicate_job', 'upsert_job_record', 'submit_job'):
            exec(compile(ast.Module(body=[FUNCTIONS[name]], type_ignores=[]), str(SOURCE), 'exec'), ns)
        return ns

    def submit(self, start=1, end=20, app_id='1479', content_id='range-test', ns=None):
        return (ns or self.ns)['submit_job'](dict(app_id=app_id, content_id=content_id,
                          episode_start=start, episode_end=end, outputs={}, advanced_options={}),
                          actor_session={'user_id': 'test-owner'})

    def test_different_and_overlapping_ranges_create_independent_tasks(self):
        first = self.submit()
        original = dict(self.dispatched[0], status='done', output_video_url='https://example.test/original.mp4')
        self.ns['upsert_job_record'](original)
        second = self.submit(20, 40)
        third = self.submit(21, 40)
        fourth = self.submit(1, 40)
        self.assertEqual(4, len({x['job_id'] for x in (first, second, third, fourth)}))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(('done', 'https://example.test/original.mp4'), conn.execute(
                'SELECT status,output_video_url FROM drama_material_job WHERE job_id=?', (first['job_id'],)).fetchone())

    def test_same_range_blocks_for_every_status(self):
        accepted = self.submit()
        for status in ('queued', 'downloading', 'processing', 'done', 'failed'):
            self.ns['upsert_job_record'](dict(self.dispatched[0], status=status))
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, accepted['job_id']):
                self.submit()
        self.assertEqual(1, len(self.dispatched))

    def test_product_and_content_are_part_of_identity(self):
        self.submit()
        self.submit(app_id='other-product')
        self.submit(content_id='other-drama')
        self.assertEqual(3, len(self.dispatched))

    def test_existing_job_update_and_retry_are_allowed(self):
        self.submit()
        job = dict(self.dispatched[0], status='failed')
        self.ns['upsert_job_record'](job)
        job['status'] = 'queued'
        self.ns['upsert_job_record'](job)
        self.assertEqual('queued', self.ns['find_duplicate_job']('1479', 'range-test', 1, 20)['status'])
        self.assertIsNone(self.ns['find_duplicate_job']('1479', 'range-test', 1, 20, job['job_id']))

    def test_duplicate_older_than_list_page_is_found(self):
        first = self.submit()
        for i in range(101):
            self.submit(i + 2, i + 30)
        with self.assertRaisesRegex(ValueError, first['job_id']):
            self.submit()

    def test_concurrent_submissions_share_atomic_insert_guard(self):
        barrier = threading.Barrier(8)
        def attempt(_):
            ns = self.namespace()  # Independent locks, as with different API processes.
            validate = ns['validate_content_request']
            def wait_validate(*args):
                barrier.wait(timeout=10)  # All callers have passed the early duplicate read.
                return validate(*args)
            ns['validate_content_request'] = wait_validate
            try:
                return self.submit(ns=ns)['job_id']
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(1, sum(x is not None for x in results))
        self.assertEqual(1, len(self.dispatched))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(1, conn.execute('SELECT count(*) FROM drama_material_job').fetchone()[0])

    def test_invalid_ranges_rejected_before_dispatch(self):
        for start, end in ((0, 20), (20, 1), (1, -1)):
            with self.assertRaises(ValueError):
                self.submit(start, end)
        self.assertEqual([], self.dispatched)

    def test_sqlite_row_factory_supported(self):
        self.submit()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual('queued', find_duplicate(conn, '1479', 'range-test', 1, 20)['status'])


if __name__ == '__main__':
    unittest.main()
