"""The X account store must not downgrade concurrent native ledger readers."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from features.x_accounts import oauth_service as service


class XStorageWALTests(unittest.TestCase):
    def test_repeated_connections_preserve_wal_and_allow_reader_writer_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(service,'DATA_DIR',root),patch.object(service,'DB_PATH',root/'accounts.sqlite3'),patch.object(service,'TOKENS_DIR',root/'tokens'):
                service.ensure_storage()
                reader=sqlite3.connect(str(service.DB_PATH),timeout=.2)
                try:
                    reader.execute('BEGIN')
                    self.assertEqual(reader.execute('SELECT count(*) FROM x_oauth_event').fetchone()[0],0)
                    writer=service.db_connect()
                    try:
                        self.assertEqual(writer.execute('PRAGMA journal_mode').fetchone()[0],'wal')
                        writer.execute("INSERT INTO x_oauth_event(event_type,outcome,x_user_id,actor_user_id,actor_tenant_key,actor_name,error_code,created_at) VALUES('test','ok','','','','','','2026-11-23T00:00:00Z')")
                        writer.commit()
                    finally:writer.close()
                    self.assertEqual(reader.execute('SELECT count(*) FROM x_oauth_event').fetchone()[0],0)
                    reader.commit()
                    self.assertEqual(reader.execute('SELECT count(*) FROM x_oauth_event').fetchone()[0],1)
                    service.ensure_storage()
                    self.assertEqual(reader.execute('PRAGMA journal_mode').fetchone()[0],'wal')
                finally:reader.close()


if __name__=='__main__':unittest.main()
