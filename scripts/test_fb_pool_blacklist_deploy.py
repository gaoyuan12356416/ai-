"""Rollback must not reinstate a blacklist over accepted pool work."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import deploy_fb_pool_blacklist as deploy


class RollbackTests(unittest.TestCase):
    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'db.sqlite'
        with self.connect() as c:
            c.executescript('''CREATE TABLE fb_video_pool_item(status TEXT);
                CREATE TABLE fb_video_task(source_kind TEXT, status TEXT, unknown_outcome INTEGER, snapshot_json TEXT);''')
        self.patch = patch.object(deploy.base, 'DB', self.db)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_pending_or_processing_pool_item_blocks_rollback(self):
        for status in ('pending', 'processing'):
            with self.subTest(status=status), self.connect() as c:
                c.execute('DELETE FROM fb_video_pool_item')
                c.execute('INSERT INTO fb_video_pool_item VALUES(?)', (status,))
                c.commit()
                with self.assertRaises(RuntimeError):
                    deploy.require_no_pending_pool()

    def test_active_unknown_and_legacy_pool_tasks_block_rollback(self):
        for kind, status, unknown, snapshot in (
                ('pool', 'queued', 0, {}), ('pool', 'published', 1, {}),
                ('manual', 'unknown', 1, {'pool': {}})):
            with self.subTest(kind=kind, status=status), self.connect() as c:
                c.execute('DELETE FROM fb_video_task')
                c.execute('INSERT INTO fb_video_task VALUES(?,?,?,?)', (kind,status,unknown,json.dumps(snapshot)))
                c.commit()
                with self.assertRaises(RuntimeError):
                    deploy.require_no_pending_pool()

    def test_terminal_pool_and_unrelated_manual_unknown_are_preserved(self):
        with self.connect() as c:
            c.execute("INSERT INTO fb_video_pool_item VALUES('published')")
            c.executemany('INSERT INTO fb_video_task VALUES(?,?,?,?)',
                          [('pool','published',0,'{}'), ('manual','unknown',1,'{}')])
        deploy.require_no_pending_pool()
        with self.connect() as c:
            self.assertEqual(2, c.execute('SELECT COUNT(*) FROM fb_video_task').fetchone()[0])

    def test_deployment_never_restores_old_code_after_resume_starts(self):
        release, pool, base = deploy.release, deploy.pool, deploy.base
        target = Path(self.temp.name) / 'live.py'; target.write_text('before')
        source = Path(self.temp.name) / 'new.py'; source.write_text('after')
        saved = {base.FB_UNIT: {'active':'active'}}
        with patch.object(base, 'BASE', Path(self.temp.name)), \
             patch.object(pool, 'saved_states', return_value=saved), \
             patch.object(pool, 'quiesce'), patch.object(release, 'assert_pool_window'), \
             patch.object(base, 'snapshot_db'), \
             patch.object(release, 'resume', side_effect=RuntimeError('health unavailable')), \
             patch.object(release, 'restore_files') as restore:
            with self.assertRaises(RuntimeError):
                deploy.apply_release([(source, target, 0o644)], 'abcd1234')
            restore.assert_not_called()
        self.assertEqual('after', target.read_text())
        manifest = next((Path(self.temp.name) / 'backups').glob('*/manifest.json'))
        self.assertEqual('needs_service_reconciliation_new_code_retained', json.loads(manifest.read_text())['phase'])


if __name__ == '__main__':
    unittest.main()
