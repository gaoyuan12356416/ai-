"""Migration/backup and exact-file rollback tests, isolated from production."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import deploy_youtube_channel_cover as deploy


class Deployment(unittest.TestCase):
    def test_migration_preserves_legacy_rows_and_unrelated_publishing_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'original.sqlite3'
            copy = Path(tmp) / 'copy.sqlite3'
            with closing(sqlite3.connect(str(source))) as c:
                c.executescript('''CREATE TABLE youtube_channel_template(
                    tenant TEXT, channel_id TEXT, title_template TEXT, description_template TEXT,
                    comment_template TEXT, version INTEGER, updated_by TEXT, updated_by_name TEXT,
                    updated_at TEXT, PRIMARY KEY(tenant,channel_id));
                    INSERT INTO youtube_channel_template VALUES('tenant','UCone','title','desc','comment',3,'u','name','stamp');
                    CREATE TABLE publish_fact(id INTEGER, body TEXT);
                    INSERT INTO publish_fact VALUES(7,'existing uploaded video');''')
            before = deploy.template_fingerprint(source)
            deploy.backup_database(source, copy)
            self.assertEqual(deploy.migrate(copy), before)
            self.assertEqual(deploy.migrate(copy), before)
            with closing(sqlite3.connect(str(copy))) as c:
                self.assertEqual(c.execute('SELECT * FROM publish_fact').fetchall(), [(7, 'existing uploaded video')])
                self.assertEqual(c.execute('SELECT cover_requirements_template FROM youtube_channel_template').fetchone(), ('',))
            with closing(sqlite3.connect(str(source))) as c:
                self.assertNotIn('cover_requirements_template', [r[1] for r in c.execute('PRAGMA table_info(youtube_channel_template)')])

    def test_rollback_retains_database_and_rejects_later_code_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'runtime'; root.mkdir()
            public = Path(tmp) / 'public'; public.mkdir()
            base = Path(tmp) / 'deploy'; backup = base / 'backups' / 'one'; backup.mkdir(parents=True)
            target = root / 'template.py'; target.write_bytes(b'new')
            database = root / 'facts.sqlite3'; database.write_bytes(b'new cover defaults and publishing facts')
            saved = deploy.common.saved_file(backup, target); saved.parent.mkdir(parents=True); saved.write_bytes(b'old')
            entry = {'old': deploy.common.digest(b'old'), 'new': deploy.common.digest(b'new'), 'mode': 0o644}
            (backup / 'manifest.json').write_text(json.dumps({'files': {str(target): entry}}))
            with patch.multiple(deploy.common, ROOT=root, PUBLIC=public, BASE=base), patch.object(deploy.common, 'run') as run, patch.object(deploy.common, 'healthy'):
                target.write_bytes(b'later')
                with self.assertRaises(RuntimeError): deploy.rollback(backup)
                run.assert_not_called()
                target.write_bytes(b'new')
                deploy.rollback(backup)
                self.assertEqual(target.read_bytes(), b'old')
                self.assertEqual(database.read_bytes(), b'new cover defaults and publishing facts')

    def test_rollback_starts_api_even_if_stop_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); backup = root / 'backups' / 'one'; backup.mkdir(parents=True)
            (backup / 'manifest.json').write_text('{"files":{}}')
            with patch.object(deploy.common, 'BASE', root), patch.object(deploy.common, 'run', side_effect=[RuntimeError('stop failed'), 'active']) as run:
                with self.assertRaises(RuntimeError): deploy.rollback(backup)
                self.assertEqual(run.call_args_list[-1].args, ('systemctl', 'start', deploy.common.UNIT))


if __name__ == '__main__':
    unittest.main()
