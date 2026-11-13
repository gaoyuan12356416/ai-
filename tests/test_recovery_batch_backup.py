import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import recovery_batch_backup as backups


class BatchBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.native, self.central = self.root/'native.sqlite3', self.root/'central.sqlite3'
        for path in (self.native,self.central):
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE facts(value TEXT)')
                db.execute("INSERT INTO facts VALUES('original')")

    def batch(self, **kwargs):
        return backups.batch_backup(self.root/'backups',kwargs.get('batch','batch-1'),self.native,
                                    kwargs.get('central',self.central),kwargs.get('scope','settlement'))

    def test_second_task_reuses_original_pair_and_has_separate_row_audit(self):
        with self.batch() as folder:
            backups.write_audit(folder/'task-1-before.json',{'id':1,'status':'unknown'})
            original=json.loads((folder/'batch.json').read_text())
        with contextlib.closing(sqlite3.connect(self.native)) as db, db:
            db.execute("INSERT INTO facts VALUES('new task activity')")
        with patch.object(backups,'snapshot',side_effect=AssertionError('must not copy')):
            with self.batch() as folder:
                backups.write_audit(folder/'task-2-before.json',{'id':2,'status':'unknown'})
                self.assertEqual(json.loads((folder/'batch.json').read_text()),original)
                with contextlib.closing(sqlite3.connect(folder/'fb.sqlite3')) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM facts').fetchone()[0],1)

    def test_changed_source_or_scope_cannot_reuse_batch(self):
        with self.batch():pass
        for args in ({'central':self.native},{'scope':'another-operation'}):
            with self.assertRaisesRegex(ValueError,'backup_batch_identity_changed'):
                with self.batch(**args):pass

    def test_changed_backup_file_is_rejected(self):
        with self.batch() as folder:pass
        with (folder/'fb.sqlite3').open('ab') as stream:stream.write(b'changed')
        with self.assertRaisesRegex(ValueError,'backup_batch_file_changed'):
            with self.batch():pass

    def test_incomplete_pair_is_retained_and_not_overwritten(self):
        folder=self.root/'backups/batch-1';folder.mkdir(parents=True)
        (folder/'fb.sqlite3').write_bytes(b'partial previous preparation')
        with self.assertRaisesRegex(ValueError,'unfinished_batch_snapshot_requires_review'):
            with self.batch():pass
        self.assertEqual((folder/'fb.sqlite3').read_bytes(),b'partial previous preparation')

    def test_audit_is_immutable_but_identical_retry_is_allowed(self):
        path=self.root/'audit.json'
        backups.write_audit(path,{'id':1});backups.write_audit(path,{'id':1})
        with self.assertRaisesRegex(ValueError,'batch_audit_changed'):
            backups.write_audit(path,{'id':2})

    def test_batch_must_be_explicit_and_path_safe(self):
        for value in ('','../escape','a/b'):
            with self.assertRaisesRegex(ValueError,'explicit_backup_batch_required'):
                with self.batch(batch=value):pass


if __name__=='__main__':unittest.main()
