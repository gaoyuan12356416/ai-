"""Bounded task-page reads and single-pass template history preserve facts."""
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from features.pipeline_dispatch.core import DispatchStore
from features.post_daily_report import accounting, plans
from tests.test_post_daily_report_x import AUTO_SCHEMA


class MonitorReadTests(unittest.TestCase):
    def test_page_matches_full_context_with_off_page_successor_and_quarantine(self):
        with tempfile.TemporaryDirectory() as folder:
            then = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
            now = then + dt.timedelta(days=1)
            store = DispatchStore(Path(folder) / 'dispatch.db', clock=lambda: then)
            jobs = {}
            for name in ('original', 'successor', 'waiting', 'unknown', 'published'):
                jobs[name] = store.offer(dict(source_key='x:auto:' + name, channel='x', family='auto',
                                             stage='publish', status='queued', scheduled_at=then.isoformat(),
                                             resource_keys=['account:1']))
            with store.transaction() as db:
                db.execute("UPDATE pipeline_job SET source_meta_json=? WHERE id=?",
                           (json.dumps({'ancestor_source_keys': ['x:auto:original']}), jobs['successor']['id']))
                db.execute("UPDATE pipeline_job SET status='unknown' WHERE id=?", (jobs['unknown']['id'],))
                db.execute("INSERT INTO pipeline_resource VALUES('account:1',?,1,1,0)", (jobs['unknown']['id'],))
                db.execute("INSERT INTO pipeline_publication VALUES(?,?,'x','','','','','post-id',?,'{}')",
                           ('x:auto:published', jobs['published']['id'], then.isoformat()))
            keys = {'x:auto:original', 'x:auto:waiting', 'x:auto:published'}
            with closing(store.connect()) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                full = accounting.load_dispatch({}, now, db)
                page = accounting.load_dispatch({}, now, db, source_keys=keys)
                empty = accounting.load_dispatch({}, now, db, source_keys=set())
                self.assertEqual(set(page['jobs']), keys)
                self.assertEqual(set(page['publications']), {'x:auto:published'})
                self.assertFalse(empty['jobs'])
                self.assertFalse(empty['publications'])
                for key in keys:
                    self.assertEqual(accounting.effective_state(page, key, now),
                                     accounting.effective_state(full, key, now))
                self.assertIsNone(accounting.effective_state(page, 'x:auto:original', now))
                self.assertEqual(accounting.effective_state(page, 'x:auto:waiting', now)['code'], 'blocked_by_unknown')

    def test_execution_event_volume_does_not_multiply_template_history_work(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.executescript(AUTO_SCHEMA)
            config = {'account_ids': [1], 'schedule': {'mode': 'fixed', 'times': ['09:00', '11:00']}}
            versions = {}
            for template in range(1, 5):
                db.execute("INSERT INTO x_auto_template VALUES(?,0,'2026-11-04T00:00:00Z')", (template,))
                versions[(template, 1)] = config
                for event, hour in [('template_enabled', '00'), ('template_disabled', '02')]:
                    db.execute('INSERT INTO x_auto_event VALUES(?,?,?)',
                               (event, json.dumps({'template_id': template, 'version': 1}),
                                '2026-11-04T' + hour + ':00:00Z'))
            db.execute("INSERT INTO x_auto_template VALUES(99,0,'2026-11-04T00:00:00Z')")
            db.executemany('INSERT INTO x_auto_event VALUES(?,?,?)',
                           [('task_finished', '{invalid unneeded payload', '2026-11-04T01:00:00Z')] * 3000)
            additions, warnings = [], []
            with patch.object(plans, 'object_value', wraps=plans.object_value) as decode:
                plans._auto_slots(db, 'x_auto', 'x', [], versions,
                                  dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc),
                                  lambda *args: additions.append(args), lambda *args: warnings.append(args))
            self.assertEqual(len(additions), 4)
            self.assertTrue(all('09:00' in row[1] for row in additions))
            self.assertIn('模板 99', warnings[0][1])
            self.assertLess(decode.call_count, 20)


if __name__ == '__main__':
    unittest.main()
