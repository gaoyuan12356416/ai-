"""Run the real catalog predicate against mixed release states, without writes."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.drama_launch_window import bounds, release_timing
from features.hit_publish import future_scope
from features.x_auto_posts.repositories import ReadOnlyMySQLRepository


class PendingLaunchTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 17, 10, tzinfo=timezone.utc)
        self.epoch = int(self.now.timestamp())
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("ATTACH ':memory:' AS kunlunads_dev")
        self.db.execute('''CREATE TABLE kunlunads_dev.ads_drama_info (
            id INTEGER PRIMARY KEY, content_id TEXT, series_code TEXT,
            language TEXT, resource_type_v2 INTEGER, deploy_time INTEGER,
            app_id INTEGER, release_status INTEGER, drama_status INTEGER,
            name TEXT, app TEXT)''')
        self.repo = ReadOnlyMySQLRepository(lambda: None)
        self.repo._stream_select = lambda sql, params: (
            dict(row) for row in self.db.execute(sql.replace('%s', '?'), params))

    def add(self, name, status=2, offset=3600, app_id=1479, enabled=1, language='en'):
        self.db.execute('''INSERT INTO kunlunads_dev.ads_drama_info
            VALUES(NULL, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'dramawave')''',
            (name, 'series-' + name, language, self.epoch + offset, app_id, enabled, status, name))

    def rows(self, window):
        low, high = bounds(window, self.epoch)
        return self.repo.list_drama_rows(language='en', now_epoch=high,
                                        deploy_since_epoch=low)

    def test_pending_and_live_pass_but_unpublished_removed_and_disabled_do_not(self):
        self.add('pending')
        self.add('live', status=3)
        for status in [-1, 1, 4]:
            self.add('excluded-' + str(status), status=status)
        self.add('disabled', enabled=0)
        self.add('wrong-product', app_id=979)
        self.add('wrong-language', language='es')
        self.assertEqual({r.content_id for r in self.rows('-1~0')}, {'pending', 'live'})

    def test_time_window_boundaries_and_legacy_due_only(self):
        for name, offset in [('past', -1), ('now', 0), ('later', 3600),
                             ('end', 86400), ('outside', 86401)]:
            self.add(name, offset=offset)
        self.assertEqual({r.content_id for r in self.rows('-1~0')}, {'now', 'later', 'end'})
        for window in [0, 7, '0~7']:
            self.assertEqual({r.content_id for r in self.rows(window)}, {'past', 'now'})
        later = next(r for r in self.rows('-1~0') if r.content_id == 'later')
        timing = release_timing(later.deploy_time, self.now)
        self.assertEqual(timing['publish_at_utc'], '2026-11-17T11:00:00+00:00')
        self.assertEqual(timing['advance_seconds'], 0)

    def test_existing_explicit_future_scope_still_reaches_catalog(self):
        self.add('target', offset=7200)
        with future_scope({'target:en': self.epoch + 7200}):
            self.assertEqual({r.content_id for r in self.rows(0)}, {'target'})
        self.assertEqual(self.rows(0), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
