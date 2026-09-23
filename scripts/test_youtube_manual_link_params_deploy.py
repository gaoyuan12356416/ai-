"""Prevent rollback from stranding incomplete V2 operations."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import deploy_youtube_manual_link_params as params


class RollbackTests(unittest.TestCase):
    def test_rollback_only_after_v2_operations_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'data').mkdir()
            with closing(sqlite3.connect(root/'data/drama_material_jobs.sqlite3')) as conn, conn:
                conn.execute('CREATE TABLE youtube_manual_short_link(link_id INTEGER,context_json TEXT)')
                conn.execute('CREATE TABLE drama_material_short_link(id INTEGER,publish_state TEXT)')
                conn.execute('INSERT INTO youtube_manual_short_link VALUES(1,?)',
                             (json.dumps({'version':'youtube-manual-link-v2'}),))
                conn.execute("INSERT INTO drama_material_short_link VALUES(1,'failed')")
            with patch.object(params.deployment, 'ROOT', root), \
                    patch.object(params.deployment, 'run') as run, \
                    patch.object(params, 'restore_code') as restore:
                with self.assertRaisesRegex(RuntimeError, 'finish pending V2'):
                    params.rollback(root/'backup')
                restore.assert_not_called()
                self.assertEqual(run.call_args.args, ('systemctl','start',params.deployment.UNIT))
                with closing(sqlite3.connect(root/'data/drama_material_jobs.sqlite3')) as conn, conn:
                    conn.execute("UPDATE drama_material_short_link SET publish_state='published'")
                params.rollback(root/'backup')
                restore.assert_called_once_with(root/'backup')


if __name__ == '__main__':
    unittest.main()
