"""Uploader options use backend login history and exact email identity mapping."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from features.youtube_auto_publish.runtime import logged_in_uploader_ids
from features.youtube_auto_publish.source import MaterialSource
from features.youtube_auto_publish.templates import WorkflowError


class UploaderLoginFilterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / 'users.sqlite3'
        with closing(sqlite3.connect(self.db)) as c:
            with c:
                c.execute('CREATE TABLE drama_admin_user(email TEXT,login_count INTEGER)')
        self.app = SimpleNamespace(JOB_DB_PATH=self.db, ADMIN_MAPPING_MYSQL_DATABASE='kunlunads_dev',
                                   DB_NAME='kunlunads_dev')

    def add_users(self, rows):
        with closing(sqlite3.connect(self.db)) as c:
            with c:
                c.executemany('INSERT INTO drama_admin_user VALUES(?,?)', rows)

    def test_only_login_history_emails_are_mapped_and_new_logins_are_read(self):
        self.add_users([(' Person@Example.com ', 2), ('person@example.com', 1),
                        ('never@example.com', 0), ('', 3), (None, 1)])
        queries = []
        def reader(sql):
            queries.append(sql)
            return [('337',), ('419',), ('0',), ('invalid',), (None,)]
        provider = logged_in_uploader_ids(self.app, reader)
        self.assertEqual(provider(), {'337', '419'})
        self.assertEqual(queries[0].count('BINARY CONVERT('), 1)
        self.assertIn('person@example.com'.encode().hex(), queries[0])
        self.assertNotIn('never@example.com'.encode().hex(), queries[0])
        self.add_users([('new@example.com', 1)])
        provider()
        self.assertIn('new@example.com'.encode().hex(), queries[-1])

    def test_no_login_history_or_email_does_not_query_mapping(self):
        self.add_users([('never@example.com', 0), ('', 4)])
        def unexpected(sql):
            self.fail('No external mapping query is needed')
        self.assertEqual(logged_in_uploader_ids(self.app, unexpected)(), set())

    def test_email_is_encoded_as_data_and_database_is_validated(self):
        email = "someone'; SELECT secret; --@example.com"
        self.add_users([(email, 1)])
        queries = []
        logged_in_uploader_ids(self.app, lambda sql: queries.append(sql) or [])()
        self.assertNotIn(email, queries[0])
        self.assertIn(email.lower().encode().hex(), queries[0])
        self.app.ADMIN_MAPPING_MYSQL_DATABASE = 'invalid`database'
        with self.assertRaises(WorkflowError):
            logged_in_uploader_ids(self.app, lambda sql: [])()

    def test_both_options_queries_filter_by_mapped_id_without_changing_materials(self):
        path = Path(self.directory.name) / 'source.sql'
        path.write_text('SELECT * FROM materials', encoding='utf-8')
        options = [{'id':'1','name':'Same name'}, {'id':'2','name':'Same name'},
                   {'id':'0','name':'Unknown'}]
        material = {'id':'15','name':'Video','uploader_id':'1',
                    'url':'https://ai.yingliangads.com/video.mp4'}
        def reader(sql):
            rows = [material] if "'thumbnail_url'" in sql else options
            return [(json.dumps(row).encode().hex(),) for row in rows]
        source = MaterialSource(path, reader, uploader_ids_provider=lambda: {'2'})
        for indexed in (False, True):
            if indexed:
                path.with_suffix('.uploaders.sql').write_text("SELECT 'indexed_options'", encoding='utf-8')
            result = source.list(refresh=True)
            self.assertEqual(result['uploaders'], [options[1]])
            self.assertEqual(result['items'][0]['id'], '15')
            self.assertEqual(result['items'][0]['uploader_id'], '1')

    def test_mapping_failure_never_returns_unfiltered_options(self):
        def unavailable():
            raise OSError('unavailable')
        row = json.dumps({'id':'1','name':'Uploader'}).encode().hex()
        source = MaterialSource('', lambda sql: [(row,)], uploader_ids_provider=unavailable)
        with self.assertRaises(WorkflowError):
            source._read_uploaders('SELECT * FROM materials')


if __name__ == '__main__':
    unittest.main()
