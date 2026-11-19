from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_tag_publish.account_ports import account_publishing_ports, template_rows


class AccountPublishingPortsTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / 'sources.db'
        self.missing = Path(folder.name) / 'missing.db'
        self.accounts = [{'id': 1, 'auto_publish_sources': ['material', 'drama']},
                         {'id': 2, 'auto_publish_sources': []}]
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""
                CREATE TABLE x_auto_template (id, name, enabled, current_version, deleted_at_utc);
                CREATE TABLE x_auto_template_version (template_id, version, config_json);
                INSERT INTO x_auto_template VALUES (1, 'A', 1, 2, ''), (2, 'C', 1, 1, ''),
                    (3, '关闭', 0, 1, ''), (4, '已删', 1, 1, 'yesterday');
                INSERT INTO x_auto_template_version VALUES (1, 1, '{"account_ids":[2]}'),
                    (1, 2, '{"account_ids":[1, 1, 3]}'), (2, 1, '{"account_ids":["1"]}'),
                    (3, 1, '{"account_ids":[2]}'), (4, 1, '{"account_ids":[2]}');
                CREATE TABLE x_post_schedule_config (source_type, enabled, account_ids_json);
                INSERT INTO x_post_schedule_config VALUES ('material', 1, '[1, 3]');
                CREATE TABLE x_video_publish_pool (account_id, status);
                INSERT INTO x_video_publish_pool VALUES (1, 'enabled'), (2, 'paused'), (3, 'enabled');
            """)

    def test_every_enabled_current_template_and_pool_in_visible_scope(self):
        result = account_publishing_ports(self.accounts, self.db, self.db, self.db, read_templates=template_rows)
        self.assertEqual(result['items'], {'1': [
            {'key': 'template:1', 'label': 'A'}, {'key': 'template:2', 'label': 'C'},
            {'key': 'tag_material_pool', 'label': '标签画像素材池'},
            {'key': 'drama_pool', 'label': '短剧池'}, {'key': 'personal_pool', 'label': '账号素材池'},
        ], '2': []})
        self.assertEqual(result['unavailable'], [])

    def test_disabled_pool_and_unreadable_template_preserve_other_results(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""
                UPDATE x_post_schedule_config SET enabled = 0;
                UPDATE x_auto_template_version SET config_json = 'invalid' WHERE template_id = 1;
            """)
        result = account_publishing_ports(self.accounts, self.db, self.db, self.missing, read_templates=template_rows)
        self.assertEqual(result['items']['1'], [{'key': 'template:2', 'label': 'C'},
                                               {'key': 'drama_pool', 'label': '短剧池'}])
        self.assertCountEqual(result['unavailable'], ['自动发布模板', '账号素材池'])
        self.assertFalse(self.missing.exists())


if __name__ == '__main__':
    unittest.main()
