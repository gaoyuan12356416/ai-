import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

from features.post_daily_report.common import read_db
from features.post_daily_report.report import total, build_card
from tests import test_post_daily_report_x as fixtures


class ReadRecoveryTests(unittest.TestCase):
    def test_writer_lock_longer_than_old_timeout_recovers_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'publisher.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE facts(value)')
                db.execute('INSERT INTO facts VALUES(7)')
                db.commit()
            locked = threading.Event()

            def writer():
                conn = sqlite3.connect(path)
                try:
                    conn.execute('BEGIN EXCLUSIVE')
                    locked.set()
                    time.sleep(2.3)
                    conn.commit()
                finally:
                    conn.close()

            thread = threading.Thread(target=writer)
            thread.start()
            try:
                self.assertTrue(locked.wait(5))
                with read_db(path) as db:
                    self.assertEqual(db.execute('SELECT value FROM facts').fetchone()[0], 7)
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute('UPDATE facts SET value=0')
            finally:
                thread.join(5)

    def test_missing_source_is_not_created_or_retried_as_lock_contention(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'missing.sqlite3'
            with self.assertRaises(sqlite3.OperationalError):
                with read_db(path):
                    pass
            self.assertFalse(path.exists())


class MissingRandomPlanTests(unittest.TestCase):
    setUp = fixtures.XCollectorTests.setUp
    insert = fixtures.XCollectorTests.insert
    plan = fixtures.XCollectorTests.plan
    result = fixtures.XCollectorTests.result

    def test_unmaterialized_interval_keeps_proven_targets_and_duplicate_rules(self):
        self.insert(self.auto, 'x_auto_template', id=1, enabled=0, created_at='2026-09-01T00:00:00Z')
        self.insert(self.auto, 'x_auto_template_version', template_id=1, version=1,
                    config_json=json.dumps({'account_ids': [1, 2, 3], 'schedule': {'mode': 'random', 'daily_count': 2}}))
        for kind, stamp in [('template_enabled', '2026-09-07T01:00:00Z'), ('template_disabled', '2026-09-07T01:00:01Z')]:
            self.insert(self.auto, 'x_auto_event', event_type=kind,
                        details_json=json.dumps({'template_id': 1, 'version': 1}), created_at=stamp)
        self.insert(self.auto, 'x_auto_run', id=1, template_id=1, template_version=1,
                    trigger_type='manual', shanghai_date=self.window.date, publish_time='10:00')
        for task, account, post in [(1, '1', 'same'), (2, '2', 'same')]:
            self.insert(self.auto, 'x_auto_task', id=task, run_id=1, account_id=account,
                        status='published', publish_id=post, published_at_utc='2026-09-07T02:01:00Z')
        self.insert(self.auto, 'x_auto_task', id=3, run_id=1, account_id='3', status='failed', error_code='publish_failed')
        result, rows = self.result()
        row = rows['auto_template']
        self.assertIsNone(row['expected'])
        self.assertEqual((row['known_expected'], row['published'], row['failed']), (2, 1, 1))
        self.assertEqual(len(row['unresolved_plans']), 1)
        sums = total(result['rows'])
        self.assertEqual((sums['known_expected'], sums['known_missing']), (2, 1))
        self.assertIsNone(sums['expected'])
        report = {'date': self.window.date, 'channels': [result]}
        card = json.dumps(build_card(report), ensure_ascii=False)
        self.assertIn('≥2', card)
        self.assertIn('≥1', card)
        self.assertIn('数量下限', card)

    def test_known_totals_do_not_hide_a_separate_unavailable_source(self):
        sums = total([{'expected': None, 'known_expected': 9, 'published': 4},
                      {'expected': None, 'data_available': False}])
        self.assertIsNone(sums['expected_lower_bound'])
        self.assertIsNone(sums['known_missing'])
        self.assertIsNone(sums['published'])


if __name__ == '__main__':
    unittest.main()
