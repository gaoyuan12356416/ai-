"""Exercise deployment guards without opening any database or systemd session."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('socialkit_install', ROOT/'ops/socialkit-analytics/install.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class DeploymentTests(unittest.TestCase):
    def test_committed_migration_runs_exactly_three_approved_ddl_statements(self):
        statements = []
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql): statements.append(sql)
            def fetchone(self): return ('ads_ai', 0)
        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass
        env = {'SOCIALKIT_ANALYTICS_WRITE_MYSQL_'+key:value for key,value in
               dict(HOST='101.32.56.53', PORT='63353', DATABASE='ads_ai', USER='fixture', PASSWORD='fixture').items()}
        connections = []
        def connect(**kwargs):
            connections.append(kwargs)
            return Connection()
        with patch.dict('sys.modules', {'pymysql':SimpleNamespace(connect=connect)}), patch.dict('os.environ', env, clear=True):
            install.migrate()
        self.assertEqual(len(statements), 4)
        self.assertEqual(statements[0], 'SELECT DATABASE(),@@read_only')
        for sql, name in zip(statements[1:], ['socialkit_effect_daily','socialkit_publish_daily','socialkit_refresh_runs']):
            self.assertIn('CREATE TABLE IF NOT EXISTS '+name+' (', sql)
        self.assertEqual(connections[0]['database'], 'ads_ai')
        self.assertEqual(connections[0]['port'], 63353)

    def test_rejected_rollback_has_no_timer_or_file_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root/'backups'/'release'
            backup.mkdir(parents=True)
            current = root/'app.py'
            current.write_text('later unrelated change', encoding='utf-8')
            manifest = backup/'manifest.json'
            manifest.write_text(json.dumps({'files':[{'path':str(current),'new_sha256':'different'}]}),encoding='utf-8')
            with patch.object(install, 'DATA', root), patch.object(install.subprocess, 'run') as commands:
                with self.assertRaisesRegex(RuntimeError, 'Later file change'):
                    install.rollback(manifest)
                commands.assert_not_called()
            self.assertEqual(current.read_text(encoding='utf-8'),'later unrelated change')


if __name__ == '__main__':
    unittest.main()
