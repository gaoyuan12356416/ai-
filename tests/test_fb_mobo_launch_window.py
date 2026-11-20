import re
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from features.fb_auto_posts.repositories import MaterialRepository


class MoboLaunchWindowTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript("""
            ATTACH ':memory:' AS ads_ai;
            CREATE TABLE ads_ai.cdreader_koc_drama (
                story_id TEXT, institution_user_id TEXT, project_type INTEGER,
                is_show INTEGER, story_name TEXT, introduce TEXT);
            CREATE TABLE ads_drama_info (content_id TEXT, app_id INTEGER, published_at INTEGER);
            CREATE TABLE ads_custom_source (
                id INTEGER PRIMARY KEY, data_source_id TEXT, data_source INTEGER,
                product TEXT, type INTEGER, is_delete INTEGER, url TEXT,
                name TEXT, tag_name TEXT, language TEXT, video_duration INTEGER);
        """)
        self.db = db

        class Source:
            schema = 'main'

            def select(self, sql, params):
                sql = re.sub(r' FORCE INDEX\(\w+\)', '', sql).replace('%s', '?')
                return [dict(row) for row in db.execute(sql, params)]

        self.repo = MaterialRepository(Source(), now_fn=lambda: datetime.fromtimestamp(self.now, timezone.utc))
        self.config = dict(message_template='{{m_url}}', material_products=['Moboreels', 'Moboshort'],
                           language='en', material_data_source=6,
                           material_rule=dict(duration_min_seconds=300, duration_max_seconds=6000))

    def story(self, mid, epochs, *, app=2373, shown=1):
        cid = str(100 + mid)
        self.db.execute("INSERT INTO ads_ai.cdreader_koc_drama VALUES (?, 'scope', 2, ?, 'story', 'desc')",
                        (cid, shown))
        self.db.execute("""INSERT INTO ads_custom_source VALUES (
            ?, ?, 6, 'Moboreels', 2, 0, 'https://media.test/a.mp4', 'video', '', 'en', 350)""",
                        (mid, cid))
        self.db.executemany("INSERT INTO ads_drama_info VALUES (?, ?, ?)", [(cid, app, epoch) for epoch in epochs])

    def select_window(self, window):
        with patch('features.fb_video_publish.moboreels.institution_id', return_value='scope'):
            rows = self.repo.candidate_snapshot(dict(self.config, drama_launch_window_days=window)).candidates
        return [(int(row.material_id), row.deploy_time) for row in rows]

    def test_rolling_week_uses_first_publication_in_mobo_catalog(self):
        start = self.now - 7 * 86400
        self.story(1, [start])
        self.story(2, [start + 1, start + 1, 0])
        self.story(3, [self.now])
        self.story(4, [self.now + 1])
        self.story(5, [self.now], app=1479)
        self.story(6, [0])
        self.story(7, [start - 1, self.now])
        self.story(8, [self.now], shown=0)
        self.assertEqual(self.select_window(7), [(2, start + 1), (3, self.now)])

    def test_signed_window_includes_endpoints_and_freezes_future_epoch(self):
        end = self.now + 86400
        self.story(1, [self.now - 1])
        self.story(2, [self.now])
        self.story(3, [end])
        self.story(4, [end + 1])
        self.assertEqual(self.select_window('-1~0'), [(2, self.now), (3, end)])

    def test_unlimited_window_keeps_listed_story_without_epoch(self):
        self.story(1, [])
        self.assertEqual(self.select_window(0), [(1, 0)])
